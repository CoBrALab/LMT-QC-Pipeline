import os
import sqlite3
import pandas as pd
from datetime import datetime
from tkinter import *
from tkinter import filedialog, messagebox
from PIL import Image, ImageTk


# QC type constants  (must match the values written by lmt_qc_sampler.py)
QC_TYPE_ASSUMED  = "Assumed Rows QC"
QC_TYPE_DETECTED = "LMT Detected QC"


# Sentinel values stored in MANUAL_QC column
#   None / NaN  = not yet labelled
#   1           = IN NEST
#   0           = OUT OF NEST
# Rows with IN_NEST = -1 are excluded from QC regardless of mode.


# Global state
qc_db_path        = ""
screenshot_folder = ""
qc_output_db      = ""   # fixed for the entire session; set once in start_qc()
df                = None
current_index     = 0


# Database helpers
def _detect_qc_type(df_full):
    if "QC_TYPE" not in df_full.columns:
        return None
    values = df_full["QC_TYPE"].dropna().unique().tolist()
    if not values:
        return None
    return str(values[0])


def load_database():
    global df
    conn    = sqlite3.connect(qc_db_path)
    df_full = pd.read_sql_query("SELECT * FROM QC_ASSUMED_SAMPLES", conn)
    conn.close()


    if "BINARY_SEARCH" not in df_full.columns:
        df_full["BINARY_SEARCH"] = 0


    qc_type        = _detect_qc_type(df_full)
    legacy_fallback = False


    if qc_type == QC_TYPE_DETECTED:
        assumption_filter = "DETECTED"
    elif qc_type == QC_TYPE_ASSUMED:
        assumption_filter = "ASSUMED"
    else:
        assumption_filter = "ASSUMED"
        legacy_fallback   = True


    mask = ((df_full["ASSUMPTION_TYPE"] == assumption_filter) & (df_full["IN_NEST"].isin([0, 1])))
    df = df_full[mask].copy().reset_index(drop=True)


    if "MANUAL_QC" not in df.columns:
        df["MANUAL_QC"] = None


    excluded = len(df_full) - len(df)
    return df, excluded, qc_type or QC_TYPE_ASSUMED, legacy_fallback


def save_database():
    """Overwrite the single session SQLite — never creates a second file."""
    if not qc_output_db:
        return
    conn = sqlite3.connect(qc_output_db)
    df.to_sql("QC_ASSUMED_SAMPLES", conn, if_exists="replace", index=False)
    conn.close()


def calculate_metrics():
    completed_df = df[df["MANUAL_QC"].notna()].copy()
    if len(completed_df) == 0:
        return None


    predicted_in  = completed_df[completed_df["IN_NEST"] == 1]
    tp = len(predicted_in[predicted_in["MANUAL_QC"] == 1])
    fp = len(predicted_in[predicted_in["MANUAL_QC"] == 0])


    predicted_out = completed_df[completed_df["IN_NEST"] != 1]
    fn = len(predicted_out[predicted_out["MANUAL_QC"] == 1])
    tn = len(predicted_out[predicted_out["MANUAL_QC"] == 0])


    total       = tp + tn + fp + fn
    accuracy    = (tp + tn) / total  if total > 0     else 0
    error_rate  = (fp + fn) / total  if total > 0     else 0
    sensitivity = tp / (tp + fn)     if (tp + fn) > 0 else 0
    specificity = tn / (tn + fp)     if (tn + fp) > 0 else 0


    return {
        "TP": tp, "TN": tn, "FP": fp, "FN": fn,
        "accuracy":      accuracy,
        "error_rate":    error_rate,
        "sensitivity":   sensitivity,
        "specificity":   specificity,
        "total_labeled": len(completed_df),
    }


# Display


