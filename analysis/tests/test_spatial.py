import numpy as np
import pytest

from analysis.src.io import load_positions
from analysis.src.spatial import pairwise_distance, proximity_summary, locomotor_distance


def test_pairwise_distance_basic(detection_sqlite_factory):
    rows = [
        {"FRAMENUMBER": 0, "ANIMALID": 101, "MASS_X": 0.0, "MASS_Y": 0.0},
        {"FRAMENUMBER": 0, "ANIMALID": 102, "MASS_X": 3.0, "MASS_Y": 4.0},
    ]
    path = detection_sqlite_factory("d.sqlite", rows)
    positions = load_positions(path, [101, 102])
    distance = pairwise_distance(positions, 101, 102)
    assert distance.iloc[0] == pytest.approx(5.0)  # 3-4-5 triangle


def test_pairwise_distance_nan_when_one_animal_missing(detection_sqlite_factory):
    rows = [
        {"FRAMENUMBER": 0, "ANIMALID": 101, "MASS_X": 0.0, "MASS_Y": 0.0},
        {"FRAMENUMBER": 1, "ANIMALID": 101, "MASS_X": 1.0, "MASS_Y": 1.0},
        {"FRAMENUMBER": 1, "ANIMALID": 102, "MASS_X": 2.0, "MASS_Y": 2.0},
    ]
    path = detection_sqlite_factory("d.sqlite", rows)
    positions = load_positions(path, [101, 102])
    distance = pairwise_distance(positions, 101, 102)
    assert np.isnan(distance.loc[0])  # animal 102 absent at frame 0
    assert not np.isnan(distance.loc[1])


def test_proximity_summary_contact_threshold(detection_sqlite_factory):
    rows = [
        {"FRAMENUMBER": i, "ANIMALID": 101, "MASS_X": 0.0, "MASS_Y": 0.0}
        for i in range(5)
    ] + [
        {"FRAMENUMBER": i, "ANIMALID": 102, "MASS_X": float(i), "MASS_Y": 0.0}
        for i in range(5)
    ]
    path = detection_sqlite_factory("d.sqlite", rows)
    positions = load_positions(path, [101, 102])
    distance = pairwise_distance(positions, 101, 102)  # distances 0,1,2,3,4
    result = proximity_summary(distance, contact_threshold=2.0)
    assert result["n_valid_frames"] == 5
    assert result["seconds_in_contact"] > 0
    # frames with distance <= 2.0: distances 0,1,2 -> 3 of 5 frames
    assert result["fraction_valid_time_in_contact"] == pytest.approx(3 / 5)


def test_proximity_summary_empty_series_returns_zero_valid():
    import pandas as pd
    empty = pd.Series([np.nan, np.nan])
    result = proximity_summary(empty, contact_threshold=1.0)
    assert result == {"n_valid_frames": 0}


def test_locomotor_distance_excludes_large_frame_gaps(detection_sqlite_factory):
    # Frames 0,1,2 adjacent (moves 1 unit each = 2 units total);
    # then a big detection gap to frame 50 (must be excluded, not
    # collapsed into one long straight-line segment).
    rows = [
        {"FRAMENUMBER": 0, "ANIMALID": 101, "MASS_X": 0.0, "MASS_Y": 0.0},
        {"FRAMENUMBER": 1, "ANIMALID": 101, "MASS_X": 1.0, "MASS_Y": 0.0},
        {"FRAMENUMBER": 2, "ANIMALID": 101, "MASS_X": 2.0, "MASS_Y": 0.0},
        {"FRAMENUMBER": 50, "ANIMALID": 101, "MASS_X": 100.0, "MASS_Y": 0.0},
    ]
    path = detection_sqlite_factory("d.sqlite", rows)
    positions = load_positions(path, [101])
    result = locomotor_distance(positions, 101, max_frame_gap=1)
    assert result["total_distance"] == pytest.approx(2.0)  # only the two 1-unit hops
    assert result["n_excluded_gap_segments"] == 1


def test_locomotor_distance_single_frame_returns_zero(detection_sqlite_factory):
    rows = [{"FRAMENUMBER": 0, "ANIMALID": 101, "MASS_X": 0.0, "MASS_Y": 0.0}]
    path = detection_sqlite_factory("d.sqlite", rows)
    positions = load_positions(path, [101])
    result = locomotor_distance(positions, 101)
    assert result["total_distance"] == 0.0
    assert result["n_valid_segments"] == 0
