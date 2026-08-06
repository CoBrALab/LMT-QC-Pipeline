import numpy as np
import pandas as pd


def _run_core_logic(gap_fill_module, df, nest, buffer):
    """
    Exercises the vectorized gap-expansion + ROI logic directly on a
    DataFrame, bypassing run_analysis()'s I/O (DB read/write, GUI dialogs).
    """
    m = gap_fill_module

    def in_roi_vec(x, y, roi):
        return (roi["xmin"] < x) & (x < roi["xmax"]) & (roi["ymin"] < y) & (y < roi["ymax"])

    frames_arr = df["FRAMENUMBER"].to_numpy(dtype=np.int64)
    x_arr = df["MASS_X"].to_numpy(dtype=np.float64)
    y_arr = df["MASS_Y"].to_numpy(dtype=np.float64)

    f1, f2 = frames_arr[:-1], frames_arr[1:]
    x1, y1 = x_arr[:-1], y_arr[:-1]
    x2, y2 = x_arr[1:], y_arr[1:]
    gap = f2 - f1

    in_nest_start = in_roi_vec(x1, y1, nest)
    in_buffer_end = in_roi_vec(x2, y2, buffer)
    gap_mask = gap > 1
    idx = np.nonzero(gap_mask)[0]

    sizes = gap[idx] - 1
    starts = f1[idx] + 1
    offsets = np.concatenate([np.arange(s) for s in sizes]) if sizes.size else np.array([], dtype=np.int64)
    frame_numbers_assumed = np.repeat(starts, sizes) + offsets
    in_nest_value_per_gap = np.where(in_nest_start[idx] & in_buffer_end[idx], 1, -1)
    in_nest_assumed = np.repeat(in_nest_value_per_gap, sizes)

    return frame_numbers_assumed, in_nest_assumed


NEST = {"xmin": 0, "xmax": 10, "ymin": 0, "ymax": 10}
BUFFER = {"xmin": -5, "xmax": 15, "ymin": -5, "ymax": 15}


def test_gap_fill_both_endpoints_qualify_fills_in_nest(gap_fill_module):
    df = pd.DataFrame({
        "FRAMENUMBER": [1, 5],
        "MASS_X": [5.0, 5.0],   # inside NEST at start, inside BUFFER at end
        "MASS_Y": [5.0, 5.0],
    })
    frames, values = _run_core_logic(gap_fill_module, df, NEST, BUFFER)
    assert list(frames) == [2, 3, 4]
    assert set(values) == {1}


def test_gap_fill_endpoint_outside_buffer_stays_uncertain(gap_fill_module):
    df = pd.DataFrame({
        "FRAMENUMBER": [1, 5],
        "MASS_X": [5.0, 100.0],  # end point far outside even the buffer
        "MASS_Y": [5.0, 100.0],
    })
    frames, values = _run_core_logic(gap_fill_module, df, NEST, BUFFER)
    assert list(frames) == [2, 3, 4]
    assert set(values) == {-1}


def test_gap_fill_no_gap_produces_no_assumed_frames(gap_fill_module):
    df = pd.DataFrame({
        "FRAMENUMBER": [1, 2, 3],
        "MASS_X": [5.0, 5.0, 5.0],
        "MASS_Y": [5.0, 5.0, 5.0],
    })
    frames, values = _run_core_logic(gap_fill_module, df, NEST, BUFFER)
    assert len(frames) == 0