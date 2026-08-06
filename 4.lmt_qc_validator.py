import os
import re
import cv2

from lmt_common import (DB_FPS, FRAME_CONVERSION, QC_MODE_DETECTED, QC_MODE_BINARY_SEARCH, 
                        QC_MODE_LOGIC, QC_MODE_ASSUMED, EXPECTED_VIDEO_FPS, build_video_map,
                        find_nearest_frame_candidates, _read_frame_from_video, _release_all_captures, compute_qc_pool_mask)

import sqlite3
import pandas as pd
from datetime import datetime
from tkinter import *
from tkinter import filedialog, messagebox
from PIL import Image, ImageTk

date_string = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
animal_label = "Aunknown"

# Video helpers
def extract_frame_to_label(video_map, global_frame, label_widget, thumb_size=(420, 340)):
    """
    Extract a frame from video_map and display it in a Tkinter Label widget.

    Uses the same nearest-available-frame resolution as scripts 2 and 3
    (exact match, else nearest of preceding/succeeding, preceding wins ties)
    instead of a backward-only, frame-by-frame linear scan. This is both
    much faster for frames far from any covering video, and behaviorally
    consistent with the frame substitution the reviewer already saw in
    2.lmt_binary_search.py for the same gap

    Returns True on success, False on failure.
    """
    import tempfile, uuid
    tmp_path = os.path.join(tempfile.gettempdir(),
                            f"lmt_qc_{uuid.uuid4().hex}.png")
    found = False
    try:
        for resolved_frame, video_entry in find_nearest_frame_candidates(video_map, global_frame):
            if _read_frame_from_video(video_entry, resolved_frame, tmp_path):
                found = True
                break

        if found and os.path.exists(tmp_path):
            img   = Image.open(tmp_path)
            img.thumbnail(thumb_size)
            photo = ImageTk.PhotoImage(img)
            label_widget.config(image=photo, text="")
            label_widget.image = photo   # keep reference
            return True

        label_widget.config(image="", text="[Frame not available]", fg="#888888")
        label_widget.image = None
        return False
    finally:
        # Always clean up the temp file, even if Image.open() raised
        # (previously the temp file was only removed after a successful
        # open, leaking a file per failure).
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


# Sentinel values stored in MANUAL_QC column
#   None / NaN  = not yet labelled
#   1           = IN NEST
#   0           = OUT OF NEST
#
# For ASSUMED QC:
#   Rows with IN_NEST = -1 are excluded entirely (not presented to the user).
#
# For DETECTED QC:
#   All DETECTED rows are eligible; IN_NEST is always 0 or 1.
#
# Backward compatibility:
#   Old 3.lmt_qc_sampler.py outputs have no QC_MODE column; they default to ASSUMED.

# Global state
qc_db_path        = ""
screenshot_folder = ""
video_paths_list  = []   # LMT videos selected by user
video_map         = []   # built from video_paths_list
df                = None
current_index     = 0
active_qc_mode    = QC_MODE_ASSUMED   # resolved after load

# Database helpers
def load_database():
    """
    Load QC_ASSUMED_SAMPLES from a 3.lmt_qc_sampler.py output.

    Reads QC_MODE from the first row (if present) to determine which filter
    to apply.  Falls back to ASSUMED for backward compatibility with old outputs
    that lack the column.

    Returns (df_eligible, excluded_count, qc_mode_string).
    """
    global df, active_qc_mode

    conn     = sqlite3.connect(qc_db_path)
    df_full  = pd.read_sql_query("SELECT * FROM QC_ASSUMED_SAMPLES", conn)
    conn.close()

    # Resolve QC mode 
    if "QC_MODE" in df_full.columns and len(df_full) > 0:
        mode = str(df_full["QC_MODE"].iloc[0]).strip().upper()
        if mode == QC_MODE_DETECTED:
            qc_mode = QC_MODE_DETECTED
        elif mode == QC_MODE_BINARY_SEARCH:
            qc_mode = QC_MODE_BINARY_SEARCH
        elif mode == QC_MODE_LOGIC:
            qc_mode = QC_MODE_LOGIC
        else:
            # Covers old "ASSUMED" value and anything else
            qc_mode = QC_MODE_ASSUMED
    else:
        # Backward-compatible default
        qc_mode = QC_MODE_ASSUMED

    active_qc_mode = qc_mode

    # Filter eligible rows 
    mask, _label = compute_qc_pool_mask(df_full, qc_mode)

    df = df_full[mask].copy().reset_index(drop=True)

    if "MANUAL_QC" not in df.columns:
        df["MANUAL_QC"] = None

    excluded = len(df_full) - len(df)
    return df, excluded, qc_mode

