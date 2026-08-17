import pandas as pd
import pytest


def test_classify_gap_type_all_four_directions(binary_search_module):
    m = binary_search_module
    lookup = {100: 1, 200: 0, 300: 1, 400: 0}

    assert m.classify_gap_type(200, 400, lookup) == m.GAP_TYPE_00
    assert m.classify_gap_type(200, 300, lookup) == m.GAP_TYPE_01
    assert m.classify_gap_type(100, 200, lookup) == m.GAP_TYPE_10
    assert m.classify_gap_type(100, 300, lookup) == m.GAP_TYPE_11


def test_classify_gap_type_missing_boundary_raises_integrity_error(binary_search_module):
    m = binary_search_module
    lookup = {100: 1, 200: 0}
    with pytest.raises(m.IntegrityError):
        m.classify_gap_type(100, 999, lookup)  # 999 not a detected frame


def test_classify_gap_type_invalid_in_nest_value_raises(binary_search_module):
    m = binary_search_module
    lookup = {100: 1, 200: 7}  # 7 is not a valid IN_NEST value
    with pytest.raises(m.IntegrityError):
        m.classify_gap_type(100, 200, lookup)


def test_check_passes_silently_when_equal(binary_search_module):
    binary_search_module._check("some identity", 10, 10)  # should not raise


def test_check_raises_integrity_error_on_mismatch(binary_search_module):
    m = binary_search_module
    with pytest.raises(m.IntegrityError):
        m._check("some identity", 10, 11)


def _make_df_all(detected_rows, assumed_rows):
    """detected_rows: list of (frame, in_nest). assumed_rows: list of
    (frame, in_nest, gap_start, gap_end)."""
    records = []
    for f, v in detected_rows:
        records.append({"FRAMENUMBER": f, "IN_NEST": v, "ASSUMPTION_TYPE": "DETECTED",
                         "GAP_START_FRAME": None, "GAP_END_FRAME": None})
    for f, v, gs, ge in assumed_rows:
        records.append({"FRAMENUMBER": f, "IN_NEST": v, "ASSUMPTION_TYPE": "ASSUMED",
                         "GAP_START_FRAME": gs, "GAP_END_FRAME": ge})
    return pd.DataFrame.from_records(records)


def test_build_initial_tasks_type_00_is_skipped_not_queued(binary_search_module):
    m = binary_search_module
    # Gap 100-200, both boundaries out-of-nest (type 00). Below the
    # duration threshold — should be skipped, not queued for checkpoint
    # review (a longer type-00 gap would be reviewed instead, see
    # test_build_initial_tasks_type00_above_threshold_is_reviewed in
    # test_type00_gap_review.py).
    df_all = _make_df_all(
        detected_rows=[(100, 0), (200, 0)],
        assumed_rows=[(150, -1, 100, 200)],
    )
    df_neg = df_all[df_all["IN_NEST"] == -1]
    tasks, skipped, skipped_gaps, gtype_map, zz_reviewed, zz_skipped, oo = m.build_initial_tasks(df_neg, df_all)
    assert tasks == []
    assert 150 in skipped
    assert (100, 200) in zz_skipped
    assert (100, 200) not in zz_reviewed
    assert gtype_map[(100, 200)] == m.GAP_TYPE_00


def test_build_initial_tasks_short_gap_skipped_by_threshold(binary_search_module):
    m = binary_search_module
    # Type 10 gap, but only 1 second long (well under the 30s threshold at
    # DB_FPS=30) — should be threshold-skipped, not queued for search.
    df_all = _make_df_all(
        detected_rows=[(1000, 1), (1030, 0)],
        assumed_rows=[(f, -1, 1000, 1030) for f in range(1001, 1030)],
    )
    df_neg = df_all[df_all["IN_NEST"] == -1]
    tasks, skipped, skipped_gaps, gtype_map, zz_reviewed, zz_skipped, oo = m.build_initial_tasks(df_neg, df_all)
    assert tasks == []
    assert (1000, 1030) in skipped_gaps
    assert gtype_map[(1000, 1030)] == m.GAP_TYPE_10


def test_build_initial_tasks_type_10_above_threshold_is_queued(binary_search_module):
    m = binary_search_module
    # Type 10 gap spanning 31 seconds (~930 frames at 30fps) — above the
    # 30s threshold, should produce exactly one queued task.
    gs, ge = 1000, 1000 + 930
    df_all = _make_df_all(
        detected_rows=[(gs, 1), (ge, 0)],
        assumed_rows=[(f, -1, gs, ge) for f in range(gs + 1, ge)],
    )
    df_neg = df_all[df_all["IN_NEST"] == -1]
    tasks, skipped, skipped_gaps, gtype_map, zz_reviewed, zz_skipped, oo = m.build_initial_tasks(df_neg, df_all)
    assert len(tasks) == 1
    task = tasks[0]
    assert task["gap_type"] == m.GAP_TYPE_10
    assert task["gap_start"] == gs + 1
    assert task["gap_end"] == ge - 1
    assert task["show_frame"] == (task["gap_start"] + task["gap_end"]) // 2


def test_find_nearest_frame_candidates_direct_hit(binary_search_module):
    m = binary_search_module
    video_map = [{"start": 0, "end": 100, "path": "a.mp4"},
                 {"start": 100, "end": 200, "path": "b.mp4"}]
    result = m.find_nearest_frame_candidates(video_map, 50)
    assert result == [(50, video_map[0])]


def test_find_nearest_frame_candidates_tie_prefers_preceding(binary_search_module):
    m = binary_search_module
    # Gap at frame 100-104 between two videos; requesting frame 102 is
    # equidistant (2 frames) from each side — preceding should win the tie.
    video_map = [{"start": 0, "end": 102, "path": "a.mp4"},
             {"start": 104, "end": 200, "path": "b.mp4"}]
    result = m.find_nearest_frame_candidates(video_map, 102)
    assert result[0][1]["path"] == "a.mp4"
    assert result[1][1]["path"] == "b.mp4"


def test_find_nearest_frame_candidates_empty_video_map(binary_search_module):
    m = binary_search_module
    assert m.find_nearest_frame_candidates([], 50) == []