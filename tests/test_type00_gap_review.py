"""
Tests for Gap Type 00 (Git Issue #19) checkpoint review.

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
  - A normal Type 10 answer is unaffected (regression check).

Uses the `binary_search_module` fixture from tests/conftest.py, which
loads 2.lmt_binary_search.py by path (its filename isn't a valid import
target) and is safe to import directly since its Tk() bootstrap is
already guarded behind `if __name__ == "__main__":`.
"""
import pandas as pd
import pytest

# Helpers
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

# _build_type00_checkpoint_tasks
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

# build_initial_tasks: threshold routing
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

# _handle_answer: GAP_TYPE_00 branch
def test_handle_answer_type00_short_segment_fills_range(binary_search_module):
    """Git Issue #19 (reopened): type-00 checkpoints are excluded from the
    generic short-segment-fill early return (see _handle_answer), so even
    a checkpoint segment <= 1 minute reaches the dedicated GAP_TYPE_00
    elif branch and fills correctly there."""
    m = binary_search_module
    gui = _new_gui(m)
    checkpoint_task = _make_task(1, 2801, 4600, m.GAP_TYPE_00)
    checkpoint_task["show_frame"] = checkpoint_task["seg_end"]
    gui.task_stack = [checkpoint_task]
    m.BinarySearchGUI._load_next_task(gui)

    m.BinarySearchGUI._handle_answer(gui, in_nest=False)
    gui.root.run_all_pending()

    assert all(gui.decisions.get(f) == 0 for f in range(2801, 4601))

def test_handle_answer_type00_long_segment_out_fills_and_continues(binary_search_module):
    """A checkpoint segment longer than the short-segment-fill threshold
    (1 minute), answered OUT, must reach the elif gtype == GAP_TYPE_00
    branch, fill correctly, and NOT trigger the type-10 hand-off (that
    only happens on an IN answer)."""
    m = binary_search_module
    gui = _new_gui(m)
    # Realistic type-00 checkpoint shape: show_frame == seg_end (== gap_end
    # here, since this is the only/last checkpoint), matching what
    # _build_type00_checkpoint_tasks actually produces.
    long_checkpoint = _make_task(1, 1, 3600, m.GAP_TYPE_00)
    long_checkpoint["show_frame"] = long_checkpoint["seg_end"]
    gui.task_stack = [long_checkpoint]
    m.BinarySearchGUI._load_next_task(gui)

    m.BinarySearchGUI._handle_answer(gui, in_nest=False)
    gui.root.run_all_pending()

    assert all(gui.decisions.get(f) == 0 for f in range(1, 3601))
    assert gui.task_stack == []  # no hand-off on an OUT answer

# Skip and undo/redo: no gap-type-specific code required
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
    """OUT answers keep sampling through the remaining precomputed
    checkpoints (t2, t3, t4 stay queued); undo must restore that exact
    remaining-checkpoint stack, and redo must reproduce it again."""
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

    m.BinarySearchGUI._handle_answer(gui, in_nest=False)
    gui.root.run_all_pending()

    stack_before_undo = [id(t) for t in gui.task_stack]
    assert len(stack_before_undo) == 2  # t3, t4 remain queued (t2 now current_task)
    m.BinarySearchGUI._go_previous(gui)
    m.BinarySearchGUI._go_next(gui)

    assert [id(t) for t in gui.task_stack] == stack_before_undo

# Issue #19 (reopened): 00 -> 10 hand-off on first IN checkpoint
def _build_00_gap(m, gs=1000, ge=6552):
    """Real end-to-end checkpoint task list for a single type-00 gap via
    build_initial_tasks: gap interior 1001-6551, checkpoints at
    2800, 4600, 6400, 6551 (4 checkpoints at the default 60s interval)."""
    df_all = _make_df_all(
        detected_rows=[(gs, 0), (ge, 0)],
        assumed_rows=[(f, -1, gs, ge) for f in range(gs + 1, ge)])
    df_neg = df_all[df_all["IN_NEST"] == -1]
    tasks, *_ = m.build_initial_tasks(df_neg, df_all)
    return tasks

