import argparse
import os
import sqlite3
import sys
import numpy as np
import pandas as pd
from datetime import datetime

# Main analysis
def run_analysis(input_db, output_folder, animal_id, nest_xmin, nest_xmax, nest_ymin, nest_ymax, buffer_xmin, buffer_xmax, buffer_ymin, buffer_ymax):

    # Validate animal_id and use a parameterized query
    try:
        animal_id = int(animal_id)
    except (TypeError, ValueError):
        raise Exception(f"Invalid Animal ID: {animal_id!r}. Animal ID must be an integer.")

    conn = sqlite3.connect(input_db)
    df = pd.read_sql_query(
        """
        SELECT
            FRAMENUMBER,
            MASS_X,
            MASS_Y
        FROM DETECTION
        WHERE ANIMALID = ?
        ORDER BY FRAMENUMBER
        """,
        conn,
        params=(animal_id,)
    )
    conn.close()

    if len(df) == 0:
        raise Exception(f"No DETECTION rows found for Animal ID {animal_id}")

    NEST        = {"xmin": nest_xmin,   "xmax": nest_xmax,   "ymin": nest_ymin,   "ymax": nest_ymax}
    NEST_BUFFER = {"xmin": buffer_xmin, "xmax": buffer_xmax, "ymin": buffer_ymin, "ymax": buffer_ymax}

    if not (nest_xmin < nest_xmax and nest_ymin < nest_ymax):
        raise Exception(
            f"Invalid nest ROI: xmin/xmax must satisfy xmin < xmax and "
            f"ymin < ymax. Got xmin={nest_xmin}, xmax={nest_xmax}, "
            f"ymin={nest_ymin}, ymax={nest_ymax}."
        )
    if not (buffer_xmin < buffer_xmax and buffer_ymin < buffer_ymax):
        raise Exception(
            f"Invalid buffer ROI: xmin/xmax must satisfy xmin < xmax and "
            f"ymin < ymax. Got xmin={buffer_xmin}, xmax={buffer_xmax}, "
            f"ymin={buffer_ymin}, ymax={buffer_ymax}."
        )
    if not (buffer_xmin <= nest_xmin and buffer_xmax >= nest_xmax and
            buffer_ymin <= nest_ymin and buffer_ymax >= nest_ymax):
        raise Exception(
            f"Invalid ROI configuration: the buffer ROI must fully contain "
            f"the nest ROI. Nest=({nest_xmin}, {nest_xmax}, {nest_ymin}, "
            f"{nest_ymax}), Buffer=({buffer_xmin}, {buffer_xmax}, "
            f"{buffer_ymin}, {buffer_ymax})."
        )

    def in_roi_vec(x, y, roi):
        return (roi["xmin"] < x) & (x < roi["xmax"]) & (roi["ymin"] < y) & (y < roi["ymax"])

    frames_arr = df["FRAMENUMBER"].to_numpy(dtype=np.int64)
    x_arr      = df["MASS_X"].to_numpy(dtype=np.float64)
    y_arr      = df["MASS_Y"].to_numpy(dtype=np.float64)

    n = len(frames_arr)

    detected_in_nest = in_roi_vec(x_arr, y_arr, NEST).astype(int)

    detected_rows = n
    detected_in_nest_frames = int(detected_in_nest.sum())
    detected_not_in_nest_frames = detected_rows - detected_in_nest_frames

    # ASSUMED rows: derived from every consecutive pair of detected frames.
    if n > 1:
        f1 = frames_arr[:-1]
        f2 = frames_arr[1:]
        x1, y1 = x_arr[:-1], y_arr[:-1]
        x2, y2 = x_arr[1:],  y_arr[1:]

        gap = f2 - f1

        # Defensive validation (Issue #5): consecutive detected frames for
        # this animal must strictly increase. This is no longer expected
        # to ever fail for input produced by the current
        # 0.Preprocessing.py, which deduplicates DETECTION on
        # (FRAMENUMBER, ANIMALID) before this script ever runs -- so for a
        # single ANIMALID, FRAMENUMBER is guaranteed unique and, combined
        # with the ORDER BY FRAMENUMBER above, strictly increasing. This
        # check exists for the case where that invariant doesn't hold
        # (e.g. this script run directly against a raw, non-deduplicated
        # LMT export): a duplicate or out-of-order FRAMENUMBER would
        # otherwise be silently treated as having "no gap" (gap > 1 is
        # False), corrupting gap sizing and the in-nest time estimate
        # without any indication something was wrong. Fail loudly here
        # instead.
        if np.any(gap <= 0):
            bad_idx = np.nonzero(gap <= 0)[0]
            examples = [(int(f1[i]), int(f2[i])) for i in bad_idx[:10]]
            raise Exception(
                f"Found {len(bad_idx):,} duplicate or non-increasing FRAMENUMBER "
                f"pair(s) for Animal ID {animal_id} after ordering by FRAMENUMBER. "
                f"This indicates duplicate or out-of-order DETECTION rows for "
                f"this animal, which 0.Preprocessing.py's deduplication should "
                f"already have resolved -- re-run this animal's data through "
                f"0.Preprocessing.py first.\n"
                f"Example (FRAMENUMBER, next FRAMENUMBER) pairs: {examples}"
            )

        in_nest_start = in_roi_vec(x1, y1, NEST)
        in_buffer_end = in_roi_vec(x2, y2, NEST_BUFFER)

        gap_mask  = gap > 1
        gap_sizes = np.where(gap_mask, gap - 1, 0)

        gap_idx = np.nonzero(gap_mask)[0]

        if gap_idx.size > 0:
            sizes  = gap_sizes[gap_idx]
            starts = f1[gap_idx] + 1

            # Build the missing FRAMENUMBER for every gap frame via a small
            # per-gap loop (bounded by number of gaps, not number of missing
            # frames) plus vectorized repeat/offset
            offsets = np.concatenate([np.arange(s) for s in sizes])
            frame_numbers_assumed = np.repeat(starts, sizes) + offsets
            gap_start_assumed     = np.repeat(f1[gap_idx], sizes)
            gap_end_assumed       = np.repeat(f2[gap_idx], sizes)

            in_nest_value_per_gap = np.where(in_nest_start[gap_idx] & in_buffer_end[gap_idx], 1, -1)
            in_nest_assumed = np.repeat(in_nest_value_per_gap, sizes)
        else:
            frame_numbers_assumed = np.array([], dtype=np.int64)
            gap_start_assumed     = np.array([], dtype=np.int64)
            gap_end_assumed       = np.array([], dtype=np.int64)
            in_nest_assumed       = np.array([], dtype=np.int64)
    else:
        frame_numbers_assumed = np.array([], dtype=np.int64)
        gap_start_assumed     = np.array([], dtype=np.int64)
        gap_end_assumed       = np.array([], dtype=np.int64)
        in_nest_assumed       = np.array([], dtype=np.int64)

    assumed_rows = int(frame_numbers_assumed.size)

    detected_df = pd.DataFrame({
        "FRAMENUMBER":     frames_arr,
        "IN_NEST":         detected_in_nest,
        "ASSUMPTION_TYPE": "DETECTED",
        "GAP_START_FRAME": None,
        "GAP_END_FRAME":   None,
        "ANIMALID":        animal_id,
    })

    assumed_df = pd.DataFrame({
        "FRAMENUMBER":     frame_numbers_assumed,
        "IN_NEST":         in_nest_assumed,
        "ASSUMPTION_TYPE": "ASSUMED",
        "GAP_START_FRAME": gap_start_assumed,
        "GAP_END_FRAME":   gap_end_assumed,
        "ANIMALID":        animal_id,
    })

    # Concatenating and sorting by FRAMENUMBER reproduces the original row
    # order exactly, since ASSUMED frames only ever fall strictly between two
    # DETECTED frames (no overlaps, given the duplicate/order check above).
    output_df = pd.concat([detected_df, assumed_df], ignore_index=True)
    output_df = output_df.sort_values("FRAMENUMBER").reset_index(drop=True)

    current_date_time = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    sqlite_name = f"lmt_gap_fill_A{animal_id}_{current_date_time}.sqlite"

    output_sqlite = os.path.join(output_folder, sqlite_name)

    conn = sqlite3.connect(output_sqlite)
    output_df.to_sql("GAP_FILL_ANALYSIS", conn, if_exists="replace", index=False)

    # Git Issue #22 follow-up: persist the Nest/Buffer ROI this run used,
    # so 2.lmt_binary_search.py can automatically reuse it for the Nest ROI
    # overlay (and the summary report) without the user having to retype
    # --nest-xmin/--nest-xmax/--nest-ymin/--nest-ymax on the CLI a second
    # time. One row, same {"xmin","xmax","ymin","ymax"} values already
    # validated above for NEST/NEST_BUFFER.
    roi_metadata_df = pd.DataFrame([{
        "ANIMALID":    animal_id,
        "NEST_XMIN":   nest_xmin,   "NEST_XMAX":   nest_xmax,
        "NEST_YMIN":   nest_ymin,   "NEST_YMAX":   nest_ymax,
        "BUFFER_XMIN": buffer_xmin, "BUFFER_XMAX": buffer_xmax,
        "BUFFER_YMIN": buffer_ymin, "BUFFER_YMAX": buffer_ymax,
    }])
    roi_metadata_df.to_sql("ROI_METADATA", conn, if_exists="replace", index=False)

    conn.close()

    print(
        f"Gap Fill Analysis Complete\n\n"
        f"Detected Frames:     {detected_rows:,}\n"
        f"  - IN NEST:         {detected_in_nest_frames:,}\n"
        f"  - NOT IN NEST:     {detected_not_in_nest_frames:,}\n\n"
        f"Assumed Frames:      {assumed_rows:,}\n"
        f"Total Frames:        {len(output_df):,}\n\n"
        f"SQLite Output:\n{output_sqlite}\n"
    )


