import os
import re
import cv2
import pandas as pd
from PIL import ImageDraw

# Configurable constants
DB_FPS           = 30    # LMT database frame rate
FRAME_CONVERSION = 2     # 30fps DB -> 15fps video

EXPECTED_VIDEO_FPS = DB_FPS / FRAME_CONVERSION
FPS_TOLERANCE       = 0.5

# QC mode constants (shared across sampler/validator; validator also
# recognizes legacy "ASSUMED" outputs)
QC_MODE_DETECTED      = "DETECTED"
QC_MODE_BINARY_SEARCH = "BINARY_SEARCH"
QC_MODE_LOGIC         = "LOGIC"
QC_MODE_ASSUMED       = "ASSUMED"

# Git Issue #22 (+ follow-ups): Nest/Buffer ROI overlay drawing and
# ROI_METADATA read/write. Originally implemented locally in
# 2.lmt_binary_search.py; centralized here so 4.lmt_qc_validator.py can
# draw the identical overlay (solid Nest / dashed Buffer) from the identical
# ROI values, without a second, independently-drifting copy of this logic.
DEFAULT_ROI_COLOR     = "yellow"
DEFAULT_ROI_THICKNESS = 2


def load_roi_metadata_from_db(conn):
    """
    Read the Nest/Buffer ROI 1.lmt_gap_fill.py used, from the ROI_METADATA
    table (one row) it writes into its output SQLite, and that
    2.lmt_binary_search.py / 3.lmt_qc_sampler.py propagate forward into
    their own outputs (via write_roi_metadata()) so 4.lmt_qc_validator.py
    can read it too, several steps downstream.

    Returns (nest_roi, buffer_roi), each either a {"xmin","xmax","ymin",
    "ymax"} dict or None if the table doesn't exist in `conn` (e.g. an
    input file produced before this feature existed, or one that skipped a
    step that would have propagated it).
    """
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='ROI_METADATA'")
    if cursor.fetchone() is None:
        return None, None

    df = pd.read_sql_query("SELECT * FROM ROI_METADATA LIMIT 1", conn)
    if len(df) == 0:
        return None, None
    row = df.iloc[0]

    nest_roi = {
        "xmin": row["NEST_XMIN"], "xmax": row["NEST_XMAX"],
        "ymin": row["NEST_YMIN"], "ymax": row["NEST_YMAX"],
    }
    buffer_roi = {
        "xmin": row["BUFFER_XMIN"], "xmax": row["BUFFER_XMAX"],
        "ymin": row["BUFFER_YMIN"], "ymax": row["BUFFER_YMAX"],
    }
    return nest_roi, buffer_roi


def write_roi_metadata(conn, animal_id, nest_roi, buffer_roi):
    """
    Write (or overwrite) the one-row ROI_METADATA table into `conn`, so a
    downstream script can read the same Nest/Buffer ROI back via
    load_roi_metadata_from_db(). Used by 2.lmt_binary_search.py and
    3.lmt_qc_sampler.py to propagate whatever ROI they read from their own
    input forward into their own output -- without this, the ROI would
    stop at whichever script last read it and never reach
    4.lmt_qc_validator.py. (1.lmt_gap_fill.py writes this table directly
    via pandas, not through this helper, since it computes the ROI itself
    rather than reading it from an input file.)

    No-ops (writes nothing, leaves any existing ROI_METADATA table alone)
    if both nest_roi and buffer_roi are None -- there is nothing to
    propagate, and writing an all-None row would be indistinguishable from
    "this run legitimately had no ROI" versus "the table is simply absent".
    """
    if nest_roi is None and buffer_roi is None:
        return
    n = nest_roi or {"xmin": None, "xmax": None, "ymin": None, "ymax": None}
    b = buffer_roi or {"xmin": None, "xmax": None, "ymin": None, "ymax": None}
    row = pd.DataFrame([{
        "ANIMALID":    animal_id,
        "NEST_XMIN":   n["xmin"], "NEST_XMAX":   n["xmax"],
        "NEST_YMIN":   n["ymin"], "NEST_YMAX":   n["ymax"],
        "BUFFER_XMIN": b["xmin"], "BUFFER_XMAX": b["xmax"],
        "BUFFER_YMIN": b["ymin"], "BUFFER_YMAX": b["ymax"],
    }])
    row.to_sql("ROI_METADATA", conn, if_exists="replace", index=False)


def _draw_dashed_edge(draw, start, end, color, thickness, dash_length=8, gap_length=5):
    """Draw one dashed straight edge (horizontal or vertical) between two points."""
    x1, y1 = start
    x2, y2 = end
    if x1 == x2:  # vertical edge
        y, y_end = min(y1, y2), max(y1, y2)
        while y < y_end:
            seg_end = min(y + dash_length, y_end)
            draw.line([(x1, y), (x1, seg_end)], fill=color, width=thickness)
            y = seg_end + gap_length
    else:  # horizontal edge
        x, x_end = min(x1, x2), max(x1, x2)
        while x < x_end:
            seg_end = min(x + dash_length, x_end)
            draw.line([(x, y1), (seg_end, y1)], fill=color, width=thickness)
            x = seg_end + gap_length


