import os
import re
import random
import cv2
import sqlite3
import pandas as pd
from datetime import datetime
from tkinter import *
from tkinter import filedialog, messagebox

# Constants
DB_FPS           = 30
FRAME_CONVERSION = 2

# QC mode constants  (stored in QC_MODE column — 4.lmt_qc_validator.py reads this)
QC_MODE_DETECTED      = "DETECTED"
QC_MODE_BINARY_SEARCH = "BINARY_SEARCH"
QC_MODE_LOGIC         = "LOGIC"

# Video helpers
def get_start_frame(video_name):
    """
    Extract the starting global frame number encoded in a video filename.

    Expects a segment of the form "t<digits>" immediately preceding the file
    extension (e.g. "..._t12345.mp4"). The match is anchored to the end of
    the filename (rather than splitting on the first "t" anywhere in the
    string, as before) so that a filename containing an unrelated "t"
    earlier on (e.g. in an experiment/cage name) is no longer misparsed.
    """
    match = re.search(r't(\d+)\.[A-Za-z0-9]+$', video_name)
    if match:
        return int(match.group(1))
    return None

def get_video_frame_count_and_fps(video_path):
    """Open the video once and return (frame_count, fps)."""
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        cap.release()
        return 0, 0.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps   = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
    cap.release()
    return total, fps

# Expected video frame rate given the DB_FPS / FRAME_CONVERSION assumption.
# Videos whose actual fps deviates from this are flagged (not blocked) so the
# user can judge whether frame alignment is trustworthy for that file.
EXPECTED_VIDEO_FPS = DB_FPS / FRAME_CONVERSION
FPS_TOLERANCE      = 0.5

def build_video_map(video_paths):
    """
    Returns (video_map, skipped_videos, fps_mismatches).

    skipped_videos : filenames that could not be parsed for a starting frame
                     number and were therefore excluded from video_map.
                     (Previously dropped completely silently.)
    fps_mismatches : descriptive strings for videos whose actual frame rate
                     does not match the DB_FPS/FRAME_CONVERSION assumption.
                     (Previously never checked at all.)
    """
    video_map      = []
    skipped_videos = []
    fps_mismatches = []

    for v in video_paths:
        name  = os.path.basename(v)
        start = get_start_frame(name)
        if start is None:
            skipped_videos.append(name)
            continue
        frames, fps = get_video_frame_count_and_fps(v)
        end = start + frames * FRAME_CONVERSION
        video_map.append({"start": start, "end": end, "path": v})

        if fps > 0 and abs(fps - EXPECTED_VIDEO_FPS) > FPS_TOLERANCE:
            fps_mismatches.append(
                f"{name}: actual {fps:.2f} fps vs expected {EXPECTED_VIDEO_FPS:.2f} fps"
            )

    video_map.sort(key=lambda x: x["start"])
    return video_map, skipped_videos, fps_mismatches

_video_capture_cache = {}

def _get_capture(path):
    cap = _video_capture_cache.get(path)
    if cap is None or not cap.isOpened():
        cap = cv2.VideoCapture(path)
        _video_capture_cache[path] = cap
    return cap

def _release_all_captures():
    for cap in _video_capture_cache.values():
        try:
            cap.release()
        except Exception:
            pass
    _video_capture_cache.clear()

def find_nearest_frame_candidates(video_map, global_frame):
    """
    Resolve a requested global frame to the video(s) that can actually supply it.
    See 2.lmt_binary_search.py for full documentation of the resolution rule
    (exact match, else nearest of preceding/succeeding, preceding wins ties).
    Returns a list of (resolved_global_frame, video_entry) tuples, ordered by
    preference. Empty list if video_map is empty.
    """
    if not video_map:
        return []

    for v in video_map:
        if v["start"] <= global_frame < v["end"]:
            return [(global_frame, v)]

    preceding  = None
    succeeding = None
    for v in video_map:
        if v["end"] <= global_frame and (preceding is None or v["end"] > preceding[1]["end"]):
            preceding = (v["end"] - FRAME_CONVERSION, v)
        if v["start"] > global_frame and (succeeding is None or v["start"] < succeeding[1]["start"]):
            succeeding = (v["start"], v)

    if preceding is not None and succeeding is not None:
        dist_preceding  = global_frame - preceding[0]
        dist_succeeding = succeeding[0] - global_frame
        return [preceding, succeeding] if dist_preceding <= dist_succeeding else [succeeding, preceding]
    elif preceding is not None:
        return [preceding]
    elif succeeding is not None:
        return [succeeding]
    return []

