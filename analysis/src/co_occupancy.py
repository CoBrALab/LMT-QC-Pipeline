"""
analysis/src/co_occupancy.py

Multi-animal simultaneous-occupancy metrics (Metric 5): dam+dam,
babysitter+babysitter, dam+babysitter overlap, and group ("N adults in
nest") cohesion.

Why this exists
----------------
No existing script in the repository merges multiple animals' outputs
together -- 1.lmt_gap_fill.py and 2.lmt_binary_search.py both run
per-animal, producing independent lmt_binary_search_A<id>_*.sqlite files.
This module is new work built strictly on top of
analysis.src.io.load_gap_fill_analysis(); it does not modify or duplicate
any pipeline script.
"""

import numpy as np
import pandas as pd

from .io import DB_FPS, load_gap_fill_analysis


def build_occupancy_matrix(animal_files: dict) -> pd.DataFrame:
    """
    What it does
    ------------
    Merges N animals' finalized GAP_FILL_ANALYSIS outputs into one wide
    table: one row per FRAMENUMBER, one IN_NEST column per animal.

    Why it exists
    -------------
    Every co-occupancy question (a specific dyad, or the full adult
    group) is a different slice of this one merged matrix, not a
    separately-implemented merge.

    Inputs
    ------
    animal_files : dict[str, str or pathlib.Path]
        Maps a role/label you choose (e.g. "dam1", "dam2", "babysitter1",
        "babysitter2" -- YOUR mapping; the pipeline does not infer roles)
        to that animal's lmt_binary_search_A<id>_<timestamp>.sqlite path.

    Outputs
    -------
    pandas.DataFrame indexed by FRAMENUMBER, one IN_NEST_<label> column
    per animal, restricted to the FRAMENUMBER range common to ALL
    supplied animals (an inner join -- see Logic).

    Logic
    -----
    Each animal's file is loaded via load_gap_fill_analysis() (which
    already validates single-animal, deduplicated, sorted input), then
    joined on FRAMENUMBER via pandas' automatic index alignment + an
    explicit dropna() to enforce the inner join. A frame present for only
    some animals (e.g. one animal's RFID chip was fitted slightly later)
    is dropped entirely -- there is no way to assess "together" or "not
    together" if one animal's state at that frame is simply absent.

    Assumptions
    -----------
    All files passed in animal_files come from the SAME recording
    session, so FRAMENUMBER refers to the same real moment for every
    animal (true for LMT: one recording session produces one shared frame
    clock for every tracked animal). This is NOT checked in code --
    verify session identity externally (e.g. compare each source SQLite's
    originating video filenames) before calling this with files from
    different sessions, which would silently produce a matrix that looks
    complete but is temporally meaningless.

    Failure modes
    -------------
    A large number of dropped frames (see the printed WARNING) means your
    effective co-occupancy analysis window is silently shorter than a
    single-animal analysis window would be for the same animals. This
    function always prints how many frames were dropped and each animal's
    own frame range, specifically so this can't go unnoticed.

    Validation
    ----------
    co_occupancy_seconds(matrix, [single_label]) for any one label (a
    1-element list, degenerating to that animal's own resolved in-nest
    time) should closely match total_time_in_nest() computed independently
    from that animal's own file (analysis.src.occupancy) -- small
    discrepancies are expected only from the inner-join frame trimming,
    and should be explainable by the printed dropped-frame count.

    Integration
    -----------
    Feeds co_occupancy_seconds(), group_occupancy_profile(), and
    group_occupancy_table() below.
    """
    per_animal = {}
    frame_ranges = {}
    for label, path in animal_files.items():
        df = load_gap_fill_analysis(path)
        per_animal[label] = df.set_index("FRAMENUMBER")["IN_NEST"]
        frame_ranges[label] = (int(df["FRAMENUMBER"].min()), int(df["FRAMENUMBER"].max()))

    wide = pd.DataFrame({f"IN_NEST_{label}": s for label, s in per_animal.items()})
    n_before = len(wide)
    wide = wide.dropna()
    n_after = len(wide)

    dropped = n_before - n_after
    if dropped > 0:
        print(
            f"[build_occupancy_matrix] WARNING: {dropped} of {n_before} "
            f"frames ({dropped / n_before:.1%}) dropped -- not every "
            f"animal had a resolved frame at that FRAMENUMBER. "
            f"Per-animal frame ranges: {frame_ranges}"
        )

    return wide.astype(int)


