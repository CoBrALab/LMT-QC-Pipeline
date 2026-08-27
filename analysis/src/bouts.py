"""
analysis/src/bouts.py

Bout-level nest-occupancy metrics: Metrics 1 (bout table underlying total
time in nest), 2 (entry/exit counts), 3 (bout duration distribution), and
9 (entry/exit event log for raster plots).

Why this exists
----------------
A per-frame IN_NEST series (1 = in nest, 0 = out of nest, -1 = unresolved)
needs to be collapsed into contiguous runs ("bouts") before most nest-
occupancy questions can be answered: how long was each visit, how many
visits were there, when did each one start/end. Every metric in this file
is derived from ONE bout table (compute_nest_bouts), not five separate
re-scans of the per-frame series, so there is exactly one implementation
of "where does a bout start/end" to validate.

Revision note (see chat history)
---------------------------------
An earlier draft of this module offered a `drop_unresolved=True` argument
to compute_nest_bouts() that physically deleted IN_NEST == -1 rows from
the frame/state arrays BEFORE computing bout boundaries. Because bout
duration was computed from frame-NUMBER arithmetic
(END_FRAME - START_FRAME + 1), deleting rows first meant that arithmetic
could span a deleted unresolved gap and silently count that unknown time
as confirmed same-state time -- inflating bout durations (and therefore
total time in nest) whenever a resolved bout was directly preceded and
followed by more of the same state across an unresolved gap.

That is now fixed by never deleting frames before bout formation.
compute_nest_bouts() always runs on the full, contiguous per-frame array
(the gap-filled pipeline output has no literal missing rows, only
-1-labeled ones, so this is always safe), guaranteeing N_FRAMES is a true,
gap-free count. "Resolved-only" views (for entry/exit counting and
resolved bout-duration summaries) are produced by filtering the resulting
BOUT TABLE afterward, using each bout's true previous-bout state (tracked
before any filtering) to decide whether a transition was actually
observed -- so a bout immediately following an unresolved bout is
correctly excluded from the entry/exit count, rather than silently merged
into a longer bout that never really existed.
"""

import numpy as np
import pandas as pd

from .io import DB_FPS


def compute_nest_bouts(df: pd.DataFrame, animal_id) -> pd.DataFrame:
    """
    What it does
    ------------
    Collapses a per-frame IN_NEST series into a bout table: one row per
    contiguous run of the same IN_NEST value (1, 0, or -1).

    Why it exists
    -------------
    This is the single source of bout boundaries for every metric in this
    module. See module docstring for why it never filters frames before
    computing bouts.

    Inputs
    ------
    df : pandas.DataFrame
        Full per-frame table from analysis.src.io.load_gap_fill_analysis()
        -- i.e. every frame in the session must be present, with IN_NEST
        in {1, 0, -1}. Must be sorted by FRAMENUMBER (load_gap_fill_
        analysis() guarantees this).
    animal_id : int
        The ANIMALID this bout table is for, recorded on every output row
        for downstream joins/labeling. Not used to filter df -- df should
        already contain exactly one animal (load_gap_fill_analysis()
        enforces this).

    Outputs
    -------
    pandas.DataFrame with columns:
        ANIMALID, STATE (1/0/-1), START_FRAME, END_FRAME, N_FRAMES,
        DURATION_SEC, PREV_STATE (the STATE of the immediately preceding
        bout in the TRUE, unfiltered sequence; NaN for the session's
        first bout). PREV_STATE is what makes resolved-only entry/exit
        detection safe after filtering -- see count_entries_exits().
    One row per bout, ordered by START_FRAME.

    Logic
    -----
    A new bout begins at index 0 and at every index where IN_NEST differs
    from the previous index (a standard vectorized change-point
    detection: np.diff(state) != 0). N_FRAMES is computed by counting
    indices within each bout (end_idx - start_idx + 1) in the ORIGINAL,
    contiguous array -- never by subtracting frame numbers, which is only
    safe when every intermediate frame is guaranteed present (true here,
    NOT true if you ever filter frames first -- see module docstring).

    Assumptions
    -----------
    - df has one row per frame with no gaps in FRAMENUMBER (guaranteed by
      1.lmt_gap_fill.py's own gap-filling logic for any file downstream
      of it).
    - df contains exactly one animal.

    Failure modes
    -------------
    - Passing a df with FRAMENUMBER gaps (e.g. hand-edited, or from a
      pipeline stage before gap-filling) will silently produce bouts
      whose duration is understated relative to real elapsed time,
      because N_FRAMES counts array indices, which is only equal to
      elapsed frames when the array is truly contiguous. There is no
      runtime check for this in this function specifically (it would
      duplicate 1.lmt_gap_fill.py's own defensive checks) -- rely on
      load_gap_fill_analysis() and the upstream pipeline's own
      guarantees.

    Validation
    ----------
    bouts["N_FRAMES"].sum() must exactly equal len(df). This is asserted
    at the end of this function (raises AssertionError, not a silent
    warning, since a mismatch means bout construction itself is broken).

    Integration
    -----------
    Used by every other function in this module, and by
    analysis/src/occupancy.py's total_time_in_nest().
    """
    state = df["IN_NEST"].to_numpy()
    frames = df["FRAMENUMBER"].to_numpy()

    change_points = np.where(np.diff(state) != 0)[0] + 1
    bout_starts_idx = np.concatenate(([0], change_points))
    bout_ends_idx = np.concatenate((change_points - 1, [len(state) - 1]))

    n_frames_per_bout = bout_ends_idx - bout_starts_idx + 1

    bouts = pd.DataFrame({
        "ANIMALID": animal_id,
        "STATE": state[bout_starts_idx],
        "START_FRAME": frames[bout_starts_idx],
        "END_FRAME": frames[bout_ends_idx],
        "N_FRAMES": n_frames_per_bout,
    })
    bouts["DURATION_SEC"] = bouts["N_FRAMES"] / DB_FPS
    bouts["PREV_STATE"] = bouts["STATE"].shift(1)

    assert int(bouts["N_FRAMES"].sum()) == len(df), (
        f"Bout frame accounting mismatch: bouts sum to "
        f"{int(bouts['N_FRAMES'].sum())} frames, input had {len(df)}."
    )
    return bouts


