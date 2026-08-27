"""
analysis/src/io.py

Shared loading functions for the analysis layer.

Why this exists
----------------
Every analysis module (bouts, occupancy, co_occupancy, spatial) needs to
read one of two SQLite tables the existing pipeline already produces:

    - GAP_FILL_ANALYSIS (from 2.lmt_binary_search.py's output), the
      per-animal, per-frame IN_NEST/FILL_SOURCE timeline.
    - DETECTION (from 0.Preprocessing.py's output), the raw per-frame
      MASS_X/MASS_Y positions.

Rather than every downstream module re-implementing its own SQLite read +
validation, both loaders live here once. This also means DB_FPS and
FRAME_CONVERSION are imported directly from the existing repo's
lmt_common.py -- not redefined -- so the analysis layer can never drift
out of sync with the pipeline's own frame-rate constants (this was a bug
in an earlier draft of this code, which locally redefined DB_FPS = 30
instead of importing it).
"""

import pathlib
import sqlite3
import sys

import pandas as pd

# Make the existing repo's lmt_common.py importable from analysis/src/.
# analysis/src/io.py -> parents[0]=src, [1]=analysis, [2]=repo root.
# Mirrors tests/conftest.py's existing REPO_ROOT-on-sys.path pattern, so
# this stays consistent with a convention the repo already established
# rather than inventing a second one.

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lmt_common import DB_FPS, FRAME_CONVERSION  # noqa: E402  (import after sys.path fix, by necessity)

__all__ = ["DB_FPS", "FRAME_CONVERSION", "load_gap_fill_analysis", "load_positions"]


def load_gap_fill_analysis(sqlite_path) -> pd.DataFrame:
    """
    What it does
    ------------
    Loads one animal's finalized per-frame nest classification from a
    2.lmt_binary_search.py output file (a lmt_binary_search_A<id>_
    <timestamp>.sqlite), validated and sorted by FRAMENUMBER.

    Why it exists
    -------------
    Every metric that isn't purely spatial (bouts, entry/exit
    counts, occupancy timelines, co-occupancy, fill-source composition)
    starts from this exact table. Centralizing the load + validation here
    means every one of those metrics is guaranteed to see the same
    sorted, deduplicated, single-animal input, rather than each function
    re-deriving (and potentially disagreeing on) that guarantee.

    Inputs
    ------
    sqlite_path : str or pathlib.Path
        Path to a lmt_binary_search_A<id>_<timestamp>.sqlite file. Using
        1.lmt_gap_fill.py's output instead is possible (it has the same
        GAP_FILL_ANALYSIS table shape minus FILL_SOURCE) but not
        recommended for final metrics, since it still contains far more
        unresolved (-1) frames than script 2's binary-search-refined
        output.

    Outputs
    -------
    pandas.DataFrame with (at least) FRAMENUMBER, IN_NEST, ANIMALID, and
    (if present -- see Failure modes) FILL_SOURCE, ASSUMPTION_TYPE,
    GAP_START_FRAME, GAP_END_FRAME. Sorted by FRAMENUMBER, index reset.

    Logic
    -----
    1. Open the SQLite file and confirm GAP_FILL_ANALYSIS exists as a
       table before querying it, so a wrong/corrupt file produces a
       clear ValueError instead of a raw sqlite3.OperationalError.
    2. Load the full table, sort by FRAMENUMBER.
    3. Validate FRAMENUMBER has no duplicates (this should be
       structurally guaranteed by the upstream pipeline's own
       invariants -- 0.Preprocessing.py's dedup + 1.lmt_gap_fill.py's own
       defensive check -- so a duplicate here indicates the input file
       was produced outside the normal 0->1->2 pipeline order).
    4. Validate exactly one ANIMALID is present. A per-animal output file
       should never contain more than one animal's rows; if it does,
       every downstream bout/metric computation would silently mix two
       animals' timelines together.

    Assumptions
    -----------
    Assumes the input file was produced by this repository's own
    pipeline (scripts 0->1->2, in order). No attempt is made to validate
    the IN_NEST/FILL_SOURCE VALUES themselves are internally consistent
    (e.g. that ASSUMED rows only ever carry FILL_SOURCE in {LOGIC,
    BINARY_SEARCH, UNKNOWN}) -- that is the existing pipeline's own
    integrity-check responsibility (see 2.lmt_binary_search.py's summary
    report), not this loader's.

    Failure modes
    -------------
    - Missing GAP_FILL_ANALYSIS table -> ValueError with a clear message.
    - Duplicate FRAMENUMBER -> ValueError (see Validation above).
    - More than one ANIMALID present -> ValueError.
    - FILL_SOURCE column absent (i.e. this is actually a script-1-only
      file) -> NOT an error here; some metrics (total time in
      nest, bout duration, occupancy timeline) don't need FILL_SOURCE at
      all and will work fine. Functions that DO need it (fill-source
      composition) raise their own clear error if it's missing -- see
      analysis/src/occupancy.py.

    Validation
    ----------
    Call with a known-good script-2 output file and confirm
    len(result) == the frame count reported in that run's own
    LMT_Summary_A<id>_*.txt report.

    Integration
    -----------
    Used by analysis/src/bouts.py, analysis/src/occupancy.py, and
    analysis/src/co_occupancy.py. Not used by analysis/src/spatial.py,
    which reads the raw DETECTION table via load_positions() below
    instead (MASS_X/MASS_Y never make it into GAP_FILL_ANALYSIS).
    """
    sqlite_path = str(sqlite_path)
    conn = sqlite3.connect(sqlite_path)
    try:
        tables = pd.read_sql_query(
            "SELECT name FROM sqlite_master WHERE type='table'", conn
        )["name"].tolist()
        if "GAP_FILL_ANALYSIS" not in tables:
            raise ValueError(
                f"{sqlite_path} has no GAP_FILL_ANALYSIS table. "
                "Is this a 1.lmt_gap_fill.py or 2.lmt_binary_search.py output file?"
            )
        df = pd.read_sql_query("SELECT * FROM GAP_FILL_ANALYSIS", conn)
    finally:
        conn.close()

    if df.empty:
        raise ValueError(f"{sqlite_path}'s GAP_FILL_ANALYSIS table is empty.")

    df = df.sort_values("FRAMENUMBER").reset_index(drop=True)

    if df["FRAMENUMBER"].duplicated().any():
        dup_count = int(df["FRAMENUMBER"].duplicated().sum())
        raise ValueError(
            f"{sqlite_path}: {dup_count} duplicate FRAMENUMBER value(s) found "
            "in GAP_FILL_ANALYSIS. This file should already be deduplicated "
            "by the upstream pipeline -- investigate before proceeding."
        )

    n_animals = df["ANIMALID"].nunique()
    if n_animals != 1:
        raise ValueError(
            f"{sqlite_path}: expected exactly one ANIMALID in a per-animal "
            f"output file, found {n_animals}."
        )

    return df


