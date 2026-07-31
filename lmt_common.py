import os
import re
import cv2

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