import os
import cv2
import sqlite3
import pandas as pd
from datetime import datetime
from tkinter import *
from tkinter import filedialog, messagebox

# Constants
DB_FPS           = 30   # LMT database frame rate
FRAME_CONVERSION = 2    # 30fps DB -> 15fps video

QC_TYPE_ASSUMED  = "Assumed Rows QC"
QC_TYPE_DETECTED = "LMT Detected QC"

QC_TYPE_SLUG = {QC_TYPE_ASSUMED:  "Assumed", QC_TYPE_DETECTED: "Detected",}

# Video helpers
def get_start_frame(video_name):
    try:
        return int(video_name.split("t")[1].split(".")[0])
    except Exception:
        return None

def get_video_frame_count(video_path):
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return 0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    return total

def build_video_map(video_paths):
    video_map = []
    for v in video_paths:
        name  = os.path.basename(v)
        start = get_start_frame(name)
        if start is None:
            continue
        frames = get_video_frame_count(v)
        end    = start + frames * FRAME_CONVERSION
        video_map.append({"start": start, "end": end, "path": v})
    video_map.sort(key=lambda x: x["start"])
    return video_map

def extract_frame(video_map, global_frame, out_path):
    """Find the right video for global_frame and save a PNG.
    Returns the local frame index on success, None on failure."""
    matched_video = None
    matched_start = None

    for v in video_map:
        if v["start"] <= global_frame < v["end"]:
            matched_video = v["path"]
            matched_start = v["start"]
            break

    if matched_video is None and video_map:
        matched_video = video_map[0]["path"]
        matched_start = video_map[0]["start"]

    if matched_video is None:
        return None

    local_frame = int((global_frame - matched_start) / FRAME_CONVERSION)
    cap         = cv2.VideoCapture(matched_video)
    if not cap.isOpened():
        return None

    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, min(local_frame, total - 1)))
    ret, frame = cap.read()
    cap.release()

    if ret:
        cv2.imwrite(out_path, frame)
        return local_frame
    return None

# Main pipeline — one call per QC type
def run(analysis_db, video_paths, output_folder, animal_id, n_samples, qc_type, run_timestamp):
    """
    Execute one sampling run for a single QC type.

    run_timestamp is generated once in start() and shared across both type
    calls so the filenames are aligned when both types are run together.

    Screenshot folder:  Screenshots_<slug>_<run_timestamp>/
    SQLite output:      lmt_qc_sampler_<slug>_<run_timestamp>.sqlite
    """
    slug = QC_TYPE_SLUG[qc_type]

    # Create a dedicated screenshot folder for this type + timestamp 
    screenshot_folder = os.path.join(output_folder, f"Screenshots_{slug}_{run_timestamp}")
    os.makedirs(screenshot_folder, exist_ok=True)

    # Load the full GAP_FILL_ANALYSIS table produced by Script 3B 
    conn = sqlite3.connect(analysis_db)
    df   = pd.read_sql_query("SELECT * FROM GAP_FILL_ANALYSIS ORDER BY FRAMENUMBER", conn)
    conn.close()

    if len(df) == 0:
        raise Exception("No GAP_FILL_ANALYSIS rows found in the selected SQLite.")
    
    if "BINARY_SEARCH" not in df.columns:
        df["BINARY_SEARCH"] = 0

    # Build the sampling pool
    if qc_type == QC_TYPE_ASSUMED:
        # Gap-filled rows only. Exclude IN_NEST = -1 
        pool = df[(df["ASSUMPTION_TYPE"] == "ASSUMED") & (df["IN_NEST"].isin([0, 1]))].copy()
        pool_description = "ASSUMED rows with IN_NEST in (0, 1)"

    elif qc_type == QC_TYPE_DETECTED:
        # Directly-tracked rows only. IN_NEST is always 0 or 1 for detected
        pool = df[(df["ASSUMPTION_TYPE"] == "DETECTED") & (df["IN_NEST"].isin([0, 1]))].copy()
        pool_description = "DETECTED rows with IN_NEST in (0, 1)"

    else:
        raise Exception(f"Unknown QC type: {qc_type!r}")

    total_available = len(pool)

    if total_available == 0:
        raise Exception(
            f"No eligible rows found for QC type '{qc_type}'.\n"
            f"Expected pool: {pool_description}.\n"
            f"Check that the selected SQLite was produced by the updated Script 3B."
        )

    if n_samples > total_available:
        raise Exception(
            f"Requested {n_samples:,} samples but only {total_available:,} "
            f"are available for '{qc_type}'.\n"
            f"Please enter a number \u2264 {total_available:,}."
        )

    # Random sample 
    df_sample = (pool
                 .sample(n=n_samples)
                 .sort_values("FRAMENUMBER")
                 .reset_index(drop=True))

    video_map = build_video_map(video_paths)
    if not video_map:
        raise Exception("No valid LMT videos found.")

    # Extract screenshots 
    results = []
    counter = 1

    for _, row in df_sample.iterrows():
        global_frame = int(row["FRAMENUMBER"])
        video_name   = ""

        for v in video_map:
            if v["start"] <= global_frame < v["end"]:
                video_name = os.path.basename(v["path"])
                break
        if not video_name and video_map:
            video_name = os.path.basename(video_map[0]["path"])

        screenshot_name = f"S{counter:04d}_A{animal_id}_G{global_frame}_{video_name}.png"
        screenshot_path = os.path.join(screenshot_folder, screenshot_name)

        local_frame = extract_frame(video_map, global_frame, screenshot_path)
        if local_frame is None:
            continue

        results.append({
            "sample_id":       counter,
            "animal_id":       animal_id,
            "video":           video_name,
            "frame_global":    global_frame,
            "IN_NEST":         int(row["IN_NEST"]),
            "ASSUMPTION_TYPE": row.get("ASSUMPTION_TYPE", "ASSUMED"),
            "GAP_START_FRAME": row.get("GAP_START_FRAME"),
            "GAP_END_FRAME":   row.get("GAP_END_FRAME"),
            "BINARY_SEARCH":   int(row.get("BINARY_SEARCH", 0)),
            "screenshot":      screenshot_name,
            "QC_TYPE":         qc_type,
        })
        counter += 1

    if not results:
        raise Exception(
            f"No screenshots could be extracted for '{qc_type}'.\n"
            "Check that the videos cover the sampled frame numbers."
        )

    # Save SQLite 
    out_db = os.path.join(output_folder, f"lmt_qc_sampler_{slug}_{run_timestamp}.sqlite")
    conn   = sqlite3.connect(out_db)
    pd.DataFrame(results).to_sql(
        "QC_ASSUMED_SAMPLES", conn, if_exists="replace", index=False)
    conn.close()

    return {
        "qc_type":          qc_type,
        "total_available":  total_available,
        "n_samples":        n_samples,
        "extracted":        len(results),
        "out_db":           out_db,
        "screenshot_folder": screenshot_folder,
    }