def save_database():
    output_db   = os.path.join(screenshot_folder, f"lmt_qc_validator_{animal_label}_{date_string}.sqlite")
    conn        = sqlite3.connect(output_db)
    df.to_sql("QC_ASSUMED_SAMPLES", conn, if_exists="replace", index=False)
    conn.close()

def save_row(row_index):
    """
    Persist only the MANUAL_QC value for a single row.

    Previously every single label click triggered a full save_database()
    call, which rewrote the entire QC_ASSUMED_SAMPLES table to disk on every
    click - an avoidable, ever-growing cost as the sample count increases.
    The very first label of a session still creates the output file/table
    via a full save_database() call (so the table and all other columns
    exist on disk), after which each subsequent click only updates the one
    row that actually changed.
    """

    output_db   = os.path.join(screenshot_folder, f"lmt_qc_validator_{animal_label}_{date_string}.sqlite")

    if not os.path.exists(output_db):
        save_database()
        return

    row        = df.iloc[row_index]
    manual_val = row["MANUAL_QC"]
    manual_val = None if pd.isna(manual_val) else int(manual_val)

    conn = sqlite3.connect(output_db)
    try:
        conn.execute("UPDATE QC_ASSUMED_SAMPLES SET MANUAL_QC = ? WHERE sample_id = ?", (manual_val, int(row["sample_id"])))
        conn.commit()
    finally:
        conn.close()

def calculate_metrics():
    """
    Two-class confusion matrix.

    For both ASSUMED and DETECTED modes the logic is identical:
      Positive class = IN NEST  (IN_NEST == 1)
      Negative class = OUT OF NEST  (IN_NEST == 0)
      Algorithm prediction = IN_NEST column
      Human ground-truth   = MANUAL_QC column
    """
    completed_df = df[df["MANUAL_QC"].notna()].copy()
    if len(completed_df) == 0:
        return None

    # Defensive check: every row reaching this point should already have
    # IN_NEST in (0, 1) thanks to the filtering in load_database(). If a
    # stray IN_NEST == -1 (or any other unexpected value) ever slips
    # through, it is excluded here and reported, rather than being silently
    # counted as "predicted OUT" (which would previously happen via
    # `IN_NEST != 1` and corrupt accuracy/sensitivity/specificity with no
    # indication anything was wrong).
    invalid_mask = ~completed_df["IN_NEST"].isin([0, 1])
    if invalid_mask.any():
        n_invalid = int(invalid_mask.sum())
        messagebox.showwarning(
            "Data Warning",
            f"{n_invalid} labelled sample(s) have an IN_NEST value other "
            f"than 0 or 1 and were excluded from the confusion matrix / "
            f"metrics. This indicates a filtering issue upstream and "
            f"should be investigated."
        )
        completed_df = completed_df[~invalid_mask].copy()
        if len(completed_df) == 0:
            return None

    predicted_in  = completed_df[completed_df["IN_NEST"] == 1]
    tp = len(predicted_in[predicted_in["MANUAL_QC"] == 1])
    fp = len(predicted_in[predicted_in["MANUAL_QC"] == 0])

    predicted_out = completed_df[completed_df["IN_NEST"] == 0]
    fn = len(predicted_out[predicted_out["MANUAL_QC"] == 1])
    tn = len(predicted_out[predicted_out["MANUAL_QC"] == 0])

    total       = tp + tn + fp + fn
    accuracy    = (tp + tn) / total  if total > 0      else 0
    error_rate  = (fp + fn) / total  if total > 0      else 0
    sensitivity = tp / (tp + fn)     if (tp + fn) > 0  else 0
    specificity = tn / (tn + fp)     if (tn + fp) > 0  else 0

    return {
        "TP": tp, "TN": tn, "FP": fp, "FN": fn,
        "accuracy":      accuracy,
        "error_rate":    error_rate,
        "sensitivity":   sensitivity,
        "specificity":   specificity,
        "total_labeled": len(completed_df),
        "qc_mode":       active_qc_mode,
    }

