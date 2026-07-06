import os
import sqlite3
import numpy as np
import pandas as pd
from datetime import datetime
from tkinter import *
from tkinter import filedialog, messagebox

# Main analysis
def run_analysis(input_db, output_folder, animal_id, nest_xmin, nest_xmax, nest_ymin, nest_ymax, buffer_xmin, buffer_xmax, buffer_ymin, buffer_ymax):

    # Validate animal_id and use a parameterized query (was an f-string SQL injection risk).
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

    def in_roi_vec(x, y, roi):
        # Vectorized equivalent of the original scalar in_roi() helper.
        # Boundary semantics (strict <) are unchanged from the original.
        return (roi["xmin"] < x) & (x < roi["xmax"]) & (roi["ymin"] < y) & (y < roi["ymax"])

    frames_arr = df["FRAMENUMBER"].to_numpy(dtype=np.int64)
    x_arr      = df["MASS_X"].to_numpy(dtype=np.float64)
    y_arr      = df["MASS_Y"].to_numpy(dtype=np.float64)

    n = len(frames_arr)

    # DETECTED rows: every detected frame gets exactly one row, identical to
    # the original code's behavior (each frame is visited as "current" once,
    # plus the final frame handled separately in the original — mathematically
    # equivalent to applying in_roi() to every detected frame here).
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

        # Missing validation: consecutive detected frames for this animal must
        # strictly increase. A duplicate or out-of-order FRAMENUMBER would
        # previously be silently treated as "no gap" (gap > 1 is False) with
        # no indication of a data-quality problem.
        if np.any(gap <= 0):
            bad_idx = np.nonzero(gap <= 0)[0]
            examples = [(int(f1[i]), int(f2[i])) for i in bad_idx[:10]]
            raise Exception(
                f"Found {len(bad_idx):,} duplicate or non-increasing FRAMENUMBER "
                f"pair(s) for Animal ID {animal_id} after ordering by FRAMENUMBER. "
                f"This indicates duplicate or out-of-order detection rows.\n"
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
            # frames) plus vectorized repeat/offset — avoids the original
            # per-missing-frame Python dict-append loop.
            offsets = np.concatenate([np.arange(s) for s in sizes])
            frame_numbers_assumed = np.repeat(starts, sizes) + offsets
            gap_start_assumed     = np.repeat(f1[gap_idx], sizes)
            gap_end_assumed       = np.repeat(f2[gap_idx], sizes)

            in_nest_value_per_gap = np.where(
                in_nest_start[gap_idx] & in_buffer_end[gap_idx], 1, -1
            )
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
    })

    assumed_df = pd.DataFrame({
        "FRAMENUMBER":     frame_numbers_assumed,
        "IN_NEST":         in_nest_assumed,
        "ASSUMPTION_TYPE": "ASSUMED",
        "GAP_START_FRAME": gap_start_assumed,
        "GAP_END_FRAME":   gap_end_assumed,
    })

    # Concatenating and sorting by FRAMENUMBER reproduces the original row
    # order exactly, since ASSUMED frames only ever fall strictly between two
    # DETECTED frames (no overlaps, given the duplicate/order check above).
    output_df = pd.concat([detected_df, assumed_df], ignore_index=True)
    output_df = output_df.sort_values("FRAMENUMBER").reset_index(drop=True)

    current_date_time = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    sqlite_name = f"lmt_gap_fill_{current_date_time}.sqlite"

    output_sqlite = os.path.join(output_folder, sqlite_name)

    conn = sqlite3.connect(output_sqlite)
    output_df.to_sql("GAP_FILL_ANALYSIS", conn, if_exists="replace", index=False)
    conn.close()

    messagebox.showinfo(
        "Analysis Complete",
        f"Gap Fill Analysis Complete\n\n"
        f"Detected Frames:     {detected_rows:,}\n"
        f"  - IN NEST:         {detected_in_nest_frames:,}\n"
        f"  - NOT IN NEST:     {detected_not_in_nest_frames:,}\n\n"
        f"Assumed Frames:      {assumed_rows:,}\n"
        f"Total Frames:        {len(output_df):,}\n\n"
        f"SQLite Output:\n{output_sqlite}\n\n"
    )


