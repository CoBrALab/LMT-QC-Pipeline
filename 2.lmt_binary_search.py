import copy
import os
import re
import cv2

from lmt_common import (DB_FPS, FRAME_CONVERSION, EXPECTED_VIDEO_FPS, build_video_map, find_nearest_frame_candidates, _read_frame_from_video, _release_all_captures)

import sqlite3
import numpy as np
import pandas as pd
from datetime import datetime
import time
from tkinter import *
from tkinter import messagebox
from PIL import Image, ImageTk

MIN_GAP_DURATION_FOR_BINARY_SEARCH = 30  # seconds
FILL_ENTIRE_SEGMENT_IF_DURATION_LESS_THAN_IN_MINUTES = 1 # minutes

# Git Issue #19 (reopened for performance): for type-00 (OUT->OUT) gaps
# above MIN_GAP_DURATION_FOR_BINARY_SEARCH, sample left-to-right at this
# interval and fill each OUT checkpoint's answer backward to the previous
# checkpoint, exactly as before. As soon as a checkpoint comes back IN,
# sampling stops there: the remainder of the gap is handed off to the
# existing type-10 (in-nest -> out-of-nest) binary search instead of
# continuing to sample every interval to the end. Lower interval = more
# accuracy, more clicks (but only up to the first IN checkpoint now).
TYPE00_REVIEW_INTERVAL_SECONDS = 60  # seconds

GAP_TYPE_00 = "00"
GAP_TYPE_10 = "10"
GAP_TYPE_01 = "01"
GAP_TYPE_11 = "11"