def _read_frame_from_video(video_entry, resolved_frame, out_path):
    local_frame = int((resolved_frame - video_entry["start"]) / FRAME_CONVERSION)
    cap = _get_capture(video_entry["path"])
    if not cap.isOpened():
        return False
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, min(local_frame, total - 1)))
    ret, frame = cap.read()
    if ret:
        cv2.imwrite(out_path, frame)
        return True
    return False

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

# Pool filtering
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
    has_fill_source = "FILL_SOURCE" in df_full.columns

    if qc_mode == QC_MODE_DETECTED:
        mask = df_full["ASSUMPTION_TYPE"] == "DETECTED"
        label = "Detected rows"

    elif qc_mode == QC_MODE_BINARY_SEARCH:
        if has_fill_source:
            mask = ((df_full["ASSUMPTION_TYPE"] == "ASSUMED") & (df_full["FILL_SOURCE"] == "BINARY_SEARCH") & (df_full["IN_NEST"].isin([0, 1])))
        elif "BINARY_SEARCH" in df_full.columns:
            # Backward compat: use legacy BINARY_SEARCH flag column
            mask = ((df_full["ASSUMPTION_TYPE"] == "ASSUMED") & (df_full["BINARY_SEARCH"] == 1) & (df_full["IN_NEST"].isin([0, 1])))
        else:
            raise Exception(
                "This SQLite has neither a FILL_SOURCE nor a BINARY_SEARCH "
                "column, so the 'BINARY_SEARCH' pool cannot be sampled. "
                "This usually means the file came directly from "
                "1.lmt_gap_fill.py rather than 2.lmt_binary_search.py. "
                "Please run 2.lmt_binary_search.py first, or deselect this pool."
            )
        label = "Binary-search-filled rows"

    elif qc_mode == QC_MODE_LOGIC:
        if has_fill_source:
            mask = ((df_full["ASSUMPTION_TYPE"] == "ASSUMED") & (df_full["FILL_SOURCE"] == "LOGIC") & (df_full["IN_NEST"].isin([0, 1])))
        else:
            # Backward compat: ASSUMED rows that were never sent to binary search
            bs_col = "BINARY_SEARCH" if "BINARY_SEARCH" in df_full.columns else None
            if bs_col:
                mask = ((df_full["ASSUMPTION_TYPE"] == "ASSUMED") & (df_full[bs_col] == 0) & (df_full["IN_NEST"].isin([0, 1])))
            else:
                # Oldest compat: all ASSUMED rows with IN_NEST in (0,1)
                mask = ((df_full["ASSUMPTION_TYPE"] == "ASSUMED") & (df_full["IN_NEST"].isin([0, 1])))
        label = "Logic-filled rows"

    else:
        raise Exception(f"Unknown QC mode: {qc_mode!r}")

    return df_full[mask].copy().reset_index(drop=True), label

# Main pipeline
def run(analysis_db, video_paths, output_folder, animal_id, n_samples, qc_mode):
    timestamp     = datetime.now().strftime("%Y-%m-%d")
    pool_folder   = os.path.join(output_folder, f"{qc_mode}_{timestamp}")
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
    df_sample = df.sample(n=n_samples, random_state=sample_seed).sort_values("FRAMENUMBER").reset_index(drop=True)

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

    out_db = os.path.join(pool_folder, f"lmt_qc_sampler_{qc_mode}_{timestamp}.sqlite")

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
            messagebox.showerror("Error", "Please select lmt_binary_search_<date>.sqlite.")
            return
        if not videos:
            messagebox.showerror("Error", "Please select at least one LMT video.")
            return
        if not out_folder:
            messagebox.showerror("Error", "Please select an output folder.")
            return

        animal_id = int(entry_animal.get())

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
                    summary = run(analysis_db, videos, out_folder,
                                  animal_id, n_samples, qc_mode)
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
      text=("Randomly selects frames from the lmt_gap_fill_<date>.sqlite GAP_FILL_ANALYSIS table\n"
          "and extracts their screenshots for manual quality control.\n\n"
          "Select QC pool(s). Each type produces its own SQLite\n"
          "and screenshot folder, labelled with the type and a shared timestamp."),
      font=("Arial", 10), justify=CENTER).pack(pady=5)

Button(root, text="Select lmt_binary_search_<date>.sqlite", command=select_db).pack(pady=5)
label_db = Label(root, text="No file selected", wraplength=700)
label_db.pack()

Button(root, text="Select LMT Videos", command=select_videos).pack(pady=5)
label_vid = Label(root, text="No videos selected")
label_vid.pack()

Button(root, text="Select Output Folder", command=select_out).pack(pady=5)
label_out = Label(root, text="No output folder selected", wraplength=700)
label_out.pack()

Label(root, text="Animal ID").pack()
entry_animal = Entry(root)
entry_animal.insert(0, "1")
entry_animal.pack()

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