# GUI state
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
            messagebox.showerror("Error", "Please select a Script 3B SQLite.")
            return
        if not videos:
            messagebox.showerror("Error", "Please select at least one LMT video.")
            return
        if not out_folder:
            messagebox.showerror("Error", "Please select an output folder.")
            return

        # Validate animal ID 
        try:
            animal_id = int(entry_animal.get())
        except ValueError:
            messagebox.showerror("Error", "Animal ID must be an integer.")
            return

        # Validate sample count 
        raw = entry_samples.get().strip()
        if not raw.isdigit() or int(raw) <= 0:
            messagebox.showerror(
                "Error", "Number of samples must be a positive integer.")
            return
        n_samples = int(raw)

        # Determine which QC types were selected 
        selected_types = []
        if var_detected.get():
            selected_types.append(QC_TYPE_DETECTED)
        if var_assumed.get():
            selected_types.append(QC_TYPE_ASSUMED)

        if not selected_types:
            messagebox.showerror(
                "Error", "Please select at least one QC type.")
            return

        # Single timestamp shared across all runs this session 
        run_timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

        # Run sampling for each selected type 
        summaries = []
        errors    = []

        for qc_type in selected_types:
            try:
                result = run(
                    analysis_db, videos, out_folder,
                    animal_id, n_samples, qc_type, run_timestamp)
                summaries.append(result)
            except Exception as e:
                errors.append(f"{qc_type}:\n  {e}")

        # Report errors for any failed type 
        if errors:
            messagebox.showerror(
                "Error",
                "One or more QC types failed:\n\n" + "\n\n".join(errors))
            if not summaries:
                return   # nothing succeeded

        # Completion summary 
        lines = ["QC Sample Extraction Complete\n"]
        for s in summaries:
            lines.append(
                f" {s['qc_type']} \n"
                f"  Total rows available:   {s['total_available']:,}\n"
                f"  Samples requested:      {s['n_samples']:,}\n"
                f"  Screenshots extracted:  {s['extracted']:,}\n"
                f"  SQLite:\n    {s['out_db']}\n"
                f"  Screenshots:\n    {s['screenshot_folder']}\n"
            )
        messagebox.showinfo("Done", "\n".join(lines))

    except Exception as e:
        messagebox.showerror("Error", str(e))

# GUI layout
root = Tk()
root.title("LMT QC Sampler")
root.geometry("750x620")

Label(root, text="LMT QC Sampler",
      font=("Arial", 16, "bold")).pack(pady=10)

Label(root,
      text=(
          "Randomly selects frames from the Script 3B GAP_FILL_ANALYSIS table\n"
          "and extracts their screenshots for manual quality control.\n\n"
          "Select one or both QC types. Each type produces its own SQLite\n"
          "and screenshot folder, labelled with the type and a shared timestamp."
      ),
      font=("Arial", 10), justify=CENTER).pack(pady=5)

Button(root, text="Select lmt_binary_search.py SQLite", command=select_db).pack(pady=5)
label_db = Label(root, text="No file selected", wraplength=700)
label_db.pack()

Button(root, text="Select LMT Videos", command=select_videos).pack(pady=5)
label_vid = Label(root, text="No videos selected")
label_vid.pack()

Button(root, text="Select Output Folder", command=select_out).pack(pady=5)
label_out = Label(root, text="No output folder selected", wraplength=700)
label_out.pack()

Label(root, text="Animal ID").pack(pady=(12, 0))
entry_animal = Entry(root)
entry_animal.insert(0, "3")
entry_animal.pack()

Label(root, text="QC Type  (select one or both)").pack(pady=(14, 4))

checkbox_frame = Frame(root)
checkbox_frame.pack()

var_detected = BooleanVar(value=False)
var_assumed  = BooleanVar(value=True)   

Checkbutton(checkbox_frame, text=QC_TYPE_DETECTED, variable=var_detected, font=("Arial", 10),).grid(row=0, column=0, padx=20, sticky=W)

Checkbutton(checkbox_frame, text=QC_TYPE_ASSUMED, variable=var_assumed, font=("Arial", 10),).grid(row=0, column=1, padx=20, sticky=W)

Label(root, text="How many samples would you like?  (applied to each selected type)").pack(pady=(14, 2))
entry_samples = Entry(root)
entry_samples.insert(0, "100")
entry_samples.pack()

Button(root, text="RUN SAMPLING", command=start, bg="green", fg="white", width=30, height=2).pack(pady=20)

root.mainloop()
