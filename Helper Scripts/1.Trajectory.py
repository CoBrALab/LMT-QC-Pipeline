"""
0.lmt_trajectory.py
-------------------
Standalone trajectory and stationary-duration analysis for a single animal.

Reads MASS_X / MASS_Y from the LMT DETECTION table.
Produces:
  - Colour-coded trajectory plot (time → colour)
  - Stationary-bout duration histogram
  - Summary statistics text report
  - Recommended stationary threshold for use in 1.lmt_interval_fill_review_v3.py

Does NOT write or modify any downstream data.
"""

import os
import sqlite3
import logging
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.cm as cm
from datetime import datetime
from tkinter import *
from tkinter import filedialog, messagebox

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

# ── Configurable constants ────────────────────────────────────────────────────
DB_FPS                  = 30    # LMT database frame rate (frames per second)
STATIONARY_PIXEL_THRESH = 10    # px; displacement below this = stationary
MIN_STATIONARY_SECONDS  = 30    # seconds; shorter bouts are ignored
# ─────────────────────────────────────────────────────────────────────────────


def seconds_to_hms(s):
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    sc = int(s % 60)
    return f"{h:02d}:{m:02d}:{sc:02d}"


def compute_stationary_bouts(df):
    """
    Return list of stationary bout durations in seconds.

    A bout is a run of consecutive detected frames (gap == 1 frame) where the
    Euclidean displacement between each pair is below STATIONARY_PIXEL_THRESH.
    Non-consecutive frame pairs (i.e. gaps in detection) break the bout.
    Bouts shorter than MIN_STATIONARY_SECONDS are discarded.
    """
    min_frames = int(MIN_STATIONARY_SECONDS * DB_FPS)
    bouts = []
    bout_len = 0

    frames = df["FRAMENUMBER"].to_numpy()
    xs     = df["MASS_X"].to_numpy()
    ys     = df["MASS_Y"].to_numpy()

    for i in range(len(frames) - 1):
        consecutive = (frames[i + 1] - frames[i]) == 1
        if not consecutive:
            if bout_len >= min_frames:
                bouts.append(bout_len / DB_FPS)
            bout_len = 0
            continue

        dx   = xs[i + 1] - xs[i]
        dy   = ys[i + 1] - ys[i]
        disp = (dx * dx + dy * dy) ** 0.5

        if disp < STATIONARY_PIXEL_THRESH:
            bout_len += 1
        else:
            if bout_len >= min_frames:
                bouts.append(bout_len / DB_FPS)
            bout_len = 0

    # close final bout
    if bout_len >= min_frames:
        bouts.append(bout_len / DB_FPS)

    return bouts


def suggest_threshold_minutes(bouts):
    """
    Suggest a stationary threshold in whole minutes.

    Strategy: 75th-percentile of bout durations, rounded up to nearest
    5 minutes, clamped to [5, 60].  Falls back to 30 min if no bouts exist.
    """
    if not bouts:
        logging.warning("No stationary bouts found; defaulting threshold to 30 min.")
        return 30
    p75_sec = float(np.percentile(bouts, 75))
    p75_min = p75_sec / 60.0
    rounded = int(np.ceil(p75_min / 5.0)) * 5
    return max(5, min(60, rounded))

def compute_gap_table(df):
    """
    Detect all missing gaps and compute per-gap velocity statistics.
    Returns a DataFrame with one row per gap.
    """
    frames = df["FRAMENUMBER"].to_numpy()
    xs     = df["MASS_X"].to_numpy()
    ys     = df["MASS_Y"].to_numpy()

    records = []
    for i in range(len(frames) - 1):
        gap_frames = int(frames[i + 1]) - int(frames[i]) - 1
        if gap_frames < 1:
            continue

        gap_dur_sec = gap_frames / DB_FPS

        # Velocity before gap: displacement between frame[i-1] and frame[i]
        # Only valid if frame[i-1] exists AND is consecutive with frame[i]
        if i >= 1 and (int(frames[i]) - int(frames[i - 1])) == 1:
            dx   = float(xs[i]) - float(xs[i - 1])
            dy   = float(ys[i]) - float(ys[i - 1])
            dt   = 1.0 / DB_FPS
            v_before = (dx**2 + dy**2) ** 0.5 / dt
        else:
            v_before = float("nan")

        # Velocity after gap: displacement between frame[i+1] and frame[i+2]
        # Only valid if frame[i+2] exists AND is consecutive with frame[i+1]
        if i + 2 < len(frames) and (int(frames[i + 2]) - int(frames[i + 1])) == 1:
            dx   = float(xs[i + 2]) - float(xs[i + 1])
            dy   = float(ys[i + 2]) - float(ys[i + 1])
            dt   = 1.0 / DB_FPS
            v_after = (dx**2 + dy**2) ** 0.5 / dt
        else:
            v_after = float("nan")

        records.append({
            "gap_index":        len(records) + 1,
            "start_frame":      int(frames[i]),
            "end_frame":        int(frames[i + 1]),
            "gap_length_frames": gap_frames,
            "gap_duration_sec": round(gap_dur_sec, 4),
            "velocity_before_px_per_sec": round(v_before, 4) if v_before == v_before else float("nan"),
            "velocity_after_px_per_sec":  round(v_after,  4) if v_after  == v_after  else float("nan"),
        })

    return pd.DataFrame(records)

