"""
Tests for Gap Type 00 (Issue #19) checkpoint review.

Covers:
  - _build_type00_checkpoint_tasks: checkpoint positions, fill-left segment
    boundaries, forced final checkpoint at gap_end, full coverage with no
    gaps/overlaps, and the short-gap (single checkpoint) case.
  - build_initial_tasks: below-threshold type-00 gaps are still skipped
    exactly as before; above-threshold gaps are routed to review instead,
    are NOT added to skipped_frames, and their checkpoint tasks cover the
    gap's interior exactly once each.
  - _handle_answer: the new GAP_TYPE_00 branch fills the correct range,
    both via the pre-existing short-segment-fill coincidence (checkpoints
    <= 1 min) and via the branch itself for longer segments.
  - Skip and undo/redo continue to work correctly against type-00
    checkpoint tasks, with no gap-type-specific changes required in either.
  - Fix 6 (unrecognized gap_type still raises IntegrityError) is
    unaffected by GAP_TYPE_00 becoming a recognized type.
  - A normal Type 10 answer is unaffected (regression check).

Uses the `binary_search_module` fixture from tests/conftest.py, which
loads 2.lmt_binary_search.py by path (its filename isn't a valid import
target) and is safe to import directly since its Tk() bootstrap is
already guarded behind `if __name__ == "__main__":`.
"""
import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_task(gap_index, seg_start, seg_end, gap_type):
    """A minimal task dict, matching the shape build_initial_tasks/
    _build_type00_checkpoint_tasks produce."""
    return {
        "gap_index": gap_index, "total_gaps": 1,
        "gap_start": seg_start, "gap_end": seg_end,
        "seg_start": seg_start, "seg_end": seg_end,
        "show_frame": (seg_start + seg_end) // 2,
        "boundary_left": 1, "boundary_right": 0,
        "gap_type": gap_type,
    }


def _make_df_all(detected_rows, assumed_rows):
    """detected_rows: list of (frame, in_nest).
    assumed_rows: list of (frame, in_nest, gap_start, gap_end)."""
    records = []
    for f, v in detected_rows:
        records.append({"FRAMENUMBER": f, "IN_NEST": v, "ASSUMPTION_TYPE": "DETECTED",
                         "GAP_START_FRAME": None, "GAP_END_FRAME": None})
    for f, v, gs, ge in assumed_rows:
        records.append({"FRAMENUMBER": f, "IN_NEST": v, "ASSUMPTION_TYPE": "ASSUMED",
                         "GAP_START_FRAME": gs, "GAP_END_FRAME": ge})
    return pd.DataFrame.from_records(records)


class _FakeRoot:
    """Stand-in for the Tk root, sufficient for _load_next_task/
    _handle_answer/_handle_skip/_go_previous/_go_next: only needs
    .after() (queues callbacks instead of scheduling on a real event
    loop) and a no-op .quit()."""
    def __init__(self):
        self.pending = []

    def after(self, ms, callback):
        self.pending.append(callback)

    def run_pending(self):
        self.pending.pop(0)()

    def run_all_pending(self):
        while self.pending:
            self.run_pending()

    def quit(self):
        pass


def _new_gui(m):
    """Build a bare BinarySearchGUI instance without invoking __init__
    (which builds the full setup screen). _refresh_display and _finish
    are stubbed to no-ops so tests can call the task-handling methods
    directly without a real display."""
    gui = object.__new__(m.BinarySearchGUI)
    gui.root = _FakeRoot()
    gui.task_stack = []
    gui.redo_stack = []
    gui.history = []
    gui.current_task = None
    gui.decisions = {}
    gui._advance_token = 0
    gui.explicitly_skipped_frames = set()
    gui._current_gap_index = None
    gui._gap_start_time = None
    gui._gap_timings = []
    gui._refresh_display = lambda *a, **k: None
    gui._finish = lambda: None
    return gui


# ---------------------------------------------------------------------------
# _build_type00_checkpoint_tasks
# ---------------------------------------------------------------------------

