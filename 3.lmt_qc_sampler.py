import argparse
import os
import numpy as np
import re
import random
import sys
import cv2

from lmt_common import (DB_FPS, FRAME_CONVERSION, QC_MODE_DETECTED, QC_MODE_BINARY_SEARCH, 
                        QC_MODE_LOGIC, EXPECTED_VIDEO_FPS, build_video_map, find_nearest_frame_candidates, _read_frame_from_video, 
                        _release_all_captures, compute_qc_pool_mask)

import sqlite3
import pandas as pd
from datetime import datetime

def extract_frame(video_map, global_frame, out_path):
    """
    Resolve global_frame to the nearest actually-available frame (preceding
    vs. succeeding, whichever is closer; preceding wins ties) and extract it.
    Never falls back to the first video arbitrarily.

    Returns (resolved_global_frame, video_name) on success, or (None, None)
    if no candidate could be read.
    """
    for resolved_frame, video_entry in find_nearest_frame_candidates(video_map, global_frame):
        if _read_frame_from_video(video_entry, resolved_frame, out_path):
            return resolved_frame, os.path.basename(video_entry["path"])
    return None, None

def _sample_proportional_to_gap(df, n_samples, seed):
    rng = np.random.default_rng(seed)

    if "GAP_START_FRAME" not in df.columns or "GAP_END_FRAME" not in df.columns:
        idx = rng.choice(df.index.to_numpy(), size=n_samples, replace=False)
        return df.loc[idx].copy()

    grouped = df.groupby(["GAP_START_FRAME", "GAP_END_FRAME"], dropna=False, sort=False)
    sizes   = grouped.size().to_numpy()          # positional, not key-indexed

    quotas     = sizes / sizes.sum() * n_samples
    allocation = np.floor(quotas).astype(int)
    remainder  = quotas - allocation
    leftover   = n_samples - int(allocation.sum())

    # Largest-remainder apportionment, by position rather than by group key
    # (group keys can be NaN — e.g. the DETECTED pool, which has no real
    # gap boundaries — and NaN keys don't reliably round-trip through
    # pandas' equality-based Series/index lookups).
    for pos in np.argsort(-remainder):
        if leftover <= 0:
            break
        if allocation[pos] < sizes[pos]:
            allocation[pos] += 1
            leftover -= 1

    if leftover > 0:  # safety net; see previous write-up
        for pos in range(len(allocation)):
            while leftover > 0 and allocation[pos] < sizes[pos]:
                allocation[pos] += 1
                leftover -= 1
            if leftover <= 0:
                break

    parts = []
    for pos, (_key, group_df) in enumerate(grouped):
        count = int(allocation[pos])
        if count <= 0:
            continue
        chosen_idx = rng.choice(group_df.index.to_numpy(), size=count, replace=False)
        parts.append(group_df.loc[chosen_idx])

    return pd.concat(parts)

def filter_pool(df_full, qc_mode):
    """
    Return the eligible subset of df_full for the requested pool.

    Pool definitions

    DETECTED      : ASSUMPTION_TYPE == "DETECTED"
                    (no IN_NEST restriction; detector always produces 0 or 1)

    BINARY_SEARCH : ASSUMPTION_TYPE == "ASSUMED"
                    AND FILL_SOURCE == "BINARY_SEARCH"  
                    AND IN_NEST in (0, 1)                (exclude residual -1)
                    Falls back to BINARY_SEARCH == 1 if FILL_SOURCE column is absent
                    (backward compat with old 2.lmt_binary_search.py outputs).

    LOGIC         : ASSUMPTION_TYPE == "ASSUMED"
                    AND FILL_SOURCE == "LOGIC"           
                    AND IN_NEST in (0, 1)
                    Falls back to BINARY_SEARCH == 0 and IN_NEST in (0,1) if absent.
    """
    mask, label = compute_qc_pool_mask(df_full, qc_mode)
    return df_full[mask].copy().reset_index(drop=True), label

def _load_animal_id(analysis_db):
    """
    Read the Animal ID directly from the source database's ANIMALID column
    (persisted by 1.lmt_gap_fill.py since Issue 10), instead of asking the
    user to re-type it. Called before any output directory/file is created.
    Raises Exception if the database predates that column, or if it
    unexpectedly contains more than one Animal ID.
    """
    conn = sqlite3.connect(analysis_db)
    try:
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='GAP_FILL_ANALYSIS'")
        table = "GAP_FILL_ANALYSIS" if cursor.fetchone() else "ASSUMED_FRAMES"
        cols = pd.read_sql_query(f"SELECT * FROM {table} LIMIT 0", conn).columns
        if "ANIMALID" not in cols:
            raise Exception(
                f"This database predates the ANIMALID column, so the Animal ID "
                f"cannot be read automatically. Please regenerate it with the "
                f"current 1.lmt_gap_fill.py."
            )
        stored_ids = pd.read_sql_query(f"SELECT DISTINCT ANIMALID FROM {table}", conn)["ANIMALID"].tolist()
    finally:
        conn.close()

    if len(stored_ids) != 1:
        raise Exception(
            f"Expected exactly one Animal ID in this database, found "
            f"{sorted(int(i) for i in stored_ids)}. This should not happen "
            f"for a single gap-fill/binary-search run; please regenerate it "
            f"from a single-animal run."
        )
    return int(stored_ids[0])