# GUI
input_db_path      = ""
output_folder_path = ""

def select_database():
    global input_db_path
    input_db_path = filedialog.askopenfilename(filetypes=[("SQLite Database", "*.sqlite *.db")])
    label_db.config(text=input_db_path)

def select_output_folder():
    global output_folder_path
    output_folder_path = filedialog.askdirectory()
    label_output.config(text=output_folder_path)

def start():
    try:
        if not input_db_path:
            raise Exception("Please select an LMT SQLite database")
        if not output_folder_path:
            raise Exception("Please select an output folder")
        run_analysis(
            input_db=input_db_path,
            output_folder=output_folder_path,
            animal_id=int(entry_animal.get()),
            nest_xmin=float(entry_nest_xmin.get()),
            nest_xmax=float(entry_nest_xmax.get()),
            nest_ymin=float(entry_nest_ymin.get()),
            nest_ymax=float(entry_nest_ymax.get()),
            buffer_xmin=float(entry_buffer_xmin.get()),
            buffer_xmax=float(entry_buffer_xmax.get()),
            buffer_ymin=float(entry_buffer_ymin.get()),
            buffer_ymax=float(entry_buffer_ymax.get()),
        )
    except Exception as e:
        messagebox.showerror("Error", str(e))

root = Tk()
root.title("LMT Gap Fill Assumption Generator")
root.geometry("800x850")

Label(root, text="LMT Gap Fill Assumption Generator", font=("Arial", 16, "bold")).pack(pady=10)

Button(root, text="Select LMT SQLite Database", command=select_database).pack()
label_db = Label(root, text="No database selected", wraplength=700)
label_db.pack(pady=5)

Label(root, text="Animal ID").pack()
entry_animal = Entry(root); entry_animal.insert(0, "1"); entry_animal.pack()

Label(root, text="Nest X Minimum").pack()
entry_nest_xmin = Entry(root); entry_nest_xmin.insert(0, "100"); entry_nest_xmin.pack()

Label(root, text="Nest X Maximum").pack()
entry_nest_xmax = Entry(root); entry_nest_xmax.insert(0, "250"); entry_nest_xmax.pack()

Label(root, text="Nest Y Minimum").pack()
entry_nest_ymin = Entry(root); entry_nest_ymin.insert(0, "50"); entry_nest_ymin.pack()

Label(root, text="Nest Y Maximum").pack()
entry_nest_ymax = Entry(root); entry_nest_ymax.insert(0, "200"); entry_nest_ymax.pack()

Label(root, text="Buffer X Minimum").pack()
entry_buffer_xmin = Entry(root); entry_buffer_xmin.insert(0, "80"); entry_buffer_xmin.pack()

Label(root, text="Buffer X Maximum").pack()
entry_buffer_xmax = Entry(root); entry_buffer_xmax.insert(0, "270"); entry_buffer_xmax.pack()

Label(root, text="Buffer Y Minimum").pack()
entry_buffer_ymin = Entry(root); entry_buffer_ymin.insert(0, "30"); entry_buffer_ymin.pack()

Label(root, text="Buffer Y Maximum").pack()
entry_buffer_ymax = Entry(root); entry_buffer_ymax.insert(0, "220"); entry_buffer_ymax.pack()

Button(root, text="Select Output Folder", command=select_output_folder).pack(pady=10)
label_output = Label(root, text="No output folder selected", wraplength=700)
label_output.pack()

Button(root, text="RUN ANALYSIS", command=start, bg="green", fg="white", width=25, height=2).pack(pady=20)

root.mainloop()