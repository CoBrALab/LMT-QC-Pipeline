import copy
import os
import cv2
import sqlite3
import pandas as pd
from datetime import datetime
import time
from tkinter import *
from tkinter import filedialog, messagebox
from PIL import Image, ImageTk


# Configurable constants
DB_FPS           = 30    # LMT database frame rate
FRAME_CONVERSION = 2     # 30fps DB -> 15fps video


# Gaps shorter than this threshold (in seconds) are skipped by binary search.
# Their frames remain IN_NEST = -1 in the output.
MIN_GAP_DURATION_FOR_BINARY_SEARCH_IN_SECONDS = 30  # seconds

FILL_ENTIRE_SEGMENT_IF_DURATION_LESS_THAN_IN_MINUTES = 1  # minutes


# Schema note
# Output table: GAP_FILL_ANALYSIS  (matches the table name from Script 1B)
# Columns (all original 1B columns plus one new column):
#   FRAMENUMBER       – frame number (int)
#   IN_NEST           – 1 = in nest, 0 = not in nest, -1 = unknown/assumed
#   ASSUMPTION_TYPE   – "DETECTED" or "ASSUMED"
#   GAP_START_FRAME   – start of the gap this row belongs to (NULL for detected)
#   GAP_END_FRAME     – end of the gap this row belongs to (NULL for detected)
#   BINARY_SEARCH     – 1 if binary search was performed on this row, 0 otherwise
#
# All rows from the Script 1B output are preserved.  Detected rows receive BINARY_SEARCH = 0. Assumed rows that were within a gap long enough to be
# searched receive BINARY_SEARCH = 1; rows in gaps below the threshold remain
# BINARY_SEARCH = 0 (and their IN_NEST stays -1).

# Helpers
def seconds_to_hms(seconds): # Converts seconds to hh:mm:ss
    hours   = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs    = int(seconds % 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"

def seconds_to_hms_ms(seconds): # Converts seconds to hh:mm:ss.ms
    hours   = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs    = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:05.2f}"

def _frames_to_seconds(n_frames):
    return n_frames / DB_FPS

def _gap_duration_seconds(gap_start_frame, gap_end_frame):
    return (gap_end_frame - gap_start_frame - 1) / DB_FPS

def _seg_dur_min(seg_start, seg_end):
    return (seg_end - seg_start + 1) / DB_FPS / 60

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

def extract_frame_to_path(video_map, global_frame, out_path):
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
        return False
    local_frame = int((global_frame - matched_start) / FRAME_CONVERSION)
    cap = cv2.VideoCapture(matched_video)
    if not cap.isOpened():
        return False
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, min(local_frame, total - 1)))
    ret, frame = cap.read()
    cap.release()
    if ret:
        cv2.imwrite(out_path, frame)
        return True
    return False

# Binary-search task builder
def build_initial_tasks(df_negative, df_all):
    df_detected = df_all[df_all["ASSUMPTION_TYPE"] == "DETECTED"].copy()
    detected_frames_sorted = sorted(df_detected["FRAMENUMBER"].tolist())

    def find_boundary_left(gap_start_frame):
        candidates = [f for f in detected_frames_sorted if f <= gap_start_frame]
        return candidates[-1] if candidates else detected_frames_sorted[0]

    def find_boundary_right(gap_end_frame):
        candidates = [f for f in detected_frames_sorted if f >= gap_end_frame]
        return candidates[0] if candidates else detected_frames_sorted[-1]

    groups = (
        df_negative
        .groupby(["GAP_START_FRAME", "GAP_END_FRAME"])
        .size()
        .reset_index(name="count")
    )
    total_gaps       = len(groups)
    tasks            = []
    skipped_frames   = set()
    skipped_gap_keys = set()

    for idx, row in enumerate(groups.itertuples(), start=1):
        gs = int(row.GAP_START_FRAME)
        ge = int(row.GAP_END_FRAME)

        gap_dur_sec = _gap_duration_seconds(gs, ge)

        if gap_dur_sec <= MIN_GAP_DURATION_FOR_BINARY_SEARCH_IN_SECONDS:
            for f in range(gs + 1, ge):
                skipped_frames.add(f)
            skipped_gap_keys.add((gs, ge))
            continue

        gap_start = gs + 1
        gap_end   = ge - 1
        if gap_start > gap_end:
            continue
        mid = (gap_start + gap_end) // 2

        boundary_left  = find_boundary_left(gs)
        boundary_right = find_boundary_right(ge)

        tasks.append({
            "gap_index":      idx,
            "total_gaps":     total_gaps,
            "gap_start":      gap_start,
            "gap_end":        gap_end,
            "show_frame":     mid,
            "boundary_left":  boundary_left,
            "boundary_right": boundary_right,
        })

    return tasks, skipped_frames, skipped_gap_keys