def show_sample():
    global current_index


    row           = df.iloc[current_index]
    screenshot_nm = row["screenshot"]
    image_path    = os.path.join(screenshot_folder, screenshot_nm)


    if not os.path.exists(image_path):
        messagebox.showerror("Missing Screenshot", image_path)
        return


    # 3-panel
    bl_name = row.get("screenshot_bl")
    br_name = row.get("screenshot_br")
    has_panels = (
        pd.notna(bl_name) and bl_name and
        pd.notna(br_name) and br_name
    )


    def _load_panel(lbl, fname):
        p = os.path.join(screenshot_folder, fname) if fname else None
        if p and os.path.exists(p):
            im = Image.open(p)
            im.thumbnail((380, 300))
            ph = ImageTk.PhotoImage(im)
            lbl.config(image=ph, text="")
            lbl.image = ph
        else:
            lbl.config(image="", text="[unavailable]", fg="#888888")
            lbl.image = None


    if has_panels:
        single_image_frame.pack_forget()
        panel_frame.pack(fill=X, pady=(0, 4))
        _load_panel(panel_img_left,   bl_name)
        _load_panel(panel_img_center, screenshot_nm)
        _load_panel(panel_img_right,  br_name)
    else:
        panel_frame.pack_forget()
        single_image_frame.pack(fill=X, pady=(0, 4))
        img = Image.open(image_path)
        img.thumbnail((380, 300))
        photo = ImageTk.PhotoImage(img)
        image_label.config(image=photo)
        image_label.image = photo


    # Metadata labels
    sample_text.config(text=f"Sample {current_index + 1} / {len(df)}")
    video_text.config(text=f"Video: {row['video']}")
    frame_text.config(text=f"Global Frame: {row['frame_global']}")
    assumption_text.config(text=f"Type: {row.get('ASSUMPTION_TYPE', 'ASSUMED')}")


    qc_type_val = row.get("QC_TYPE", "")
    qc_type_display = (
        str(qc_type_val)
        if pd.notna(qc_type_val) and qc_type_val != ""
        else "Legacy"
    )
    qc_type_text_label.config(text=f"QC Mode: {qc_type_display}")


    bs_val = row.get("BINARY_SEARCH", 0)
    if pd.notna(bs_val) and int(bs_val) == 1:
        binary_search_text.config(text="Binary Search: Yes", fg="#0055cc")
    else:
        binary_search_text.config(text="Binary Search: No",  fg="#888888")


    gap_start = row.get("GAP_START_FRAME", None)
    gap_end   = row.get("GAP_END_FRAME",   None)
    if pd.notna(gap_start) and pd.notna(gap_end):
        gap_text.config(text=f"Gap: Frame {int(gap_start)} → {int(gap_end)}")
    else:
        gap_text.config(text="Gap: N/A")


    in_nest_val = int(row["IN_NEST"])
    if in_nest_val == 1:
        prediction_text.config(text="Algorithm: IN NEST",     fg="green")
    else:
        prediction_text.config(text="Algorithm: OUT OF NEST", fg="red")


    manual_value = row["MANUAL_QC"]
    if pd.isna(manual_value):
        manual_text.config(text="Manual QC: Not labelled yet", fg="black")
    elif int(manual_value) == 1:
        manual_text.config(text="Manual QC: IN NEST",          fg="green")
    elif int(manual_value) == 0:
        manual_text.config(text="Manual QC: OUT OF NEST",      fg="red")


# Actions


def set_manual_qc(value):
    global current_index
    df.at[current_index, "MANUAL_QC"] = value
    save_database()
    show_sample()


def previous_sample():
    global current_index
    if current_index > 0:
        current_index -= 1
        show_sample()


def next_sample():
    global current_index
    if current_index < len(df) - 1:
        current_index += 1
        show_sample()
    else:
        save_database()
        metrics = calculate_metrics()
        if metrics is None:
            messagebox.showinfo("Done", "No labelled samples found.")
            return


        qc_type_for_report = QC_TYPE_ASSUMED
        if "QC_TYPE" in df.columns:
            vals = df["QC_TYPE"].dropna().unique().tolist()
            if vals:
                qc_type_for_report = str(vals[0])


        results = (
            f"QC Validation Complete\n\n"
            f"QC Type: {qc_type_for_report}\n\n"
            f"Total Labelled Samples: {metrics['total_labeled']}\n\n"
            f"TP (algorithm IN NEST,  human: IN NEST):     {metrics['TP']}\n"
            f"FP (algorithm IN NEST,  human: OUT OF NEST): {metrics['FP']}\n"
            f"TN (algorithm OUT,      human: OUT OF NEST): {metrics['TN']}\n"
            f"FN (algorithm OUT,      human: IN NEST):     {metrics['FN']}\n\n"
            f"Accuracy:    {metrics['accuracy']:.4f}\n"
            f"Error Rate:  {metrics['error_rate']:.4f}\n"
            f"Sensitivity: {metrics['sensitivity']:.4f}\n"
            f"Specificity: {metrics['specificity']:.4f}"
        )


        date_string = datetime.now().strftime("%Y-%m-%d")
        report_file = os.path.join(
            screenshot_folder, f"lmt_qc_validator_{date_string}.txt")


        completed_df = df[df["MANUAL_QC"].notna()].copy()


        fp_rows = completed_df[
            (completed_df["IN_NEST"] == 1) &
            (completed_df["MANUAL_QC"] == 0)
        ]
        fn_rows = completed_df[
            (completed_df["IN_NEST"] == 0) &
            (completed_df["MANUAL_QC"] == 1)
        ]


        with open(report_file, "w") as f:
            f.write("LMT QC Validation Report\n\n")
            f.write(f"QC Type: {qc_type_for_report}\n\n")
            f.write(f"Total Labelled Samples: {metrics['total_labeled']}\n\n")
            f.write("Confusion Matrix\n\n")
            f.write("  Positive class: IN NEST\n")
            f.write("  Negative class: OUT OF NEST\n")


            if qc_type_for_report == QC_TYPE_ASSUMED:
                f.write("  Note: rows with IN_NEST = -1 (gap below binary-search\n")
                f.write("        threshold) were excluded from QC entirely.\n")
                f.write("  Note: only ASSUMED rows are included in this QC mode.\n\n")
            else:
                f.write("  Note: only DETECTED rows are included in this QC mode.\n\n")


            f.write(f"TP (algorithm IN NEST,  human: IN NEST):     {metrics['TP']}\n")
            f.write(f"FP (algorithm IN NEST,  human: OUT OF NEST): {metrics['FP']}\n")
            f.write(f"TN (algorithm OUT,      human: OUT OF NEST): {metrics['TN']}\n")
            f.write(f"FN (algorithm OUT,      human: IN NEST):     {metrics['FN']}\n\n")
            f.write("Performance Metrics\n\n")
            f.write(f"Accuracy:    {metrics['accuracy']:.4f}\n")
            f.write(f"Error Rate:  {metrics['error_rate']:.4f}\n")
            f.write(f"Sensitivity: {metrics['sensitivity']:.4f}\n")
            f.write(f"Specificity: {metrics['specificity']:.4f}\n")


            # Mismatch details with filenames
            f.write("\n\n")
            f.write("False Positives (Algorithm IN NEST, Human OUT OF NEST)\n")
            f.write("------------------------------------------------------\n")
            if len(fp_rows) == 0:
                f.write("None\n")
            else:
                for _, row in fp_rows.iterrows():
                    f.write(f"- {row['screenshot']}\n")


            f.write("\n")
            f.write("False Negatives (Algorithm OUT OF NEST, Human IN NEST)\n")
            f.write("------------------------------------------------------\n")
            if len(fn_rows) == 0:
                f.write("None\n")
            else:
                for _, row in fn_rows.iterrows():
                    f.write(f"- {row['screenshot']}\n")


        messagebox.showinfo(
            "Results",
            results + f"\n\nValidation report saved to:\n{report_file}"
        )