def test_in_found_at_first_checkpoint_switches_immediately(binary_search_module):
    m = binary_search_module
    gui = _new_gui(m)
    gui.task_stack = _build_00_gap(m)  # t1: 1001-2800, t2..t4 follow
    m.BinarySearchGUI._load_next_task(gui)  # pops t1

    m.BinarySearchGUI._handle_answer(gui, in_nest=True)
    gui.root.run_all_pending()

    # Sampled portion (t1) filled IN, exactly as the existing fill-left rule.
    assert all(gui.decisions.get(f) == 1 for f in range(1001, 2801))
    # No leftover OUT-checkpoint tasks for this gap anywhere in the stack.
    assert all(t["gap_type"] != m.GAP_TYPE_00 for t in gui.task_stack)
    assert gui.current_task is not None
    assert gui.current_task["gap_type"] == m.GAP_TYPE_10
    assert gui.current_task["seg_start"] == 2801        # mid + 1
    assert gui.current_task["seg_end"]   == 6551         # gap_end, unchanged
    assert gui.current_task["boundary_left"]  == 2800     # the confirmed-IN checkpoint
    assert gui.current_task["boundary_right"] == 6552     # unchanged, original OUT boundary
    assert gui.task_stack == []  # only the hand-off task existed; it's now current

def test_in_found_after_several_checkpoints(binary_search_module):
    m = binary_search_module
    gui = _new_gui(m)
    gui.task_stack = _build_00_gap(m)
    m.BinarySearchGUI._load_next_task(gui)               # pops t1 (1001-2800)

    m.BinarySearchGUI._handle_answer(gui, in_nest=False)  # OUT
    gui.root.run_all_pending()                            # pops t2 (2801-4600)
    m.BinarySearchGUI._handle_answer(gui, in_nest=False)  # OUT
    gui.root.run_all_pending()                            # pops t3 (4601-6400)

    m.BinarySearchGUI._handle_answer(gui, in_nest=True)   # IN at t3
    gui.root.run_all_pending()

    # OUT-answered checkpoints filled OUT exactly as before.
    assert all(gui.decisions.get(f) == 0 for f in range(1001, 4601))
    # IN-answered checkpoint's sampled portion filled IN.
    assert all(gui.decisions.get(f) == 1 for f in range(4601, 6401))
    # t4 (the remaining precomputed OUT-style checkpoint) must be gone --
    # superseded by the hand-off, not left in the stack or shown.
    assert all(t["gap_type"] != m.GAP_TYPE_00 for t in gui.task_stack)
    assert gui.current_task["gap_type"] == m.GAP_TYPE_10
    assert gui.current_task["seg_start"] == 6401  # mid + 1, mid = t3's checkpoint frame
    assert gui.current_task["seg_end"]   == 6551
    assert gui.current_task["boundary_left"] == 6400

def test_in_found_at_final_checkpoint_no_handoff_needed(binary_search_module):
    """When the LAST checkpoint (which always lands exactly on gap_end) is
    the one confirmed IN, there is nothing left to search -- mid + 1 >
    gap_end, so no type-10 subtask is created."""
    m = binary_search_module
    gui = _new_gui(m)
    tasks = _build_00_gap(m)
    gui.task_stack = [tasks[-1]]        # keep only t4 (6401-6551, the final checkpoint)
    m.BinarySearchGUI._load_next_task(gui)  # pops t4

    m.BinarySearchGUI._handle_answer(gui, in_nest=True)
    gui.root.run_all_pending()

    assert all(gui.decisions.get(f) == 1 for f in range(6401, 6552))
    assert gui.task_stack == []
    assert gui.current_task is None  # nothing left to load -- _finish() path

def test_no_in_found_preserves_existing_00_behavior_end_to_end(binary_search_module):
    """Every checkpoint answered OUT: the hand-off never triggers, and the
    whole gap resolves via plain fill-left checkpoint review, exactly like
    the original (non-reopened) implementation."""
    m = binary_search_module
    gui = _new_gui(m)
    gui.task_stack = _build_00_gap(m)
    m.BinarySearchGUI._load_next_task(gui)

    for _ in range(4):  # 4 checkpoints total
        m.BinarySearchGUI._handle_answer(gui, in_nest=False)
        gui.root.run_all_pending()

    assert all(gui.decisions.get(f) == 0 for f in range(1001, 6552))
    assert gui.task_stack == []
    assert gui.current_task is None

