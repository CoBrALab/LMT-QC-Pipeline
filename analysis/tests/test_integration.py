"""
analysis/tests/test_integration.py

End-to-end test of analysis/scripts/run_analysis.py: builds a
synthetic, multi-animal dataset (2 dams, 2 babysitters) matching the
pipeline's real output schema, writes a real analysis_config.yaml
pointing at it, invokes the script exactly as a user would from the
command line (via subprocess, not by importing its internals), and
checks that every expected output CSV is produced with sane content.

Every run now writes into a freshly created, timestamped subdirectory of
its parent output directory (see analysis.src.run_utils) -- these tests
locate that subdirectory via _find_run_dir() rather than assuming output
files land directly in the configured/passed output_dir, and separately
cover the --output-dir CLI override and per-run uniqueness.

This is the "integration test on a representative input" required
alongside the unit tests in test_bouts.py / test_occupancy.py /
test_co_occupancy.py / test_spatial.py / test_io.py / test_config.py /
test_run_utils.py.
"""

import subprocess
import sys
import pathlib

import pandas as pd
import yaml

from analysis.tests.conftest import make_frames, _write_gap_fill_sqlite, _write_detection_sqlite

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _find_run_dir(parent_dir: pathlib.Path) -> pathlib.Path:
    """
    Test helper: every run_analysis.py execution creates exactly
    one new timestamped subdirectory under `parent_dir` (see
    analysis.src.run_utils.create_run_output_dir). Asserts that
    invariant and returns that single subdirectory, so tests can locate
    a run's actual output files without hard-coding a timestamp.
    """
    subdirs = [p for p in parent_dir.iterdir() if p.is_dir()]
    assert len(subdirs) == 1, (
        f"Expected exactly one run subdirectory under {parent_dir}, found "
        f"{len(subdirs)}: {subdirs}"
    )
    return subdirs[0]