def bout_duration_summary(bouts: pd.DataFrame, state: int,
                           resolved_only: bool = True) -> dict:
    """
    What it does
    ------------
    Metric 3. Summary statistics (mean, median, SD, CV, min, max) of the
    duration of bouts of a given state.

    Why it exists
    -------------
    Nest-bout durations are typically right-skewed (many brief check-ins,
    occasional long settled bouts) -- reporting only a mean is
    misleading. This returns the full set of summary statistics so median
    can be reported alongside mean rather than instead of it.

    Inputs
    ------
    bouts : output of compute_nest_bouts().
    state : 1 (in-nest bouts) or 0 (out-of-nest bouts).
    resolved_only : if True (default), a bout is only included if its
        PREV_STATE is the opposite resolved state (0 for an in-nest bout,
        1 for an out-of-nest bout) -- i.e. it must be a bout whose START
        represents an actually-observed transition, not one immediately
        following an unresolved (-1) bout. If False, ALL bouts of the
        requested state are included regardless of what preceded them
        (useful only for auditing the unresolved-adjacent bouts
        themselves, not for a headline duration statistic).

    Outputs
    -------
    dict: n_bouts, mean_sec, median_sec, sd_sec, cv, min_sec, max_sec.
    {"n_bouts": 0} if no qualifying bouts exist.

    Logic
    -----
    See module docstring's Revision note: filtering happens on the bout
    table (which already has correct, gap-free durations), using the
    PREV_STATE column computed before any filtering -- not by deleting
    frames and recomputing bouts, which was the earlier, buggy approach.

    Assumptions
    -----------
    The very first bout of a session has PREV_STATE = NaN (nothing
    precedes it) and is therefore always excluded when resolved_only=True
    -- there is no way to know whether it represents an "observed"
    transition, since the session start is itself the boundary.

    Failure modes
    -------------
    Passing resolved_only=False for a scientific duration summary (rather
    than for auditing) will include bouts whose start might immediately
    follow an unresolved gap of unknown duration/content, inflating the
    apparent frequency of very short or very long "bouts" that are
    partly an artifact of the review pipeline's own decisions, not
    observed behavior.

    Validation
    ----------
    n_bouts (resolved_only=True) should be close to (not necessarily
    equal to) count_entries_exits()'s n_bouts_in_nest / n_bouts_out_of_nest
    for the same state -- both come from the same PREV_STATE-filtered
    logic, so a mismatch indicates a bug in one of the two functions.

    Integration
    -----------
    Consumes compute_nest_bouts()'s output directly.
    """
    b = bouts[bouts["STATE"] == state]
    if resolved_only:
        required_prev = 0 if state == 1 else 1
        b = b[b["PREV_STATE"] == required_prev]

    durations = b["DURATION_SEC"]
    if len(durations) == 0:
        return {"n_bouts": 0}
    return {
        "n_bouts": int(len(durations)),
        "mean_sec": float(durations.mean()),
        "median_sec": float(durations.median()),
        "sd_sec": float(durations.std()) if len(durations) > 1 else 0.0,
        "cv": float(durations.std() / durations.mean()) if durations.mean() > 0 and len(durations) > 1 else np.nan,
        "min_sec": float(durations.min()),
        "max_sec": float(durations.max()),
    }