def co_occupancy_seconds(matrix: pd.DataFrame, labels: list) -> dict:
    """
    What it does
    ------------
    Metric 5. Computes simultaneous-occupancy time for a specific subset
    of animals (2 for a dyad such as dam+dam; more for group cohesion).

    Why it exists
    -------------
    Answers the specific biological questions your research goal lists:
    do the two dams co-nest, do the two babysitters co-occupy, does a dam
    overlap with a babysitter, and (for len(labels) > 2) how many adults
    are in the nest together at once.

    Inputs
    ------
    matrix : output of build_occupancy_matrix().
    labels : list of animal labels (as used in build_occupancy_matrix's
        animal_files dict) to require simultaneous occupancy for.

    Outputs
    -------
    dict: labels, seconds_all_together, total_resolved_seconds,
    fraction_of_resolved_time, and (only when len(labels) > 2) an
    n_in_nest_distribution_sec dict mapping "how many of these N animals
    were in-nest simultaneously" -> seconds, for a group-cohesion view
    beyond strict all-or-nothing overlap.

    Logic
    -----
    A frame only counts toward co-occupancy if EVERY animal in `labels`
    has a resolved (0 or 1) state at that frame -- a frame where any one
    of them is unresolved (-1) is excluded from the co-occupancy
    calculation entirely, since "together" or "not together" cannot be
    claimed about an animal whose own state is unknown at that instant.

    Assumptions
    -----------
    matrix came from build_occupancy_matrix() (guarantees IN_NEST_<label>
    columns exist and share a common, aligned FRAMENUMBER index).

    Failure modes
    -------------
    A label not present in matrix's columns raises a KeyError with the
    missing column name -- check animal_files in build_occupancy_matrix()
    for a typo'd label.

    Validation
    ----------
    For a 2-dam, 2-babysitter cage, n_in_nest_distribution_sec's values
    for n=0..4 should sum to total_resolved_seconds.

    Integration
    -----------
    Called once per dyad/group of interest by
    analysis/scripts/run_analysis.py, using the role groupings
    defined in analysis/config/analysis_config.yaml.
    """
    cols = [f"IN_NEST_{l}" for l in labels]
    resolved_mask = (matrix[cols] != -1).all(axis=1)
    resolved = matrix.loc[resolved_mask, cols]

    all_together = (resolved == 1).all(axis=1)
    seconds_together = float(all_together.sum() / DB_FPS)
    total_resolved_seconds = float(len(resolved) / DB_FPS)

    result = {
        "labels": labels,
        "seconds_all_together": seconds_together,
        "total_resolved_seconds": total_resolved_seconds,
        "fraction_of_resolved_time": (
            seconds_together / total_resolved_seconds
            if total_resolved_seconds > 0 else np.nan
        ),
    }
    if len(labels) > 2:
        n_in_nest = resolved.sum(axis=1)
        result["n_in_nest_distribution_sec"] = {
            int(k): float(v) / DB_FPS
            for k, v in n_in_nest.value_counts().sort_index().items()
        }
    return result


def group_occupancy_profile(matrix: pd.DataFrame, all_labels: list) -> pd.Series:
    """
    What it does
    ------------
    Metric 5 (group-level time series). Number of adults in the nest at
    every resolved frame.

    Why it exists
    -------------
    The raw series behind a group-cohesion-over-time figure (e.g. an
    occupancy heatmap): how many of the tracked adults are together in
    the nest, over the course of the session.

    Inputs
    ------
    matrix : output of build_occupancy_matrix().
    all_labels : the full set of animal labels to include in the count.

    Outputs
    -------
    pandas.Series indexed by FRAMENUMBER, integer values 0..len(all_labels)
    where every animal in all_labels is resolved, NaN where any one of
    them is unresolved at that frame (reindexed to matrix's full index so
    gaps are explicit, not silently dropped).

    Logic / Assumptions / Failure modes
    ------------------------------------
    Same resolved-frame requirement as co_occupancy_seconds() -- a frame
    contributes a count only if every requested animal has a resolved
    state there.

    Validation
    ----------
    Non-NaN values should never exceed len(all_labels).

    Integration
    -----------
    Feed directly into a heatmap/line plot after binning with
    analysis.src.occupancy.occupancy_timeline()-style time bucketing if a
    smoothed view is preferred over the raw per-frame series.
    """
    cols = [f"IN_NEST_{l}" for l in all_labels]
    resolved_mask = (matrix[cols] != -1).all(axis=1)
    n_in_nest = matrix.loc[resolved_mask, cols].sum(axis=1)
    return n_in_nest.reindex(matrix.index)


