import sqlite3

import pandas as pd
import pytest


DETECTION_COLUMNS = [
    "FRAMENUMBER", "ANIMALID", "MASS_X", "MASS_Y",
    "FRONT_X", "FRONT_Y", "FRONT_Z", "BACK_X", "BACK_Y", "BACK_Z",
]


def _make_sqlite(path, rows, with_surrogate_pk=False):
    """
    rows: list of tuples matching DETECTION_COLUMNS order.
    Inserted one at a time (not executemany) so row order == insertion
    order == rowid order, matching a real exported LMT file.
    """
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    pk_clause = "ID INTEGER PRIMARY KEY AUTOINCREMENT, " if with_surrogate_pk else ""
    cur.execute(f"""
        CREATE TABLE DETECTION (
            {pk_clause}
            FRAMENUMBER INTEGER, ANIMALID INTEGER,
            MASS_X REAL, MASS_Y REAL,
            FRONT_X REAL, FRONT_Y REAL, FRONT_Z REAL,
            BACK_X REAL, BACK_Y REAL, BACK_Z REAL
        )
    """)
    for row in rows:
        cur.execute(
            f"INSERT INTO DETECTION ({', '.join(DETECTION_COLUMNS)}) "
            f"VALUES ({', '.join('?' * len(DETECTION_COLUMNS))})",
            row,
        )
    conn.commit()
    conn.close()


def _read_detection(path):
    conn = sqlite3.connect(path)
    df = pd.read_sql_query("SELECT rowid AS _rowid, * FROM DETECTION ORDER BY rowid", conn)
    conn.close()
    return df


def _sha256(path):
    import hashlib
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


ROW = lambda fn, aid, mx, my, fx=10.0, fy=10.0, fz=10.0, bx=10.0, by=10.0, bz=10.0: \
    (fn, aid, mx, my, fx, fy, fz, bx, by, bz)


def test_exact_duplicate_rows_keep_first(tmp_path, preprocessing_module):
    m = preprocessing_module
    src = tmp_path / "src.sqlite"
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    rows = [
        ROW(1, 1, 100.0, 200.0),
        ROW(2, 1, 101.0, 201.0),   # exact duplicate of the row below
        ROW(2, 1, 101.0, 201.0),
        ROW(2, 1, 101.0, 201.0),   # exact duplicate, 3 copies total
    ]
    _make_sqlite(src, rows)

    rc = m.process_database(str(src), str(out_dir))
    assert rc == 0

    out_path = out_dir / "src_processed.sqlite"
    df = _read_detection(out_path)

    assert len(df) == 2
    # First occurrence (lowest rowid) survives.
    frame2_rows = df[df["FRAMENUMBER"] == 2]
    assert len(frame2_rows) == 1
    assert frame2_rows.iloc[0]["_rowid"] == 2  # the first of the three inserted


def test_conflicting_same_frame_animal_removed_entirely(tmp_path, preprocessing_module):
    m = preprocessing_module
    src = tmp_path / "src.sqlite"
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    rows = [
        ROW(1, 1, 100.0, 200.0),
        ROW(2, 1, 101.0, 201.0),   # conflicting: same frame+animal, different MASS_X
        ROW(2, 1, 999.0, 999.0),
    ]
    _make_sqlite(src, rows)

    rc = m.process_database(str(src), str(out_dir))
    assert rc == 0

    df = _read_detection(out_dir / "src_processed.sqlite")

    assert len(df) == 1
    assert df.iloc[0]["FRAMENUMBER"] == 1
    # Neither conflicting frame-2 row survives -- not even the first one.
    assert (df["FRAMENUMBER"] == 2).sum() == 0