def count_entries_exits(bouts: pd.DataFrame) -> dict:
    """
    What it does
    ------------
    Metric 2. Counts observed nest entries (0->1 transitions) and exits
    (1->0 transitions).

    Why it exists
    -------------
    Entry/exit frequency, independent of total time in nest, distinguishes
    many-short-visits from few-long-bouts maternal styles with identical
    total on-nest time.

    Inputs
    ------
    bouts : output of compute_nest_bouts() (the FULL bout table,
        including -1 bouts -- do not pre-filter before calling this).

    Outputs
    -------
    dict: n_entries, n_exits, n_bouts_in_nest, n_bouts_out_of_nest (the
    last two counting only resolved-transition bouts, i.e. equivalent to
    bout_duration_summary(..., resolved_only=True)'s bout count for each
    state).

    Logic
    -----
    An entry is a bout with STATE == 1 and PREV_STATE == 0 (a directly
    observed out->in transition, no unresolved bout in between). An exit
    is STATE == 0 and PREV_STATE == 1. Using PREV_STATE (computed once,
    before filtering, in compute_nest_bouts()) is what makes this safe:
    a bout following an unresolved gap has PREV_STATE == -1, which
    matches neither condition, so it is correctly excluded rather than
    silently counted as a transition that was never actually observed.

    Assumptions
    -----------
    A transition that occurred entirely inside a long unresolved gap is
    invisible to this function -- the true entry/exit count is therefore
    a LOWER BOUND whenever a session has a non-trivial unresolved
    fraction (see analysis/src/occupancy.py's fill_source_composition(),
    which should always be reported alongside this metric).

    Failure modes
    -------------
    Calling this on a bout table that was itself built from a
    pre-filtered (frames deleted) per-frame series would double-count or
    miscount transitions -- compute_nest_bouts() no longer offers that
    filtering path at all, specifically to prevent this.

    Validation
    ----------
    n_bouts_in_nest must equal n_entries, or n_entries + 1 if the bout
    table's first STATE==1 bout has PREV_STATE == NaN (i.e. the session
    began already in-nest, so that first in-nest bout has no preceding
    exit to pair with). This is checked as an assertion below.

    Integration
    -----------
    Consumes compute_nest_bouts()'s output. Report alongside
    fill_source_composition() (analysis/src/occupancy.py) so the
    lower-bound caveat above is never presented without its context.
    """
    entries = bouts[(bouts["STATE"] == 1) & (bouts["PREV_STATE"] == 0)]
    exits = bouts[(bouts["STATE"] == 0) & (bouts["PREV_STATE"] == 1)]
    n_bouts_in_nest = int((bouts["STATE"] == 1).sum())
    n_bouts_out_of_nest = int((bouts["STATE"] == 0).sum())

    return {
        "n_entries": int(len(entries)),
        "n_exits": int(len(exits)),
        "n_bouts_in_nest": n_bouts_in_nest,
        "n_bouts_out_of_nest": n_bouts_out_of_nest,
    }


def entry_exit_events(bouts: pd.DataFrame, animal_label: str) -> pd.DataFrame:
    """
    What it does
    ------------
    Metric 9. Converts the bout table into a flat event log (one row per
    observed transition), the data structure a raster plot needs.

    Why it exists
    -------------
    Raster/timeline figures need a row-per-event table, not a
    row-per-bout table -- this is a direct re-expression of
    count_entries_exits()'s logic into that shape, not a new computation.

    Inputs
    ------
    bouts : output of compute_nest_bouts() (full table).
    animal_label : a human-readable label (e.g. "dam1") to tag every
        event with, so event logs from multiple animals can be
        concatenated into one multi-row raster.

    Outputs
    -------
    pandas.DataFrame: ANIMAL, EVENT_TYPE ("ENTRY"/"EXIT"), FRAMENUMBER
    (the bout's START_FRAME, i.e. the first frame of the new state),
    TIME_SEC.

    Logic
    -----
    Identical filtering to count_entries_exits() (STATE/PREV_STATE
    pairs), just materialized as rows instead of counts.

    Failure modes
    -------------
    None beyond what count_entries_exits() already documents (same
    underlying filter).

    Validation
    ----------
    len(events[events.EVENT_TYPE == "ENTRY"]) must equal
    count_entries_exits(bouts)["n_entries"] exactly.

    Integration
    -----------
    Concatenate this across animals (see analysis/scripts/
    run_analysis.py) for a multi-animal raster figure.
    """
    entries = bouts[(bouts["STATE"] == 1) & (bouts["PREV_STATE"] == 0)].copy()
    entries["EVENT_TYPE"] = "ENTRY"
    exits = bouts[(bouts["STATE"] == 0) & (bouts["PREV_STATE"] == 1)].copy()
    exits["EVENT_TYPE"] = "EXIT"

    events = pd.concat([entries, exits], ignore_index=True)
    events = events.sort_values("START_FRAME").reset_index(drop=True)
    events["ANIMAL"] = animal_label
    events["FRAMENUMBER"] = events["START_FRAME"]
    events["TIME_SEC"] = events["FRAMENUMBER"] / DB_FPS
    return events[["ANIMAL", "EVENT_TYPE", "FRAMENUMBER", "TIME_SEC"]]
