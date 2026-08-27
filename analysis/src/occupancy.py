"""
analysis/src/occupancy.py

Single-animal occupancy metrics that operate directly on the full
per-frame table (not the bout table): Metric 1's headline time-in-nest
statistic, Metric 4 (binned occupancy timeline), and Metric 6
(fill-source/tracking-confidence composition, a QC covariate rather than
a behavioral result).
"""

import numpy as np
import pandas as pd

from .io import DB_FPS
from .bouts import compute_nest_bouts


def total_time_in_nest(bouts: pd.DataFrame) -> dict:
    """
    What it does
    ------------
    Metric 1. Total (and fractional) time in nest, out of nest, and
    unresolved, for one animal.

    Why it exists
    -------------
    This is the most basic maternal/social-behavior proxy the pipeline
    supports, and the foundation every other occupancy metric builds on
    or is compared against.

    Inputs
    ------
    bouts : output of analysis.src.bouts.compute_nest_bouts() -- the FULL
        bout table (all three states). Passing a pre-filtered table would
        silently exclude unresolved time from seconds_unresolved instead
        of reporting it, defeating the purpose of this function.

    Outputs
    -------
    dict: seconds_in_nest, seconds_out_of_nest, seconds_unresolved,
    total_seconds, fraction_in_nest, fraction_unresolved.

    Logic
    -----
    fraction_in_nest is computed over RESOLVED time only
    (seconds_in_nest / (seconds_in_nest + seconds_out_of_nest)), not over
    total session time. Dividing by total time would let a high
    unresolved fraction silently understate both in-nest and out-of-nest
    time symmetrically, making the estimate look more "average" rather
    than flagging that it's less certain. fraction_unresolved is reported
    as its own explicit number specifically so this can't happen.

    Assumptions
    -----------
    bouts came from the FULL (unfiltered) compute_nest_bouts() output --
    see Inputs above.

    Failure modes
    -------------
    A near-zero total_seconds (e.g. an almost-empty input file) will
    produce NaN for the fraction fields rather than a divide-by-zero
    exception -- check total_seconds before trusting the fractions.

    Validation
    ----------
    seconds_unresolved / DB_FPS should match the unresolved-frame count
    in that run's own LMT_Summary_A<id>_*.txt report from
    2.lmt_binary_search.py (script 2's own audit trail) -- any
    discrepancy means this function's accounting and the pipeline's own
    have diverged, and should be treated as a bug until explained.

    Integration
    -----------
    Called once per animal by analysis/scripts/run_analysis.py,
    alongside fill_source_composition() below so both are always reported
    together.
    """
    sec_in = float(bouts.loc[bouts["STATE"] == 1, "DURATION_SEC"].sum())
    sec_out = float(bouts.loc[bouts["STATE"] == 0, "DURATION_SEC"].sum())
    sec_unresolved = float(bouts.loc[bouts["STATE"] == -1, "DURATION_SEC"].sum())
    total = sec_in + sec_out + sec_unresolved
    resolved = sec_in + sec_out
    return {
        "seconds_in_nest": sec_in,
        "seconds_out_of_nest": sec_out,
        "seconds_unresolved": sec_unresolved,
        "total_seconds": total,
        "fraction_in_nest": (sec_in / resolved) if resolved > 0 else np.nan,
        "fraction_unresolved": (sec_unresolved / total) if total > 0 else np.nan,
    }