def test_different_animal_same_framenumber_remains_valid(tmp_path, preprocessing_module):
    m = preprocessing_module
    src = tmp_path / "src.sqlite"
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    rows = [
        ROW(5, 1, 100.0, 200.0),
        ROW(5, 2, 300.0, 400.0),   # same FRAMENUMBER, different ANIMALID
    ]
    _make_sqlite(src, rows)

    rc = m.process_database(str(src), str(out_dir))
    assert rc == 0

    df = _read_detection(out_dir / "src_processed.sqlite")

    assert len(df) == 2
    assert set(df["ANIMALID"]) == {1, 2}
    assert all(df["FRAMENUMBER"] == 5)


def test_negative_one_coords_no_longer_removed(tmp_path, preprocessing_module):
    m = preprocessing_module
    src = tmp_path / "src.sqlite"
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    rows = [
        ROW(1, 1, 100.0, 200.0, fx=-1, fy=-1, fz=-1, bx=-1, by=-1, bz=-1),
        ROW(2, 1, 101.0, 201.0, fx=-1, fy=-1, fz=-1, bx=-1, by=-1, bz=-1),
    ]
    _make_sqlite(src, rows)

    rc = m.process_database(str(src), str(out_dir))
    assert rc == 0

    df = _read_detection(out_dir / "src_processed.sqlite")

    # Both rows survive -- FRONT_*/BACK_* = -1 is no longer a deletion criterion.
    assert len(df) == 2
    assert list(df["MASS_X"]) == [100.0, 101.0]
    assert list(df["MASS_Y"]) == [200.0, 201.0]


def test_surrogate_primary_key_excluded_from_identity_check(tmp_path, preprocessing_module):
    """A table with its own auto-increment ID column must still detect two
    otherwise-identical rows as Case A duplicates (the differing ID alone
    shouldn't make every row look unique)."""
    m = preprocessing_module
    src = tmp_path / "src.sqlite"
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    rows = [
        ROW(1, 1, 100.0, 200.0),
        ROW(1, 1, 100.0, 200.0),  # identical data, but gets a different auto ID
    ]
    _make_sqlite(src, rows, with_surrogate_pk=True)

    rc = m.process_database(str(src), str(out_dir))
    assert rc == 0

    df = _read_detection(out_dir / "src_processed.sqlite")
    assert len(df) == 1


def test_original_database_never_modified(tmp_path, preprocessing_module):
    m = preprocessing_module
    src = tmp_path / "src.sqlite"
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    rows = [ROW(1, 1, 100.0, 200.0), ROW(1, 1, 100.0, 200.0)]
    _make_sqlite(src, rows)

    before_hash = _sha256(src)
    rc = m.process_database(str(src), str(out_dir))
    assert rc == 0
    after_hash = _sha256(src)

    assert before_hash == after_hash


def test_existing_output_not_silently_overwritten(tmp_path, preprocessing_module):
    m = preprocessing_module
    src = tmp_path / "src.sqlite"
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    _make_sqlite(src, [ROW(1, 1, 100.0, 200.0)])

    assert m.process_database(str(src), str(out_dir)) == 0

    out_path = out_dir / "src_processed.sqlite"
    before_mtime = out_path.stat().st_mtime_ns

    # Re-run without --overwrite equivalent (overwrite=False): must abort,
    # not silently replace the existing output.
    rc = m.process_database(str(src), str(out_dir), overwrite=False)
    assert rc == 1
    assert out_path.stat().st_mtime_ns == before_mtime

    # With overwrite=True, it proceeds.
    rc = m.process_database(str(src), str(out_dir), overwrite=True)
    assert rc == 0


def test_missing_detection_table_handled_cleanly(tmp_path, preprocessing_module):
    m = preprocessing_module
    src = tmp_path / "src.sqlite"
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    conn = sqlite3.connect(src)
    conn.execute("CREATE TABLE NOT_DETECTION (X INTEGER)")
    conn.commit()
    conn.close()

    rc = m.process_database(str(src), str(out_dir))
    assert rc == 1  # clean, handled failure -- not an unhandled exception


