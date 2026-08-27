import pandas as pd
import pytest

from analysis.src.bouts import (
    compute_nest_bouts,
    bout_duration_summary,
    count_entries_exits,
    entry_exit_events,
)
from analysis.tests.conftest import make_frames


def _bouts_from_sequence(state_sequence, animal_id=101):
    df = pd.DataFrame(make_frames(state_sequence, animal_id=animal_id))
    return compute_nest_bouts(df, animal_id)


# compute_nest_bouts: basic correctness

def test_simple_alternating_bouts():
    bouts = _bouts_from_sequence([(0, 3), (1, 5), (0, 2)])
    assert list(bouts["STATE"]) == [0, 1, 0]
    assert list(bouts["N_FRAMES"]) == [3, 5, 2]
    # Frame accounting: total must equal 3+5+2 = 10 frames.
    assert bouts["N_FRAMES"].sum() == 10


def test_one_frame_bouts():
    # Edge case: single-frame bouts on both sides of a longer bout.
    bouts = _bouts_from_sequence([(0, 1), (1, 1), (0, 1)])
    assert list(bouts["N_FRAMES"]) == [1, 1, 1]
    assert bouts["N_FRAMES"].sum() == 3


def test_session_begins_in_nest():
    # Edge case explicitly requested: first bout has STATE == 1 with no
    # preceding bout at all (PREV_STATE must be NaN, not misread as 0).
    bouts = _bouts_from_sequence([(1, 4), (0, 3)])
    assert bouts.iloc[0]["STATE"] == 1
    assert pd.isna(bouts.iloc[0]["PREV_STATE"])


def test_session_ends_in_nest():
    # Edge case explicitly requested: last bout has STATE == 1; must not
    # be dropped or mishandled just because there's no bout after it.
    bouts = _bouts_from_sequence([(0, 3), (1, 4)])
    assert bouts.iloc[-1]["STATE"] == 1
    assert bouts.iloc[-1]["N_FRAMES"] == 4


def test_single_bout_whole_session():
    # Degenerate case: the entire session is one uninterrupted state.
    bouts = _bouts_from_sequence([(1, 10)])
    assert len(bouts) == 1
    assert bouts.iloc[0]["N_FRAMES"] == 10


def test_bout_frame_accounting_matches_input_length():
    # General invariant, checked across a less trivial sequence: total
    # bout N_FRAMES must always equal the number of input rows.
    seq = [(1, 7), (0, 2), (1, 1), (-1, 5), (0, 9), (1, 3)]
    df = pd.DataFrame(make_frames(seq))
    bouts = compute_nest_bouts(df, animal_id=101)
    assert bouts["N_FRAMES"].sum() == len(df)


# The bug this module was rewritten to fix: unresolved (-1) frames must
# never be silently absorbed into a neighboring resolved bout's duration.

def test_unresolved_gap_is_not_merged_into_surrounding_bouts():
    # 3 frames in-nest, 6 frames unresolved, 3 frames in-nest.
    # A buggy frame-arithmetic implementation would report this as ONE
    # 12-frame in-nest bout. The correct behavior is THREE bouts: a
    # 3-frame in-nest bout, a 6-frame unresolved bout, and a separate
    # 3-frame in-nest bout -- with the unresolved bout's frames excluded
    # from any "resolved in-nest time" total.
    bouts = _bouts_from_sequence([(1, 3), (-1, 6), (1, 3)])
    assert list(bouts["STATE"]) == [1, -1, 1]
    assert list(bouts["N_FRAMES"]) == [3, 6, 3]

    resolved_in_nest_frames = bouts.loc[bouts["STATE"] == 1, "N_FRAMES"].sum()
    assert resolved_in_nest_frames == 6  # 3 + 3, NOT 12.


def test_long_unresolved_gap_duration_is_correct():
    # A long unresolved gap must report its OWN correct duration (not
    # be silently split or merged into neighboring bouts).
    bouts = _bouts_from_sequence([(1, 2), (-1, 100), (0, 2)])
    unresolved_bout = bouts[bouts["STATE"] == -1].iloc[0]
    assert unresolved_bout["N_FRAMES"] == 100


def test_entries_and_bout_duration_exclude_gap_adjacent_bouts():
    # A bout immediately following an unresolved gap must NOT be counted
    # as an observed entry, and must NOT be included in a resolved-only
    # bout-duration summary -- because we never actually observed the
    # 0->1 transition; it happened somewhere inside the unresolved gap.
    bouts = _bouts_from_sequence([(0, 3), (-1, 5), (1, 4)])
    result = count_entries_exits(bouts)
    assert result["n_entries"] == 0  # NOT 1 -- the transition wasn't observed.

    duration_stats = bout_duration_summary(bouts, state=1, resolved_only=True)
    assert duration_stats["n_bouts"] == 0


# count_entries_exits / entry_exit_events
def test_count_entries_exits_simple_case():
    bouts = _bouts_from_sequence([(0, 3), (1, 5), (0, 2), (1, 4)])
    result = count_entries_exits(bouts)
    assert result["n_entries"] == 2
    assert result["n_exits"] == 1
    assert result["n_bouts_in_nest"] == 2
    assert result["n_bouts_out_of_nest"] == 2


def test_count_entries_exits_session_begins_in_nest():
    # The first in-nest bout has no preceding bout at all, so it must
    # NOT be counted as an entry (there is nothing to transition from).
    bouts = _bouts_from_sequence([(1, 4), (0, 3), (1, 2)])
    result = count_entries_exits(bouts)
    assert result["n_bouts_in_nest"] == 2
    assert result["n_entries"] == 1  # only the second in-nest bout is a real entry


def test_entry_exit_events_matches_counts():
    bouts = _bouts_from_sequence([(0, 3), (1, 5), (0, 2), (1, 4)])
    events = entry_exit_events(bouts, animal_label="dam_101")
    counts = count_entries_exits(bouts)
    assert (events["EVENT_TYPE"] == "ENTRY").sum() == counts["n_entries"]
    assert (events["EVENT_TYPE"] == "EXIT").sum() == counts["n_exits"]
    assert set(events["ANIMAL"]) == {"dam_101"}


# bout_duration_summary
def test_bout_duration_summary_empty_state():
    # Edge case: an animal that is NEVER in-nest.
    bouts = _bouts_from_sequence([(0, 10)])
    result = bout_duration_summary(bouts, state=1, resolved_only=False)
    assert result == {"n_bouts": 0}


def test_bout_duration_summary_single_bout_no_sd():
    # Edge case: exactly one qualifying bout -- SD should be 0, not NaN
    # or an error, and resolved_only=False includes the first bout even
    # though PREV_STATE is NaN.
    bouts = _bouts_from_sequence([(1, 5)])
    result = bout_duration_summary(bouts, state=1, resolved_only=False)
    assert result["n_bouts"] == 1
    assert result["sd_sec"] == 0.0



# empty input
def test_compute_nest_bouts_empty_dataframe_raises_index_error():
    # An empty per-frame table has no valid bout to anchor on. This is
    # documented behavior (not silently returning an empty bout table),
    # since an empty GAP_FILL_ANALYSIS table is itself rejected earlier,
    # by analysis.src.io.load_gap_fill_analysis(), before it would ever
    # reach this function in the normal pipeline flow.
    df = pd.DataFrame(columns=["FRAMENUMBER", "IN_NEST", "ANIMALID"])
    with pytest.raises(IndexError):
        compute_nest_bouts(df, animal_id=101)