# Main pipeline
def run(analysis_db, video_paths, output_folder, n_samples, qc_mode, overwrite=False):
    """
    Returns (summary_str, warnings_list).

    Raises Exception on any fatal error, including the "output already
    exists" guard below when `overwrite` is not set.
    """
    animal_id = _load_animal_id(analysis_db)

    timestamp     = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    pool_folder   = os.path.join(output_folder, f"{qc_mode}_A{animal_id}_{timestamp}")
    screenshot_folder = os.path.join(pool_folder, "Screenshots")

    # Guard against silently overwriting a previous run's screenshots/SQLite
    # for this pool + date (e.g. re-running the sampler twice in one day).
    if os.path.isdir(pool_folder) and os.listdir(pool_folder):
        if not overwrite:
            raise Exception(
                f"Aborted: output folder already exists and contains files:\n{pool_folder}\n"
                f"Pass --overwrite to overwrite it."
            )

    os.makedirs(screenshot_folder, exist_ok=True)

    conn = sqlite3.connect(analysis_db)
    try:
        # Read from GAP_FILL_ANALYSIS (2.lmt_binary_search.py output); fall back to legacy
        # ASSUMED_FRAMES table for backward compatibility with old outputs.
        cursor  = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='GAP_FILL_ANALYSIS'")
        table   = "GAP_FILL_ANALYSIS" if cursor.fetchone() else "ASSUMED_FRAMES"
        df_full = pd.read_sql_query(f"SELECT * FROM {table} ORDER BY FRAMENUMBER", conn)
    finally:
        conn.close()

    if len(df_full) == 0:
        raise Exception(f"No rows found in {table} in the selected SQLite.")

    df, mode_label = filter_pool(df_full, qc_mode)

    if len(df) == 0:
        raise Exception(f"No eligible rows found for pool '{mode_label}'.\nCheck that the selected SQLite was produced by 2.lmt_binary_search.py.")

    total_available = len(df)

    if n_samples > total_available:
        raise Exception(f"Requested {n_samples:,} samples but only {total_available:,} are available in the '{mode_label}' pool.\nPlease enter a number \u2264 {total_available:,}.")

    # A fresh random seed is generated and used for this draw (so the run is
    # still effectively random each time, matching prior behavior), but the
    # seed is recorded and reported below so this exact sample can be
    # reproduced later if needed 
    sample_seed = random.randint(0, 2**31 - 1)
    df_sample = _sample_proportional_to_gap(df, n_samples, sample_seed).sort_values("FRAMENUMBER").reset_index(drop=True)

    video_map, skipped_videos, fps_mismatches = build_video_map(video_paths)
    if not video_map:
        raise Exception("No valid LMT videos found.")

    video_warnings = []
    if skipped_videos:
        video_warnings.append(
            "The following video file(s) could not be parsed for a starting "
            "frame number (expected a 't<digits>' segment immediately before "
            "the file extension) and were excluded from this run:\n\n"
            + "\n".join(skipped_videos)
        )
    if fps_mismatches:
        video_warnings.append(
            f"The following video file(s) have a frame rate that does not "
            f"match the assumed {EXPECTED_VIDEO_FPS:.2f} fps "
            f"(DB_FPS={DB_FPS} / FRAME_CONVERSION={FRAME_CONVERSION}). "
            f"Frame alignment for these videos may be inaccurate:\n\n"
            + "\n".join(fps_mismatches)
        )
    results = []
    counter = 1

    for _, row in df_sample.iterrows():
        requested_frame = int(row["FRAMENUMBER"])

        screenshot_name = f"S{counter:04d}_A{animal_id}_TMP.png"
        screenshot_path = os.path.join(screenshot_folder, screenshot_name)
        resolved_frame, video_name = extract_frame(video_map, requested_frame, screenshot_path)
        if resolved_frame is None:
            continue

        final_screenshot_name = (
            f"S{counter:04d}_A{animal_id}_G{resolved_frame}_{video_name}.png")
        final_screenshot_path = os.path.join(screenshot_folder, final_screenshot_name)
        os.replace(screenshot_path, final_screenshot_path)

        # Gap boundary frames for three-panel display in 4.lmt_qc_validator.py.
        # Only present for ASSUMED rows; None for DETECTED.
        gap_start = row.get("GAP_START_FRAME")
        gap_end   = row.get("GAP_END_FRAME")

        results.append({
            "sample_id":              counter,
            "animal_id":              animal_id,
            "video":                  video_name,
            "frame_global":           resolved_frame,
            "requested_frame":        requested_frame,
            "IN_NEST":                row["IN_NEST"],
            "ASSUMPTION_TYPE":        row.get("ASSUMPTION_TYPE"),
            "FILL_SOURCE":            row.get("FILL_SOURCE"),
            "GAP_START_FRAME":        gap_start,
            "GAP_END_FRAME":          gap_end,
            "screenshot":             final_screenshot_name,
            # QC_MODE is read by 4.lmt_qc_validator.py to determine display/metrics context
            "QC_MODE":                qc_mode,
        })
        counter += 1

    if not results:
        raise Exception("No screenshots could be extracted. Check that the videos cover the sampled frame numbers.")

    out_db = os.path.join(pool_folder, f"lmt_qc_sampler_{qc_mode}_A{animal_id}_{timestamp}.sqlite")

    conn = sqlite3.connect(out_db)
    pd.DataFrame(results).to_sql("QC_ASSUMED_SAMPLES", conn, if_exists="replace", index=False)
    conn.close()

    summary = (
        f"Pool: {mode_label}\n"
        f"  Available:  {total_available:,}\n"
        f"  Extracted:  {len(results):,}\n"
        f"  Sample seed: {sample_seed}\n"
        f"  SQLite:     {out_db}\n"
        f"  Folder:     {screenshot_folder}"
    )
    return summary, video_warnings