# Display
def show_sample():
    global current_index

    row           = df.iloc[current_index]
    screenshot_nm = row["screenshot"]
    image_path    = os.path.join(screenshot_folder, screenshot_nm)

    is_assumed_mode = active_qc_mode in (
        QC_MODE_BINARY_SEARCH, QC_MODE_LOGIC, QC_MODE_ASSUMED)

    if is_assumed_mode:
        # Three-panel display: left boundary | QC frame | right boundary
        gap_start = row.get("GAP_START_FRAME")
        gap_end   = row.get("GAP_END_FRAME")
        has_boundaries = (
            video_map and
            pd.notna(gap_start) and pd.notna(gap_end)
        )

        if has_boundaries:
            panels_frame.pack(side=LEFT, padx=10)
            single_frame.pack_forget()
            extract_frame_to_label(video_map, int(gap_start), img_left, thumb_size=(300, 260))
            lbl_left_title.config(text=f"Before gap  (frame {int(gap_start)})")

            extract_frame_to_label(video_map, int(row["frame_global"]), img_center, thumb_size=(300, 260))
            lbl_center_title.config(text=f"QC frame  (frame {int(row['frame_global'])})")

            extract_frame_to_label(video_map, int(gap_end), img_right, thumb_size=(300, 260))
            lbl_right_title.config(text=f"After gap  (frame {int(gap_end)})")
        else:
            # No video map or no boundary info — fall back to single panel
            panels_frame.pack_forget()
            single_frame.pack(side=LEFT, padx=20)
            if os.path.exists(image_path):
                img   = Image.open(image_path)
                img.thumbnail((900, 700))
                photo = ImageTk.PhotoImage(img)
                image_label.config(image=photo)
                image_label.image = photo
            else:
                messagebox.showerror("Missing Screenshot", image_path)
                return
    else:
        # DETECTED mode: single panel as before
        panels_frame.pack_forget()
        single_frame.pack(side=LEFT, padx=20)
        if not os.path.exists(image_path):
            messagebox.showerror("Missing Screenshot", image_path)
            return
        img   = Image.open(image_path)
        img.thumbnail((900, 700))
        photo = ImageTk.PhotoImage(img)
        image_label.config(image=photo)
        image_label.image = photo

    sample_text.config(text=f"Sample {current_index + 1} / {len(df)}")
    video_text.config(text=f"Video: {row['video']}")
    frame_text.config(text=f"Global Frame: {row['frame_global']}")

    #  Mode-aware labels 
    row_type = row.get("ASSUMPTION_TYPE", active_qc_mode)
    assumption_text.config(text=f"Row Type: {row_type}")

    # Gap info is only meaningful for ASSUMED rows
    if active_qc_mode in (QC_MODE_BINARY_SEARCH, QC_MODE_LOGIC, QC_MODE_ASSUMED):
        gap_start = row.get("GAP_START_FRAME", None)
        gap_end   = row.get("GAP_END_FRAME",   None)
        if pd.notna(gap_start) and pd.notna(gap_end):
            gap_text.config(text=f"Gap: Frame {int(gap_start)} \u2192 {int(gap_end)}")
        else:
            gap_text.config(text="Gap: N/A")
    else:
        gap_text.config(text="")

    in_nest_val = int(row["IN_NEST"])
    if in_nest_val == 1:
        prediction_text.config(text="Algorithm: IN NEST", fg="green")
    else:
        prediction_text.config(text="Algorithm: OUT OF NEST", fg="red")

    manual_value = row["MANUAL_QC"]
    if pd.isna(manual_value):
        manual_text.config(text="Manual QC: Not labelled yet", fg="black")
    elif int(manual_value) == 1:
        manual_text.config(text="Manual QC: IN NEST", fg="green")
    elif int(manual_value) == 0:
        manual_text.config(text="Manual QC: OUT OF NEST", fg="red")

    # Update mode banner
    banner_map = {
        QC_MODE_DETECTED:      ("QC MODE: LMT-DETECTED ROWS",        "#0055cc"),
        QC_MODE_BINARY_SEARCH: ("QC MODE: BINARY-SEARCH-FILLED ROWS", "#006622"),
        QC_MODE_LOGIC:         ("QC MODE: LOGIC-FILLED ROWS",         "#774400"),
        QC_MODE_ASSUMED:       ("QC MODE: ASSUMED ROWS (LEGACY)",     "#555555"),
    }
    btext, bcolor = banner_map.get(active_qc_mode, ("QC MODE: UNKNOWN", "#000000"))
    mode_banner.config(text=btext, fg=bcolor)