def group_occupancy_table(matrix: pd.DataFrame, all_labels: list) -> pd.DataFrame:
    """
    What it does
    ------------
    Metric 5 (group-level time series, per-animal detail). Wide-format
    version of group_occupancy_profile() above: one row per frame, one
    binary-ish column per animal showing whether THAT SPECIFIC animal
    was in the nest, plus an n_in_nest column giving their sum -- so a
    reader can distinguish, e.g., "0 animals in nest" from "1 animal in
    nest, specifically animal X" from "3 animals in nest, specifically
    X/Y/Z", not just the count.

    Why it exists
    -------------
    group_occupancy_profile() answers "how many" per frame; this answers
    "how many, AND WHICH ONES" per frame -- a strict superset of that
    information, in the shape needed for group_occupancy_profile.csv.
    Built from the exact same `matrix` (build_occupancy_matrix()'s
    output) as every other co-occupancy function in this module, not a
    separate merge/loading path.

    Inputs
    ------
    matrix : output of build_occupancy_matrix().
    all_labels : the animal labels (as used in build_occupancy_matrix's
        animal_files dict) to include as columns, in the given order.

    Outputs
    -------
    pandas.DataFrame with columns:
        FRAMENUMBER -- matrix's own index, as a plain column.
        n_in_nest -- count of animals with value 1 in this row (i.e.
            CONFIRMED in-nest -- see the -1 note below for what this
            does and doesn't include).
        <label> (one column per entry in all_labels, in that order) --
            that animal's IN_NEST value at this frame, taken AS-IS from
            the matrix: 1 (confirmed in nest), 0 (confirmed not in
            nest), or -1 (unresolved at this frame for this animal).

    IMPORTANT -- why -1 is preserved rather than collapsed to 0/1
    ------------------------------------------------------------------
    This intentionally does NOT force every value to a strict 0/1: an
    unresolved frame for one animal is reported as -1 for that animal
    specifically, not silently reinterpreted as "confirmed not in nest"
    (0). Every other part of this codebase treats "unresolved" and
    "confirmed absent" as different facts that must never be conflated
    (see analysis.src.occupancy.total_time_in_nest()'s
    fraction_unresolved, and co_occupancy_seconds()'s own exclusion of
    frames where any requested animal is unresolved) -- silently
    collapsing -1 to 0 here would violate that same principle and could
    make an animal look confirmed-absent when its status was actually
    unknown. n_in_nest counts ONLY confirmed-present (value == 1)
    animals; it does not subtract or otherwise account for -1 columns,
    so a row with some -1 columns still reports a real, meaningful
    n_in_nest for whichever animals WERE resolved that frame.

    If your downstream use genuinely needs strict {0,1} values with
    every -1 forced to 0 (e.g. a plotting library that can't handle a
    third value), that is a one-line transform on this function's
    output (`df[all_labels] = df[all_labels].clip(lower=0)`) -- do this
    explicitly at the point of use, not inside this function, so the
    conflation is a visible, deliberate choice in your own analysis
    script rather than hidden in shared library code.

    Logic
    -----
    Selects and renames the matrix's IN_NEST_<label> columns to their
    plain label names, computes n_in_nest as a row-wise count of
    value == 1 across those columns, and resets the FRAMENUMBER index
    into an ordinary leading column.

    Assumptions
    -----------
    Same as group_occupancy_profile(): matrix came from
    build_occupancy_matrix() with all_labels' columns present.

    Failure modes
    -------------
    A label not present in matrix's columns raises a KeyError, same as
    co_occupancy_seconds().

    Validation
    ----------
    n_in_nest must equal (table[all_labels] == 1).sum(axis=1) exactly
    for every row (this is definitional, not a coincidence -- asserted
    in analysis/tests/test_co_occupancy.py); FRAMENUMBER must be
    strictly increasing and match matrix's own index.

    Integration
    -----------
    Written directly to group_occupancy_profile.csv by
    analysis/scripts/run_analysis.py, replacing that CSV's
    previous single-column (count-only) shape.
    """
    cols = [f"IN_NEST_{l}" for l in all_labels]
    table = matrix[cols].copy()
    table.columns = list(all_labels)
    table.insert(0, "n_in_nest", (table[list(all_labels)] == 1).sum(axis=1))
    table.index.name = "FRAMENUMBER"
    return table.reset_index()