# Helpers
def seconds_to_hms(seconds):
    hours   = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs    = int(seconds % 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"

def decimal_hours(seconds):
    return seconds / 3600.0

def _frames_to_seconds(n_frames):
    return n_frames / DB_FPS

def _gap_duration_seconds(gap_start_frame, gap_end_frame):
    return (gap_end_frame - gap_start_frame - 1) / DB_FPS

def _seg_dur_min(seg_start, seg_end):
    return (seg_end - seg_start + 1) / DB_FPS / 60

def _build_type00_checkpoint_tasks(gs, ge, b_left, b_right, gap_idx, total_gaps):
    """
    Git Issue #19: build the flat, left-to-right sequence of review checkpoint
    tasks for a single type-00 (OUT->OUT) gap. Sample every
    TYPE00_REVIEW_INTERVAL_SECONDS starting from the gap's left edge; when
    a checkpoint is answered OUT, the answer fills every frame from the end
    of the previous checkpoint (exclusive) through this checkpoint
    (inclusive) -- i.e. "fill in towards the left" -- and review continues
    to the next checkpoint in this list. The final checkpoint always lands
    exactly on gap_end, so the right edge is always explicitly reviewed
    rather than extrapolated from the last regular interval.

    Reopened (performance): the first checkpoint answered IN stops this
    sampling early -- _handle_answer discards any remaining checkpoint
    tasks for the gap from the task stack and instead queues a single
    type-10 (in-nest -> out-of-nest) binary-search subtask covering the
    rest of the gap, converging on the exact transition frame in
    O(log n) clicks instead of continuing to sample every interval to
    gap_end. This function itself is unchanged: it always builds the full
    would-be checkpoint sequence, since which checkpoint (if any) will come
    back IN is unknown until the reviewer answers.

    Precomputed and flat, not synthesized at answer time -- consistent
    with how type 01/10 tasks are structured, but with no subtask
    creation in _handle_answer for the OUT-checkpoint case. The IN-found
    case does create a subtask, using the same _make_subtask helper type
    01/10 already use.
    """
    gap_start = gs + 1
    gap_end   = ge - 1
    interval_frames = TYPE00_REVIEW_INTERVAL_SECONDS * DB_FPS

    checkpoints = []
    pos = gap_start + interval_frames - 1
    while pos < gap_end:
        checkpoints.append(pos)
        pos += interval_frames
    checkpoints.append(gap_end)

    tasks = []
    seg_start = gap_start
    for checkpoint_frame in checkpoints:
        tasks.append({
            "gap_index":      gap_idx,
            "total_gaps":     total_gaps,
            "gap_start":      gap_start,
            "gap_end":        gap_end,
            "seg_start":      seg_start,
            "seg_end":        checkpoint_frame,
            "show_frame":     checkpoint_frame,
            "boundary_left":  b_left,
            "boundary_right": b_right,
            "gap_type":       GAP_TYPE_00,
        })
        seg_start = checkpoint_frame + 1

    return tasks

# Video helpers
def extract_frame_to_path(video_map, global_frame, out_path):
    for resolved_frame, video_entry in find_nearest_frame_candidates(video_map, global_frame):
        if _read_frame_from_video(video_entry, resolved_frame, out_path):
            return resolved_frame
    return None

# Gap boundary classification
def classify_gap_type(gap_start_frame, gap_end_frame, in_nest_lookup):
    # Previously this defaulted missing/unexpected boundary lookups to 0
    # (out-of-nest), which could silently mask a data-integrity problem in
    # the source GAP_FILL_ANALYSIS table (e.g. a GAP_START_FRAME/GAP_END_FRAME
    # value that is not itself a detected FRAMENUMBER). Both boundary frames
    # are now required to be present; if not, an IntegrityError is raised so
    # the problem surfaces clearly instead of being silently absorbed into a
    # "type 00 / skip" bucket.
    if gap_start_frame not in in_nest_lookup or gap_end_frame not in in_nest_lookup:
        raise IntegrityError(
            f"Gap boundary frame missing from detected-frame lookup: "
            f"gap_start_frame={gap_start_frame}, gap_end_frame={gap_end_frame}. "
            f"This indicates the source GAP_FILL_ANALYSIS data is inconsistent "
            f"(a GAP_START_FRAME/GAP_END_FRAME value that is not itself a "
            f"detected FRAMENUMBER)."
        )

    before = in_nest_lookup[gap_start_frame]
    after  = in_nest_lookup[gap_end_frame]

    if before == 0 and after == 0:
        return GAP_TYPE_00   # out-of-nest → out-of-nest

    if before == 0 and after == 1:
        return GAP_TYPE_01   # out-of-nest → in-nest

    if before == 1 and after == 0:
        return GAP_TYPE_10   # in-nest → out-of-nest

    if before == 1 and after == 1:
        return GAP_TYPE_11   # in-nest → in-nest

    # Should never reach here given IN_NEST is always 0 or 1 for detected frames.
    # Previously silently returned GAP_TYPE_00; now raised explicitly so an
    # unexpected IN_NEST value on a detected frame isn't miscategorized and
    # silently skipped.
    raise IntegrityError(
        f"Unexpected IN_NEST value(s) for gap boundary frames "
        f"(gap_start_frame={gap_start_frame} -> {before}, "
        f"gap_end_frame={gap_end_frame} -> {after}); expected 0 or 1."
    )

# Binary-search task builder
def build_initial_tasks(df_negative, df_all):
    df_detected    = df_all[df_all["ASSUMPTION_TYPE"] == "DETECTED"].copy()
    in_nest_lookup = dict(zip(df_detected["FRAMENUMBER"], df_detected["IN_NEST"]))
    detected_frames_sorted = sorted(in_nest_lookup.keys())

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
    total_gaps           = len(groups)
    tasks                = []
    skipped_frames       = set()
    skipped_gap_keys     = set()   # threshold-skipped 01/10 gaps
    zero_zero_reviewed_gap_keys = set()   # type-00 gaps routed to checkpoint review
    zero_zero_skipped_gap_keys  = set()   # type-00 gaps below the duration threshold
    one_one_gap_keys = set()   # type-11 gaps
    gap_type_map         = {}

    for idx, row in enumerate(groups.itertuples(), start=1):
        gs = int(row.GAP_START_FRAME)
        ge = int(row.GAP_END_FRAME)

        b_left  = find_boundary_left(gs)
        b_right = find_boundary_right(ge)
        gtype   = classify_gap_type(gs, ge, in_nest_lookup)
        gap_type_map[(gs, ge)] = gtype

        # Type 00 (Git Issue #19): above the duration threshold, review via
        # left-to-right checkpoint sampling; below it, skip exactly as
        # before (remain IN_NEST = -1).
        if gtype == GAP_TYPE_00:
            gap_dur_sec = _gap_duration_seconds(gs, ge)
            if gap_dur_sec <= MIN_GAP_DURATION_FOR_BINARY_SEARCH:
                for f in range(gs + 1, ge):
                    skipped_frames.add(f)
                zero_zero_skipped_gap_keys.add((gs, ge))
                continue
            if gs + 1 > ge - 1:
                zero_zero_skipped_gap_keys.add((gs, ge))
                continue
            zero_zero_reviewed_gap_keys.add((gs, ge))
            tasks.extend(_build_type00_checkpoint_tasks(gs, ge, b_left, b_right, idx, total_gaps))
            continue

        # Type 11: animal was in-nest on both sides → skip (logic-filled)
        # 1.lmt_gap_fill.py should have already marked these frames IN_NEST=1,
        # but if any -1 frames appear here they belong to type-11 gaps and are also not binary-searched
        # (they will default to OUT=0 like other skipped frames, though in practice 1.lmt_gap_fill.py should prevent this).
        if gtype == GAP_TYPE_11:
            for f in range(gs + 1, ge):
                skipped_frames.add(f)
            one_one_gap_keys.add((gs, ge))
            continue

        gap_dur_sec = _gap_duration_seconds(gs, ge)
        if gap_dur_sec <= MIN_GAP_DURATION_FOR_BINARY_SEARCH:
            for f in range(gs + 1, ge):
                skipped_frames.add(f)
            skipped_gap_keys.add((gs, ge))
            continue

        gap_start = gs + 1
        gap_end   = ge - 1
        if gap_start > gap_end:
            continue
        mid = (gap_start + gap_end) // 2

        tasks.append({
            "gap_index":      idx,
            "total_gaps":     total_gaps,
            "gap_start":      gap_start,
            "gap_end":        gap_end,
            "show_frame":     mid,
            "boundary_left":  b_left,
            "boundary_right": b_right,
            "gap_type":       gtype,
        })

    return (tasks, skipped_frames, skipped_gap_keys, gap_type_map, zero_zero_reviewed_gap_keys, zero_zero_skipped_gap_keys, one_one_gap_keys)

# Integrity checks
class IntegrityError(Exception):
    """Raised when a frame-count integrity check fails."""

def _check(label, expected, actual):
    """Raise IntegrityError with a clear message if expected != actual."""
    if expected != actual:
        raise IntegrityError(f"INTEGRITY CHECK FAILED: {label}\n  Expected : {expected:,}\n  Actual   : {actual:,}\n  Delta    : {actual - expected:+,}")

# Build a complete gap_type_map covering ALL assumed gaps
# (not just those with -1 frames, which is what build_initial_tasks covers).
def build_full_gap_type_map(df_all):
    """
    Classify every gap in df_all (both logic-filled and -1 assumed frames)
    so that the summary report never encounters a gap missing from gap_type_map.

    Returns a dict: (gap_start_frame, gap_end_frame) -> gap_type_str
    """
    df_detected    = df_all[df_all["ASSUMPTION_TYPE"] == "DETECTED"].copy()
    in_nest_lookup = dict(zip(df_detected["FRAMENUMBER"], df_detected["IN_NEST"]))

    df_asm = df_all[df_all["ASSUMPTION_TYPE"] == "ASSUMED"].copy()
    full_map = {}
    for gs, ge in df_asm[["GAP_START_FRAME", "GAP_END_FRAME"]].drop_duplicates().itertuples(index=False):
        key = (int(gs), int(ge))
        full_map[key] = classify_gap_type(int(gs), int(ge), in_nest_lookup)
    return full_map

# Consolidated summary report
def write_summary_report(report_path, source_db_path,
                         video_paths, output_folder,  # CLI inputs (Git Issue #21): logged below for traceability
                         df_all, df_out_assumed,
                         final_clf,            # dict: frame -> final IN_NEST value
                         searchable_frames,    # set: frames routed to binary-search reviewer
                         skipped_gap_keys,     # set: (gs,ge) threshold-skipped 01/10 gaps
                         zero_zero_reviewed_gap_keys,  # set: (gs,ge) type-00 gaps, checkpoint-reviewed
                         zero_zero_skipped_gap_keys,   # set: (gs,ge) type-00 gaps, below threshold
                         one_one_gap_keys, # set: (gs,ge) type-11 gaps
                         gap_type_map,         # partial map from build_initial_tasks
                         total_review_seconds,
                         gap_timings):
    """
    Build the pipeline summary report.

    Gap boundary type (00/01/10/11) and processing method (logic-fill /
    binary-search / threshold-skip / unknown) are reported in separate sections.

    All statistics are derived from `final_clf` (the authoritative per-frame
    classification dict).  Every assumed frame is accounted for exactly once.

    Raises IntegrityError (via _check) if any frame-count identity fails.
    """

    def pct(n, total):
        return (n / total * 100) if total > 0 else 0.0

    def fmt_triple(n_frames):
        secs = _frames_to_seconds(n_frames)
        dec  = decimal_hours(secs)
        return f"{n_frames:,}", seconds_to_hms(secs), f"{dec:.3f}"

    # 0.  BUILD COMPLETE GAP TYPE MAP
    full_gap_type_map = build_full_gap_type_map(df_all)
    # build_initial_tasks classifications take precedence (same values anyway,but kept explicit for auditability).
    merged_gap_type_map = {**full_gap_type_map, **gap_type_map}

    # 1.  RAW COUNTS FROM SOURCE-OF-TRUTH STRUCTURES
    df_det    = df_all[df_all["ASSUMPTION_TYPE"] == "DETECTED"]
    det_total = len(df_det)
    det_in    = int((df_det["IN_NEST"] == 1).sum())
    det_out   = det_total - det_in

    df_asm_orig = df_all[df_all["ASSUMPTION_TYPE"] == "ASSUMED"]
    asm_total   = len(df_asm_orig)

    logic_filled   = int((df_asm_orig["IN_NEST"] == 1).sum())
    bs_input_total = int((df_asm_orig["IN_NEST"] == -1).sum())

    # 2.  INTEGRITY CHECK: assumed frame decomposition
    _check("logic_filled + bs_input = asm_total", asm_total, logic_filled + bs_input_total)

    total_expected = det_total + asm_total
    _check("det_total + asm_total = total_expected", total_expected, det_total + asm_total)

    # 3.  FINAL CLASSIFICATIONS – derived entirely from final_clf
    bs_in_frames  = sum(1 for fn in searchable_frames if final_clf.get(fn) == 1)
    bs_out_frames = sum(1 for fn in searchable_frames if final_clf.get(fn) == 0)
    bs_unknown    = sum(1 for fn in searchable_frames if final_clf.get(fn) == -1)

    threshold_skipped = sum(len(range(gs + 1, ge)) for gs, ge in skipped_gap_keys)

    zz_skipped = sum(len(range(gs + 1, ge)) for gs, ge in zero_zero_skipped_gap_keys)
    zz_reviewed = sum(len(range(gs + 1, ge)) for gs, ge in zero_zero_reviewed_gap_keys)

    ze_skipped = sum(len(range(gs + 1, ge)) for gs, ge in one_one_gap_keys)

    # 4.  INTEGRITY CHECK: binary-search input balanced
    bs_accounted = (bs_in_frames + bs_out_frames + bs_unknown + threshold_skipped + zz_skipped + ze_skipped)
    _check("binary_search_in = bs_in + bs_out + bs_unknown + threshold_skipped + zz_skipped + ze_skipped", bs_input_total, bs_accounted)

    # 5.  GLOBAL FINAL COUNTS
    final_in  = det_in + logic_filled + bs_in_frames
    final_out = det_out + bs_out_frames
    final_unk = threshold_skipped + zz_skipped + ze_skipped + bs_unknown

    _check("final_in + final_out + final_unk = total_expected", total_expected, final_in + final_out + final_unk)

    # 6.  GAP STATISTICS
    gap_groups = (
        df_asm_orig
        .groupby(["GAP_START_FRAME", "GAP_END_FRAME"])
        .size()
        .reset_index(name="frame_count")
        .sort_values("GAP_START_FRAME")
        .reset_index(drop=True)
    )
    total_gaps = len(gap_groups)

    # Gap type frame counts (boundary-state classification, 4 categories)
    type_frames = {GAP_TYPE_00: 0, GAP_TYPE_01: 0, GAP_TYPE_10: 0, GAP_TYPE_11: 0}

    for row in gap_groups.itertuples():
        key = (int(row.GAP_START_FRAME), int(row.GAP_END_FRAME))
        # Integrity: every gap must have a known type in the merged map
        if key not in merged_gap_type_map:
            raise IntegrityError(f"Gap {key} exists in gap_groups but is missing from gap_type_map.\nThis indicates a gap whose boundary frames are not in the detected set.")
        gtype = merged_gap_type_map[key]
        if gtype not in type_frames:
            raise IntegrityError(f"Gap {key} has unrecognised gap type '{gtype}'.")
        type_frames[gtype] += int(row.frame_count)

    total_gap_frames = int(gap_groups["frame_count"].sum()) if total_gaps > 0 else 0
    avg_gap_sec      = _frames_to_seconds(total_gap_frames / total_gaps) if total_gaps > 0 else 0.0

    # INTEGRITY CHECK: four gap types must account for all assumed frames
    type_frames_total = sum(type_frames.values())
    _check("sum of all gap-type frame counts = asm_total", asm_total, type_frames_total)

    # 7.  GAP-LEVEL LABEL (counts from final_clf)
    def gap_label_and_counts(gs, ge, n_frames):
        key   = (gs, ge)
        gtype = merged_gap_type_map[key]   # guaranteed present (checked above)

        if key in zero_zero_skipped_gap_keys:
            return (f"Skipped \u2014 type 00, \u2264 {MIN_GAP_DURATION_FOR_BINARY_SEARCH}s (OUT\u2192OUT)", 0, 0, n_frames)

        if key in one_one_gap_keys:
            return (f"Skipped \u2014 type 11 (IN\u2192IN, auto-filled by 1.lmt_gap_fill.py)", 0, 0, n_frames)

        gap_frames_orig = df_asm_orig[(df_asm_orig["GAP_START_FRAME"] == gs) & (df_asm_orig["GAP_END_FRAME"]   == ge)]
        original_values = set(gap_frames_orig["IN_NEST"].unique())

        if -1 not in original_values:
            # Logic-filled by 1.lmt_gap_fill.py (IN_NEST=1 for all frames in this gap)
            return (f"Auto IN NEST = 1 (type {gtype}, logic-filled by 1.lmt_gap_fill.py)", n_frames, 0, 0)

        if key in skipped_gap_keys:
            return (f"Skipped \u2264 {MIN_GAP_DURATION_FOR_BINARY_SEARCH}s threshold (type {gtype})", 0, 0, n_frames)

        # Binary-searched gap: count from final_clf
        frames_in_gap = list(range(gs + 1, ge))
        in_cnt  = sum(1 for f in frames_in_gap if final_clf.get(f) == 1)
        out_cnt = sum(1 for f in frames_in_gap if final_clf.get(f) == 0)
        unk_cnt = sum(1 for f in frames_in_gap if final_clf.get(f) == -1)

        if in_cnt + out_cnt + unk_cnt != n_frames:
            raise IntegrityError(f"Gap ({gs}, {ge}) frame count mismatch: {in_cnt}+{out_cnt}+{unk_cnt} != {n_frames}")

        if in_cnt > 0 and out_cnt == 0 and unk_cnt == 0:
            label = f"Binary Search \u2192 IN NEST = 1 (type {gtype})"
        elif out_cnt > 0 and in_cnt == 0 and unk_cnt == 0:
            label = f"Binary Search \u2192 OUT = 0 (type {gtype})"
        else:
            label = (f"Binary Search \u2192 mixed {in_cnt:,} IN / {out_cnt:,} OUT"
                     + (f" / {unk_cnt:,} UNK" if unk_cnt else "")
                     + f" (type {gtype})")

        return label, in_cnt, out_cnt, unk_cnt

    # 8.  WRITE REPORT
    with open(report_path, "w", encoding="utf-8") as f:

        f.write("LMT Pipeline Summary Report\n")
        f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Source database (-i/--input): {source_db_path}\n")
        f.write(f"Video file(s) (-v/--videos): {', '.join(video_paths)}\n")
        f.write(f"Output folder (-o/--output-folder): {output_folder}\n")
        f.write("\n")

        # Section 0: Timing
        f.write("=" * 70 + "\n")
        f.write("BINARY SEARCH REVIEW TIMING\n")
        f.write("=" * 70 + "\n\n")
        f.write(f"  Total review duration:   {seconds_to_hms(total_review_seconds)}  ({decimal_hours(total_review_seconds):.3f} h)\n\n")

        # Section 1: Detection Summary
        f.write("=" * 70 + "\n")
        f.write("LMT DETECTION SUMMARY\n")
        f.write("=" * 70 + "\n\n")

        fr, hms, dec = fmt_triple(det_total)
        f.write(f"Total detected frames:   {fr}  ({hms})  [{dec} h]\n\n")

        fr, hms, dec = fmt_triple(det_in)
        f.write(f"  IN NEST (IN_NEST = 1)\n")
        f.write(f"  Frames: {fr}  ({hms})  [{dec} h]\n")
        f.write(f"  % of detected: {pct(det_in, det_total):.1f}%\n\n")

        fr, hms, dec = fmt_triple(det_out)
        f.write(f"  OUT OF NEST (IN_NEST = 0)\n")
        f.write(f"  Frames: {fr}  ({hms})  [{dec} h]\n")
        f.write(f"  % of detected: {pct(det_out, det_total):.1f}%\n\n")

        # Section 2: Assumed Frames
        f.write("=" * 70 + "\n")
        f.write("MISSING / ASSUMED FRAMES SUMMARY\n")
        f.write("=" * 70 + "\n\n")

        fr, hms, dec = fmt_triple(asm_total)
        f.write(f"Total assumed frames:    {fr}  ({hms})  [{dec} h]\n\n")

        fr, hms, dec = fmt_triple(logic_filled)
        f.write(f"  Logic-filled IN NEST = 1 (1.lmt_gap_fill.py auto-classification)\n")
        f.write(f"  Frames: {fr}  ({hms})  [{dec} h]\n")
        f.write(f"  % of assumed: {pct(logic_filled, asm_total):.1f}%\n\n")

        fr, hms, dec = fmt_triple(bs_input_total)
        f.write(f"  Sent to binary search (IN_NEST = -1 in 1.lmt_gap_fill.py)\n")
        f.write(f"  Frames: {fr}  ({hms})  [{dec} h]\n")
        f.write(f"  % of assumed: {pct(bs_input_total, asm_total):.1f}%\n\n")

        #  Section 3: Gap Type Breakdown (boundary states only)
        f.write("=" * 70 + "\n")
        f.write("GAP TYPE BREAKDOWN  (boundary state classification)\n")
        f.write("=" * 70 + "\n\n")
        f.write("  Gap type is determined by the IN_NEST state of the last\n")
        f.write("  detected frame BEFORE the gap and the first detected frame\n")
        f.write("  AFTER the gap.  This is a boundary property only — it does\n")
        f.write("  not describe how the frames were processed (see Processing\n")
        f.write("  Breakdown below).\n\n")

        for gtype, label in [(GAP_TYPE_00, "00  (out-of-nest \u2192 out-of-nest)"), (GAP_TYPE_01, "01  (out-of-nest \u2192 in-nest)"), (GAP_TYPE_10, "10  (in-nest \u2192 out-of-nest)"), (GAP_TYPE_11, "11  (in-nest \u2192 in-nest)"),]:
            n = type_frames[gtype]
            fr, hms, dec = fmt_triple(n)
            f.write(f"  {label}\n")
            f.write(f"    Frames:        {fr}\n")
            f.write(f"    Duration:      {hms}\n")
            f.write(f"    Decimal hours: {dec}\n\n")

        f.write(f"  Balance check: {type_frames[GAP_TYPE_00]:,} + {type_frames[GAP_TYPE_01]:,} + {type_frames[GAP_TYPE_10]:,} + {type_frames[GAP_TYPE_11]:,} = {type_frames_total:,}  [assumed total: {asm_total:,}]  {'OK' if type_frames_total == asm_total else 'MISMATCH'}\n\n")

        #  Section 4: Processing Breakdown (method, not boundary state)
        f.write("=" * 70 + "\n")
        f.write("PROCESSING BREAKDOWN  (how assumed frames were handled)\n")
        f.write("=" * 70 + "\n\n")
        f.write("  This section describes the processing method applied to each\n")
        f.write("  assumed frame, independent of gap boundary type.\n\n")

        fr, hms, dec = fmt_triple(logic_filled)
        f.write(f"  Logic-filled IN NEST = 1  (1.lmt_gap_fill.py auto-classification)\n")
        f.write(f"    Frames:        {fr}  ({hms})  [{dec} h]\n")
        f.write(f"    % of assumed:  {pct(logic_filled, asm_total):.1f}%\n\n")

        fr, hms, dec = fmt_triple(len(searchable_frames))
        f.write(f"  Binary-search reviewed  (01/10/00 gaps above {MIN_GAP_DURATION_FOR_BINARY_SEARCH}s threshold)\n")
        f.write(f"    Frames:        {fr}  ({hms})  [{dec} h]\n")
        f.write(f"    % of assumed:  {pct(len(searchable_frames), asm_total):.1f}%\n")

        fr, hms, dec = fmt_triple(bs_in_frames)
        f.write(f"      → Reclassified IN NEST = 1:   {fr}  ({hms})\n")
        fr, hms, dec = fmt_triple(bs_out_frames)
        f.write(f"      → Reclassified OUT = 0:       {fr}  ({hms})\n")
        if bs_unknown > 0:
            fr, hms, dec = fmt_triple(bs_unknown)
            f.write(f"      → Residual unknown (-1):      {fr}  (unexpected)\n")
        f.write("\n")

        fr, hms, dec = fmt_triple(threshold_skipped)
        f.write(f"  Threshold-skipped  (\u2264 {MIN_GAP_DURATION_FOR_BINARY_SEARCH}s, types 01/10, remain IN_NEST = -1)\n")
        f.write(f"    Frames:        {fr}  ({hms})  [{dec} h]\n")
        f.write(f"    % of assumed:  {pct(threshold_skipped, asm_total):.1f}%\n\n")

        fr, hms, dec = fmt_triple(zz_reviewed)
        f.write(f"  Type-00 reviewed  (OUT\u2192OUT, checkpoint sampled every {TYPE00_REVIEW_INTERVAL_SECONDS}s)\n")
        f.write(f"    Frames:        {fr}  ({hms})  [{dec} h]\n")
        f.write(f"    % of assumed:  {pct(zz_reviewed, asm_total):.1f}%\n\n")

        fr, hms, dec = fmt_triple(zz_skipped)
        f.write(f"  Type-00 skipped  (\u2264 {MIN_GAP_DURATION_FOR_BINARY_SEARCH}s threshold, remain IN_NEST = -1)\n")
        f.write(f"    Frames:        {fr}  ({hms})  [{dec} h]\n")
        f.write(f"    % of assumed:  {pct(zz_skipped, asm_total):.1f}%\n\n")

        fr, hms, dec = fmt_triple(ze_skipped)
        f.write(f"  Type-11 skipped  (IN\u2192IN frames with IN_NEST=-1, remain IN_NEST = -1)\n")
        f.write(f"    Note: 1.lmt_gap_fill.py should logic-fill these; non-zero count is unexpected.\n")
        f.write(f"    Frames:        {fr}  ({hms})  [{dec} h]\n")
        f.write(f"    % of assumed:  {pct(ze_skipped, asm_total):.1f}%\n\n")

        # Section 5: Binary Search Results
        f.write("=" * 70 + "\n")
        f.write("BINARY SEARCH RESULTS\n")
        f.write("=" * 70 + "\n\n")
        f.write(f"  Input: {bs_input_total:,} frames initially IN_NEST = -1\n\n")

        fr, hms, dec = fmt_triple(len(searchable_frames))
        f.write(f"  Frames routed to reviewer (01/10 gaps above threshold)\n")
        f.write(f"    Frames:        {fr}  ({hms})  [{dec} h]\n\n")

        fr, hms, dec = fmt_triple(bs_in_frames)
        f.write(f"  Reclassified to IN NEST = 1\n")
        f.write(f"    Frames:        {fr}  ({hms})  [{dec} h]\n")
        f.write(f"    % of bs input: {pct(bs_in_frames, bs_input_total):.1f}%\n\n")

        fr, hms, dec = fmt_triple(bs_out_frames)
        f.write(f"  Reclassified to OUT OF NEST = 0\n")
        f.write(f"    Frames:        {fr}  ({hms})  [{dec} h]\n")
        f.write(f"    % of bs input: {pct(bs_out_frames, bs_input_total):.1f}%\n\n")

        if bs_unknown > 0:
            fr, hms, dec = fmt_triple(bs_unknown)
            f.write(f"  Remaining IN_NEST = -1 (binary-search residual, unexpected)\n")
            f.write(f"    Frames:        {fr}  ({hms})  [{dec} h]\n")
            f.write(f"    % of bs input: {pct(bs_unknown, bs_input_total):.1f}%\n\n")

        fr, hms, dec = fmt_triple(threshold_skipped)
        f.write(f"  Remaining IN_NEST = -1 (\u2264 {MIN_GAP_DURATION_FOR_BINARY_SEARCH}s threshold, types 01/10, not searched)\n")
        f.write(f"    Frames:        {fr}  ({hms})  [{dec} h]\n")
        f.write(f"    % of bs input: {pct(threshold_skipped, bs_input_total):.1f}%\n\n")

        fr, hms, dec = fmt_triple(zz_skipped)
        f.write(f"  Remaining IN_NEST = -1 (type-00 gaps, \u2264 {MIN_GAP_DURATION_FOR_BINARY_SEARCH}s threshold)\n")
        f.write(f"    Frames:        {fr}  ({hms})  [{dec} h]\n")
        f.write(f"    % of bs input: {pct(zz_skipped, bs_input_total):.1f}%\n\n")

        if ze_skipped > 0:
            fr, hms, dec = fmt_triple(ze_skipped)
            f.write(f"  Remaining IN_NEST = -1 (type-11 gaps, IN\u2192IN, unexpected in -1 pool)\n")
            f.write(f"    Frames:        {fr}  ({hms})  [{dec} h]\n")
            f.write(f"    % of bs input: {pct(ze_skipped, bs_input_total):.1f}%\n\n")

        bs_check = (bs_in_frames + bs_out_frames + bs_unknown + threshold_skipped + zz_skipped + ze_skipped)
        f.write(f"  Balance check: {bs_in_frames:,} IN + {bs_out_frames:,} OUT + {bs_unknown:,} UNK + {threshold_skipped:,} thresh + {zz_skipped:,} type-00 + {ze_skipped:,} type-11 = {bs_check:,}  [input: {bs_input_total:,}]  {'OK' if bs_check == bs_input_total else 'MISMATCH'}\n\n")

        # Section 6: Classification Audit Table
        f.write("=" * 70 + "\n")
        f.write("CLASSIFICATION AUDIT TABLE\n")
        f.write("=" * 70 + "\n\n")
        f.write("  Every frame in the pipeline is assigned to exactly one cell.\n\n")

        col = 38
        f.write(f"  {'Category':<{col}} {'Frames':>12}  {'Duration':>10}  {'Dec hrs':>8}\n")
        f.write("  " + "-" * (col + 36) + "\n")

        rows_audit = [("Detected IN NEST", det_in), ("Detected OUT OF NEST", det_out), ("Logic-filled IN NEST", logic_filled), ("Binary-search IN NEST", bs_in_frames), ("Binary-search OUT OF NEST", bs_out_frames), ("Remaining UNKNOWN (IN_NEST=-1)", final_unk)]
        running = 0
        for cat, n in rows_audit:
            secs = _frames_to_seconds(n)
            f.write(f"  {cat:<{col}} {n:>12,}  {seconds_to_hms(secs):>10}  "
                    f"{decimal_hours(secs):>8.3f}\n")
            running += n

        f.write("  " + "-" * (col + 36) + "\n")
        secs = _frames_to_seconds(running)
        f.write(f"  {'GRAND TOTAL':<{col}} {running:>12,}  {seconds_to_hms(secs):>10}  {decimal_hours(secs):>8.3f}\n")
        f.write(f"\n  Expected total: {total_expected:,}   {'MATCH' if running == total_expected else 'MISMATCH — SEE LOG'}\n\n")

        # Section 7: Time in Nest
        f.write("=" * 70 + "\n")
        f.write("TIME IN NEST SUMMARY\n")
        f.write("=" * 70 + "\n\n")
        f.write("  Source: Detected IN + Logic-filled IN + Binary-search IN\n\n")

        fr, hms, dec = fmt_triple(final_in)
        f.write(f"  Total time in nest\n")
        f.write(f"    Frames:        {fr}  ({hms})  [{dec} h]\n\n")

        fr, hms, dec = fmt_triple(det_in)
        f.write(f"  From LMT-detected frames\n")
        f.write(f"    Frames:        {fr}  ({hms})  [{dec} h]\n\n")

        fr, hms, dec = fmt_triple(logic_filled)
        f.write(f"  From logic-filled (1.lmt_gap_fill.py auto-assumed IN)\n")
        f.write(f"    Frames:        {fr}  ({hms})  [{dec} h]\n\n")

        fr, hms, dec = fmt_triple(bs_in_frames)
        f.write(f"  From binary-search resolution\n")
        f.write(f"    Frames:        {fr}  ({hms})  [{dec} h]\n\n")

        # Section 8: Gap Statistics
        f.write("=" * 70 + "\n")
        f.write("GAP STATISTICS\n")
        f.write("=" * 70 + "\n\n")
        f.write(f"  Total number of gaps:  {total_gaps:,}\n")
        f.write(f"  Average gap duration:  {seconds_to_hms(avg_gap_sec)}\n\n")

        # Section 9: Gap Details Table
        f.write("=" * 70 + "\n")
        f.write("GAP DETAILS\n")
        f.write("=" * 70 + "\n\n")
        f.write("  For binary-searched gaps, IN + OUT + UNK must equal the\n")
        f.write("  total frames in the gap (verified per-row below).\n\n")

        col_w = [6, 5, 12, 12, 10, 10, 10, 10, 12, 14, 52]
        hdr = (f"{'Gap #':<{col_w[0]}}{'Type':<{col_w[1]}}{'Gap Start':<{col_w[2]}}{'Gap End':<{col_w[3]}}{'Frames':<{col_w[4]}}{'IN':<{col_w[5]}}{'OUT':<{col_w[6]}}{'UNK':<{col_w[7]}}{'Duration':<{col_w[8]}}{'Review Time':<{col_w[9]}}{'Assumption':<{col_w[10]}}\n")
        f.write(hdr)
        f.write("-" * sum(col_w) + "\n")

        gap_in_total  = 0
        gap_out_total = 0
        gap_unk_total = 0

        for i, row in enumerate(gap_groups.itertuples(), start=1):
            gs       = int(row.GAP_START_FRAME)
            ge       = int(row.GAP_END_FRAME)
            n_frames = int(row.frame_count)
            dur_sec  = _frames_to_seconds(n_frames)
            key      = (gs, ge)

            # Guaranteed present — checked during type_frames accumulation above
            gtype = merged_gap_type_map[key]

            label, in_cnt, out_cnt, unk_cnt = gap_label_and_counts(gs, ge, n_frames)

            gap_in_total  += in_cnt
            gap_out_total += out_cnt
            gap_unk_total += unk_cnt

            review_time_str = "N/A"
            for gt in gap_timings:
                if gt["gap_start"] == gs + 1 and gt["gap_end"] == ge - 1:
                    review_time_str = seconds_to_hms(gt["duration_seconds"])
                    break

            f.write(f"{i:<{col_w[0]}}{gtype:<{col_w[1]}}{gs:<{col_w[2]}}{ge:<{col_w[3]}}{n_frames:<{col_w[4]}}{in_cnt:<{col_w[5]}}{out_cnt:<{col_w[6]}}{unk_cnt:<{col_w[7]}}{seconds_to_hms(dur_sec):<{col_w[8]}}{review_time_str:<{col_w[9]}}{label:<{col_w[10]}}\n")

        f.write("-" * sum(col_w) + "\n")
        f.write(f"{'TOTALS':<{col_w[0]}}{'':<{col_w[1]}}{'':<{col_w[2]}}{'':<{col_w[3]}}{total_gap_frames:<{col_w[4]}}{gap_in_total:<{col_w[5]}}{gap_out_total:<{col_w[6]}}{gap_unk_total:<{col_w[7]}}{'':<{col_w[8]}}{'':<{col_w[9]}}\n")
        gap_sum = gap_in_total + gap_out_total + gap_unk_total
        f.write(f"\n  Gap table balance: {gap_in_total:,} IN + {gap_out_total:,} OUT + {gap_unk_total:,} UNK = {gap_sum:,}  [expected: {total_gap_frames:,}]  {'OK' if gap_sum == total_gap_frames else 'MISMATCH'}\n\n")

# Main GUI class
class BinarySearchGUI:

    def __init__(self, root, db_path, video_paths, output_folder):
        self.root = root
        self.root.title("LMT Binary Search Gap Filler")
        self.root.geometry("1600x950")

        self.db_path           = db_path
        self.output_folder     = output_folder
        self.video_paths       = list(video_paths)
        self.video_map         = []

        self.df                = None
        self.df_all            = None
        self.df_neg            = None
        self.temp_dir          = ""

        self.task_stack          = []
        self.redo_stack          = []
        self.history             = []
        self.current_task        = None
        self.decisions           = {}
        self._advance_token      = 0
        self.explicitly_skipped_frames = set()
        self.skipped_frames      = set()
        self.skipped_gap_keys    = set()
        self.zero_zero_reviewed_gap_keys = set()
        self.zero_zero_skipped_gap_keys  = set()
        self.one_one_gap_keys = set()
        self.gap_type_map        = {}

        self._review_start_time  = None
        self._gap_start_time     = None
        self._current_gap_index  = None
        self._gap_timings        = []

        self._photo_left   = None
        self._photo_center = None
        self._photo_right  = None

        # Ensure cached video captures and the temp frame-cache directory are
        # cleaned up even if the user closes the window mid-review instead
        # of reaching _finish() normally (previously this cleanup only ever
        # happened inside _finish()).
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # db_path/video_paths/output_folder are now supplied via CLI
        # arguments (parsed and validated in __main__) instead of being
        # collected through the GUI file-picker setup screen that used to
        # live here.
        self._start()

    def _start(self):
        conn = sqlite3.connect(self.db_path)
        self.df_all = pd.read_sql_query("SELECT * FROM GAP_FILL_ANALYSIS ORDER BY FRAMENUMBER", conn)
        conn.close()

        self.df = self.df_all[self.df_all["ASSUMPTION_TYPE"] == "ASSUMED"].copy().reset_index(drop=True)

        self.df_neg = self.df[self.df["IN_NEST"] == -1].copy()
        if len(self.df_neg) == 0:
            messagebox.showinfo("Nothing to do","No frames with IN_NEST = -1 found.")
            return

        self.video_map, skipped_videos, fps_mismatches = build_video_map(self.video_paths)
        if not self.video_map:
            messagebox.showerror("Error", "Could not parse any valid videos."); return

        video_warnings = []
        if skipped_videos:
            video_warnings.append(
                "The following video file(s) could not be parsed for a "
                "starting frame number (expected a 't<digits>' segment "
                "immediately before the file extension) and were excluded "
                "from this run:\n\n" + "\n".join(skipped_videos)
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

        self.temp_dir = os.path.join(self.output_folder, "_binsearch_tmp")
        os.makedirs(self.temp_dir, exist_ok=True)

        try:
            (tasks, self.skipped_frames, self.skipped_gap_keys, self.gap_type_map, self.zero_zero_reviewed_gap_keys, self.zero_zero_skipped_gap_keys, self.one_one_gap_keys) = build_initial_tasks(self.df_neg, self.df_all)
        except IntegrityError as e:
            messagebox.showerror(
                "Data Integrity Error",
                f"Could not classify gaps due to inconsistent source data.\n\n{e}"
            )
            return

        self.task_stack    = tasks
        self.redo_stack    = []
        self.history       = []
        self.decisions     = {}
        self.current_task  = None
        self._advance_token = 0
        self.explicitly_skipped_frames = set()
        self._gap_timings  = []

        if not self.task_stack:
            messagebox.showinfo("Nothing to search", f"All IN_NEST = -1 gaps are either type-11, or below the duration threshold (\u2264 {MIN_GAP_DURATION_FOR_BINARY_SEARCH}s).\nProceeding directly to output.")
            self._finish()
            return

        self._review_start_time = time.time()
        self._build_qc_ui()
        self._load_next_task()

    #  QC screen
    def _build_qc_ui(self):
        self.qc_frame = Frame(self.root)
        self.qc_frame.pack(fill=BOTH, expand=True)

        images_frame = Frame(self.qc_frame, bg="#1a1a1a")
        images_frame.pack(side=TOP, fill=X, padx=5, pady=5)

        left_panel = Frame(images_frame, bg="#1a1a1a")
        left_panel.pack(side=LEFT, expand=True, fill=BOTH, padx=3)
        Label(left_panel, text="LAST DETECTED BEFORE GAP", font=("Arial", 9, "bold"), fg="#aaaaaa", bg="#1a1a1a").pack()
        self.lbl_left_frame_num = Label(left_panel, text="Frame —", font=("Arial", 8), fg="#888888", bg="#1a1a1a")
        self.lbl_left_frame_num.pack()
        self.img_left = Label(left_panel, bg="#1a1a1a")
        self.img_left.pack(pady=2)

        center_panel = Frame(images_frame, bg="#0d2a0d", bd=2, relief=GROOVE)
        center_panel.pack(side=LEFT, expand=True, fill=BOTH, padx=3)
        Label(center_panel, text="▶  FRAME UNDER REVIEW  ◀", font=("Arial", 9, "bold"), fg="#55ff55", bg="#0d2a0d").pack()
        self.lbl_center_frame_num = Label(center_panel, text="Frame —", font=("Arial", 8), fg="#88cc88", bg="#0d2a0d")
        self.lbl_center_frame_num.pack()
        self.img_center = Label(center_panel, bg="#0d2a0d")
        self.img_center.pack(pady=2)

        right_panel = Frame(images_frame, bg="#1a1a1a")
        right_panel.pack(side=LEFT, expand=True, fill=BOTH, padx=3)
        Label(right_panel, text="FIRST DETECTED AFTER GAP", font=("Arial", 9, "bold"), fg="#aaaaaa", bg="#1a1a1a").pack()
        self.lbl_right_frame_num = Label(right_panel, text="Frame —", font=("Arial", 8), fg="#888888", bg="#1a1a1a")
        self.lbl_right_frame_num.pack()
        self.img_right = Label(right_panel, bg="#1a1a1a")
        self.img_right.pack(pady=2)

        bottom = Frame(self.qc_frame)
        bottom.pack(side=BOTTOM, fill=X, padx=20, pady=8)

        info_left = Frame(bottom)
        info_left.pack(side=LEFT, anchor=W)

        self.lbl_gap_counter = Label(info_left, text="", font=("Arial", 14, "bold"))
        self.lbl_gap_counter.pack(anchor=W)
        self.lbl_gap_type    = Label(info_left, text="", font=("Arial", 11), fg="#335588")
        self.lbl_gap_type.pack(anchor=W)
        self.lbl_tasks_left  = Label(info_left, text="", font=("Arial", 10), fg="#555555")
        self.lbl_tasks_left.pack(anchor=W)
        self.lbl_gap         = Label(info_left, text="", font=("Arial", 10))
        self.lbl_gap.pack(anchor=W)
        self.lbl_segment     = Label(info_left, text="", font=("Arial", 10))
        self.lbl_segment.pack(anchor=W)
        self.lbl_answer      = Label(info_left, text="", font=("Arial", 12, "bold"))
        self.lbl_answer.pack(anchor=W, pady=(6, 0))

        btn_frame = Frame(bottom)
        btn_frame.pack(side=RIGHT, anchor=E)

        Button(btn_frame, text="IN NEST  (A)", bg="green", fg="white", width=20, height=2, command=lambda: self._handle_answer(True)).grid(row=0, column=0, padx=6, pady=4)
        Button(btn_frame, text="OUT OF NEST  (D)", bg="red", fg="white", width=20, height=2, command=lambda: self._handle_answer(False)).grid(row=0, column=1, padx=6, pady=4)
        Button(btn_frame, text="◀  Undo  (Left)",  width=18, command=self._go_previous).grid(row=1, column=0, padx=6, pady=2)
        Button(btn_frame, text="Redo  (Right)  ▶", width=18, command=self._go_next).grid(row=1, column=1, padx=6, pady=2)
        Button(btn_frame, text="Skip \u2014 Cannot Judge  (W)", bg="#b8860b", fg="white", width=38, command=self._handle_skip).grid(row=2, column=0, columnspan=2, padx=6, pady=4)
        Label(btn_frame, text="A = IN NEST   D = OUT   W = Skip (cannot judge)   ← Undo   → Redo", font=("Arial", 9), fg="#777777").grid(row=3, column=0, columnspan=2, pady=2)

        self.root.bind("<a>",     lambda e: self._handle_answer(True))
        self.root.bind("<A>",     lambda e: self._handle_answer(True))
        self.root.bind("<d>",     lambda e: self._handle_answer(False))
        self.root.bind("<D>",     lambda e: self._handle_answer(False))
        self.root.bind("<w>",     lambda e: self._handle_skip())
        self.root.bind("<W>",     lambda e: self._handle_skip())
        self.root.bind("<Left>",  lambda e: self._go_previous())
        self.root.bind("<Right>", lambda e: self._go_next())

    #  Frame loading
    def _load_frame_into_label(self, label, frame_number, cache_key):
        frame_path = os.path.join(self.temp_dir, f"frame_{cache_key}.png")
        resolved_frame = extract_frame_to_path(self.video_map, frame_number, frame_path)
        if resolved_frame is not None and os.path.exists(frame_path):
            img   = Image.open(frame_path)
            img.thumbnail((480, 380))
            photo = ImageTk.PhotoImage(img)
            label.config(image=photo, text="")
            label.image = photo
        else:
            label.config(image="", text="[Frame not available]", fg="#888888")
            label.image = None
        return resolved_frame

    @staticmethod
    def _frame_label_text(requested_frame, resolved_frame):
        if resolved_frame is None or resolved_frame == requested_frame:
            return f"Frame {requested_frame}"
        return f"Frame {resolved_frame}  (nearest available \u2014 requested {requested_frame})"

    #  Display
    def _refresh_display(self, task, answer_text="", answer_color="black"):
        gap_start_inner = task["gap_start"]
        gap_end_inner   = task["gap_end"]
        gap_dur_min     = _seg_dur_min(gap_start_inner, gap_end_inner)
        seg_dur_min     = _seg_dur_min(task.get("seg_start", gap_start_inner), task.get("seg_end",   gap_end_inner))
        same_gap_ahead  = sum(1 for t in self.task_stack if t["gap_index"] == task["gap_index"])

        gtype = task.get("gap_type", "??")
        gtype_labels = {GAP_TYPE_00: "Type 00  (out-of-nest \u2192 out-of-nest)  |  checkpoint review", GAP_TYPE_10: "Type 10  (in-nest \u2192 out-of-nest)  |  searching for exit", GAP_TYPE_01: "Type 01  (out-of-nest \u2192 in-nest)  |  searching for entry",}
        gtype_str = gtype_labels.get(gtype, f"Type {gtype}")

        self.lbl_gap_counter.config(text=f"Gap  {task['gap_index']}  /  {task['total_gaps']}")
        self.lbl_gap_type.config(text=gtype_str)
        self.lbl_tasks_left.config(text=f"{same_gap_ahead} sub-task(s) remaining in this gap")
        self.lbl_gap.config(text=f"Full gap:  frame {gap_start_inner} \u2013 {gap_end_inner}  ({gap_dur_min:.1f} min)")
        self.lbl_segment.config(text=f"Segment:   frame {task.get('seg_start', gap_start_inner)} \u2013 {task.get('seg_end', gap_end_inner)}  ({seg_dur_min:.1f} min)")
        self.lbl_answer.config(text=answer_text, fg=answer_color)

        b_left  = task["boundary_left"]
        b_right = task["boundary_right"]
        mid     = task["show_frame"]

        resolved_left = self._load_frame_into_label(self.img_left, b_left, f"bl_{b_left}")
        self.lbl_left_frame_num.config(text=self._frame_label_text(b_left, resolved_left))

        resolved_mid = self._load_frame_into_label(self.img_center, mid, f"mid_{mid}")
        self.lbl_center_frame_num.config(text=self._frame_label_text(mid, resolved_mid))

        resolved_right = self._load_frame_into_label(self.img_right, b_right, f"br_{b_right}")
        self.lbl_right_frame_num.config(text=self._frame_label_text(b_right, resolved_right))

    #  Task loading
    def _load_next_task(self):
        if not self.task_stack:
            self._finish()
            return

        incoming_task = self.task_stack[0]

        if self._current_gap_index != incoming_task["gap_index"]:
            if self._gap_start_time is not None and self._current_gap_index is not None:
                elapsed = time.time() - self._gap_start_time
                for past_task, _, _, _ in reversed(self.history):
                    if past_task["gap_index"] == self._current_gap_index:
                        self._gap_timings.append({"gap_index": self._current_gap_index, "gap_start": past_task["gap_start"], "gap_end": past_task["gap_end"], "duration_seconds": elapsed,})
                        break
            self._gap_start_time    = time.time()
            self._current_gap_index = incoming_task["gap_index"]

        self.current_task = self.task_stack.pop(0)
        self._refresh_display(self.current_task)

    def _deferred_advance(self, token):
        """
        Callback target for the 300ms root.after(...) delay in
        _handle_answer. Only advances if the token still matches
        self._advance_token -- i.e. nothing has changed state (a second
        answer, an undo, or a redo) since this callback was scheduled.
        A stale callback (superseded by one of those) is silently ignored
        instead of acting on outdated state. This makes duplicate/stale
        advances structurally impossible rather than merely unlikely.
        """
        if token == self._advance_token:
            self._load_next_task()

    #  Answer handler
    def _handle_answer(self, in_nest: bool):
        task = self.current_task
        if task is None:
            return
        self.current_task = None
        self._advance_token += 1
        token = self._advance_token

        snapshot = copy.deepcopy(self.decisions)
        skip_snapshot = copy.deepcopy(self.explicitly_skipped_frames)
        stack_snapshot = list(self.task_stack)
        self.history.append((task, snapshot, skip_snapshot, stack_snapshot))
        self.redo_stack.clear()

        seg_start = task.get("seg_start", task["gap_start"])
        seg_end   = task.get("seg_end",   task["gap_end"])
        mid       = task["show_frame"]
        b_left    = task["boundary_left"]
        b_right   = task["boundary_right"]
        gtype     = task.get("gap_type", GAP_TYPE_10)

        seg_duration_minutes = (seg_end - seg_start + 1) / DB_FPS / 60

        def _make_subtask(seg_s, seg_e):
            return {
                "gap_index":      task["gap_index"],
                "total_gaps":     task["total_gaps"],
                "gap_start":      task["gap_start"],
                "gap_end":        task["gap_end"],
                "seg_start":      seg_s,
                "seg_end":        seg_e,
                "show_frame":     (seg_s + seg_e) // 2,
                "boundary_left":  b_left,
                "boundary_right": b_right,
                "gap_type":       gtype,
            }

        #  Short segment: fill entirely
        #  Git Issue #19 (reopened): type-00 checkpoints are excluded from
        #  this early return. Their checkpoint interval (default 60s) is
        #  the same length as this short-segment threshold (default 1 min
        #  = 60s), so nearly every type-00 checkpoint segment would
        #  otherwise land exactly on this boundary and short-circuit here
        #  -- on an IN answer, that would skip the type-10 hand-off below
        #  entirely, since this branch only fills and advances, it never
        #  hands off. Type-00 always goes through its own elif below
        #  instead, which fills identically on an OUT answer and adds the
        #  hand-off on an IN answer, regardless of segment duration.
        if gtype != GAP_TYPE_00 and seg_duration_minutes <= FILL_ENTIRE_SEGMENT_IF_DURATION_LESS_THAN_IN_MINUTES:
            fill_value = 1 if in_nest else 0
            for f in range(seg_start, seg_end + 1):
                self.decisions[f] = fill_value
            label = "IN NEST" if in_nest else "OUT OF NEST"
            color = "green"   if in_nest else "red"
            self._refresh_display(task, answer_text=f"{label} \u2014 short segment, filling entirely", answer_color=color)
            self.root.after(300, lambda t=token: self._deferred_advance(t))
            return

        #  Type 10: in-nest → out-of-nest
        if gtype == GAP_TYPE_10:
            if in_nest:
                for f in range(seg_start, mid + 1):
                    self.decisions[f] = 1
                self._refresh_display(task, answer_text="IN NEST \u2014 filling left, continuing right", answer_color="green")
                if mid + 1 <= seg_end:
                    self.task_stack.insert(0, _make_subtask(mid + 1, seg_end))
            else:
                if seg_start <= mid - 1:
                    for f in range(mid, seg_end + 1):
                        self.decisions[f] = 0
                    self.task_stack.insert(0, _make_subtask(seg_start, mid - 1))
                else:
                    for f in range(mid, seg_end + 1):
                        self.decisions[f] = 0
                self._refresh_display(task, answer_text="OUT OF NEST — searching left half", answer_color="red")
            self.root.after(300, lambda t=token: self._deferred_advance(t))

        #  Type 01: out-of-nest → in-nest
        elif gtype == GAP_TYPE_01:
            if in_nest:
                for f in range(mid, seg_end + 1):
                    self.decisions[f] = 1
                self._refresh_display(task, answer_text="IN NEST \u2014 filling right, continuing left", answer_color="green")
                if seg_start <= mid - 1:
                    self.task_stack.insert(0, _make_subtask(seg_start, mid - 1))
            else:
                if mid + 1 <= seg_end:
                    for f in range(seg_start, mid + 1):
                        self.decisions[f] = 0
                    self.task_stack.insert(0, _make_subtask(mid + 1, seg_end))
                else:
                    for f in range(seg_start, mid + 1):
                        self.decisions[f] = 0
                self._refresh_display(task, answer_text="OUT OF NEST — searching right half", answer_color="red")
            self.root.after(300, lambda t=token: self._deferred_advance(t))

        #  Type 00: OUT-of-nest -> OUT-of-nest (Issue #19 checkpoint review,
        #  reopened for performance)
        elif gtype == GAP_TYPE_00:
            if in_nest:
                # First checkpoint confirmed IN: stop OUT->OUT sampling here.
                # Keep the existing fill-left behavior for the sampled
                # portion up to and including this checkpoint (mid ==
                # seg_end by construction -- see _build_type00_checkpoint_
                # tasks), then hand the remainder of the gap off to the
                # existing type-10 (in-nest -> out-of-nest) binary search:
                # this checkpoint is now a known IN left boundary, and the
                # gap's right edge is already known OUT (that's what made
                # this a type-00 gap in the first place), so the remainder
                # is exactly a type-10 sub-problem.
                for f in range(seg_start, seg_end + 1):
                    self.decisions[f] = 1

                # Discard any remaining precomputed OUT-checkpoint tasks for
                # this gap -- they're superseded by the type-10 search below
                # and must not be shown (would re-review already-decided
                # frames and duplicate/contradict the search in progress).
                self.task_stack = [t for t in self.task_stack if t["gap_index"] != task["gap_index"]]

                gap_end_overall = task["gap_end"]
                if mid + 1 <= gap_end_overall:
                    handoff_task = _make_subtask(mid + 1, gap_end_overall)
                    handoff_task["gap_type"]      = GAP_TYPE_10
                    handoff_task["boundary_left"] = mid  # the checkpoint just confirmed IN
                    self.task_stack.insert(0, handoff_task)
                    self._refresh_display(task, answer_text="IN NEST \u2014 switching to binary search for exit", answer_color="green")
                else:
                    # This checkpoint was already gap_end (the last one) --
                    # nothing remains to search.
                    self._refresh_display(task, answer_text="IN NEST \u2014 filled toward left", answer_color="green")
            else:
                for f in range(seg_start, seg_end + 1):
                    self.decisions[f] = 0
                self._refresh_display(task, answer_text="OUT OF NEST \u2014 filled toward left", answer_color="red")
            self.root.after(300, lambda t=token: self._deferred_advance(t))

        else:
            # Every task reaching _handle_answer should have gap_type
            # GAP_TYPE_10, GAP_TYPE_01, or GAP_TYPE_00 -- build_initial_tasks
            # never queues type-11 gaps, and _make_subtask always copies
            # gap_type unchanged from its parent. An unrecognized gap_type
            # here means task/decision bookkeeping has gone wrong somewhere
            # upstream. Fail loudly here, at the moment the inconsistency is
            # discovered, rather than doing nothing and letting the review
            # silently freeze on this frame -- the frames in it would
            # otherwise eventually surface as the same "unresolved"
            # integrity failure at _finish(), with no indication of why.
            raise IntegrityError(
                f"_handle_answer received a task with unrecognized "
                f"gap_type {gtype!r} for gap_index {task['gap_index']} "
                f"(segment {seg_start}-{seg_end}). Expected "
                f"{GAP_TYPE_10!r}, {GAP_TYPE_01!r}, or {GAP_TYPE_00!r}."
            )

    def _handle_skip(self):
        """
        An explicit "I cannot judge this" control. Unlike an answer, this
        does not narrow the search -- it deliberately marks the whole
        current segment UNKNOWN (-1) and stops searching it, recording the
        frames in self.explicitly_skipped_frames so _finish()'s integrity
        check (which exists to catch frames *accidentally* left without a
        decision) does not treat a deliberate skip as a bug. Counted in
        bs_unknown exactly like any other -1, per write_summary_report's
        existing accounting.
        """
        task = self.current_task
        if task is None:
            return
        self.current_task = None
        self._advance_token += 1
        token = self._advance_token

        snapshot = copy.deepcopy(self.decisions)
        skip_snapshot = copy.deepcopy(self.explicitly_skipped_frames)
        stack_snapshot = list(self.task_stack)
        self.history.append((task, snapshot, skip_snapshot, stack_snapshot))
        self.redo_stack.clear()

        seg_start = task.get("seg_start", task["gap_start"])
        seg_end   = task.get("seg_end",   task["gap_end"])

        for f in range(seg_start, seg_end + 1):
            self.decisions[f] = -1
            self.explicitly_skipped_frames.add(f)

        self._refresh_display(task, answer_text="SKIPPED \u2014 cannot judge, marked UNKNOWN", answer_color="#b8860b")
        self.root.after(300, lambda t=token: self._deferred_advance(t))

    #  Undo / Redo
    def _go_previous(self):
        if not self.history:
            return
        self._advance_token += 1
        if self.current_task is not None:
            self.redo_stack.append(
                (self.current_task, copy.deepcopy(self.decisions),
                 copy.deepcopy(self.explicitly_skipped_frames), list(self.task_stack)))
        prev_task, prev_decisions, prev_skipped, prev_stack = self.history.pop()
        self.decisions    = prev_decisions
        self.explicitly_skipped_frames = prev_skipped
        self.current_task = prev_task
        self.task_stack   = prev_stack
        self._refresh_display(prev_task, answer_text="Undone \u2014 re-answer or continue", answer_color="#888888")

    def _go_next(self):
        if self.redo_stack:
            self._advance_token += 1
            redo_task, redo_decisions, redo_skipped, redo_stack_snapshot = self.redo_stack.pop()
            if self.current_task is not None:
                self.history.append((self.current_task, copy.deepcopy(self.decisions),
                                      copy.deepcopy(self.explicitly_skipped_frames), list(self.task_stack)))
            self.decisions    = redo_decisions
            self.explicitly_skipped_frames = redo_skipped
            self.current_task = redo_task
            self.task_stack   = redo_stack_snapshot
            self._refresh_display(redo_task,
                answer_text="Redone", answer_color="#555555")
        # If there is nothing to redo, do nothing. The current task is
        # still pending and has not been answered -- advancing past it here
        # discarded it without ever writing its frame range to
        # self.decisions or re-queuing it in self.task_stack, permanently
        # orphaning those frames. There is no legitimate "skip without
        # deciding" action in this workflow; the task must be answered or
        # undone.

    #  Window close cleanup
    def _on_close(self):
        import shutil
        _release_all_captures()
        if self.temp_dir and os.path.isdir(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)
        self.root.destroy()

    #  Finish
    def _finish(self):
        import shutil
        _release_all_captures()
        if self.temp_dir and os.path.isdir(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)
        #  Close last gap timer
        if self._gap_start_time is not None and self._current_gap_index is not None:
            elapsed = time.time() - self._gap_start_time
            for past_task, _, _, _ in reversed(self.history):
                if past_task["gap_index"] == self._current_gap_index:
                    self._gap_timings.append({
                        "gap_index":        self._current_gap_index,
                        "gap_start":        past_task["gap_start"],
                        "gap_end":          past_task["gap_end"],
                        "duration_seconds": elapsed,
                    })
                    break

        total_review_seconds = (time.time() - self._review_start_time if self._review_start_time else 0.0)

        # BUILD THE AUTHORITATIVE PER-FRAME CLASSIFICATION
        neg_frames        = set(self.df_neg["FRAMENUMBER"].tolist())
        searchable_frames = neg_frames - self.skipped_frames

        df_out = self.df.copy()

        fn_arr       = df_out["FRAMENUMBER"].to_numpy(dtype=np.int64)
        orig_in_nest = df_out["IN_NEST"].to_numpy()

        is_original_valid = orig_in_nest != -1
        is_skipped        = np.array([int(fn) in self.skipped_frames for fn in fn_arr])

        final_in_nest = np.empty(len(fn_arr), dtype=np.int64)
        final_in_nest[is_original_valid] = orig_in_nest[is_original_valid].astype(np.int64)

        remaining_mask     = ~is_original_valid
        skipped_remaining  = remaining_mask & is_skipped
        decision_remaining = remaining_mask & ~is_skipped

        final_in_nest[skipped_remaining] = -1
        final_in_nest[decision_remaining] = [
            self.decisions.get(int(fn), -1) for fn in fn_arr[decision_remaining]
        ]

        final_clf = dict(zip(fn_arr.tolist(), final_in_nest.tolist()))

        unresolved = [fn for fn in searchable_frames if final_clf[fn] == -1 and fn not in self.explicitly_skipped_frames]
        if unresolved:
            messagebox.showerror(
                "Integrity Check Failed",
                f"{len(unresolved)} searchable frame(s) never received an explicit "
                f"binary-search decision (e.g. frame {unresolved[0]}).\n\n"
                f"No output was written. This indicates a bug in the review "
                f"session's task/decision bookkeeping — please report this."
            )
            self.root.quit()
            return

        df_out["IN_NEST"] = final_in_nest

        is_searchable = np.array([int(fn) in searchable_frames for fn in fn_arr])

        df_out["FILL_SOURCE"] = np.where(
            is_searchable, "BINARY_SEARCH",
            np.where(final_in_nest == -1, "UNKNOWN", "LOGIC")
        )

        # Merge detected rows back in to produce the full output table
        df_detected_out = self.df_all[self.df_all["ASSUMPTION_TYPE"] == "DETECTED"].copy()
        df_detected_out["FILL_SOURCE"]   = "DETECTED"

        df_out = pd.concat(
            [df_detected_out, df_out], ignore_index=True
        ).sort_values("FRAMENUMBER").reset_index(drop=True)

        # DERIVE SUMMARY COUNTS FROM final_clf
        bs_in_frames  = sum(1 for fn in searchable_frames if final_clf[fn] == 1)
        bs_out_frames = sum(1 for fn in searchable_frames if final_clf[fn] == 0)
        bs_unknown    = sum(1 for fn in searchable_frames if final_clf[fn] == -1)

        threshold_skipped = sum(len(range(gs + 1, ge)) for gs, ge in self.skipped_gap_keys)
        zz_skipped = sum(len(range(gs + 1, ge)) for gs, ge in self.zero_zero_skipped_gap_keys)
        ze_skipped = sum(len(range(gs + 1, ge)) for gs, ge in self.one_one_gap_keys)
        bs_input_total = len(neg_frames)

        # SAVE SQLITE
        timestamp  = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        animal_label = f"A{int(self.df_all['ANIMALID'].iloc[0])}" if "ANIMALID" in self.df_all.columns else "Aunknown"
        out_sqlite = os.path.join(self.output_folder, f"lmt_binary_search_{animal_label}_{timestamp}.sqlite")
        conn = sqlite3.connect(out_sqlite)
        df_out.to_sql("GAP_FILL_ANALYSIS", conn, if_exists="replace", index=False)
        conn.close()

        # WRITE REPORT
        report_path = os.path.join(self.output_folder, f"LMT_Summary_{animal_label}_{timestamp}.txt")
        try:
            write_summary_report(
                report_path,
                self.db_path,
                self.video_paths,
                self.output_folder,
                self.df_all,
                df_out,
                final_clf,
                searchable_frames,
                self.skipped_gap_keys,
                self.zero_zero_reviewed_gap_keys,
                self.zero_zero_skipped_gap_keys,
                self.one_one_gap_keys,
                self.gap_type_map,
                total_review_seconds,
                self._gap_timings,
            )
        except IntegrityError as e:
            messagebox.showerror("Integrity Check Failed", f"The report could not be generated because a frame-count integrity check failed.\n\n{e}\n\nThe SQLite output has been saved and is internally consistent,\nbut the text report was not written.\n\nSQLite: {out_sqlite}")
            self.root.quit()
            return

        # COMPLETION DIALOG
        neg_remaining = int((df_out["IN_NEST"] == -1).sum())

        messagebox.showinfo("Binary Search Complete", f"All gaps processed.\n\nTotal review time:               {seconds_to_hms(total_review_seconds)}\n\nBinary-search input frames:      {bs_input_total:,}\n  Routed to reviewer:            {len(searchable_frames):,}\n    - Reclassified IN NEST:      {bs_in_frames:,}\n    - Reclassified OUT OF NEST:  {bs_out_frames:,}\n    - Residual unknown:          {bs_unknown:,}\n  Skipped (\u2264 threshold):          {threshold_skipped:,}\n  Skipped (type-00):             {zz_skipped:,}\n  Skipped (type-11):             {ze_skipped:,}\n  Balance: {bs_in_frames+bs_out_frames+bs_unknown+threshold_skipped+zz_skipped+ze_skipped:,} {'== OK' if bs_in_frames+bs_out_frames+bs_unknown+threshold_skipped+zz_skipped+ze_skipped==bs_input_total else '!= MISMATCH'}\n\nRemaining IN_NEST = -1:          {neg_remaining:,}\n\nSQLite output:\n{out_sqlite}\n\nSummary report:\n{report_path}\n\nFeed the SQLite into 3.lmt_qc_sampler.py.")
        self.root.quit()

# CLI entry point
def _build_arg_parser():
    parser = argparse.ArgumentParser(
        description=(
            "LMT Binary Search Gap Filler: resolves ASSUMED frames where "
            "IN_NEST = -1 (from 1.lmt_gap_fill.py output) via a "
            "human-in-the-loop review (binary search for 01/10 gaps, "
            "checkpoint sampling for above-threshold 00 gaps)."
        )
    )
    parser.add_argument(
        "-i", "--input", required=True,
        help="Path to lmt_gap_fill_A<animal_id>_<timestamp>.sqlite "
             "(1.lmt_gap_fill.py output).",
    )
    parser.add_argument(
        "-v", "--videos", required=True, nargs="+",
        help="One or more LMT video files (*.mp4).",
    )
    parser.add_argument(
        "-o", "--output-folder", required=True,
        help="Directory to write the resulting SQLite/report into.",
    )
    return parser


def _validate_cli_args(args):
    errors = []
    if not os.path.isfile(args.input):
        errors.append(f"Input SQLite not found: {args.input}")
    missing_videos = [v for v in args.videos if not os.path.isfile(v)]
    if missing_videos:
        errors.append("Video file(s) not found:\n  " + "\n  ".join(missing_videos))
    if not os.path.isdir(args.output_folder):
        errors.append(f"Output folder not found: {args.output_folder}")
    if errors:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    import argparse
    import sys
    from PIL import Image, ImageTk

    cli_args = _build_arg_parser().parse_args()
    _validate_cli_args(cli_args)

    root = Tk()
    app  = BinarySearchGUI(root, cli_args.input, cli_args.videos, cli_args.output_folder)
    root.mainloop()