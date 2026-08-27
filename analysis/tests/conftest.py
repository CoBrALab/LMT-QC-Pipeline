"""
analysis/tests/conftest.py

Shared fixtures for the analysis/ test suite. Builds small, synthetic
SQLite files matching the pipeline's real schemas (GAP_FILL_ANALYSIS,
DETECTION) so tests run fully offline, with no dependency on a real LMT
export -- consistent with the existing tests/conftest.py's own headless,
synthetic-data-first approach (see its docstring on why
test_preprocessing_dedup.py uses real temp SQLite files rather than
in-memory DataFrames for I/O-shaped logic).
"""

import sqlite3

import pandas as pd
import pytest


def _write_gap_fill_sqlite(path, rows, include_fill_source=True):
    """
    Writes a GAP_FILL_ANALYSIS table (optionally with FILL_SOURCE, i.e.
    mimicking either 1.lmt_gap_fill.py's or 2.lmt_binary_search.py's
    output shape) to a fresh SQLite file at `path`.

    rows : list of dicts, each with at least FRAMENUMBER, IN_NEST,
        ANIMALID. ASSUMPTION_TYPE/GAP_START_FRAME/GAP_END_FRAME/
        FILL_SOURCE are filled with sensible defaults if not provided.
    """
    df = pd.DataFrame(rows)
    if "ASSUMPTION_TYPE" not in df.columns:
        df["ASSUMPTION_TYPE"] = "DETECTED"
    if "GAP_START_FRAME" not in df.columns:
        df["GAP_START_FRAME"] = None
    if "GAP_END_FRAME" not in df.columns:
        df["GAP_END_FRAME"] = None
    if include_fill_source and "FILL_SOURCE" not in df.columns:
        df["FILL_SOURCE"] = "DETECTED"

    conn = sqlite3.connect(str(path))
    try:
        df.to_sql("GAP_FILL_ANALYSIS", conn, index=False, if_exists="replace")
    finally:
        conn.close()


def _write_detection_sqlite(path, rows):
    """
    Writes a DETECTION table (mimicking 0.Preprocessing.py's output
    shape) to a fresh SQLite file at `path`.

    rows : list of dicts with FRAMENUMBER, ANIMALID, MASS_X, MASS_Y.
    """
    df = pd.DataFrame(rows)
    conn = sqlite3.connect(str(path))
    try:
        df.to_sql("DETECTION", conn, index=False, if_exists="replace")
    finally:
        conn.close()


@pytest.fixture
def gap_fill_sqlite_factory(tmp_path):
    """
    Returns a function(filename, rows, include_fill_source=True) that
    writes a synthetic GAP_FILL_ANALYSIS SQLite file under pytest's tmp_path
    and returns its path. A factory (not a single fixture value) because
    several tests need more than one animal's file in the same test.
    """
    def _factory(filename, rows, include_fill_source=True):
        path = tmp_path / filename
        _write_gap_fill_sqlite(path, rows, include_fill_source=include_fill_source)
        return path
    return _factory


@pytest.fixture
def detection_sqlite_factory(tmp_path):
    """
    Returns a function(filename, rows) that writes a synthetic DETECTION
    SQLite file under pytest's tmp_path and returns its path.
    """
    def _factory(filename, rows):
        path = tmp_path / filename
        _write_detection_sqlite(path, rows)
        return path
    return _factory


def make_frames(state_sequence, start_frame=0, animal_id=101):
    """
    Helper (not a fixture): expands a compact [(state, n_frames), ...]
    sequence into a full list of per-frame row dicts, e.g.
    make_frames([(1, 3), (0, 2), (1, 4)]) produces 9 contiguous frames:
    3 in-nest, 2 out-of-nest, 4 in-nest -- exactly the shape
    compute_nest_bouts() expects (one row per frame, no gaps).
    """
    rows = []
    frame = start_frame
    for state, n in state_sequence:
        for _ in range(n):
            rows.append({"FRAMENUMBER": frame, "IN_NEST": state, "ANIMALID": animal_id})
            frame += 1
    return rows
