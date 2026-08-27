import pytest

from analysis.src.co_occupancy import (
    build_occupancy_matrix,
    co_occupancy_seconds,
    group_occupancy_profile,
    group_occupancy_table,
)
from analysis.src.io import DB_FPS
from analysis.tests.conftest import make_frames


def test_build_occupancy_matrix_and_dyad_overlap(gap_fill_sqlite_factory):
    # Animal A: in-nest frames 0-9. Animal B: in-nest frames 5-14.
    # Overlap (both in-nest) should be frames 5-9 -> 5 frames.
    rows_a = make_frames([(1, 10), (0, 10)], animal_id=101)
    rows_b = make_frames([(0, 5), (1, 10), (0, 5)], animal_id=102)

    path_a = gap_fill_sqlite_factory("a101.sqlite", rows_a)
    path_b = gap_fill_sqlite_factory("a102.sqlite", rows_b)

    matrix = build_occupancy_matrix({"101": path_a, "102": path_b})
    result = co_occupancy_seconds(matrix, ["101", "102"])
    assert result["seconds_all_together"] == pytest.approx(5 / DB_FPS)


def test_build_occupancy_matrix_drops_frames_outside_common_range(
    gap_fill_sqlite_factory, capsys
):
    # Animal A covers frames 0-19; Animal B only covers frames 10-29
    # (e.g. chipped/placed later). The merge must be an inner join: only
    # frames 10-19 should survive, and a warning should be printed.
    rows_a = make_frames([(1, 20)], animal_id=101, start_frame=0)
    rows_b = make_frames([(1, 20)], animal_id=102, start_frame=10)

    path_a = gap_fill_sqlite_factory("a101.sqlite", rows_a)
    path_b = gap_fill_sqlite_factory("a102.sqlite", rows_b)

    matrix = build_occupancy_matrix({"101": path_a, "102": path_b})
    assert len(matrix) == 10  # only frames 10-19 are common to both
    captured = capsys.readouterr()
    assert "WARNING" in captured.out


def test_co_occupancy_excludes_frames_where_either_animal_unresolved(
    gap_fill_sqlite_factory,
):
    # Both animals in-nest for 10 frames, but animal B is unresolved (-1)
    # for the first 4 of those -- those 4 frames must NOT count as
    # "together" or "not together"; they must be excluded entirely.
    rows_a = make_frames([(1, 10)], animal_id=101)
    rows_b = make_frames([(-1, 4), (1, 6)], animal_id=102)

    path_a = gap_fill_sqlite_factory("a101.sqlite", rows_a)
    path_b = gap_fill_sqlite_factory("a102.sqlite", rows_b)

    matrix = build_occupancy_matrix({"101": path_a, "102": path_b})
    result = co_occupancy_seconds(matrix, ["101", "102"])
    assert result["total_resolved_seconds"] == pytest.approx(6 / DB_FPS)
    assert result["seconds_all_together"] == pytest.approx(6 / DB_FPS)


def test_group_occupancy_profile_counts_simultaneous_animals(gap_fill_sqlite_factory):
    rows_a = make_frames([(1, 10)], animal_id=101)
    rows_b = make_frames([(1, 5), (0, 5)], animal_id=102)

    path_a = gap_fill_sqlite_factory("a101.sqlite", rows_a)
    path_b = gap_fill_sqlite_factory("a102.sqlite", rows_b)

    matrix = build_occupancy_matrix({"101": path_a, "102": path_b})
    profile = group_occupancy_profile(matrix, ["101", "102"])
    assert profile.iloc[0] == 2  # both in nest at frame 0
    assert profile.iloc[9] == 1  # only animal 101 in nest at frame 9


def test_co_occupancy_group_distribution_for_more_than_two_animals(
    gap_fill_sqlite_factory,
):
    rows_a = make_frames([(1, 10)], animal_id=101)
    rows_b = make_frames([(1, 10)], animal_id=102)
    rows_c = make_frames([(0, 5), (1, 5)], animal_id=103)

    paths = {
        "101": gap_fill_sqlite_factory("a101.sqlite", rows_a),
        "102": gap_fill_sqlite_factory("a102.sqlite", rows_b),
        "103": gap_fill_sqlite_factory("a103.sqlite", rows_c),
    }
    matrix = build_occupancy_matrix(paths)
    result = co_occupancy_seconds(matrix, ["101", "102", "103"])
    dist = result["n_in_nest_distribution_sec"]
    # First 5 frames: 2 animals in nest. Last 5: 3 animals in nest.
    assert dist[2] == pytest.approx(5 / DB_FPS)
    assert dist[3] == pytest.approx(5 / DB_FPS)



