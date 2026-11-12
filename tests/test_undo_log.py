"""
Tests for the undo/redo log of BinarySearchGUI (Git Issue #33).

History entries no longer hold full copies of self.decisions and
self.explicitly_skipped_frames. They hold only the frame ranges that each
action wrote. These tests compare the GUI against a reference timeline of
full state copies, made by the test itself, through long random sequences of
answer / skip / undo / redo actions. They also check that the log size stays
proportional to the frames written, not to (answers x decided frames).
"""

import random

import pandas as pd
import pytest


class _FakeRoot:
    def __init__(self):
        self.pending = []

    def after(self, ms, callback):
        self.pending.append(callback)

    def run_all_pending(self):
        while self.pending:
            self.pending.pop(0)()

    def quit(self):
        pass


def _new_gui(m):
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
    gui.finished = False

    def _finish():
        gui.finished = True

    gui._finish = _finish
    return gui


def _make_df_all(detected_rows, gaps):
    """detected_rows: list of (frame, in_nest). gaps: list of (gs, ge), whose
    interior frames gs+1..ge-1 are added as ASSUMED IN_NEST = -1 rows."""
    records = [
        {
            "FRAMENUMBER": f,
            "IN_NEST": v,
            "ASSUMPTION_TYPE": "DETECTED",
            "GAP_START_FRAME": None,
            "GAP_END_FRAME": None,
        }
        for f, v in detected_rows
    ]
    for gs, ge in gaps:
        records.extend(
            {
                "FRAMENUMBER": f,
                "IN_NEST": -1,
                "ASSUMPTION_TYPE": "ASSUMED",
                "GAP_START_FRAME": gs,
                "GAP_END_FRAME": ge,
            }
            for f in range(gs + 1, ge)
        )
    return pd.DataFrame.from_records(records)


def _build_tasks(m):
    """One gap of each reviewed type: 10 (in -> out), 01 (out -> in) and
    00 (out -> out, checkpoint review), each several minutes long."""
    detected = [
        (1000, 1),
        (10000, 0),  # type 10
        (20000, 0),
        (29000, 1),  # type 01
        (40000, 0),
        (49000, 0),  # type 00
    ]
    gaps = [(1000, 10000), (20000, 29000), (40000, 49000)]
    df_all = _make_df_all(detected, gaps)
    df_neg = df_all[df_all["IN_NEST"] == -1]
    tasks, *_ = m.build_initial_tasks(df_neg, df_all)
    assert {t["gap_type"] for t in tasks} == {
        m.GAP_TYPE_10,
        m.GAP_TYPE_01,
        m.GAP_TYPE_00,
    }
    return tasks, gaps


def _task_key(task):
    return None if task is None else tuple(sorted(task.items()))


def _state(gui):
    """A full, independent copy of the state that undo/redo must restore."""
    return (
        dict(gui.decisions),
        frozenset(gui.explicitly_skipped_frames),
        _task_key(gui.current_task),
        [_task_key(t) for t in gui.task_stack],
    )