def draw_roi_overlay(img, roi, color=DEFAULT_ROI_COLOR, thickness=DEFAULT_ROI_THICKNESS, dashed=False):
    """
    Git Issue #22 (+ follow-ups): draw a Nest or Buffer ROI as an outline
    rectangle on a PIL Image, in place, and return it. Nest ROI is drawn
    solid; Buffer ROI is drawn dashed, so the two stay visually
    distinguishable while sharing the same colour/thickness. Used by both
    2.lmt_binary_search.py and 4.lmt_qc_validator.py so their overlays
    render identically.

    roi uses the same {"xmin","xmax","ymin","ymax"} dict shape as
    1.lmt_gap_fill.py's NEST/NEST_BUFFER, persisted into that script's
    output SQLite (ROI_METADATA table) and read back via
    load_roi_metadata_from_db() -- no script downstream of
    1.lmt_gap_fill.py accepts ROI values on its own command line.
    MASS_X/MASS_Y and video pixel coordinates share the same coordinate
    space in this pipeline, so the bounds are drawn directly with no
    rescaling.

    Outline-only, never filled, so the overlay marks the boundary without
    covering the animal or nest contents underneath it (non-obscuring).
    Call this before any thumbnail/resize of `img` so the rectangle is
    drawn in the same pixel space as roi, then let the resize scale the
    whole image (overlay included) down together.
    """
    draw = ImageDraw.Draw(img)
    x0, y0, x1, y1 = roi["xmin"], roi["ymin"], roi["xmax"], roi["ymax"]
    if not dashed:
        draw.rectangle([x0, y0, x1, y1], outline=color, width=thickness)
    else:
        _draw_dashed_edge(draw, (x0, y0), (x1, y0), color, thickness)  # top
        _draw_dashed_edge(draw, (x0, y1), (x1, y1), color, thickness)  # bottom
        _draw_dashed_edge(draw, (x0, y0), (x0, y1), color, thickness)  # left
        _draw_dashed_edge(draw, (x1, y0), (x1, y1), color, thickness)  # right
    return img


def get_start_frame(video_name):
    """
    Extract the starting global frame number encoded in a video filename.

    Expects a segment of the form "t<digits>" immediately preceding the file
    extension (e.g. "..._t12345.mp4"). The match is anchored to the end of
    the filename so a filename containing an unrelated "t" earlier on (e.g.
    in an experiment/cage name) is not misparsed.
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


def build_video_map(video_paths):
    """
    Returns (video_map, skipped_videos, fps_mismatches).

    skipped_videos : filenames that could not be parsed for a starting frame
                     number and were therefore excluded from video_map.
    fps_mismatches : descriptive strings for videos whose actual frame rate
                     does not match the DB_FPS/FRAME_CONVERSION assumption.
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

    - If a video directly covers global_frame, that is the only candidate.
    - Otherwise, find the nearest preceding available frame and the nearest
      succeeding available frame. Whichever is closer to global_frame is
      preferred; on a tie, the preceding frame is preferred. The runner-up is
      kept as a fallback candidate in case the preferred video can't be
      opened/read.

    Returns a list of (resolved_global_frame, video_entry) tuples, ordered by
    preference. Empty list if video_map is empty.
    """
    if not video_map:
        return []

    for v in video_map:
        if v["start"] <= global_frame < v["end"]:
            return [(global_frame, v)]

    preceding  = None  # (resolved_frame, video_entry)
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
    local_frame = round((resolved_frame - video_entry["start"]) / FRAME_CONVERSION)
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


def compute_qc_pool_mask(df_full, qc_mode):
    """
    Return (mask, label): the eligible-row boolean mask and a human-readable
    pool label for the requested QC mode, given a GAP_FILL_ANALYSIS /
    QC_ASSUMED_SAMPLES-shaped DataFrame.

    Pool definitions
    DETECTED      : ASSUMPTION_TYPE == "DETECTED"
    BINARY_SEARCH : ASSUMPTION_TYPE == "ASSUMED" AND FILL_SOURCE == "BINARY_SEARCH"
                    AND IN_NEST in (0, 1). Falls back to BINARY_SEARCH == 1
                    if FILL_SOURCE is absent (old 2.lmt_binary_search.py outputs).
    LOGIC         : ASSUMPTION_TYPE == "ASSUMED" AND FILL_SOURCE == "LOGIC"
                    AND IN_NEST in (0, 1). Falls back to BINARY_SEARCH == 0 and
                    IN_NEST in (0,1) if absent.
    ASSUMED       : legacy pool — all ASSUMED rows with IN_NEST in (0, 1).
    """
    has_fill_source = "FILL_SOURCE" in df_full.columns

    if qc_mode == QC_MODE_DETECTED:
        mask  = df_full["ASSUMPTION_TYPE"] == "DETECTED"
        label = "Detected rows"

    elif qc_mode == QC_MODE_BINARY_SEARCH:
        if has_fill_source:
            mask = ((df_full["ASSUMPTION_TYPE"] == "ASSUMED") & (df_full["FILL_SOURCE"] == "BINARY_SEARCH") & (df_full["IN_NEST"].isin([0, 1])))
        elif "BINARY_SEARCH" in df_full.columns:
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
            bs_col = "BINARY_SEARCH" if "BINARY_SEARCH" in df_full.columns else None
            if bs_col:
                mask = ((df_full["ASSUMPTION_TYPE"] == "ASSUMED") & (df_full[bs_col] == 0) & (df_full["IN_NEST"].isin([0, 1])))
            else:
                mask = ((df_full["ASSUMPTION_TYPE"] == "ASSUMED") & (df_full["IN_NEST"].isin([0, 1])))
        label = "Logic-filled rows"

    elif qc_mode == QC_MODE_ASSUMED:
        mask  = ((df_full["ASSUMPTION_TYPE"] == "ASSUMED") & (df_full["IN_NEST"].isin([0, 1])))
        label = "Assumed rows (legacy pool)"

    else:
        raise Exception(f"Unknown QC mode: {qc_mode!r}")

    return mask, label