# CLI
ALL_POOLS = [QC_MODE_DETECTED, QC_MODE_BINARY_SEARCH, QC_MODE_LOGIC]


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="LMT Random QC Sampler: draws random QC samples from a "
                    "2.lmt_binary_search.py output and extracts their "
                    "screenshots for manual quality control."
    )
    parser.add_argument(
        "-i", "--input", required=True,
        help="Path to lmt_binary_search_A<animal_id>_<timestamp>.sqlite "
             "(2.lmt_binary_search.py output).",
    )
    parser.add_argument(
        "-v", "--videos", required=True, nargs="+",
        help="One or more LMT video files (*.mp4).",
    )
    parser.add_argument(
        "-o", "--output-folder", required=True,
        help="Directory to write per-pool results into.",
    )
    parser.add_argument(
        "-n", "--samples", type=int, default=100,
        help="Number of samples to draw, applied independently per pool (default: 100).",
    )
    parser.add_argument(
        "--pools", nargs="+", choices=ALL_POOLS, default=list(ALL_POOLS),
        help="Which QC pool(s) to sample from (default: all three).",
    )
    parser.add_argument(
        "--overwrite", action="store_true",
        help="Overwrite a pool's output folder if it already contains files "
             "from an earlier run today. Without this flag, that pool aborts.",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    if not os.path.isfile(args.input):
        print(f"ERROR: Please provide a valid SQLite database. Not found: {args.input}", file=sys.stderr)
        return 1
    missing_videos = [v for v in args.videos if not os.path.isfile(v)]
    if missing_videos:
        print("ERROR: Video file(s) not found:\n  " + "\n  ".join(missing_videos), file=sys.stderr)
        return 1
    if not os.path.isdir(args.output_folder):
        print(f"ERROR: Please provide a valid output folder. Not found: {args.output_folder}", file=sys.stderr)
        return 1
    if args.samples <= 0:
        print("ERROR: --samples must be a positive integer.", file=sys.stderr)
        return 1

    summaries = []
    errors    = []
    try:
        for qc_mode in args.pools:
            try:
                summary, warnings = run(
                    args.input, args.videos, args.output_folder,
                    args.samples, qc_mode, overwrite=args.overwrite,
                )
                summaries.append(summary)
                for w in warnings:
                    print(f"WARNING [{qc_mode}]: {w}", file=sys.stderr)
            except Exception as e:
                errors.append(f"{qc_mode}: {e}")
    finally:
        # Release any cached video handles opened during this run instead
        # of leaving them open for the lifetime of the process.
        _release_all_captures()

    if summaries:
        print("Random QC Sample Extraction Complete\n")
        print(f"Samples per pool requested: {args.samples:,}\n")
        print("\n\n".join(summaries))
    if errors:
        print("\nERRORS:", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)

    return 0 if summaries else 1


if __name__ == "__main__":
    sys.exit(main())