def run_analysis(input_db, output_folder, animal_id):
    logging.info(f"Loading DETECTION data for Animal {animal_id} from {input_db}")

    conn = sqlite3.connect(input_db)
    df   = pd.read_sql_query(
        f"""
        SELECT FRAMENUMBER, MASS_X, MASS_Y
        FROM   DETECTION
        WHERE  ANIMALID = {animal_id}
        ORDER  BY FRAMENUMBER
        """,
        conn,
    )
    conn.close()

    if len(df) == 0:
        raise Exception(f"No DETECTION rows found for Animal ID {animal_id}.")

    logging.info(f"  {len(df):,} detected frames loaded.")
    os.makedirs(output_folder, exist_ok=True)

    date_str   = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    traj_png   = os.path.join(output_folder, f"trajectory_A{animal_id}_{date_str}.png")
    hist_png   = os.path.join(output_folder, f"stationary_hist_A{animal_id}_{date_str}.png")
    report_txt = os.path.join(output_folder, f"trajectory_report_A{animal_id}_{date_str}.txt")

    frames_arr = df["FRAMENUMBER"].to_numpy(dtype=float)
    xs         = df["MASS_X"].to_numpy(dtype=float)
    ys         = df["MASS_Y"].to_numpy(dtype=float)

    recording_sec = (frames_arr[-1] - frames_arr[0]) / DB_FPS

    # ── Trajectory plot ───────────────────────────────────────────────────────
    logging.info("Generating trajectory plot …")
    norm_t  = (frames_arr - frames_arr[0]) / max(frames_arr[-1] - frames_arr[0], 1)
    colours = cm.plasma(norm_t)

    fig, ax = plt.subplots(figsize=(11, 9))
    for i in range(len(xs) - 1):
        ax.plot([xs[i], xs[i + 1]], [ys[i], ys[i + 1]],
                color=colours[i], linewidth=0.35, alpha=0.75)

    sm = plt.cm.ScalarMappable(
        cmap="plasma",
        norm=plt.Normalize(vmin=0, vmax=recording_sec))
    sm.set_array([])
    cbar = fig.colorbar(sm, ax=ax, fraction=0.03, pad=0.02)
    cbar.set_label("Time from start (h:m:s)", fontsize=9)
    tick_sec = np.linspace(0, recording_sec, 7)
    cbar.set_ticks(tick_sec)
    cbar.set_ticklabels([seconds_to_hms(s) for s in tick_sec], fontsize=7)

    ax.set_xlabel("MASS_X (px)")
    ax.set_ylabel("MASS_Y (px)")
    ax.set_title(
        f"Trajectory  —  Animal {animal_id}\n"
        f"Recording duration: {seconds_to_hms(recording_sec)}  "
        f"({len(df):,} detected frames)",
        fontsize=11,
    )
    ax.invert_yaxis()   # LMT origin is top-left
    fig.tight_layout()
    fig.savefig(traj_png, dpi=150)
    plt.close(fig)
    logging.info(f"  Saved: {traj_png}")

    # ── Stationary bouts ──────────────────────────────────────────────────────
    logging.info("Computing stationary bouts …")
    bouts     = compute_stationary_bouts(df)
    bouts_min = [b / 60.0 for b in bouts]
    logging.info(f"  {len(bouts):,} bouts found (≥ {MIN_STATIONARY_SECONDS} s).")

    suggested_min = suggest_threshold_minutes(bouts)

    # ── Gap analysis ──────────────────────────────────────────────────────────
    gap_df      = compute_gap_table(df)
    gap_csv     = os.path.join(output_folder, f"gaps_A{animal_id}_{date_str}.csv")
    gap_hist_png = os.path.join(output_folder, f"gap_hist_A{animal_id}_{date_str}.png")

    if not gap_df.empty:
        gap_df.to_csv(gap_csv, index=False)
        logging.info(f"  Saved gap table: {gap_csv}")

        gap_dur_min = gap_df["gap_duration_sec"].to_numpy() / 60.0
        g_arr       = gap_df["gap_duration_sec"].to_numpy()
        g_count     = len(g_arr)
        g_mean_s    = float(g_arr.mean())
        g_median_s  = float(pd.Series(g_arr).median())
        g_p25_s     = float(pd.Series(g_arr).quantile(0.25))
        g_p75_s     = float(pd.Series(g_arr).quantile(0.75))
        g_max_s     = float(g_arr.max())

        # Gap histogram
        fig3, ax3 = plt.subplots(figsize=(9, 5))
        max_g  = gap_dur_min.max()
        n_bins = min(60, max(10, g_count // 2))
        bins_g = np.linspace(0, max_g + 0.1, n_bins + 1)
        ax3.hist(gap_dur_min, bins=bins_g, color="#aa4477",
                 edgecolor="white", linewidth=0.5, alpha=0.85)
        ax3.set_xlabel("Gap duration (minutes)")
        ax3.set_ylabel("Count")
        ax3.set_title(
            f"Missing-gap duration distribution  —  Animal {animal_id}\n"
            f"({g_count:,} gaps detected)",
            fontsize=10,
        )
        fig3.tight_layout()
        fig3.savefig(gap_hist_png, dpi=150)
        plt.close(fig3)
        logging.info(f"  Saved: {gap_hist_png}")
    else:
        g_count = g_mean_s = g_median_s = g_p25_s = g_p75_s = g_max_s = 0.0
        gap_csv = gap_hist_png = None
        logging.info("  No gaps detected in DETECTION table.")

    # histogram
    fig2, ax2 = plt.subplots(figsize=(9, 5))
    if bouts_min:
        max_b = max(bouts_min)
        n_bins = min(60, max(10, len(bouts_min) // 2))
        bins   = np.linspace(0, max_b + 1, n_bins + 1)
        ax2.hist(bouts_min, bins=bins, color="#4477aa",
                 edgecolor="white", linewidth=0.5, alpha=0.85)
        ax2.axvline(suggested_min, color="red", linestyle="--", linewidth=1.5,
                    label=f"Suggested threshold: {suggested_min} min")
        ax2.legend(fontsize=9)
    else:
        ax2.text(0.5, 0.5, "No stationary bouts detected",
                 ha="center", va="center", transform=ax2.transAxes)

    ax2.set_xlabel("Stationary bout duration (minutes)")
    ax2.set_ylabel("Count")
    ax2.set_title(
        f"Stationary bout distribution  —  Animal {animal_id}\n"
        f"(displacement threshold: {STATIONARY_PIXEL_THRESH} px, "
        f"min bout: {MIN_STATIONARY_SECONDS} s)",
        fontsize=10,
    )
    fig2.tight_layout()
    fig2.savefig(hist_png, dpi=150)
    plt.close(fig2)
    logging.info(f"  Saved: {hist_png}")

    # ── Summary stats ─────────────────────────────────────────────────────────
    if bouts:
        b_arr    = np.array(bouts)
        mean_s   = float(b_arr.mean())
        median_s = float(np.median(b_arr))
        p25_s    = float(np.percentile(b_arr, 25))
        p75_s    = float(np.percentile(b_arr, 75))
        max_s    = float(b_arr.max())
    else:
        mean_s = median_s = p25_s = p75_s = max_s = 0.0

    def _m(s):   # seconds → "X.X min"
        return f"{s / 60:.1f} min"

    with open(report_txt, "w", encoding="utf-8") as f:
        f.write("LMT Trajectory & Stationary Duration Report\n")
        f.write(f"Generated : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Animal ID : {animal_id}\n")
        f.write(f"Source    : {input_db}\n\n")

        f.write("=" * 60 + "\n")
        f.write("RECORDING OVERVIEW\n")
        f.write("=" * 60 + "\n\n")
        f.write(f"  Detected frames  : {len(df):,}\n")
        f.write(f"  First frame      : {int(frames_arr[0]):,}\n")
        f.write(f"  Last frame       : {int(frames_arr[-1]):,}\n")
        f.write(f"  Recording length : {seconds_to_hms(recording_sec)}\n\n")

        f.write("=" * 60 + "\n")
        f.write("STATIONARY BOUT STATISTICS\n")
        f.write(f"  displacement threshold : {STATIONARY_PIXEL_THRESH} px\n")
        f.write(f"  minimum bout length    : {MIN_STATIONARY_SECONDS} s\n")
        f.write("=" * 60 + "\n\n")
        f.write(f"  Bouts found    : {len(bouts):,}\n")
        f.write(f"  Mean           : {_m(mean_s)}\n")
        f.write(f"  Median         : {_m(median_s)}\n")
        f.write(f"  25th pct       : {_m(p25_s)}\n")
        f.write(f"  75th pct       : {_m(p75_s)}\n")
        f.write(f"  Longest bout   : {_m(max_s)}\n\n")

        f.write("=" * 60 + "\n")
        f.write("SUGGESTED INTERVAL THRESHOLD\n")
        f.write("=" * 60 + "\n\n")
        f.write(f"  Suggested      : {suggested_min} min\n")
        f.write(f"  Method         : 75th-percentile of stationary bouts,\n")
        f.write(f"                   rounded up to nearest 5 min, clamped [5,60].\n\n")
        f.write("  Use this value as the Interval (minutes) field in\n")
        f.write("  1.lmt_interval_fill_review_v3.py\n\n")

        f.write("=" * 60 + "\n")
        f.write("MISSING-GAP STATISTICS\n")
        f.write("=" * 60 + "\n\n")
        if g_count > 0:
            f.write(f"  Gaps found     : {g_count:,}\n")
            f.write(f"  Mean           : {g_mean_s/60:.1f} min\n")
            f.write(f"  Median         : {g_median_s/60:.1f} min\n")
            f.write(f"  25th pct       : {g_p25_s/60:.1f} min\n")
            f.write(f"  75th pct       : {g_p75_s/60:.1f} min\n")
            f.write(f"  Longest gap    : {g_max_s/60:.1f} min\n")
            f.write(f"  Gap table CSV  : {gap_csv}\n\n")
        else:
            f.write("  No missing gaps detected.\n\n")

    logging.info(f"  Saved: {report_txt}")

    gap_output_lines = (
        f"\nGap CSV:                {gap_csv}" if gap_csv else ""
    ) + (
        f"\nGap histogram:          {gap_hist_png}" if gap_hist_png else ""
    )

    messagebox.showinfo(
        "Analysis Complete",
        f"Animal ID:              {animal_id}\n"
        f"Detected frames:        {len(df):,}\n"
        f"Recording length:       {seconds_to_hms(recording_sec)}\n\n"
        f"Stationary bouts:       {len(bouts):,}\n"
        f"Median bout duration:   {_m(median_s)}\n"
        f"Longest bout:           {_m(max_s)}\n\n"
        f"Missing gaps:           {g_count:,}\n"
        f"Median gap duration:    {g_median_s/60:.1f} min\n"
        f"Longest gap:            {g_max_s/60:.1f} min\n\n"
        f"Suggested threshold:    {suggested_min} min\n\n"
        f"Outputs:\n{traj_png}\n{hist_png}\n{report_txt}"
        + gap_output_lines,
    )


# ── GUI ───────────────────────────────────────────────────────────────────────
_input_db     = ""
_output_folder = ""


def _sel_db():
    global _input_db
    _input_db = filedialog.askopenfilename(
        filetypes=[("SQLite Database", "*.sqlite *.db")])
    lbl_db.config(text=_input_db or "No database selected")


def _sel_out():
    global _output_folder
    _output_folder = filedialog.askdirectory()
    lbl_out.config(text=_output_folder or "No folder selected")


def _start():
    try:
        if not _input_db:
            raise Exception("Please select an LMT SQLite database.")
        if not _output_folder:
            raise Exception("Please select an output folder.")
        run_analysis(
            input_db=_input_db,
            output_folder=_output_folder,
            animal_id=int(ent_animal.get()),
        )
    except Exception as exc:
        messagebox.showerror("Error", str(exc))


root = Tk()
root.title("LMT Trajectory Analyser  [V3 Script 0]")
root.geometry("680x300")

Label(root, text="LMT Trajectory & Stationary Duration Analyser",
      font=("Arial", 14, "bold")).pack(pady=10)
Label(root,
      text="Reads MASS_X / MASS_Y from DETECTION table.\n"
           "Outputs trajectory plot, bout histogram, and recommended interval threshold.",
      font=("Arial", 10), justify=CENTER).pack()

Button(root, text="Select LMT SQLite Database", command=_sel_db).pack(pady=5)
lbl_db = Label(root, text="No database selected", wraplength=640, fg="#555")
lbl_db.pack()

Label(root, text="Animal ID").pack(pady=(8, 0))
ent_animal = Entry(root, width=10)
ent_animal.insert(0, "1")
ent_animal.pack()

Button(root, text="Select Output Folder", command=_sel_out).pack(pady=5)
lbl_out = Label(root, text="No folder selected", wraplength=640, fg="#555")
lbl_out.pack()

Button(root, text="RUN ANALYSIS", command=_start,
       bg="green", fg="white", width=26, height=2).pack(pady=18)

root.mainloop()