def _run_random_session(m, seed):
    """Drive one review session with random actions. A reference timeline of
    full state copies (the old snapshot method) is kept next to the GUI, and
    the GUI state must match the timeline at each step."""
    rng = random.Random(seed)
    gui = _new_gui(m)
    tasks, gaps = _build_tasks(m)
    gui.task_stack = tasks
    m.BinarySearchGUI._load_next_task(gui)

    timeline = [_state(gui)]
    cursor = 0

    for _ in range(5000):
        if gui.finished:
            break
        assert _state(gui) == timeline[cursor]
        action = rng.choices(
            ["in", "out", "skip", "undo", "redo", "undo_while_pending"],
            weights=[30, 30, 5, 15, 15, 5],
        )[0]

        if action in ("in", "out", "skip", "undo_while_pending"):
            if action == "skip":
                m.BinarySearchGUI._handle_skip(gui)
            else:
                m.BinarySearchGUI._handle_answer(gui, in_nest=(action != "out"))

            if action == "undo_while_pending":
                # Undo in the 300 ms window before the next task loads: the
                # answer is reverted, there is nothing to redo, and the
                # stale callback must do nothing.
                m.BinarySearchGUI._go_previous(gui)
                del timeline[cursor + 1 :]
                assert _state(gui) == timeline[cursor]
                assert gui.redo_stack == []
                gui.root.run_all_pending()
                assert _state(gui) == timeline[cursor]
                continue

            gui.root.run_all_pending()
            del timeline[cursor + 1 :]
            timeline.append(_state(gui))
            cursor += 1

        elif action == "undo":
            m.BinarySearchGUI._go_previous(gui)
            cursor = max(cursor - 1, 0)

        elif action == "redo":
            m.BinarySearchGUI._go_next(gui)
            cursor = min(cursor + 1, len(timeline) - 1)

        assert _state(gui) == timeline[cursor]

    assert gui.finished, "session did not reach _finish() within the step limit"
    # Every gap frame has a decision at the end of the session.
    interior = set().union(*(range(gs + 1, ge) for gs, ge in gaps))
    assert set(gui.decisions) == interior
    return gui


@pytest.mark.parametrize("seed", range(25))
def test_random_session_matches_full_snapshot_reference(binary_search_module, seed):
    _run_random_session(binary_search_module, seed)


def test_undo_after_finish_is_not_needed_but_history_is_consistent(
    binary_search_module,
):
    """After a full session, undoing every step returns to an empty state."""
    m = binary_search_module
    gui = _run_random_session(m, seed=1)
    while gui.history:
        m.BinarySearchGUI._go_previous(gui)
    assert gui.decisions == {}
    assert gui.explicitly_skipped_frames == set()


def test_undo_restores_frames_written_twice(binary_search_module):
    """The log must not depend on ranges being disjoint: a write over frames
    that already have a decision is reverted to the old values."""
    m = binary_search_module
    gui = _new_gui(m)
    gui.decisions = {f: 0 for f in range(100, 200)}
    gui.explicitly_skipped_frames = {150}

    writes = []
    m.BinarySearchGUI._fill_range(gui, writes, 150, 250, 1)
    m.BinarySearchGUI._fill_range(gui, writes, 120, 160, -1, skipped=True)
    assert (
        gui.decisions[130] == -1 and gui.decisions[170] == 1 and gui.decisions[240] == 1
    )
    assert gui.explicitly_skipped_frames == set(range(120, 161))

    m.BinarySearchGUI._undo_writes(gui, writes)
    assert gui.decisions == {f: 0 for f in range(100, 200)}
    assert gui.explicitly_skipped_frames == {150}

    m.BinarySearchGUI._redo_writes(gui, writes)
    assert (
        gui.decisions[130] == -1 and gui.decisions[170] == 1 and gui.decisions[240] == 1
    )
    assert gui.explicitly_skipped_frames == set(range(120, 161))


def test_history_size_is_proportional_to_frames_written(binary_search_module):
    """Git Issue #33: each history entry used to hold a full copy of
    self.decisions, so the history grew with (answers x decided frames).
    Now the stored old values stay empty when ranges are disjoint (the
    normal case), and the total frames recorded equals the frames decided."""
    m = binary_search_module
    gui = _new_gui(m)
    tasks, _ = _build_tasks(m)
    gui.task_stack = tasks
    m.BinarySearchGUI._load_next_task(gui)

    answers = 0
    while not gui.finished:
        m.BinarySearchGUI._handle_answer(gui, in_nest=(answers % 2 == 0))
        gui.root.run_all_pending()
        answers += 1
    assert answers > 10

    all_writes = [w for _task, _stack, writes in gui.history for w in writes]
    stored_old_values = sum(
        len(prev_values) for *_, prev_values, _prev_skipped in all_writes
    )
    frames_recorded = sum(end - start + 1 for start, end, *_ in all_writes)
    assert stored_old_values == 0
    assert frames_recorded == len(gui.decisions)