# CLI
def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="LMT Gap Fill Assumption Generator: classifies detected "
                    "frames and logic-fills/flags gaps for one animal."
    )
    parser.add_argument(
        "-i", "--input", required=True,
        help="Path to <SQLite_Name>_processed.sqlite (0.Preprocessing.py output).",
    )
    parser.add_argument(
        "-o", "--output-folder", required=True,
        help="Directory to write the gap-fill result SQLite into.",
    )
    parser.add_argument("--animal-id", type=int, required=True, help="Animal ID to process. Required, no default.")
    parser.add_argument("--nest-xmin", type=float, required=True, help="Nest ROI X minimum. Required, no default.")
    parser.add_argument("--nest-xmax", type=float, required=True, help="Nest ROI X maximum. Required, no default.")
    parser.add_argument("--nest-ymin", type=float, required=True, help="Nest ROI Y minimum. Required, no default.")
    parser.add_argument("--nest-ymax", type=float, required=True, help="Nest ROI Y maximum. Required, no default.")
    parser.add_argument("--buffer-xmin", type=float, required=True, help="Buffer ROI X minimum. Required, no default.")
    parser.add_argument("--buffer-xmax", type=float, required=True, help="Buffer ROI X maximum. Required, no default.")
    parser.add_argument("--buffer-ymin", type=float, required=True, help="Buffer ROI Y minimum. Required, no default.")
    parser.add_argument("--buffer-ymax", type=float, required=True, help="Buffer ROI Y maximum. Required, no default.")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    if not os.path.isfile(args.input):
        print(f"ERROR: Please provide a valid LMT SQLite database. Not found: {args.input}", file=sys.stderr)
        return 1
    if not os.path.isdir(args.output_folder):
        print(f"ERROR: Please provide a valid output folder. Not found: {args.output_folder}", file=sys.stderr)
        return 1

    try:
        run_analysis(
            input_db=args.input,
            output_folder=args.output_folder,
            animal_id=args.animal_id,
            nest_xmin=args.nest_xmin,
            nest_xmax=args.nest_xmax,
            nest_ymin=args.nest_ymin,
            nest_ymax=args.nest_ymax,
            buffer_xmin=args.buffer_xmin,
            buffer_xmax=args.buffer_xmax,
            buffer_ymin=args.buffer_ymin,
            buffer_ymax=args.buffer_ymax,
        )
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())