def load_positions(processed_sqlite_path, animal_ids) -> pd.DataFrame:
    """
    What it does
    ------------
    Loads MASS_X/MASS_Y for a set of animals from ONE processed (post-
    0.Preprocessing.py) SQLite file, merged into a single wide,
    per-frame table (one X/Y column pair per animal).

    Why it exists
    -------------
    MASS_X/MASS_Y are consumed by 1.lmt_gap_fill.py's nest-ROI logic but
    never written into its GAP_FILL_ANALYSIS output -- only the resulting
    binary in-nest label survives. Any position-based metric (inter-
    animal proximity, locomotor activity) therefore has to go back to the
    raw, deduplicated DETECTION table directly, not to
    load_gap_fill_analysis()'s output.

    Inputs
    ------
    processed_sqlite_path : str or pathlib.Path
        The SAME {name}_processed.sqlite file that feeds
        1.lmt_gap_fill.py -- i.e. a single shared-session file containing
        ALL animals' raw detections together, not a per-animal output.
    animal_ids : list[int]
        ANIMALID values to include.

    Outputs
    -------
    pandas.DataFrame indexed by FRAMENUMBER, with columns X_<id>, Y_<id>
    per requested animal. This is an OUTER join across animals (unlike
    co_occupancy's inner join): a frame where only some of the requested
    animals were detected is kept, with NaN for the missing animal(s).
    Each downstream spatial metric decides for itself how to handle NaNs,
    since "missing" means different things for a distance (both animals
    must be present) vs. a per-animal locomotion metric (only that one
    animal needs to be present).

    Logic
    -----
    A single parameterized SQL query pulls all requested animals' rows at
    once, then pandas' pivot_table reshapes from long (one row per
    frame+animal) to wide (one row per frame, columns per animal).

    Assumptions
    -----------
    Assumes processed_sqlite_path is a single session's processed
    DETECTION table (post-0.Preprocessing.py dedup), so (FRAMENUMBER,
    ANIMALID) is a unique key -- if it isn't, pivot_table will silently
    aggregate (mean, by default) duplicate entries rather than raising,
    which is exactly the kind of silent corruption 0.Preprocessing.py's
    dedup exists to prevent. Run that script first.

    Failure modes
    -------------
    - An animal_id not present in DETECTION at all produces all-NaN
      columns for that animal, not an error -- check the returned
      DataFrame's per-column non-null counts if an animal is
      unexpectedly missing from the result.
    - Passing a per-animal GAP_FILL_ANALYSIS file here instead of the
      shared processed DETECTION file will fail with a clear error (no
      DETECTION table), not silently return wrong data.

    Validation
    ----------
    result[f"X_{animal_id}"].notna().sum() should be close to (not
    necessarily identical to, since DETECTION includes anonymous/
    unidentified rows too) that animal's DETECTED-only frame count from
    its own load_gap_fill_analysis() output.

    Integration
    -----------
    Used by analysis/src/spatial.py's pairwise_distance() and
    locomotor_distance().
    """
    conn = sqlite3.connect(str(processed_sqlite_path))
    try:
        tables = pd.read_sql_query(
            "SELECT name FROM sqlite_master WHERE type='table'", conn
        )["name"].tolist()
        if "DETECTION" not in tables:
            raise ValueError(
                f"{processed_sqlite_path} has no DETECTION table. "
                "This should be a 0.Preprocessing.py *_processed.sqlite "
                "output, not a per-animal GAP_FILL_ANALYSIS file."
            )
        placeholders = ",".join("?" * len(animal_ids))
        query = f"""
            SELECT FRAMENUMBER, ANIMALID, MASS_X, MASS_Y
            FROM DETECTION
            WHERE ANIMALID IN ({placeholders})
            ORDER BY FRAMENUMBER
        """
        df = pd.read_sql_query(query, conn, params=list(animal_ids))
    finally:
        conn.close()

    if df.empty:
        raise ValueError(
            f"No DETECTION rows found for animal_ids={animal_ids} in "
            f"{processed_sqlite_path}."
        )

    wide = df.pivot_table(index="FRAMENUMBER", columns="ANIMALID",
                           values=["MASS_X", "MASS_Y"])
    wide.columns = [f"{coord.replace('MASS_', '')}_{int(aid)}"
                    for coord, aid in wide.columns]
    return wide.sort_index()