# Consolidated summary report
def write_summary_report(report_path, source_db_path, df_all, decisions, skipped_gap_keys, bsearch_in_nest_frames, bsearch_out_frames, defaulted_to_out_frames, total_review_seconds, gap_timings):
    def pct(n, total):
        return (n / total * 100) if total > 0 else 0.0

    df_det = df_all[df_all["ASSUMPTION_TYPE"] == "DETECTED"]
    det_total     = len(df_det)
    det_in_nest   = int((df_det["IN_NEST"] == 1).sum())
    det_out       = det_total - det_in_nest
    det_total_sec = _frames_to_seconds(det_total)
    det_in_sec    = _frames_to_seconds(det_in_nest)
    det_out_sec   = _frames_to_seconds(det_out)

    df_asm = df_all[df_all["ASSUMPTION_TYPE"] == "ASSUMED"]
    asm_total     = len(df_asm)
    asm_total_sec = _frames_to_seconds(asm_total)

    auto_in_nest  = int((df_asm["IN_NEST"] == 1).sum())
    auto_unknown  = int((df_asm["IN_NEST"] == -1).sum())
    auto_in_sec   = _frames_to_seconds(auto_in_nest)
    auto_unk_sec  = _frames_to_seconds(auto_unknown)

    bs_in_sec          = _frames_to_seconds(bsearch_in_nest_frames)
    bs_out_sec         = _frames_to_seconds(bsearch_out_frames)
    defaulted_out_sec  = _frames_to_seconds(defaulted_to_out_frames)

    skipped_frames_count = sum(
        len(range(gs + 1, ge))
        for gs, ge in skipped_gap_keys
    )
    skipped_sec = _frames_to_seconds(skipped_frames_count)

    df_asm_work = df_all[df_all["ASSUMPTION_TYPE"] == "ASSUMED"].copy()
    gap_groups = (
        df_asm_work
        .groupby(["GAP_START_FRAME", "GAP_END_FRAME"])
        .size()
        .reset_index(name="frame_count")
        .sort_values("GAP_START_FRAME")
        .reset_index(drop=True)
    )
    total_gaps = len(gap_groups)

    if total_gaps > 0:
        total_gap_frames = int(gap_groups["frame_count"].sum())
        avg_gap_sec      = _frames_to_seconds(total_gap_frames / total_gaps)
    else:
        total_gap_frames = 0
        avg_gap_sec      = 0.0

    def gap_label(gs, ge):
        key = (gs, ge)
        gap_frames = df_asm_work[
            (df_asm_work["GAP_START_FRAME"] == gs) & (df_asm_work["GAP_END_FRAME"] == ge)
        ]
        original_values = set(gap_frames["IN_NEST"].unique())
        if -1 not in original_values:
            return "Auto IN NEST = 1"
        if key in skipped_gap_keys:
            return f"Skipped (gap \u2264 {MIN_GAP_DURATION_FOR_BINARY_SEARCH_IN_SECONDS}s threshold)"
        frames_in_gap = list(range(gs + 1, ge))
        resolved = {f: decisions[f] for f in frames_in_gap if f in decisions}
        if not resolved:
            return "Unresolved"
        in_nest_count = sum(1 for v in resolved.values() if v == 1)
        out_count     = sum(1 for v in resolved.values() if v == 0)
        if in_nest_count > 0 and out_count == 0:
            return "Binary Search \u2192 IN NEST = 1"
        if out_count > 0 and in_nest_count == 0:
            return "Binary Search \u2192 OUT OF NEST = 0"
        return f"Binary Search \u2192 mixed ({in_nest_count} IN / {out_count} OUT)"

    with open(report_path, "w", encoding="utf-8") as f:

        f.write("LMT Pipeline Summary Report\n")
        f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Source database: {source_db_path}\n")
        f.write("\n")

        f.write("=" * 70 + "\n")
        f.write("BINARY SEARCH REVIEW TIMING\n")
        f.write("=" * 70 + "\n\n")
        f.write(f"  Total review duration:   {seconds_to_hms(total_review_seconds)}  ({total_review_seconds:.1f}s)\n\n")

        f.write("=" * 70 + "\n")
        f.write("LMT DETECTION SUMMARY\n")
        f.write("=" * 70 + "\n\n")
        f.write(f"Total detected frames:   {det_total:>10,}  ({seconds_to_hms(det_total_sec)})\n\n")
        f.write("  IN NEST (IN_NEST = 1)\n")
        f.write("  " + "-" * 46 + "\n")
        f.write(f"  Frames:    {det_in_nest:>10,}  ({seconds_to_hms(det_in_sec)})\n")
        f.write(f"  % of detected:  {pct(det_in_nest, det_total):>6.1f}%\n\n")
        f.write("  OUT OF NEST (IN_NEST = 0)\n")
        f.write("  " + "-" * 46 + "\n")
        f.write(f"  Frames:    {det_out:>10,}  ({seconds_to_hms(det_out_sec)})\n")
        f.write(f"  % of detected:  {pct(det_out, det_total):>6.1f}%\n\n")

        f.write("=" * 70 + "\n")
        f.write("MISSING / ASSUMED FRAMES SUMMARY\n")
        f.write("=" * 70 + "\n\n")
        f.write(f"Total assumed frames:    {asm_total:>10,}  ({seconds_to_hms(asm_total_sec)})\n\n")
        f.write("  Automatically classified IN NEST = 1 (Script 1B)\n")
        f.write("  " + "-" * 46 + "\n")
        f.write(f"  Frames:    {auto_in_nest:>10,}  ({seconds_to_hms(auto_in_sec)})\n")
        f.write(f"  % of assumed:   {pct(auto_in_nest, asm_total):>6.1f}%\n\n")
        f.write("  Automatically classified IN_NEST = -1 (unknown)\n")
        f.write("  " + "-" * 46 + "\n")
        f.write(f"  Frames:    {auto_unknown:>10,}  ({seconds_to_hms(auto_unk_sec)})\n")
        f.write(f"  % of assumed:   {pct(auto_unknown, asm_total):>6.1f}%\n\n")

        #  Reconciliation check 
        # Three outcomes sum back to auto_unknown:
        #   IN (explicit) + OUT (all, from df_out) + skipped = starting population
        # defaulted_to_out_frames is a sub-breakdown of bsearch_out_frames,
        # not a separate addend.
        reconciled_total = bsearch_in_nest_frames + bsearch_out_frames + skipped_frames_count
        reconciliation_ok = (reconciled_total == auto_unknown)

        f.write("=" * 70 + "\n")
        f.write("BINARY SEARCH RESULTS\n")
        f.write("=" * 70 + "\n\n")
        f.write(f"  Starting population (IN_NEST = -1):  {auto_unknown:,} frames\n\n")

        f.write("  (1) Reclassified to IN NEST = 1  [binary search — explicit]\n")
        f.write("  " + "-" * 56 + "\n")
        f.write(f"  Frames:    {bsearch_in_nest_frames:>10,}  ({seconds_to_hms(bs_in_sec)})\n")
        f.write(f"  % of starting population:  {pct(bsearch_in_nest_frames, auto_unknown):>6.1f}%\n\n")

        f.write("  (2) Assigned OUT OF NEST = 0  [all OUT frames combined]\n")
        f.write("  " + "-" * 56 + "\n")
        f.write(f"  Frames:    {bsearch_out_frames:>10,}  ({seconds_to_hms(bs_out_sec)})\n")
        f.write(f"  % of starting population:  {pct(bsearch_out_frames, auto_unknown):>6.1f}%\n\n")

        explicit_out_count = bsearch_out_frames - defaulted_to_out_frames
        f.write(f"    Of which explicitly decided by binary search:\n")
        f.write(f"      Frames:  {explicit_out_count:>10,}  ({seconds_to_hms(_frames_to_seconds(explicit_out_count))})\n\n")

        f.write(f"    Of which defaulted to OUT (not explicitly decided):\n")
        f.write(f"      Frames:  {defaulted_to_out_frames:>10,}  ({seconds_to_hms(defaulted_out_sec)})\n")
        f.write(f"      These frames were in the searchable population but received\n")
        f.write(f"      IN_NEST = 0 via the default fallback, not a user answer.\n")
        f.write(f"      They fall into three sub-categories:\n")
        f.write(f"        a) Gaps queued for binary search but never reached before\n")
        f.write(f"           the session ended (labelled 'Unresolved' in Gap Details).\n")
        f.write(f"        b) Sub-tasks in mixed gaps still in the queue at session end.\n")
        f.write(f"        c) Frames to the right of an OUT boundary resolved by the\n")
        f.write(f"           default-zero fallback rather than an explicit answer.\n\n")

        f.write(f"  (3) Remaining IN_NEST = -1  (gaps \u2264 {MIN_GAP_DURATION_FOR_BINARY_SEARCH_IN_SECONDS}s threshold, not searched)\n")
        f.write("  " + "-" * 56 + "\n")
        f.write(f"  Frames:    {skipped_frames_count:>10,}  ({seconds_to_hms(skipped_sec)})\n")
        f.write(f"  % of starting population:  {pct(skipped_frames_count, auto_unknown):>6.1f}%\n\n")

        f.write("  " + "-" * 56 + "\n")
        f.write(f"  Reconciliation (1) + (2) + (3):  {reconciled_total:,}")
        if reconciliation_ok:
            f.write("  \u2713 matches starting population\n\n")
        else:
            f.write(f"  \u2717 MISMATCH (expected {auto_unknown:,})\n\n")

        f.write("=" * 70 + "\n")
        f.write("GAP STATISTICS\n")
        f.write("=" * 70 + "\n\n")
        f.write(f"Total number of gaps:    {total_gaps:,}\n")
        f.write(f"Average gap duration:    {seconds_to_hms(avg_gap_sec)}\n\n")

        f.write("=" * 70 + "\n")
        f.write("GAP DETAILS\n")
        f.write("=" * 70 + "\n\n")

        col_w = [6, 13, 13, 15, 22, 18, 42]
        header = (
            f"{'Gap #':<{col_w[0]}}"
            f"{'Gap Start':<{col_w[1]}}"
            f"{'Gap End':<{col_w[2]}}"
            f"{'Frames in Gap':<{col_w[3]}}"
            f"{'Duration':<{col_w[4]}}"
            f"{'Review Time':<{col_w[5]}}"
            f"{'Assumption':<{col_w[6]}}\n"
        )
        f.write(header)
        f.write("-" * sum(col_w) + "\n")

        for i, row in enumerate(gap_groups.itertuples(), start=1):
            gs       = int(row.GAP_START_FRAME)
            ge       = int(row.GAP_END_FRAME)
            n_frames = int(row.frame_count)
            dur_sec  = _frames_to_seconds(n_frames)
            label    = gap_label(gs, ge)

            review_time_str = "N/A"
            for gt in gap_timings:
                if gt["gap_start"] == gs + 1 and gt["gap_end"] == ge - 1:
                    review_time_str = seconds_to_hms(gt["duration_seconds"])
                    break

            f.write(
                f"{i:<{col_w[0]}}"
                f"{gs:<{col_w[1]}}"
                f"{ge:<{col_w[2]}}"
                f"{n_frames:<{col_w[3]}}"
                f"{seconds_to_hms(dur_sec):<{col_w[4]}}"
                f"{review_time_str:<{col_w[5]}}"
                f"{label:<{col_w[6]}}\n"
            )

        f.write("\n")