# Setup actions


def select_database():
    global qc_db_path
    qc_db_path = filedialog.askopenfilename(
        filetypes=[("SQLite Database", "*.sqlite")])
    db_label.config(text=qc_db_path)


def select_folder():
    global screenshot_folder
    screenshot_folder = filedialog.askdirectory()
    folder_label.config(text=screenshot_folder)


def start_qc():
    global df, current_index, qc_output_db


    if not qc_db_path:
        messagebox.showerror("Error", "Please select QC SQLite database"); return
    if not screenshot_folder:
        messagebox.showerror("Error", "Please select screenshot folder");   return


    loaded_df, excluded, qc_type, legacy_fallback = load_database()


    if legacy_fallback:
        messagebox.showwarning(
            "Legacy Database",
            "The selected SQLite has no QC_TYPE column.\n"
            "Falling back to Assumed Rows QC filter."
        )


    if len(loaded_df) == 0:
        if qc_type == QC_TYPE_DETECTED:
            filter_desc = "ASSUMPTION_TYPE = DETECTED and IN_NEST in (0, 1)"
        else:
            filter_desc = (
                "ASSUMPTION_TYPE = ASSUMED and IN_NEST in (0, 1)\n"
                "(rows with IN_NEST = -1 are excluded as undecided)"
            )
        messagebox.showinfo(
            "Nothing to validate",
            f"No eligible rows found after filtering.\n\n"
            f"QC Mode: {qc_type}\n"
            f"Filter applied: {filter_desc}\n\n"
            f"Rows excluded: {excluded:,}"
        )
        return


    # Fix the output path for the entire session — never changes again
    run_ts       = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    qc_output_db = os.path.join(
        screenshot_folder, f"lmt_qc_validator_{run_ts}.sqlite")


    current_index = 0


    if excluded > 0:
        if qc_type == QC_TYPE_ASSUMED:
            exclusion_reason = (
                "IN_NEST = -1 (below binary-search threshold), or "
                "ASSUMPTION_TYPE ≠ ASSUMED"
            )
        else:
            exclusion_reason = (
                "IN_NEST = -1, or "
                "ASSUMPTION_TYPE ≠ DETECTED"
            )
        messagebox.showinfo(
            "Rows Excluded",
            f"QC Mode: {qc_type}\n\n"
            f"{excluded:,} row(s) excluded from QC:\n"
            f"  • {exclusion_reason}\n\n"
            f"Remaining eligible samples: {len(loaded_df):,}"
        )


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
root = Tk()
root.title("LMT QC Validator")
root.geometry("1400x960")


bind_keys(root)


# Top setup bar
top_frame = Frame(root)
top_frame.pack(pady=8, fill=X, padx=10)