# Actions
def set_manual_qc(value):
    global current_index
    df.at[current_index, "MANUAL_QC"] = value
    save_row(current_index)
    # Auto-advance: pressing A or D immediately moves to the next sample.
    # next_sample() handles both advancement and end-of-session finalisation.
    next_sample()

def previous_sample():
    global current_index
    if current_index > 0:
        current_index -= 1
        show_sample()

def next_sample():
    global current_index

    completed_df = df[df["MANUAL_QC"].notna()].copy()

    if current_index < len(df) - 1:
        current_index += 1
        show_sample()
    else:
        save_database()
        metrics = calculate_metrics()
        if metrics is None:
            messagebox.showinfo("Done", "No labelled samples found.")
            return

        completed_df = df[df["MANUAL_QC"].notna()].copy()
        completed_df = completed_df[completed_df["IN_NEST"].isin([0, 1])]
        fp_files = completed_df[(completed_df["IN_NEST"] == 1) & (completed_df["MANUAL_QC"] == 0)]["screenshot"].tolist()
        fn_files = completed_df[(completed_df["IN_NEST"] == 0) & (completed_df["MANUAL_QC"] == 1)]["screenshot"].tolist()

        qc_mode      = metrics["qc_mode"]
        mode_label = {
            QC_MODE_DETECTED:      "LMT-detected rows",
            QC_MODE_BINARY_SEARCH: "Binary-search-filled rows",
            QC_MODE_LOGIC:         "Logic-filled rows",
            QC_MODE_ASSUMED:       "Assumed rows (legacy)",
        }.get(qc_mode, qc_mode)

        if qc_mode == QC_MODE_DETECTED:
            tp_desc = "predicted IN NEST (LMT),  human: IN NEST"
            fp_desc = "predicted IN NEST (LMT),  human: OUT OF NEST"
            tn_desc = "predicted OUT OF NEST (LMT),  human: OUT OF NEST"
            fn_desc = "predicted OUT OF NEST (LMT),  human: IN NEST"
        else:
            tp_desc = "predicted IN NEST,  human: IN NEST"
            fp_desc = "predicted IN NEST,  human: OUT OF NEST"
            tn_desc = "predicted OUT,      human: OUT OF NEST"
            fn_desc = "predicted OUT,      human: IN NEST"

        results = (
            f"QC Validation Complete\n\nQC Mode:                {mode_label}\n\nTotal Labelled Samples: {metrics['total_labeled']}\n\nTP ({tp_desc}):  {metrics['TP']}\nFP ({fp_desc}):  {metrics['FP']}\nTN ({tn_desc}):  {metrics['TN']}\nFN ({fn_desc}):  {metrics['FN']}\n\nAccuracy:    {metrics['accuracy']:.4f}\nError Rate:  {metrics['error_rate']:.4f}\nSensitivity: {metrics['sensitivity']:.4f}\nSpecificity: {metrics['specificity']:.4f}\n"
        )

        report_file = os.path.join(screenshot_folder, f"lmt_qc_validator_{animal_label}_{date_string}.txt")

        with open(report_file, "w") as f:
            f.write("LMT QC Validation Report\n\n")
            f.write(f"QC Mode: {mode_label}\n\n")
            f.write(f"Total Labelled Samples: {metrics['total_labeled']}\n\n")
            f.write("Confusion Matrix\n\n")
            f.write(f"  Positive class: IN NEST\n")
            f.write(f"  Negative class: OUT OF NEST\n")

            if qc_mode == QC_MODE_ASSUMED:
                f.write("  Note: rows with IN_NEST = -1 (gap below binary-search\n")
                f.write("        threshold) were excluded from QC entirely.\n\n")
            else:
                f.write("  Note: algorithm prediction is the LMT detector's IN_NEST value.\n\n")

            f.write(f"TP ({tp_desc}):  {metrics['TP']}\n")
            f.write(f"FP ({fp_desc}):  {metrics['FP']}\n")
            f.write(f"TN ({tn_desc}):  {metrics['TN']}\n")
            f.write(f"FN ({fn_desc}):  {metrics['FN']}\n\n")
            f.write("Performance Metrics\n\n")
            f.write(f"Accuracy:    {metrics['accuracy']:.4f}\n")
            f.write(f"Error Rate:  {metrics['error_rate']:.4f}\n")
            f.write(f"Sensitivity: {metrics['sensitivity']:.4f}\n")
            f.write(f"Specificity: {metrics['specificity']:.4f}\n")
            
            f.write("\nFalse Positive Screenshots (predicted IN NEST, human: OUT)\n\n")
            if fp_files:
                for s in fp_files:
                    f.write(f"  {s}\n")
            else:
                f.write("  None\n")

            f.write("\nFalse Negative Screenshots (predicted OUT, human: IN NEST)\n\n")
            if fn_files:
                for s in fn_files:
                    f.write(f"  {s}\n")
            else:
                f.write("  None\n")

        def _fmt_file_list(files, label):
            if not files:
                return f"\n{label}: none"
            return f"\n{label} ({len(files)}):\n" + "\n".join(f"  {s}" for s in files)

        messagebox.showinfo(
            "Results",
            results
            + _fmt_file_list(fp_files, "FP screenshots")
            + _fmt_file_list(fn_files, "FN screenshots")
            + f"\n\nValidation report saved to:\n{report_file}"
        )