def _build_synthetic_session(tmp_path):
    """
    Shared setup for the tests below: 2 dams + 2 babysitters with a mix
    of overlapping and non-overlapping nest bouts, plus a shared
    processed DETECTION table for the spatial metrics. Returns the
    config dict (NOT yet written to disk) so each test can adjust
    output_dir/output-dir handling differently.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)

    animals = {
        101: make_frames([(1, 20), (0, 10), (-1, 5), (1, 5)], animal_id=101),
        102: make_frames([(0, 10), (1, 20), (0, 10)], animal_id=102),
        103: make_frames([(1, 15), (0, 25)], animal_id=103),
        104: make_frames([(0, 20), (1, 20)], animal_id=104),
    }
    animal_config = {}
    for animal_id, rows in animals.items():
        path = data_dir / f"lmt_binary_search_A{animal_id}.sqlite"
        _write_gap_fill_sqlite(path, rows, include_fill_source=True)
        role = "dam" if animal_id in (101, 102) else "babysitter"
        animal_config[animal_id] = {"role": role, "gap_fill_sqlite": str(path)}

    detection_rows = []
    for animal_id in animals:
        for frame in range(40):
            detection_rows.append({
                "FRAMENUMBER": frame, "ANIMALID": animal_id,
                "MASS_X": float(animal_id) + frame * 0.1,
                "MASS_Y": float(animal_id),
            })
    detection_path = data_dir / "session_processed.sqlite"
    _write_detection_sqlite(detection_path, detection_rows)

    return {
        "occupancy_timeline_bin_seconds": 1,
        "proximity_contact_threshold": 5.0,
        "animals": animal_config,
        "processed_detection_sqlite": str(detection_path),
    }, animals


def _write_config(tmp_path, config: dict, output_dir: pathlib.Path) -> pathlib.Path:
    full_config = dict(config)
    full_config["output_dir"] = str(output_dir)
    config_path = tmp_path / "analysis_config.yaml"
    with open(config_path, "w") as f:
        yaml.safe_dump(full_config, f)
    return config_path


def _run_script(config_path: pathlib.Path, extra_args: list = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "analysis" / "scripts" / "run_analysis.py"),
            "--config", str(config_path),
            *(extra_args or []),
        ],
        capture_output=True, text=True,
    )


def test_run_analysis_end_to_end(tmp_path):
    config, animals = _build_synthetic_session(tmp_path)
    parent_output_dir = tmp_path / "outputs"
    config_path = _write_config(tmp_path, config, parent_output_dir)

    result = _run_script(config_path)
    assert result.returncode == 0, (
        f"Script failed.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "Done." in result.stdout

    run_dir = _find_run_dir(parent_output_dir)

    # Per-animal outputs
    summary = pd.read_csv(run_dir / "per_animal_summary.csv")
    assert len(summary) == 4
    assert set(summary["animal_id"]) == {101, 102, 103, 104}
    assert (summary["fraction_unresolved"] >= 0).all()

    for animal_id in animals:
        timeline_path = run_dir / f"occupancy_timeline_{animal_id}.csv"
        assert timeline_path.exists()
        timeline = pd.read_csv(timeline_path)
        assert len(timeline) > 0

    events = pd.read_csv(run_dir / "entry_exit_events.csv")
    assert set(events["EVENT_TYPE"].unique()) <= {"ENTRY", "EXIT"}

    # Co-occupancy outputs
    co_occ = pd.read_csv(run_dir / "co_occupancy_summary.csv")
    assert "dam_101_dam_102" in co_occ["group"].values
    assert "babysitter_103_babysitter_104" in co_occ["group"].values
    assert "all_adults" in co_occ["group"].values

    # group_occupancy_profile.csv: FRAMENUMBER + n_in_nest + one column
    # per adult (labels are the animal IDs as configured, e.g. "101").
    profile = pd.read_csv(run_dir / "group_occupancy_profile.csv")
    assert len(profile) > 0
    expected_cols = {"FRAMENUMBER", "n_in_nest", "101", "102", "103", "104"}
    assert expected_cols <= set(profile.columns)
    # n_in_nest must equal the count of 1s across the per-animal columns,
    # for every row -- this is the whole point of the extended table.
    animal_cols = ["101", "102", "103", "104"]
    assert (profile["n_in_nest"] == (profile[animal_cols] == 1).sum(axis=1)).all()
    # Every per-animal value must be one of {1, 0, -1} (confirmed
    # in-nest / confirmed out / unresolved) -- never anything else.
    assert set(profile[animal_cols].values.ravel()) <= {1, 0, -1}

    # --- Spatial outputs (enabled via config) ---
    proximity = pd.read_csv(run_dir / "proximity_summary.csv")
    assert len(proximity) == 6  # C(4,2) pairs

    locomotion = pd.read_csv(run_dir / "locomotion_summary.csv")
    assert len(locomotion) == 4


def test_run_analysis_skips_spatial_when_not_configured(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    parent_output_dir = tmp_path / "outputs"

    rows = make_frames([(1, 10), (0, 10)], animal_id=101)
    path = data_dir / "a101.sqlite"
    _write_gap_fill_sqlite(path, rows, include_fill_source=True)

    config = {
        "occupancy_timeline_bin_seconds": 5,
        "proximity_contact_threshold": None,
        "animals": {101: {"role": "dam", "gap_fill_sqlite": str(path)}},
        # processed_detection_sqlite intentionally omitted
    }
    config_path = _write_config(tmp_path, config, parent_output_dir)

    result = _run_script(config_path)
    assert result.returncode == 0, result.stderr

    run_dir = _find_run_dir(parent_output_dir)
    assert not (run_dir / "proximity_summary.csv").exists()
    assert not (run_dir / "locomotion_summary.csv").exists()
    assert (run_dir / "per_animal_summary.csv").exists()


def test_run_analysis_does_not_write_directly_into_parent(tmp_path):
    # The parent output directory itself must never contain result
    # files directly -- only the one timestamped run subdirectory.
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    parent_output_dir = tmp_path / "outputs"

    rows = make_frames([(1, 10), (0, 10)], animal_id=101)
    path = data_dir / "a101.sqlite"
    _write_gap_fill_sqlite(path, rows, include_fill_source=True)

    config = {
        "occupancy_timeline_bin_seconds": 5,
        "proximity_contact_threshold": None,
        "animals": {101: {"role": "dam", "gap_fill_sqlite": str(path)}},
    }
    config_path = _write_config(tmp_path, config, parent_output_dir)

    result = _run_script(config_path)
    assert result.returncode == 0, result.stderr

    direct_files = [p for p in parent_output_dir.iterdir() if p.is_file()]
    assert direct_files == [], (
        f"Expected no files directly in {parent_output_dir}, found: {direct_files}"
    )
    subdirs = [p for p in parent_output_dir.iterdir() if p.is_dir()]
    assert len(subdirs) == 1


def test_run_analysis_output_dir_flag_overrides_config_parent(tmp_path):
    # --output-dir must override the config's own output_dir as the
    # PARENT directory -- the config's own output_dir should be left
    # completely untouched when this flag is given.
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config_parent = tmp_path / "config_default_outputs"
    override_parent = tmp_path / "custom_location"

    rows = make_frames([(1, 10), (0, 10)], animal_id=101)
    path = data_dir / "a101.sqlite"
    _write_gap_fill_sqlite(path, rows, include_fill_source=True)

    config = {
        "occupancy_timeline_bin_seconds": 5,
        "proximity_contact_threshold": None,
        "animals": {101: {"role": "dam", "gap_fill_sqlite": str(path)}},
    }
    config_path = _write_config(tmp_path, config, config_parent)

    result = _run_script(config_path, extra_args=["--output-dir", str(override_parent)])
    assert result.returncode == 0, result.stderr

    assert not config_parent.exists(), (
        "The config's own output_dir should never be created/written to "
        "when --output-dir is given."
    )
    run_dir = _find_run_dir(override_parent)
    assert (run_dir / "per_animal_summary.csv").exists()


def test_run_analysis_two_runs_get_distinct_directories(tmp_path):
    # Running the script twice against the same parent output directory
    # must never overwrite the first run's results.
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    parent_output_dir = tmp_path / "outputs"

    rows = make_frames([(1, 10), (0, 10)], animal_id=101)
    path = data_dir / "a101.sqlite"
    _write_gap_fill_sqlite(path, rows, include_fill_source=True)

    config = {
        "occupancy_timeline_bin_seconds": 5,
        "proximity_contact_threshold": None,
        "animals": {101: {"role": "dam", "gap_fill_sqlite": str(path)}},
    }
    config_path = _write_config(tmp_path, config, parent_output_dir)

    result_1 = _run_script(config_path)
    result_2 = _run_script(config_path)
    assert result_1.returncode == 0 and result_2.returncode == 0

    subdirs = [p for p in parent_output_dir.iterdir() if p.is_dir()]
    assert len(subdirs) == 2, (
        f"Expected 2 distinct run directories after 2 runs, found {len(subdirs)}: {subdirs}"
    )
    for d in subdirs:
        assert (d / "per_animal_summary.csv").exists()