Button(top_frame, text="Select lmt_qc_sampler.py SQLite output",
       command=select_database).grid(row=0, column=0, padx=10, sticky=W)
db_label = Label(top_frame, text="No database selected", wraplength=500, anchor=W)
db_label.grid(row=0, column=1, sticky=W)


Button(top_frame, text="Select Screenshot Folder",
       command=select_folder).grid(row=1, column=0, padx=10, pady=2, sticky=W)
folder_label = Label(top_frame, text="No folder selected", wraplength=500, anchor=W)
folder_label.grid(row=1, column=1, sticky=W)


Button(top_frame, text="START QC", command=start_qc,
       bg="green", fg="white", width=20).grid(row=2, column=0, columnspan=2, pady=8)


# Image area (panel OR single) — sits above the button/info row
image_area = Frame(root)
image_area.pack(fill=X, padx=10)


# 3-panel frame (assumed QC with boundary context)
panel_frame = Frame(image_area, bg="#1a1a1a")
# not packed yet — show_sample() controls visibility


for col, (title, fg_col, bg_col) in enumerate([
    ("LAST DETECTED BEFORE GAP", "#aaaaaa", "#1a1a1a"),
    ("▶  FRAME UNDER REVIEW  ◀", "#55ff55", "#0d2a0d"),
    ("FIRST DETECTED AFTER GAP", "#aaaaaa", "#1a1a1a"),
]):
    panel_frame.grid_columnconfigure(col, weight=1, uniform="panels")
    sub = Frame(panel_frame, bg=bg_col,
                bd=(2 if col == 1 else 0), relief=(GROOVE if col == 1 else FLAT))
    sub.grid(row=0, column=col, padx=6, pady=4, sticky="nsew")
    Label(sub, text=title, font=("Arial", 8, "bold"),
          fg=fg_col, bg=bg_col).pack()


panel_img_left   = Label(panel_frame, bg="#1a1a1a")
panel_img_left.grid(row=1, column=0, padx=6, pady=4)
panel_img_center = Label(panel_frame, bg="#0d2a0d")
panel_img_center.grid(row=1, column=1, padx=6, pady=4)
panel_img_right  = Label(panel_frame, bg="#1a1a1a")
panel_img_right.grid(row=1, column=2, padx=6, pady=4)


# Single-image frame (detected QC, or assumed without boundary shots)
single_image_frame = Frame(image_area)
# not packed yet — show_sample() controls visibility
image_label = Label(single_image_frame)
image_label.pack()


# Bottom row: metadata (left) + action buttons (right)
bottom_frame = Frame(root)
bottom_frame.pack(fill=X, padx=10, pady=6)


# Left: metadata labels
meta_frame = Frame(bottom_frame)
meta_frame.pack(side=LEFT, anchor=NW, padx=(0, 30))


sample_text        = Label(meta_frame, text="Sample",          font=("Arial", 14, "bold"))
sample_text.pack(anchor=W, pady=2)
video_text         = Label(meta_frame, text="Video",           font=("Arial", 11))
video_text.pack(anchor=W)
frame_text         = Label(meta_frame, text="Frame",           font=("Arial", 11))
frame_text.pack(anchor=W)
assumption_text    = Label(meta_frame, text="Type",            font=("Arial", 11))
assumption_text.pack(anchor=W)
qc_type_text_label = Label(meta_frame, text="QC Mode",        font=("Arial", 11))
qc_type_text_label.pack(anchor=W)
binary_search_text = Label(meta_frame, text="Binary Search",   font=("Arial", 11))
binary_search_text.pack(anchor=W)
gap_text           = Label(meta_frame, text="Gap",             font=("Arial", 11))
gap_text.pack(anchor=W)
prediction_text    = Label(meta_frame, text="Algorithm",       font=("Arial", 12, "bold"))
prediction_text.pack(anchor=W, pady=(8, 2))
manual_text        = Label(meta_frame, text="Manual QC",       font=("Arial", 12, "bold"))
manual_text.pack(anchor=W)


# Right: action buttons
btn_frame = Frame(bottom_frame)
btn_frame.pack(side=RIGHT, anchor=NE)


Button(btn_frame, text="IN NEST  (A)",
       bg="green", fg="white", width=22, height=2,
       command=lambda: set_manual_qc(1)).pack(pady=4)
Button(btn_frame, text="OUT OF NEST  (D)",
       bg="red",   fg="white", width=22, height=2,
       command=lambda: set_manual_qc(0)).pack(pady=4)


Label(btn_frame, text="", font=("Arial", 4)).pack()   # spacer


Button(btn_frame, text="◄  PREVIOUS  (←)", width=22,
       command=previous_sample).pack(pady=3)
Button(btn_frame, text="NEXT  (→)  ►",     width=22,
       command=next_sample).pack(pady=3)


root.mainloop()