def test_gap_fill_receives_no_duplicate_frames_from_dedup_output(tmp_path, preprocessing_module, gap_fill_module):
    """End-to-end: preprocessing's dedup output must never trigger
    1.lmt_gap_fill.py's duplicate/non-increasing FRAMENUMBER guard."""
    prep = preprocessing_module
    gf   = gap_fill_module

    src = tmp_path / "src.sqlite"
    prep_out = tmp_path / "prep_out"
    prep_out.mkdir()
    gap_out = tmp_path / "gap_out"
    gap_out.mkdir()

    rows = [
        ROW(1, 1, 5.0, 5.0),
        ROW(2, 1, 5.0, 5.0),
        ROW(2, 1, 5.0, 5.0),        # Case A: exact duplicate of frame 2
        ROW(3, 1, 5.0, 5.0),
        ROW(3, 1, 999.0, 999.0),    # Case B: conflicting frame 3 -- both removed
        ROW(4, 1, 5.0, 5.0),
    ]
    _make_sqlite(src, rows)

    assert prep.process_database(str(src), str(prep_out)) == 0
    processed_path = prep_out / "src_processed.sqlite"

    # Must not raise: after dedup, FRAMENUMBER is unique and increasing
    # for this animal (frame 3 having been fully removed by Case B just
    # means a bigger gap, not a duplicate/decreasing FRAMENUMBER).
    gf.run_analysis(
        input_db=str(processed_path), output_folder=str(gap_out), animal_id=1,
        nest_xmin=0, nest_xmax=10, nest_ymin=0, nest_ymax=10,
        buffer_xmin=-5, buffer_xmax=15, buffer_ymin=-5, buffer_ymax=15,
    )

    out_files = list(gap_out.glob("*.sqlite"))
    assert len(out_files) == 1
    conn = sqlite3.connect(out_files[0])
    out_df = pd.read_sql_query("SELECT FRAMENUMBER FROM GAP_FILL_ANALYSIS", conn)
    conn.close()

    # No duplicate FRAMENUMBER anywhere in the final gap-filled output.
    assert out_df["FRAMENUMBER"].is_unique


def test_duplicate_framenumber_input_raises_in_gap_fill(tmp_path, gap_fill_module):
    """Defensive validation (Issue #5): if 1.lmt_gap_fill.py is ever run
    against data that was NOT deduplicated first, a duplicate/non-increasing
    FRAMENUMBER for one animal must fail loudly, not silently corrupt gap
    sizing."""
    gf = gap_fill_module
    src = tmp_path / "raw.sqlite"
    out = tmp_path / "out"
    out.mkdir()

    conn = sqlite3.connect(src)
    conn.execute("""
        CREATE TABLE DETECTION (
            FRAMENUMBER INTEGER, ANIMALID INTEGER, MASS_X REAL, MASS_Y REAL,
            FRONT_X REAL, FRONT_Y REAL, FRONT_Z REAL,
            BACK_X REAL, BACK_Y REAL, BACK_Z REAL
        )
    """)
    # Duplicate FRAMENUMBER = 2 for the same animal, never deduplicated.
    for fn, mx, my in [(1, 5.0, 5.0), (2, 5.0, 5.0), (2, 6.0, 6.0), (3, 5.0, 5.0)]:
        conn.execute(
            "INSERT INTO DETECTION (FRAMENUMBER, ANIMALID, MASS_X, MASS_Y, "
            "FRONT_X, FRONT_Y, FRONT_Z, BACK_X, BACK_Y, BACK_Z) "
            "VALUES (?, 1, ?, ?, 10, 10, 10, 10, 10, 10)",
            (fn, mx, my),
        )
    conn.commit()
    conn.close()

    with pytest.raises(Exception, match="duplicate or non-increasing"):
        gf.run_analysis(
            input_db=str(src), output_folder=str(out), animal_id=1,
            nest_xmin=0, nest_xmax=10, nest_ymin=0, nest_ymax=10,
            buffer_xmin=-5, buffer_xmax=15, buffer_ymin=-5, buffer_ymax=15,
        )
