import numpy as np
import pandas as pd
import pytest

from analysis.src.bouts import compute_nest_bouts
from analysis.src.occupancy import (
    total_time_in_nest,
    occupancy_timeline,
    fill_source_composition,
)
from analysis.src.io import DB_FPS
from analysis.tests.conftest import make_frames


def test_total_time_in_nest_basic():
    rows = make_frames([(1, 30), (0, 60)])  # 30 frames in, 60 out
    df = pd.DataFrame(rows)
    bouts = compute_nest_bouts(df, animal_id=101)
    result = total_time_in_nest(bouts)
    assert result["seconds_in_nest"] == pytest.approx(30 / DB_FPS)
    assert result["seconds_out_of_nest"] == pytest.approx(60 / DB_FPS)
    assert result["seconds_unresolved"] == 0.0
    # fraction_in_nest is over RESOLVED time only.
    assert result["fraction_in_nest"] == pytest.approx(30 / 90)


def test_total_time_in_nest_unresolved_reported_separately():
    # This directly exercises the "don't silently drop unresolved time"
    # requirement: fraction_in_nest must be computed over resolved frames
    # only, and fraction_unresolved must be reported, not folded in.
    rows = make_frames([(1, 10), (0, 10), (-1, 80)])
    df = pd.DataFrame(rows)
    bouts = compute_nest_bouts(df, animal_id=101)
    result = total_time_in_nest(bouts)
    assert result["fraction_in_nest"] == pytest.approx(0.5)  # 10 / (10+10)
    assert result["fraction_unresolved"] == pytest.approx(80 / 100)


def test_occupancy_timeline_fractions_sum_to_one():
    rows = make_frames([(1, 45), (0, 45), (-1, 30)])
    df = pd.DataFrame(rows)
    timeline = occupancy_timeline(df, bin_seconds=1.0)  # tiny bins for the test
    frac_sum = timeline[["FRAC_IN_NEST", "FRAC_OUT_OF_NEST", "FRAC_UNRESOLVED"]].sum(axis=1)
    assert np.allclose(frac_sum, 1.0)
    assert timeline["N_FRAMES"].sum() == len(df)


def test_occupancy_timeline_last_bin_is_partial():
    # 100 frames at DB_FPS=30 -> 100/30 = 3.33s of data; with 1s bins,
    # the last bin should have fewer frames than a full bin.
    rows = make_frames([(1, 100)])
    df = pd.DataFrame(rows)
    timeline = occupancy_timeline(df, bin_seconds=1.0)
    full_bin_frames = DB_FPS * 1.0
    assert timeline.iloc[-1]["N_FRAMES"] < full_bin_frames


def test_fill_source_composition_requires_fill_source_column():
    rows = make_frames([(1, 5), (0, 5)])
    df = pd.DataFrame(rows)  # no FILL_SOURCE column at all
    with pytest.raises(ValueError, match="FILL_SOURCE"):
        fill_source_composition(df)


def test_fill_source_composition_fractions_sum_to_one():
    rows = make_frames([(1, 5), (0, 5)])
    df = pd.DataFrame(rows)
    df["FILL_SOURCE"] = ["DETECTED"] * 5 + ["BINARY_SEARCH"] * 5
    result = fill_source_composition(df)
    assert result["FRACTION"].sum() == pytest.approx(1.0)
    assert result.loc["DETECTED", "N_FRAMES"] == 5