# Setup actions
def select_database():
    global qc_db_path
    qc_db_path = filedialog.askopenfilename(filetypes=[("SQLite Database", "*.sqlite")])
    db_label.config(text=qc_db_path)

def select_videos():
    global video_paths_list
    video_paths_list = list(filedialog.askopenfilenames(filetypes=[("MP4 Video", "*.mp4")]))
    vid_label.config(text=f"{len(video_paths_list)} video(s) selected" if video_paths_list else "No videos selected")

def select_folder():
    global screenshot_folder
    screenshot_folder = filedialog.askdirectory()
    folder_label.config(text=screenshot_folder)

def start_qc():
    global df, current_index, video_map

    if not qc_db_path:
        messagebox.showerror("Error", "Please select QC SQLite database"); return
    if not screenshot_folder:
        messagebox.showerror("Error", "Please select screenshot folder");   return

    # Build video map if videos were provided (optional; enables three-panel display)
    if video_paths_list:
        video_map, skipped_videos, fps_mismatches = build_video_map(video_paths_list)

        video_warnings = []
        if skipped_videos:
            video_warnings.append(
                "The following video file(s) could not be parsed for a "
                "starting frame number (expected a 't<digits>' segment "
                "immediately before the file extension) and were excluded "
                "from this session:\n\n" + "\n".join(skipped_videos)
            )
        if fps_mismatches:
            video_warnings.append(
                f"The following video file(s) have a frame rate that does "
                f"not match the assumed {EXPECTED_VIDEO_FPS:.2f} fps "
                f"(DB_FPS={DB_FPS} / FRAME_CONVERSION={FRAME_CONVERSION}). "
                f"Frame alignment for these videos may be inaccurate:\n\n"
                + "\n".join(fps_mismatches)
            )
        if video_warnings:
            messagebox.showwarning("Video Warnings", "\n\n".join(video_warnings))
    else:
        video_map = []

    loaded_df, excluded, qc_mode = load_database()
    global animal_label

    if "animal_id" in loaded_df.columns and len(loaded_df):
        animal_label = f"A{int(loaded_df['animal_id'].iloc[0])}"
    else:
        animal_label = "Aunknown"

    if len(loaded_df) == 0:
        filter_desc_map = {
            QC_MODE_DETECTED:
                f"Eligible rows require:\n  \u2022 ASSUMPTION_TYPE = DETECTED\n\n"
                f"Rows excluded: {excluded:,}",
            QC_MODE_BINARY_SEARCH:
                f"Eligible rows require:\n  \u2022 ASSUMPTION_TYPE = ASSUMED\n"
                f"  \u2022 FILL_SOURCE = BINARY_SEARCH\n  \u2022 IN_NEST = 0 or 1\n\n"
                f"Rows excluded: {excluded:,}",
            QC_MODE_LOGIC:
                f"Eligible rows require:\n  \u2022 ASSUMPTION_TYPE = ASSUMED\n"
                f"  \u2022 FILL_SOURCE = LOGIC\n  \u2022 IN_NEST = 0 or 1\n\n"
                f"Rows excluded: {excluded:,}",
        }
        filter_desc = filter_desc_map.get(qc_mode,
            f"Eligible rows require:\n  \u2022 IN_NEST = 0 or 1\n\nRows excluded: {excluded:,}")
        messagebox.showinfo("Nothing to validate", f"No eligible rows found after filtering.\n\n{filter_desc}")
        return

    current_index = 0

    if excluded > 0:
        reason_map = {
            QC_MODE_DETECTED: "ASSUMPTION_TYPE \u2260 DETECTED",
            QC_MODE_BINARY_SEARCH: "FILL_SOURCE \u2260 BINARY_SEARCH, or IN_NEST = -1",
            QC_MODE_LOGIC: "FILL_SOURCE \u2260 LOGIC, or IN_NEST = -1",
            QC_MODE_ASSUMED: "IN_NEST = -1 or ASSUMPTION_TYPE \u2260 ASSUMED",
        }
        excl_reason = reason_map.get(qc_mode, "did not match filter criteria")
        messagebox.showinfo("Rows Excluded", f"{excluded:,} row(s) excluded from QC:\n  \u2022 {excl_reason}\n\nRemaining eligible samples: {len(loaded_df):,}")

    show_sample()