def test_checkpoint_positions_and_fill_left_segments(binary_search_module):
    m = binary_search_module
    # gap_start=1001, gap_end=6551 (5551 frames), interval=60s=1800 frames
    # at DB_FPS=30 -> 3 full intervals (2800, 4600, 6400) + forced final at 6551.
    tasks = m._build_type00_checkpoint_tasks(
        gs=1000, ge=6552, b_left=1000, b_right=6552, gap_idx=1, total_gaps=1)

    assert [t["seg_end"] for t in tasks] == [2800, 4600, 6400, 6551]
    assert [t["seg_start"] for t in tasks] == [1001, 2801, 4601, 6401]
    assert all(t["gap_type"] == m.GAP_TYPE_00 for t in tasks)
    assert all(t["show_frame"] == t["seg_end"] for t in tasks)


def test_checkpoints_cover_gap_exactly_no_gaps_no_overlap(binary_search_module):
    m = binary_search_module
    tasks = m._build_type00_checkpoint_tasks(
        gs=1000, ge=6552, b_left=1000, b_right=6552, gap_idx=1, total_gaps=1)

    total_covered = sum(t["seg_end"] - t["seg_start"] + 1 for t in tasks)
    assert total_covered == 6551 - 1001 + 1
    assert all(tasks[i]["seg_end"] + 1 == tasks[i + 1]["seg_start"]
               for i in range(len(tasks) - 1))


def test_final_checkpoint_always_forced_to_gap_end(binary_search_module):
    m = binary_search_module
    tasks = m._build_type00_checkpoint_tasks(
        gs=1000, ge=6552, b_left=1000, b_right=6552, gap_idx=1, total_gaps=1)
    assert tasks[-1]["seg_end"] == 6551  # gap_end, regardless of interval alignment


def test_gap_shorter_than_interval_gets_a_single_checkpoint(binary_search_module):
    m = binary_search_module
    # 35-second gap (1050 frames) with a 60s interval -- shorter than one interval.
    tasks = m._build_type00_checkpoint_tasks(
        gs=1000, ge=2051, b_left=1000, b_right=2051, gap_idx=1, total_gaps=1)
    assert len(tasks) == 1
    assert tasks[0]["seg_start"] == 1001
    assert tasks[0]["seg_end"] == 2050


def test_checkpoints_share_gap_level_boundary_frames(binary_search_module):
    m = binary_search_module
    # boundary_left/boundary_right are the flanking DETECTED frame numbers
    # for the whole gap, constant across every checkpoint in that gap.
    tasks = m._build_type00_checkpoint_tasks(
        gs=1000, ge=6552, b_left=1000, b_right=6552, gap_idx=1, total_gaps=1)
    assert all(t["boundary_left"] == 1000 and t["boundary_right"] == 6552 for t in tasks)


# ---------------------------------------------------------------------------
# build_initial_tasks: threshold routing
# ---------------------------------------------------------------------------

def test_build_initial_tasks_type00_below_threshold_is_skipped(binary_search_module):
    m = binary_search_module
    # 20-second gap: below MIN_GAP_DURATION_FOR_BINARY_SEARCH (30s).
    df_all = _make_df_all(
        detected_rows=[(1000, 0), (1600, 0)],
        assumed_rows=[(f, -1, 1000, 1600) for f in range(1001, 1600)])
    df_neg = df_all[df_all["IN_NEST"] == -1]

    (tasks, skipped, skipped_gaps, gtype_map,
     zz_reviewed, zz_skipped, oo) = m.build_initial_tasks(df_neg, df_all)

    assert tasks == []
    assert (1000, 1600) in zz_skipped
    assert (1000, 1600) not in zz_reviewed
    assert all(f in skipped for f in range(1001, 1600))


def test_build_initial_tasks_type00_above_threshold_is_reviewed(binary_search_module):
    m = binary_search_module
    gs, ge = 1000, 6552  # 5551-frame interior, well above the 30s threshold
    df_all = _make_df_all(
        detected_rows=[(gs, 0), (ge, 0)],
        assumed_rows=[(f, -1, gs, ge) for f in range(gs + 1, ge)])
    df_neg = df_all[df_all["IN_NEST"] == -1]

    (tasks, skipped, skipped_gaps, gtype_map,
     zz_reviewed, zz_skipped, oo) = m.build_initial_tasks(df_neg, df_all)

    assert len(tasks) == 4
    assert (gs, ge) in zz_reviewed
    assert (gs, ge) not in zz_skipped
    assert all(f not in skipped for f in range(gs + 1, ge))
    assert sum(t["seg_end"] - t["seg_start"] + 1 for t in tasks) == ge - 1 - (gs + 1) + 1


# ---------------------------------------------------------------------------
# _handle_answer: GAP_TYPE_00 branch
# ---------------------------------------------------------------------------