# group_occupancy_table

def test_group_occupancy_table_columns_and_shape(gap_fill_sqlite_factory):
    rows_a = make_frames([(1, 10)], animal_id=101)
    rows_b = make_frames([(1, 5), (0, 5)], animal_id=102)

    path_a = gap_fill_sqlite_factory("a101.sqlite", rows_a)
    path_b = gap_fill_sqlite_factory("a102.sqlite", rows_b)

    matrix = build_occupancy_matrix({"101": path_a, "102": path_b})
    table = group_occupancy_table(matrix, ["101", "102"])

    assert list(table.columns) == ["FRAMENUMBER", "n_in_nest", "101", "102"]
    assert len(table) == len(matrix)
    assert list(table["FRAMENUMBER"]) == list(matrix.index)


def test_group_occupancy_table_identifies_which_animals(gap_fill_sqlite_factory):
    # Frame 0-4: both in nest. Frame 5-9: only 101 in nest.
    rows_a = make_frames([(1, 10)], animal_id=101)
    rows_b = make_frames([(1, 5), (0, 5)], animal_id=102)

    path_a = gap_fill_sqlite_factory("a101.sqlite", rows_a)
    path_b = gap_fill_sqlite_factory("a102.sqlite", rows_b)

    matrix = build_occupancy_matrix({"101": path_a, "102": path_b})
    table = group_occupancy_table(matrix, ["101", "102"])

    row0 = table.iloc[0]
    assert row0["101"] == 1 and row0["102"] == 1
    assert row0["n_in_nest"] == 2

    row9 = table.iloc[9]
    assert row9["101"] == 1 and row9["102"] == 0
    assert row9["n_in_nest"] == 1


def test_group_occupancy_table_n_in_nest_matches_definition(gap_fill_sqlite_factory):
    # General invariant: n_in_nest must equal the count of 1s across the
    # animal columns, for every row, regardless of scenario specifics.
    rows_a = make_frames([(1, 5), (0, 5), (1, 5)], animal_id=101)
    rows_b = make_frames([(0, 5), (1, 10)], animal_id=102)
    rows_c = make_frames([(1, 15)], animal_id=103)

    paths = {
        "101": gap_fill_sqlite_factory("a101.sqlite", rows_a),
        "102": gap_fill_sqlite_factory("a102.sqlite", rows_b),
        "103": gap_fill_sqlite_factory("a103.sqlite", rows_c),
    }
    matrix = build_occupancy_matrix(paths)
    table = group_occupancy_table(matrix, ["101", "102", "103"])

    expected_n_in_nest = (table[["101", "102", "103"]] == 1).sum(axis=1)
    assert (table["n_in_nest"] == expected_n_in_nest).all()


def test_group_occupancy_table_preserves_unresolved_as_minus_one(gap_fill_sqlite_factory):
    # Animal 102 is unresolved (-1) for the first 3 frames -- these must
    # show up as -1 in that animal's own column, NOT be silently
    # collapsed to 0 (which would misrepresent "unknown" as "confirmed
    # absent").
    rows_a = make_frames([(1, 10)], animal_id=101)
    rows_b = make_frames([(-1, 3), (1, 7)], animal_id=102)

    path_a = gap_fill_sqlite_factory("a101.sqlite", rows_a)
    path_b = gap_fill_sqlite_factory("a102.sqlite", rows_b)

    matrix = build_occupancy_matrix({"101": path_a, "102": path_b})
    table = group_occupancy_table(matrix, ["101", "102"])

    unresolved_rows = table.iloc[0:3]
    assert (unresolved_rows["102"] == -1).all()
    # n_in_nest still correctly counts animal 101 as present in these
    # frames despite animal 102's unresolved status -- it is not NaN'd
    # out wholesale the way the older group_occupancy_profile() is.
    assert (unresolved_rows["n_in_nest"] == 1).all()


def test_group_occupancy_table_column_order_matches_labels_argument(gap_fill_sqlite_factory):
    rows_a = make_frames([(1, 5)], animal_id=101)
    rows_b = make_frames([(1, 5)], animal_id=102)
    rows_c = make_frames([(1, 5)], animal_id=103)
    paths = {
        "101": gap_fill_sqlite_factory("a101.sqlite", rows_a),
        "102": gap_fill_sqlite_factory("a102.sqlite", rows_b),
        "103": gap_fill_sqlite_factory("a103.sqlite", rows_c),
    }
    matrix = build_occupancy_matrix(paths)
    table = group_occupancy_table(matrix, ["103", "101", "102"])
    assert list(table.columns) == ["FRAMENUMBER", "n_in_nest", "103", "101", "102"]