# Keyboard bindings
def bind_keys(root):
    root.bind("<a>",     lambda e: set_manual_qc(1))
    root.bind("<A>",     lambda e: set_manual_qc(1))
    root.bind("<d>",     lambda e: set_manual_qc(0))
    root.bind("<D>",     lambda e: set_manual_qc(0))
    root.bind("<Right>", lambda e: next_sample())
    root.bind("<Left>",  lambda e: previous_sample())

# GUI layout
if __name__ == "__main__":
    root = Tk()
    root.title("LMT QC Validator")
    root.geometry("1600x950")

    bind_keys(root)

    def _on_close():
        _release_all_captures()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", _on_close)

    # Top setup bar
    top_frame = Frame(root)
    top_frame.pack(pady=10)

    Button(top_frame, text="Select lmt_qc_sampler_<qc_mode>__A<animal_id>_<timestamp>.sqlite", command=select_database).grid(row=0, column=0, padx=10)
    db_label = Label(top_frame, text="No database selected", wraplength=500)
    db_label.grid(row=0, column=1)

    Button(top_frame, text="Select LMT Videos  (for 3-panel view)", command=select_videos).grid(row=1, column=0, padx=10)
    vid_label = Label(top_frame, text="No videos selected  (optional — enables boundary panels)", wraplength=500)
    vid_label.grid(row=1, column=1)

    Button(top_frame, text="Select Screenshot Folder", command=select_folder).grid(row=2, column=0, padx=10)
    folder_label = Label(top_frame, text="No folder selected", wraplength=500)
    folder_label.grid(row=2, column=1)

    Button(top_frame, text="START QC", command=start_qc, bg="green", fg="white", width=20).grid(row=3, column=0, columnspan=2, pady=10)

    # Main area
    main_frame = Frame(root)
    main_frame.pack(fill=BOTH, expand=True)

    # Three-panel image area (used for ASSUMED modes when videos are loaded)
    panels_frame = Frame(main_frame, bg="#111")
    # (packed/unpacked dynamically in show_sample)

    lbl_left_title  = Label(panels_frame, text="Before gap",  font=("Arial", 8, "bold"), fg="#aaa", bg="#111")
    lbl_left_title.grid(row=0, column=0, padx=4)
    img_left        = Label(panels_frame, bg="#111")
    img_left.grid(row=1, column=0, padx=4, pady=2)

    lbl_center_title = Label(panels_frame, text="QC frame",   font=("Arial", 8, "bold"), fg="#55ff55", bg="#0d2a0d")
    lbl_center_title.grid(row=0, column=1, padx=4)
    img_center      = Label(panels_frame, bg="#0d2a0d", bd=2, relief=GROOVE)
    img_center.grid(row=1, column=1, padx=4, pady=2)

    lbl_right_title = Label(panels_frame, text="After gap",   font=("Arial", 8, "bold"), fg="#aaa", bg="#111")
    lbl_right_title.grid(row=0, column=2, padx=4)
    img_right       = Label(panels_frame, bg="#111")
    img_right.grid(row=1, column=2, padx=4, pady=2)

    # Single-panel image area (used for DETECTED mode or when no videos loaded)
    single_frame = Frame(main_frame)
    # (packed/unpacked dynamically in show_sample)

    image_label = Label(single_frame)
    image_label.pack()

    right_frame = Frame(main_frame)
    right_frame.pack(side=RIGHT, padx=40, anchor=N)

    # QC mode banner
    mode_banner = Label(right_frame, text="", font=("Arial", 10, "bold"))
    mode_banner.pack(pady=(4, 0))

    sample_text     = Label(right_frame, text="Sample", font=("Arial", 16, "bold"))
    sample_text.pack(pady=10)

    video_text      = Label(right_frame, text="Video", font=("Arial", 12))
    video_text.pack(pady=5)

    frame_text      = Label(right_frame, text="Frame", font=("Arial", 12))
    frame_text.pack(pady=5)

    assumption_text = Label(right_frame, text="Row Type", font=("Arial", 12))
    assumption_text.pack(pady=5)

    gap_text        = Label(right_frame, text="Gap", font=("Arial", 12))
    gap_text.pack(pady=5)

    prediction_text = Label(right_frame, text="Algorithm", font=("Arial", 12, "bold"))
    prediction_text.pack(pady=10)

    manual_text     = Label(right_frame, text="Manual QC", font=("Arial", 12, "bold"))
    manual_text.pack(pady=10)

    # Action buttons
    Button(right_frame, text="IN NEST  (A)", bg="green", fg="white", width=22, height=2, command=lambda: set_manual_qc(1)).pack(pady=5)

    Button(right_frame, text="OUT OF NEST  (D)", bg="red", fg="white", width=22, height=2, command=lambda: set_manual_qc(0)).pack(pady=5)

    Label(right_frame, text="", font=("Arial", 6)).pack()

    Button(right_frame, text="\u25c4  PREVIOUS  (\u2190)", width=22, command=previous_sample).pack(pady=5)
    Button(right_frame, text="NEXT  (\u2192)  \u25ba", width=22, command=next_sample).pack(pady=5)

    Label(right_frame,
        text=("\nKeyboard shortcuts:\n"
                "A = IN NEST  (auto-advances)\n"
                "D = OUT OF NEST  (auto-advances)\n"
                "\u2190 = Previous\n"
                "\u2192 = Next (without labelling)"),
        font=("Arial", 10), fg="gray", justify=CENTER).pack(pady=15)

    root.mainloop()