def occupancy_timeline(df: pd.DataFrame, bin_seconds: float = 60.0) -> pd.DataFrame:
    """
    What it does
    ------------
    Metric 4. Bins a per-frame IN_NEST series into fixed-width time
    windows, reporting the fraction of each window in each state.

    Why it exists
    -------------
    Every occupancy-over-time figure (rasters, heatmaps,
    coordination plots) needs a regular, binned time series, not
    ~30 x session-length raw frame points. This is that shared data
    structure.

    Inputs
    ------
    df : the FULL per-frame table from
        analysis.src.io.load_gap_fill_analysis() (frame-level resolution
        input, NOT the bout table -- binning needs every frame, not
        pre-collapsed bouts).
    bin_seconds : width of each bin. 60s is reasonable for a multi-hour
        session; use something finer (e.g. 10s) for a short session or
        when the figure needs to resolve individual bouts. Sanity-check
        this choice against bout_duration_summary()'s output for the same
        animal before committing to a value -- a bin much longer than
        typical bout duration will wash out the very signal you want to
        see.

    Outputs
    -------
    pandas.DataFrame indexed by BIN_INDEX with BIN_START_SEC,
    BIN_END_SEC, FRAC_IN_NEST, FRAC_OUT_OF_NEST, FRAC_UNRESOLVED,
    N_FRAMES.

    Logic
    -----
    Each frame is assigned a bin index via integer division of its
    time-in-seconds by bin_seconds; within each bin, the fraction of
    frames in each IN_NEST state is computed via a vectorized
    groupby/value_counts/unstack, not a per-frame loop.

    Assumptions
    -----------
    df is sorted by FRAMENUMBER and contiguous (guaranteed by
    load_gap_fill_analysis()).

    Failure modes
    -------------
    The LAST bin is typically partial (session length is rarely an exact
    multiple of bin_seconds) -- its N_FRAMES will be lower than other
    bins. This function does not drop or flag it automatically; check
    N_FRAMES before treating every bin as equally reliable in a figure or
    statistic.

    Validation
    ----------
    out[["FRAC_IN_NEST","FRAC_OUT_OF_NEST","FRAC_UNRESOLVED"]].sum(axis=1)
    must equal 1.0 for every bin (within floating-point tolerance);
    out["N_FRAMES"].sum() must equal len(df). Both are asserted below.

    Integration
    -----------
    Feeds any future occupancy-heatmap and coordination figures; also
    the natural input for time-of-day analysis once a
    session-start wall-clock timestamp is supplied externally (the LMT
    database itself gives no such anchor beyond frame number).
    """
    frame_sec = df["FRAMENUMBER"].to_numpy() / DB_FPS
    bin_idx = np.floor(frame_sec / bin_seconds).astype(int)

    tmp = pd.DataFrame({"BIN_INDEX": bin_idx, "IN_NEST": df["IN_NEST"].to_numpy()})
    grouped = tmp.groupby("BIN_INDEX")["IN_NEST"].value_counts().unstack(fill_value=0)

    for col in [1, 0, -1]:
        if col not in grouped.columns:
            grouped[col] = 0

    n_frames = grouped.sum(axis=1)
    out = pd.DataFrame({
        "BIN_START_SEC": grouped.index * bin_seconds,
        "BIN_END_SEC": (grouped.index + 1) * bin_seconds,
        "FRAC_IN_NEST": grouped[1] / n_frames,
        "FRAC_OUT_OF_NEST": grouped[0] / n_frames,
        "FRAC_UNRESOLVED": grouped[-1] / n_frames,
        "N_FRAMES": n_frames,
    }).reset_index(drop=True)

    frac_sum = out[["FRAC_IN_NEST", "FRAC_OUT_OF_NEST", "FRAC_UNRESOLVED"]].sum(axis=1)
    assert np.allclose(frac_sum, 1.0), "Per-bin fractions do not sum to 1.0."
    assert int(out["N_FRAMES"].sum()) == len(df), (
        f"Binned frame count {int(out['N_FRAMES'].sum())} != input length {len(df)}."
    )
    return out


def fill_source_composition(df: pd.DataFrame) -> pd.DataFrame:
    """
    What it does
    ------------
    Metric 6. Summarizes what fraction of an animal's timeline came from
    direct detection vs. each fallback mechanism (LOGIC / BINARY_SEARCH /
    UNKNOWN), as a standalone, reportable QC statistic.

    Why it exists
    -------------
    Every metric elsewhere in this package inherits its reliability from
    this breakdown. It's not a behavioral result -- it's the data-quality
    covariate that should accompany every figure and statistical model in
    later tasks, so a reader can judge how much of a reported number rests
    on direct observation vs. inference.

    Inputs
    ------
    df : full per-frame table from load_gap_fill_analysis(). Must include
        a FILL_SOURCE column, i.e. this must be 2.lmt_binary_search.py's
        output, not 1.lmt_gap_fill.py's (which never writes that column).

    Outputs
    -------
    pandas.DataFrame indexed by FILL_SOURCE value, columns N_FRAMES,
    SECONDS, FRACTION.

    Logic
    -----
    A single value_counts() over FILL_SOURCE, converted to seconds via
    DB_FPS and to a fraction of the session total.

    Assumptions
    -----------
    None beyond FILL_SOURCE being present and populated for every row
    (true for any complete 2.lmt_binary_search.py output).

    Failure modes
    -------------
    Raises ValueError immediately if FILL_SOURCE is missing, rather than
    silently producing a result that omits the reviewer-driven categories
    -- calling this on a script-1-only file is a usage error, not a
    scenario to guess around.

    Validation
    ----------
    Cross-check against 2.lmt_binary_search.py's own
    LMT_Summary_A<id>_*.txt, which reports the same categories from its
    own independent accounting.

    Integration
    -----------
    Report alongside total_time_in_nest() and
    analysis.src.bouts.count_entries_exits() for every animal -- both of
    those metrics' reliability depends directly on this breakdown.
    """
    if "FILL_SOURCE" not in df.columns:
        raise ValueError(
            "FILL_SOURCE column not found -- this function requires "
            "2.lmt_binary_search.py's output, not 1.lmt_gap_fill.py's."
        )
    counts = df["FILL_SOURCE"].value_counts()
    out = pd.DataFrame({
        "N_FRAMES": counts,
        "SECONDS": counts / DB_FPS,
        "FRACTION": counts / counts.sum(),
    })
    return out
