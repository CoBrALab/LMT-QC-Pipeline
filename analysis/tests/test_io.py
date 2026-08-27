import sqlite3

import pytest

from analysis.src.io import load_gap_fill_analysis, load_positions
from analysis.tests.conftest import make_frames


def test_load_gap_fill_analysis_happy_path(gap_fill_sqlite_factory):
    rows = make_frames([(1, 3), (0, 2)], animal_id=101)
    path = gap_fill_sqlite_factory("a101.sqlite", rows)
    df = load_gap_fill_analysis(path)
    assert len(df) == 5
    assert df["ANIMALID"].nunique() == 1
    assert list(df["FRAMENUMBER"]) == sorted(df["FRAMENUMBER"])


def test_load_gap_fill_analysis_missing_table_raises(tmp_path):
    # Edge case: malformed / wrong-schema SQLite database.
    path = tmp_path / "not_a_pipeline_output.sqlite"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE SOME_OTHER_TABLE (x INTEGER)")
    conn.commit()
    conn.close()

    with pytest.raises(ValueError, match="GAP_FILL_ANALYSIS"):
        load_gap_fill_analysis(path)


def test_load_gap_fill_analysis_empty_table_raises(gap_fill_sqlite_factory):
    path = gap_fill_sqlite_factory("empty.sqlite", [])
    with pytest.raises(ValueError, match="empty"):
        load_gap_fill_analysis(path)


def test_load_gap_fill_analysis_duplicate_framenumber_raises(gap_fill_sqlite_factory):
    # Edge case explicitly requested: duplicate frames.
    rows = make_frames([(1, 3)], animal_id=101)
    rows.append(dict(rows[0]))  # duplicate the first frame's row
    path = gap_fill_sqlite_factory("dup.sqlite", rows)
    with pytest.raises(ValueError, match="duplicate"):
        load_gap_fill_analysis(path)


def test_load_gap_fill_analysis_multiple_animals_raises(gap_fill_sqlite_factory):
    # Edge case explicitly requested: a per-animal file that somehow
    # contains more than one ANIMALID must be rejected, not silently
    # processed as if it were one animal's timeline. Frame ranges are
    # deliberately DISJOINT (0-2 vs 100-102) so this exercises the
    # ANIMALID check specifically, not the (separately tested)
    # duplicate-FRAMENUMBER check -- two animals sharing overlapping
    # FRAMENUMBERs would trip that check first instead.
    rows_a = make_frames([(1, 3)], animal_id=101, start_frame=0)
    rows_b = make_frames([(1, 3)], animal_id=102, start_frame=100)
    path = gap_fill_sqlite_factory("two_animals.sqlite", rows_a + rows_b)
    with pytest.raises(ValueError, match="ANIMALID"):
        load_gap_fill_analysis(path)


def test_load_positions_happy_path(detection_sqlite_factory):
    rows = [
        {"FRAMENUMBER": 0, "ANIMALID": 101, "MASS_X": 1.0, "MASS_Y": 2.0},
        {"FRAMENUMBER": 0, "ANIMALID": 102, "MASS_X": 5.0, "MASS_Y": 6.0},
        {"FRAMENUMBER": 1, "ANIMALID": 101, "MASS_X": 1.5, "MASS_Y": 2.5},
    ]
    path = detection_sqlite_factory("detection.sqlite", rows)
    positions = load_positions(path, [101, 102])
    assert "X_101" in positions.columns and "Y_102" in positions.columns
    # Frame 1 has no ANIMALID 102 row -- outer join should leave NaN,
    # not drop the row entirely.
    assert positions.loc[1, "X_101"] == 1.5
    import pandas as pd
    assert pd.isna(positions.loc[1, "X_102"])


def test_load_positions_missing_table_raises(tmp_path):
    path = tmp_path / "wrong_schema.sqlite"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE SOME_OTHER_TABLE (x INTEGER)")
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="DETECTION"):
        load_positions(path, [101])


def test_load_positions_unknown_animal_id_raises(detection_sqlite_factory):
    # Edge case explicitly requested: incorrect animal IDs.
    rows = [{"FRAMENUMBER": 0, "ANIMALID": 101, "MASS_X": 1.0, "MASS_Y": 2.0}]
    path = detection_sqlite_factory("detection.sqlite", rows)
    with pytest.raises(ValueError, match="No DETECTION rows"):
        load_positions(path, [999])
