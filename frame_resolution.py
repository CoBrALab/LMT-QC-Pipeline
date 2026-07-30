"""
Shared video/frame-resolution logic used by 2.lmt_binary_search.py,
3.lmt_qc_sampler.py, and 4.lmt_qc_validator.py.

Every script that reads frames out of LMT video files needs the same three
things: (1) figure out which video covers a given global DB frame number,
(2) resolve to the nearest available frame when nothing covers it exactly,
and (3) actually read and cache that frame from disk. This logic used to be
hand-duplicated in all three scripts; keeping three copies in sync had
already caused at least one real behavioral regression (script 4's copy
drifted to an inferior backward-only search). All three scripts now import
from here instead of maintaining their own copy - see GitHub issue #7.

Script-specific wrappers (e.g. how a resolved frame gets written to a
Tkinter label vs. a plain file vs. renamed with a video name) are NOT here;
each caller builds its own thin wrapper on top of the primitives below.
"""

import os
import re
import cv2

# Configurable constants
DB_FPS           = 30    # LMT database frame rate
FRAME_CONVERSION = 2     # 30fps DB -> 15fps video

# Expected video frame rate given the DB_FPS / FRAME_CONVERSION assumption.
# Videos whose actual fps deviates from this are flagged (not blocked) so the
# user can judge whether frame alignment is trustworthy for that file.
EXPECTED_VIDEO_FPS = DB_FPS / FRAME_CONVERSION
FPS_TOLERANCE      = 0.5


def get_start_frame(video_name):
    """
    Extract the starting global frame number encoded in a video filename.

    Expects a segment of the form "t<digits>" immediately preceding the file
    extension (e.g. "..._t12345.mp4"). The match is anchored to the end of
    the filename (rather than splitting on the first "t" anywhere in the
    string) so that a filename containing an unrelated "t" earlier on (e.g.
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


def find_nearest_frame_candidates(video_map, global_frame):
    """
    Resolve a requested global frame to the video(s) that can actually supply it.

    - If a video directly covers global_frame, that is the only candidate.
    - Otherwise, find the nearest preceding available frame (the last frame of
      the video ending closest to, but before, global_frame) and the nearest
      succeeding available frame (the first frame of the video starting
      closest to, but after, global_frame). Whichever is closer to
      global_frame is preferred; on a tie, the preceding frame is preferred.
      The runner-up is kept as a fallback candidate in case the preferred
      video can't be opened/read.

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


# Cached, reusable VideoCapture handles.
# Opening a cv2.VideoCapture per frame extraction is expensive; callers may
# request hundreds of frames from the same handful of video files in one
# session, so handles are opened once and reused. Call release_all_captures()
# when a session/run ends (or the window closes) to release them.
_video_capture_cache = {}


def get_capture(path):
    cap = _video_capture_cache.get(path)
    if cap is None or not cap.isOpened():
        cap = cv2.VideoCapture(path)
        _video_capture_cache[path] = cap
    return cap


def release_all_captures():
    for cap in _video_capture_cache.values():
        try:
            cap.release()
        except Exception:
            pass
    _video_capture_cache.clear()


def read_frame_from_video(video_entry, resolved_frame, out_path):
    local_frame = int((resolved_frame - video_entry["start"]) / FRAME_CONVERSION)
    cap = get_capture(video_entry["path"])
    if not cap.isOpened():
        return False
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, min(local_frame, total - 1)))
    ret, frame = cap.read()
    if ret:
        cv2.imwrite(out_path, frame)
        return True
    return False