def test_00_to_10_handoff_no_duplicate_or_missing_frames(binary_search_module):
    """Full run: two OUT checkpoints, then IN, then binary-search the
    handed-off type-10 section to completion. Every frame in the gap's
    interior must end up decided exactly once, with a coherent OUT-then-IN
    boundary and no frame revisited."""
    m = binary_search_module
    gui = _new_gui(m)
    gs, ge = 1000, 6552
    gui.task_stack = _build_00_gap(m, gs, ge)
    m.BinarySearchGUI._load_next_task(gui)

    m.BinarySearchGUI._handle_answer(gui, in_nest=False)  # t1 OUT
    gui.root.run_all_pending()
    m.BinarySearchGUI._handle_answer(gui, in_nest=False)  # t2 OUT
    gui.root.run_all_pending()
    m.BinarySearchGUI._handle_answer(gui, in_nest=True)   # t3 IN -> hand-off
    gui.root.run_all_pending()

    # Drive the resulting type-10 binary search to completion.
    seen_frames = set()
    guard = 0
    while gui.current_task is not None:
        guard += 1
        assert guard < 1000  # safety valve against an infinite loop bug
        seg_start = gui.current_task["seg_start"]
        seg_end   = gui.current_task["seg_end"]
        assert not (seen_frames & set(range(seg_start, seg_end + 1))), \
            "a frame was reviewed by more than one task"
        seen_frames.update(range(seg_start, seg_end + 1))
        m.BinarySearchGUI._handle_answer(gui, in_nest=False)  # always OUT: converge to gap_end
        gui.root.run_all_pending()

    gap_start, gap_end = gs + 1, ge - 1
    all_frames = set(range(gap_start, gap_end + 1))
    decided_frames = {f for f, v in gui.decisions.items() if gap_start <= f <= gap_end}
    assert decided_frames == all_frames  # every interior frame decided exactly once
    assert gui.decisions[gap_start] == 0
    assert gui.decisions[4600] == 0     # t2's checkpoint, OUT
    assert gui.decisions[6400] == 1     # t3's checkpoint, IN

def test_handoff_task_ordering_goes_to_front_of_stack(binary_search_module):
    """The hand-off subtask must be inserted at the front of the stack (like
    every other binary-search subtask), so it's reviewed immediately next,
    ahead of any other gap's already-queued tasks."""
    m = binary_search_module
    gui = _new_gui(m)
    other_gap_task = _make_task(2, 9000, 9100, m.GAP_TYPE_10)
    tasks = _build_00_gap(m)
    gui.task_stack = tasks + [other_gap_task]
    m.BinarySearchGUI._load_next_task(gui)

    m.BinarySearchGUI._handle_answer(gui, in_nest=True)  # IN at t1
    gui.root.run_all_pending()

    assert gui.current_task["gap_index"] == 1
    assert gui.current_task["gap_type"] == m.GAP_TYPE_10
    assert gui.task_stack == [other_gap_task]  # other gap's task pushed behind it

def test_skip_works_on_handed_off_10_subtask(binary_search_module):
    m = binary_search_module
    gui = _new_gui(m)
    gui.task_stack = _build_00_gap(m)
    m.BinarySearchGUI._load_next_task(gui)

    m.BinarySearchGUI._handle_answer(gui, in_nest=True)  # IN at t1 -> hand-off
    gui.root.run_all_pending()

    m.BinarySearchGUI._handle_skip(gui)
    gui.root.run_all_pending()

    assert all(gui.decisions.get(f) == -1 for f in range(2801, 6552))
    assert all(f in gui.explicitly_skipped_frames for f in range(2801, 6552))

def test_undo_redo_across_00_to_10_handoff(binary_search_module):
    """Undo right after the hand-off must restore the discarded OUT-style
    checkpoint tasks (t2..t4); redo must reproduce the hand-off again."""
    m = binary_search_module
    gui = _new_gui(m)
    gui.task_stack = _build_00_gap(m)
    m.BinarySearchGUI._load_next_task(gui)

    m.BinarySearchGUI._handle_answer(gui, in_nest=True)  # IN at t1 -> hand-off
    gui.root.run_all_pending()
    assert gui.current_task["gap_type"] == m.GAP_TYPE_10

    m.BinarySearchGUI._go_previous(gui)
    # Undo restores the pre-answer state: t1 pending again, t2/t3/t4 back in the stack.
    assert gui.current_task["gap_type"] == m.GAP_TYPE_00
    assert gui.current_task["seg_start"] == 1001
    assert [t["gap_type"] for t in gui.task_stack] == [m.GAP_TYPE_00, m.GAP_TYPE_00, m.GAP_TYPE_00]
    assert all(gui.decisions.get(f) is None for f in range(1001, 2801))

    m.BinarySearchGUI._go_next(gui)
    # Redo reproduces the hand-off exactly.
    assert gui.current_task["gap_type"] == m.GAP_TYPE_10
    assert gui.current_task["seg_start"] == 2801
    assert gui.task_stack == []
    assert all(gui.decisions.get(f) == 1 for f in range(1001, 2801))

# Regression: unrecognized gap_type handling and normal Type 10 flow unaffected
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
