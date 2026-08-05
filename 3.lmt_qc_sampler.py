import os
import numpy as np
import re
import random
import cv2

from lmt_common import (DB_FPS, FRAME_CONVERSION, QC_MODE_DETECTED, QC_MODE_BINARY_SEARCH, 
                        QC_MODE_LOGIC, EXPECTED_VIDEO_FPS, build_video_map, find_nearest_frame_candidates, _read_frame_from_video, 
                        _release_all_captures, compute_qc_pool_mask)

import sqlite3
import pandas as pd
from datetime import datetime
from tkinter import *
from tkinter import filedialog, messagebox

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
def run(analysis_db, video_paths, output_folder, n_samples, qc_mode):
    animal_id = _load_animal_id(analysis_db)

    timestamp     = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    pool_folder   = os.path.join(output_folder, f"{qc_mode}_A{animal_id}_{timestamp}")
    screenshot_folder = os.path.join(pool_folder, "Screenshots")

    # Guard against silently overwriting a previous run's screenshots/SQLite
    # for this pool + date (e.g. re-running the sampler twice in one day).
    if os.path.isdir(pool_folder) and os.listdir(pool_folder):
        proceed = messagebox.askyesno(
            "Output Already Exists",
            f"The output folder already contains files:\n{pool_folder}\n\n"
            f"Continuing will overwrite existing screenshots/SQLite in this folder.\n\n"
            f"Do you want to continue?"
        )
        if not proceed:
            raise Exception(
                f"Aborted: output folder already exists and contains files:\n{pool_folder}"
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
    if video_warnings:
        messagebox.showwarning("Video Warnings", "\n\n".join(video_warnings))

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
            "ASSUMPTION_TYPE":        row.get("ASSUMPTION_TYPE", "ASSUMED"),
            "FILL_SOURCE":            row.get("FILL_SOURCE", qc_mode),
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

    return (
        f"Pool: {mode_label}\n"
        f"  Available:  {total_available:,}\n"
        f"  Extracted:  {len(results):,}\n"
        f"  Sample seed: {sample_seed}\n"
        f"  SQLite:     {out_db}\n"
        f"  Folder:     {screenshot_folder}"
    )

# GUI
analysis_db = ""
videos      = []
out_folder  = ""

def select_db():
    global analysis_db
    analysis_db = filedialog.askopenfilename(filetypes=[("SQLite", "*.sqlite")])
    label_db.config(text=analysis_db)

def select_videos():
    global videos
    videos = list(filedialog.askopenfilenames(filetypes=[("MP4", "*.mp4")]))
    label_vid.config(text=f"{len(videos)} video(s) selected")

def select_out():
    global out_folder
    out_folder = filedialog.askdirectory()
    label_out.config(text=out_folder)

def start():
    try:
        if not analysis_db:
            messagebox.showerror("Error", "Please select lmt_binary_search__A<animal_id>_<timestamp>.sqlite.")
            return
        if not videos:
            messagebox.showerror("Error", "Please select at least one LMT video.")
            return
        if not out_folder:
            messagebox.showerror("Error", "Please select an output folder.")
            return

        raw = entry_samples.get().strip()
        if not raw.isdigit() or int(raw) <= 0:
            messagebox.showerror(
                "Error", "Number of samples must be a positive integer.")
            return
        n_samples = int(raw)

        selected_pools = [
            mode for mode, var in [
                (QC_MODE_DETECTED,      var_detected),
                (QC_MODE_BINARY_SEARCH, var_binary),
                (QC_MODE_LOGIC,         var_logic),
            ] if var.get()
        ]

        if not selected_pools:
            messagebox.showerror("Error", "Please select at least one QC pool.")
            return

        summaries = []
        errors    = []
        try:
            for qc_mode in selected_pools:
                try:
                    summary = run(analysis_db, videos, out_folder, n_samples, qc_mode)
                    summaries.append(summary)
                except Exception as e:
                    errors.append(f"{qc_mode}: {e}")
        finally:
            # Release any cached video handles opened during this run instead
            # of leaving them open for the lifetime of the application.
            _release_all_captures()

        msg = ""
        if summaries:
            msg += "Random QC Sample Extraction Complete\n\n"
            msg += f"Samples per pool requested: {n_samples:,}\n\n"
            msg += "\n\n".join(summaries)
        if errors:
            msg += "\n\nERRORS:\n" + "\n".join(errors)

        messagebox.showinfo("Done", msg)

    except Exception as e:
        messagebox.showerror("Error", str(e))


root = Tk()
root.title("LMT Random QC Sampler")
root.geometry("750x520")

Label(root, text="LMT Random QC Sampler",
      font=("Arial", 16, "bold")).pack(pady=10)

Label(root,
      text=("Randomly selects frames from the lmt_binary_search__A<animal_id>_<timestamp>.sqlite\n"
          "and extracts their screenshots for manual quality control.\n\n"
          "Select QC pool(s). Each type produces its own SQLite\n"
          "and screenshot folder, labelled with the type and a shared timestamp."),
      font=("Arial", 10), justify=CENTER).pack(pady=5)

Button(root, text="Select lmt_binary_search__A<animal_id>_<timestamp>.sqlite", command=select_db).pack(pady=5)
label_db = Label(root, text="No file selected", wraplength=700)
label_db.pack()

Button(root, text="Select LMT Videos", command=select_videos).pack(pady=5)
label_vid = Label(root, text="No videos selected")
label_vid.pack()

Button(root, text="Select Output Folder", command=select_out).pack(pady=5)
label_out = Label(root, text="No output folder selected", wraplength=700)
label_out.pack()

Label(root, text="How many samples would you like? (applied per pool)").pack(pady=(15, 2))
entry_samples = Entry(root)
entry_samples.insert(0, "100")
entry_samples.pack()

Label(root, text="Select QC pool(s):", font=("Arial", 11, "bold")).pack(pady=(14, 2))
pool_frame = Frame(root)
pool_frame.pack()

var_detected = BooleanVar(value=True)
var_binary   = BooleanVar(value=True)
var_logic    = BooleanVar(value=True)

Checkbutton(pool_frame, text="DETECTED rows", variable=var_detected, font=("Arial", 10)).grid(row=0, column=0, padx=15)
Checkbutton(pool_frame, text="BINARY_SEARCH rows", variable=var_binary, font=("Arial", 10)).grid(row=0, column=1, padx=15)
Checkbutton(pool_frame, text="LOGIC rows", variable=var_logic, font=("Arial", 10)).grid(row=0, column=2, padx=15)

Button(root, text="RUN SAMPLING", command=start, bg="green", fg="white", width=30, height=2).pack(pady=20)

root.mainloop()