# Main GUI class
class BinarySearchGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("LMT Binary Search Gap Filler")
        self.root.geometry("1600x950")

        self.db_path          = ""
        self.output_folder    = ""
        self.video_paths      = []
        self.video_map        = []

        self.df               = None   # assumed rows only (for binary search logic)
        self.df_all           = None   # ALL rows from 1B (preserved for final output)
        self.df_neg           = None   # assumed rows with IN_NEST == -1

        self.temp_dir         = ""

        self.task_stack       = []
        self.redo_stack       = []
        self.history          = []
        self.current_task     = None
        self.decisions        = {}
        self.skipped_frames   = set()
        self.skipped_gap_keys = set()

        self._review_start_time   = None
        self._gap_start_time      = None
        self._current_gap_index   = None
        self._gap_timings         = []

        self._photo_left   = None
        self._photo_center = None
        self._photo_right  = None

        self._build_setup_ui()

    #  Setup screen 
    def _build_setup_ui(self):
        self.setup_frame = Frame(self.root)
        self.setup_frame.pack(fill=BOTH, expand=True, padx=20, pady=20)

        Label(self.setup_frame, text="LMT Binary Search Gap Filler", font=("Arial", 16, "bold")).pack(pady=10)

        Label(self.setup_frame,
              text=(
                  "Fills ASSUMED frames where IN_NEST = -1 using binary search.\n"
                  f"Gaps \u2264 {MIN_GAP_DURATION_FOR_BINARY_SEARCH_IN_SECONDS}s are skipped "
                  f"(frames remain IN_NEST = -1).\n\n"
                  "Reads directly from a Script 1B output SQLite.\n\n"
                  "Each gap always shows:  LEFT = last detected frame before gap  |  "
                  "CENTER = frame under review  |  RIGHT = first detected frame after gap\n\n"
                  "Keyboard:  A = IN NEST              D = OUT OF NEST\n"
                  "           \u2190 = Undo last answer    \u2192 = Redo / advance\n\n"
                  "Output: GAP_FILL_ANALYSIS table (all rows from 1B preserved, "
                  "BINARY_SEARCH column added)"
              ),
              font=("Arial", 11), justify=LEFT).pack(pady=10)

        Button(self.setup_frame, text="Select Script 1B SQLite (GAP_FILL_ANALYSIS)", command=self._select_db).pack(pady=5)
        self.lbl_db = Label(self.setup_frame, text="No database selected", wraplength=1000)
        self.lbl_db.pack()

        Button(self.setup_frame, text="Select LMT Videos", command=self._select_videos).pack(pady=5)
        self.lbl_vid = Label(self.setup_frame, text="No videos selected")
        self.lbl_vid.pack()

        Button(self.setup_frame, text="Select Output Folder", command=self._select_output).pack(pady=5)
        self.lbl_out = Label(self.setup_frame, text="No output folder selected", wraplength=1000)
        self.lbl_out.pack()

        Button(self.setup_frame, text="START BINARY SEARCH", command=self._start, bg="green", fg="white", width=30, height=2).pack(pady=20)

    def _select_db(self):
        self.db_path = filedialog.askopenfilename(filetypes=[("SQLite Database", "*.sqlite")])
        self.lbl_db.config(text=self.db_path)

    def _select_videos(self):
        self.video_paths = list(filedialog.askopenfilenames(filetypes=[("MP4 Video", "*.mp4")]))
        self.lbl_vid.config(text=f"{len(self.video_paths)} video(s) selected")

    def _select_output(self):
        self.output_folder = filedialog.askdirectory()
        self.lbl_out.config(text=self.output_folder)

    def _start(self):
        if not self.db_path:
            messagebox.showerror("Error", "Please select the Script 1B SQLite."); return
        if not self.video_paths:
            messagebox.showerror("Error", "Please select at least one LMT video."); return
        if not self.output_folder:
            messagebox.showerror("Error", "Please select an output folder."); return

        conn = sqlite3.connect(self.db_path)
        # Load ALL rows — this is the full 1B output we will extend
        self.df_all = pd.read_sql_query("SELECT * FROM GAP_FILL_ANALYSIS ORDER BY FRAMENUMBER", conn)
        conn.close()

        # Assumed subset used for binary-search logic (unchanged from original)
        self.df = self.df_all[self.df_all["ASSUMPTION_TYPE"] == "ASSUMED"].copy().reset_index(drop=True)

        self.df_neg = self.df[self.df["IN_NEST"] == -1].copy()
        if len(self.df_neg) == 0:
            messagebox.showinfo("Nothing to do", "No frames with IN_NEST = -1 found. Nothing to fill.")
            return

        self.video_map = build_video_map(self.video_paths)
        if not self.video_map:
            messagebox.showerror("Error", "Could not parse any valid videos."); return

        self.temp_dir = os.path.join(self.output_folder, "_binsearch_tmp")
        os.makedirs(self.temp_dir, exist_ok=True)

        tasks, self.skipped_frames, self.skipped_gap_keys = build_initial_tasks(self.df_neg, self.df_all)
        self.task_stack   = tasks
        self.redo_stack   = []
        self.history      = []

        self.decisions    = {}
        self.current_task = None
        self._gap_timings = []

        if not self.task_stack:
            messagebox.showinfo(
                "Nothing to search",
                f"All IN_NEST = -1 gaps are \u2264 {MIN_GAP_DURATION_FOR_BINARY_SEARCH_IN_SECONDS}s "
                f"and will be skipped.\nProceeding directly to output."
            )
            self._finish()
            return

        self._review_start_time = time.time()

        self.setup_frame.pack_forget()
        self._build_qc_ui()
        self._load_next_task()

    # QC screen 
    def _build_qc_ui(self):
        self.qc_frame = Frame(self.root)
        self.qc_frame.pack(fill=BOTH, expand=True)

        images_frame = Frame(self.qc_frame, bg="#1a1a1a")
        images_frame.pack(side=TOP, fill=X, padx=5, pady=5)

        # Configure equal-width columns
        images_frame.grid_columnconfigure(0, weight=1, uniform="screens")
        images_frame.grid_columnconfigure(1, weight=1, uniform="screens")
        images_frame.grid_columnconfigure(2, weight=1, uniform="screens")
        images_frame.grid_rowconfigure(0, weight=1)

        # LEFT PANEL
        left_panel = Frame(images_frame, bg="#1a1a1a")
        left_panel.grid(row=0, column=0, sticky="nsew", padx=8, pady=4)

        Label(
            left_panel,
            text="LAST DETECTED BEFORE GAP",
            font=("Arial", 9, "bold"),
            fg="#aaaaaa",
            bg="#1a1a1a"
        ).pack()

        self.lbl_left_frame_num = Label(
            left_panel,
            text="Frame —",
            font=("Arial", 8),
            fg="#888888",
            bg="#1a1a1a"
        )
        self.lbl_left_frame_num.pack()

        self.img_left = Label(left_panel, bg="#1a1a1a")
        self.img_left.pack(pady=5, expand=True)

        # CENTER PANEL
        center_panel = Frame(images_frame, bg="#0d2a0d", bd=2, relief=GROOVE)
        center_panel.grid(row=0, column=1, sticky="nsew", padx=8, pady=4)

        Label(
            center_panel,
            text="▶  FRAME UNDER REVIEW  ◀",
            font=("Arial", 9, "bold"),
            fg="#55ff55",
            bg="#0d2a0d"
        ).pack()

        self.lbl_center_frame_num = Label(
            center_panel,
            text="Frame —",
            font=("Arial", 8),
            fg="#88cc88",
            bg="#0d2a0d"
        )
        self.lbl_center_frame_num.pack()

        self.img_center = Label(center_panel, bg="#0d2a0d")
        self.img_center.pack(pady=5, expand=True)

        # RIGHT PANEL
        right_panel = Frame(images_frame, bg="#1a1a1a")
        right_panel.grid(row=0, column=2, sticky="nsew", padx=8, pady=4)

        Label(
            right_panel,
            text="FIRST DETECTED AFTER GAP",
            font=("Arial", 9, "bold"),
            fg="#aaaaaa",
            bg="#1a1a1a"
        ).pack()

        self.lbl_right_frame_num = Label(
            right_panel,
            text="Frame —",
            font=("Arial", 8),
            fg="#888888",
            bg="#1a1a1a"
        )
        self.lbl_right_frame_num.pack()

        self.img_right = Label(right_panel, bg="#1a1a1a")
        self.img_right.pack(pady=5, expand=True)

        bottom = Frame(self.qc_frame)
        bottom.pack(side=BOTTOM, fill=X, padx=20, pady=8)

        info_left = Frame(bottom)
        info_left.pack(side=LEFT, anchor=W)

        self.lbl_gap_counter = Label(info_left, text="", font=("Arial", 14, "bold"))
        self.lbl_gap_counter.pack(anchor=W)
        self.lbl_tasks_left = Label(info_left, text="", font=("Arial", 10), fg="#555555")
        self.lbl_tasks_left.pack(anchor=W)
        self.lbl_gap = Label(info_left, text="", font=("Arial", 10))
        self.lbl_gap.pack(anchor=W)
        self.lbl_segment = Label(info_left, text="", font=("Arial", 10))
        self.lbl_segment.pack(anchor=W)
        self.lbl_answer = Label(info_left, text="", font=("Arial", 12, "bold"))
        self.lbl_answer.pack(anchor=W, pady=(6, 0))

        btn_frame = Frame(bottom)
        btn_frame.pack(side=RIGHT, anchor=E)

        Button(btn_frame, text="IN NEST  (A)", bg="green", fg="white", width=20, height=2, command=lambda: self._handle_answer(True)).grid(row=0, column=0, padx=6, pady=4)
        Button(btn_frame, text="OUT OF NEST  (D)", bg="red", fg="white", width=20, height=2, command=lambda: self._handle_answer(False)).grid(row=0, column=1, padx=6, pady=4)
        Button(btn_frame, text="◀  Undo  (Left)",  width=18, command=self._go_previous).grid(row=1, column=0, padx=6, pady=2)
        Button(btn_frame, text="Redo  (Right)  ▶", width=18, command=self._go_next).grid(row=1, column=1, padx=6, pady=2)

        Label(btn_frame, text="A = IN NEST   D = OUT   ← Undo   → Redo", font=("Arial", 9), fg="#777777").grid(row=2, column=0, columnspan=2, pady=2)

        self.root.bind("<a>",     lambda e: self._handle_answer(True))
        self.root.bind("<A>",     lambda e: self._handle_answer(True))
        self.root.bind("<d>",     lambda e: self._handle_answer(False))
        self.root.bind("<D>",     lambda e: self._handle_answer(False))
        self.root.bind("<Left>",  lambda e: self._go_previous())
        self.root.bind("<Right>", lambda e: self._go_next())

    def _load_frame_into_label(self, label, frame_number, cache_key):
        frame_path = os.path.join(self.temp_dir, f"frame_{cache_key}.png")
        success = extract_frame_to_path(self.video_map, frame_number, frame_path)
        if success and os.path.exists(frame_path):
            img = Image.open(frame_path)
            img.thumbnail((480, 380))
            photo = ImageTk.PhotoImage(img)
            label.config(image=photo, text="")
            label.image = photo
            return True
        else:
            label.config(image="", text="[Frame not available]", fg="#888888")
            label.image = None
            return False

    def _refresh_display(self, task, answer_text="", answer_color="black"):
        gap_start_inner = task["gap_start"]
        gap_end_inner   = task["gap_end"]
        gap_dur_min     = _seg_dur_min(gap_start_inner, gap_end_inner)
        seg_dur_min     = _seg_dur_min(task.get("seg_start", gap_start_inner), task.get("seg_end",   gap_end_inner))
        same_gap_ahead  = sum(1 for t in self.task_stack if t["gap_index"] == task["gap_index"])

        self.lbl_gap_counter.config(text=f"Gap  {task['gap_index']}  /  {task['total_gaps']}")
        self.lbl_tasks_left.config(text=f"{same_gap_ahead} sub-task(s) remaining in this gap")
        self.lbl_gap.config(text=f"Full gap:   frame {gap_start_inner} \u2013 {gap_end_inner}  ({gap_dur_min:.1f} min)")
        self.lbl_segment.config(text=f"Segment:   frame {task.get('seg_start', gap_start_inner)} \u2013 {task.get('seg_end', gap_end_inner)}  ({seg_dur_min:.1f} min)")
        self.lbl_answer.config(text=answer_text, fg=answer_color)

        b_left  = task["boundary_left"]
        b_right = task["boundary_right"]
        mid     = task["show_frame"]

        self.lbl_left_frame_num.config(text=f"Frame {b_left}")
        self._load_frame_into_label(self.img_left, b_left, f"bl_{b_left}")

        self.lbl_center_frame_num.config(text=f"Frame {mid}")
        self._load_frame_into_label(self.img_center, mid, f"mid_{mid}")

        self.lbl_right_frame_num.config(text=f"Frame {b_right}")
        self._load_frame_into_label(self.img_right, b_right, f"br_{b_right}")


    def _load_next_task(self):
        if not self.task_stack:
            self._finish()
            return

        incoming_task = self.task_stack[0]

        if self._current_gap_index != incoming_task["gap_index"]:
            if self._gap_start_time is not None and self._current_gap_index is not None:
                elapsed = time.time() - self._gap_start_time
                for past_task, _ in reversed(self.history):
                    if past_task["gap_index"] == self._current_gap_index:
                        self._gap_timings.append({
                            "gap_index":       self._current_gap_index,
                            "gap_start":       past_task["gap_start"],
                            "gap_end":         past_task["gap_end"],
                            "duration_seconds": elapsed,
                        })
                        break

            self._gap_start_time      = time.time()
            self._current_gap_index   = incoming_task["gap_index"]

        self.current_task = self.task_stack.pop(0)
        self._refresh_display(self.current_task)


    def _handle_answer(self, in_nest: bool):
        task = self.current_task
        if task is None:
            return

        snapshot = copy.deepcopy(self.decisions)
        self.history.append((task, snapshot))
        self.redo_stack.clear()

        seg_start = task.get("seg_start", task["gap_start"])
        seg_end   = task.get("seg_end",   task["gap_end"])
        mid       = task["show_frame"]

        b_left  = task["boundary_left"]
        b_right = task["boundary_right"]

        seg_duration_minutes = (seg_end - seg_start + 1) / DB_FPS / 60

        if seg_duration_minutes <= FILL_ENTIRE_SEGMENT_IF_DURATION_LESS_THAN_IN_MINUTES:
            fill_value = 1 if in_nest else 0
            for f in range(seg_start, seg_end + 1):
                self.decisions[f] = fill_value
            label = "IN NEST" if in_nest else "OUT OF NEST"
            color = "green"  if in_nest else "red"
            self._refresh_display(
                task,
                answer_text=f"{label} \u2014 short segment, filling entire segment",
                answer_color=color)
            self.root.after(300, self._load_next_task)
            return

        if in_nest:
            for f in range(seg_start, mid + 1):
                self.decisions[f] = 1
            self._refresh_display(
                task,
                answer_text="IN NEST \u2014 filling left half, continuing right",
                answer_color="green")
            right_start = mid + 1
            right_end   = seg_end
            if right_start <= right_end:
                right_mid = (right_start + right_end) // 2
                self.task_stack.insert(0, {
                    "gap_index":      task["gap_index"],
                    "total_gaps":     task["total_gaps"],
                    "gap_start":      task["gap_start"],
                    "gap_end":        task["gap_end"],
                    "seg_start":      right_start,
                    "seg_end":        right_end,
                    "show_frame":     right_mid,
                    "boundary_left":  b_left,
                    "boundary_right": b_right,
                })
            self.root.after(300, self._load_next_task)

        else:
            left_start = seg_start
            left_end   = mid - 1
            self._refresh_display(
                task,
                answer_text="OUT OF NEST \u2014 searching left half",
                answer_color="red")
            if left_start <= left_end:
                left_mid = (left_start + left_end) // 2
                self.task_stack.insert(0, {
                    "gap_index":      task["gap_index"],
                    "total_gaps":     task["total_gaps"],
                    "gap_start":      task["gap_start"],
                    "gap_end":        task["gap_end"],
                    "seg_start":      left_start,
                    "seg_end":        left_end,
                    "show_frame":     left_mid,
                    "boundary_left":  b_left,
                    "boundary_right": b_right,
                })
            else:
                for f in range(mid, seg_end + 1):
                    self.decisions[f] = 0
            self.root.after(300, self._load_next_task)

    def _go_previous(self):
        if not self.history:
            return
        if self.current_task is not None:
            self.redo_stack.append(
                (self.current_task, copy.deepcopy(self.decisions)))
            self.task_stack.insert(0, self.current_task)
        prev_task, prev_decisions = self.history.pop()
        self.decisions    = prev_decisions
        self.current_task = prev_task
        self.task_stack   = [
            t for t in self.task_stack
            if not (
                t["gap_index"] == prev_task["gap_index"] and
                t.get("seg_start", t["gap_start"]) >= prev_task.get("seg_start", prev_task["gap_start"]) and
                t.get("seg_end",   t["gap_end"])   <= prev_task.get("seg_end",   prev_task["gap_end"]) and
                t is not prev_task
            )
        ]
        self._refresh_display(prev_task,
            answer_text="Undone \u2014 re-answer or continue",
            answer_color="#888888")

    def _go_next(self):
        if self.redo_stack:
            redo_task, redo_decisions = self.redo_stack.pop()
            if self.current_task is not None:
                self.history.append(
                    (self.current_task, copy.deepcopy(self.decisions)))
            self.decisions    = redo_decisions
            self.current_task = redo_task
            self.task_stack   = [t for t in self.task_stack if t is not redo_task]
            self._refresh_display(redo_task,
                answer_text="Redone", answer_color="#555555")
        else:
            if self.task_stack:
                if self.current_task is not None:
                    self.history.append(
                        (self.current_task, copy.deepcopy(self.decisions)))
                self._load_next_task()

    # Finish 
    def _finish(self):
        # Close out the last gap timer
        if self._gap_start_time is not None and self._current_gap_index is not None:
            elapsed = time.time() - self._gap_start_time
            for past_task, _ in reversed(self.history):
                if past_task["gap_index"] == self._current_gap_index:
                    self._gap_timings.append({
                        "gap_index":        self._current_gap_index,
                        "gap_start":        past_task["gap_start"],
                        "gap_end":          past_task["gap_end"],
                        "duration_seconds": elapsed,
                    })
                    break

        total_review_seconds = (
            time.time() - self._review_start_time
            if self._review_start_time else 0.0
        )

        #  Build the output by working from the FULL 1B dataset ─
        # We preserve every row from df_all and apply binary-search decisions
        # only to assumed rows with IN_NEST == -1.
        df_out = self.df_all.copy()

        # Set of all frame numbers that were candidates for binary search
        # (assumed rows with original IN_NEST == -1)
        neg_frames = set(self.df_all[(self.df_all["ASSUMPTION_TYPE"] == "ASSUMED") & (self.df_all["IN_NEST"] == -1)]["FRAMENUMBER"].tolist())

        # Searchable = neg_frames minus the ones skipped due to short gap threshold
        searchable_frames = neg_frames - self.skipped_frames

        def apply_decision(row):
            """Update IN_NEST for assumed rows that were binary-searched."""
            if row["ASSUMPTION_TYPE"] != "ASSUMED":
                # Detected rows are never modified
                return row["IN_NEST"]
            if row["IN_NEST"] != -1:
                # Assumed rows already classified by Script 1B (IN_NEST = 1)
                return row["IN_NEST"]
            fn = int(row["FRAMENUMBER"])
            if fn in self.skipped_frames:
                # Below threshold — stays -1
                return -1
            # Binary-search result; default to 0 (OUT OF NEST) if no decision recorded
            return self.decisions.get(fn, 0)

        df_out["IN_NEST"] = df_out.apply(apply_decision, axis=1)

        # BINARY_SEARCH = 1 only for frames that were actually searched
        df_out["BINARY_SEARCH"] = df_out["FRAMENUMBER"].apply(
            lambda fn: 1 if int(fn) in searchable_frames else 0
        )

        '''
        Compute summary stats from df_out (ground truth) 
        Using df_out rather than self.decisions ensures every frame that ended up with a value in the database is counted, including frames that were never explicitly answered and received IN_NEST=0 via the default fallback in apply_decision().
        '''
        df_searched = df_out[(df_out["ASSUMPTION_TYPE"] == "ASSUMED") &(df_out["FRAMENUMBER"].apply(lambda fn: int(fn) in searchable_frames))]
        bsearch_in_nest_frames = int((df_searched["IN_NEST"] == 1).sum())
        bsearch_out_frames     = int((df_searched["IN_NEST"] == 0).sum())

        # Explicitly-decided OUT frames (answered by the user directly)
        explicit_out = sum(
            1 for fn, val in self.decisions.items()
            if val == 0 and fn in neg_frames and fn not in self.skipped_frames
        )
        # Defaulted-to-zero frames: in the database as OUT but never explicitly
        # answered.  These are the frames that make bsearch_out_frames >
        # explicit_out, and are the source of the reconciliation gap reported
        # in previous versions.
        defaulted_to_out_frames = bsearch_out_frames - explicit_out

        # Write output SQLite 
        # Table name is GAP_FILL_ANALYSIS — matches Script 1B for consistency.
        timestamp  = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        out_sqlite = os.path.join(self.output_folder, f"lmt_binary_search_{timestamp}.sqlite")
        conn = sqlite3.connect(out_sqlite)
        df_out.to_sql("GAP_FILL_ANALYSIS", conn, if_exists="replace", index=False)
        conn.close()


        #  Write summary report 
        report_path = os.path.join(self.output_folder, f"LMT_Summary_{timestamp}.txt")
        write_summary_report(
            report_path,
            self.db_path,
            self.df_all,
            self.decisions,
            self.skipped_gap_keys,
            bsearch_in_nest_frames,
            bsearch_out_frames,
            defaulted_to_out_frames,
            total_review_seconds,
            self._gap_timings,
        )


        # Count remaining unknowns (only in assumed rows)
        neg_remaining = int(
            (df_out[df_out["ASSUMPTION_TYPE"] == "ASSUMED"]["IN_NEST"] == -1).sum()
        )


        messagebox.showinfo(
            "Binary Search Complete",
            f"All gaps processed.\n\n"
            f"Total review time:              {seconds_to_hms(total_review_seconds)}\n\n"
            f"Starting population (IN_NEST=-1): {len(searchable_frames) + len(self.skipped_frames):,}\n\n"
            f"Outcomes:\n"
            f"  IN NEST = 1  (explicit):       {bsearch_in_nest_frames:,}\n"
            f"  OUT OF NEST = 0  (explicit):   {explicit_out:,}\n"
            f"  OUT OF NEST = 0  (defaulted):  {defaulted_to_out_frames:,}\n"
            f"  Skipped (below threshold):     {len(self.skipped_frames):,}\n\n"
            f"Remaining IN_NEST = -1:          {neg_remaining:,}\n\n"
            f"Total rows in output:            {len(df_out):,}\n"
            f"  (detected + assumed, all preserved from 1B)\n\n"
            f"SQLite output:\n{out_sqlite}\n\n"
            f"Summary report:\n{report_path}\n\n"
            f"Feed the SQLite into Script 4B."
        )
        self.root.quit()

if __name__ == "__main__":
    from PIL import Image, ImageTk
    root = Tk()
    app  = BinarySearchGUI(root)
    root.mainloop()