def test_handle_answer_type00_short_segment_fills_range(binary_search_module):
    """At the default 60s interval, checkpoint segments are <= 1 minute and
    are actually caught by the pre-existing short-segment-fill branch
    before reaching the new GAP_TYPE_00 elif. Confirms that path still
    produces the correct fill for a type-00 checkpoint task."""
    m = binary_search_module
    gui = _new_gui(m)
    checkpoint_task = _make_task(1, 2801, 4600, m.GAP_TYPE_00)
    gui.task_stack = [checkpoint_task]
    m.BinarySearchGUI._load_next_task(gui)

    m.BinarySearchGUI._handle_answer(gui, in_nest=False)
    gui.root.run_all_pending()

    assert all(gui.decisions.get(f) == 0 for f in range(2801, 4601))


def test_handle_answer_type00_long_segment_reaches_new_branch(binary_search_module):
    """A checkpoint segment longer than the short-segment-fill threshold
    (1 minute) must reach the new elif gtype == GAP_TYPE_00 branch and
    fill correctly, not fall through to the Fix 6 else/raise."""
    m = binary_search_module
    gui = _new_gui(m)
    long_checkpoint = _make_task(1, 1, 3600, m.GAP_TYPE_00)  # 2 min at 30fps
    gui.task_stack = [long_checkpoint]
    m.BinarySearchGUI._load_next_task(gui)

    m.BinarySearchGUI._handle_answer(gui, in_nest=True)
    gui.root.run_all_pending()

    assert all(gui.decisions.get(f) == 1 for f in range(1, 3601))


# ---------------------------------------------------------------------------
# Skip and undo/redo: no gap-type-specific code required
# ---------------------------------------------------------------------------

def test_skip_works_on_type00_checkpoint_task(binary_search_module):
    m = binary_search_module
    gui = _new_gui(m)
    checkpoint_task = _make_task(1, 2801, 4600, m.GAP_TYPE_00)
    gui.task_stack = [checkpoint_task]
    m.BinarySearchGUI._load_next_task(gui)

    m.BinarySearchGUI._handle_skip(gui)
    gui.root.run_all_pending()

    assert all(gui.decisions.get(f) == -1 for f in range(2801, 4601))
    assert all(f in gui.explicitly_skipped_frames for f in range(2801, 4601))


def test_undo_redo_round_trip_across_type00_checkpoints(binary_search_module):
    m = binary_search_module
    gui = _new_gui(m)
    gs, ge = 1000, 6552
    df_all = _make_df_all(
        detected_rows=[(gs, 0), (ge, 0)],
        assumed_rows=[(f, -1, gs, ge) for f in range(gs + 1, ge)])
    df_neg = df_all[df_all["IN_NEST"] == -1]
    tasks, *_ = m.build_initial_tasks(df_neg, df_all)
    gui.task_stack = tasks
    m.BinarySearchGUI._load_next_task(gui)

    m.BinarySearchGUI._handle_answer(gui, in_nest=True)
    gui.root.run_all_pending()

    stack_before_undo = [id(t) for t in gui.task_stack]
    m.BinarySearchGUI._go_previous(gui)
    m.BinarySearchGUI._go_next(gui)

    assert [id(t) for t in gui.task_stack] == stack_before_undo


# ---------------------------------------------------------------------------
# Regression: Fix 6 and normal Type 10 flow unaffected
# ---------------------------------------------------------------------------

def test_fix6_still_raises_for_truly_unrecognized_gap_type(binary_search_module):
    m = binary_search_module
    gui = _new_gui(m)
    bad_task = _make_task(1, 1001, 6400, "99")  # not 00, 01, or 10
    gui.task_stack = [bad_task]
    m.BinarySearchGUI._load_next_task(gui)

    with pytest.raises(m.IntegrityError):
        m.BinarySearchGUI._handle_answer(gui, in_nest=True)


def test_regression_type10_normal_flow_unaffected(binary_search_module):
    m = binary_search_module
    gui = _new_gui(m)
    task = _make_task(1, 5051, 6400, m.GAP_TYPE_10)
    gui.task_stack = [task]
    m.BinarySearchGUI._load_next_task(gui)

    m.BinarySearchGUI._handle_answer(gui, in_nest=True)
    gui.root.run_all_pending()

    assert all(gui.decisions.get(f) == 1 for f in range(5051, 6401))
