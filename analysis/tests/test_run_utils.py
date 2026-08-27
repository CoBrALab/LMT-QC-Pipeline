import datetime

from analysis.src.run_utils import create_run_output_dir


def test_create_run_output_dir_exact_format(tmp_path):
    fixed_time = datetime.datetime(2026, 8, 27, 19, 6, 4)
    run_dir = create_run_output_dir(tmp_path, now=fixed_time)
    assert run_dir.name == "2026-08-27_19-06-04"
    assert run_dir.parent == tmp_path
    assert run_dir.exists()
    assert run_dir.is_dir()


def test_create_run_output_dir_creates_parent_if_missing(tmp_path):
    parent = tmp_path / "does" / "not" / "exist" / "yet"
    assert not parent.exists()
    run_dir = create_run_output_dir(parent, now=datetime.datetime(2026, 1, 1, 0, 0, 0))
    assert parent.exists()
    assert run_dir.exists()


def test_create_run_output_dir_never_collides_same_second(tmp_path):
    fixed_time = datetime.datetime(2026, 8, 27, 19, 6, 4)
    run_dir_1 = create_run_output_dir(tmp_path, now=fixed_time)
    run_dir_2 = create_run_output_dir(tmp_path, now=fixed_time)
    run_dir_3 = create_run_output_dir(tmp_path, now=fixed_time)

    assert run_dir_1 != run_dir_2 != run_dir_3
    assert run_dir_1.name == "2026-08-27_19-06-04"
    assert run_dir_2.name == "2026-08-27_19-06-04_2"
    assert run_dir_3.name == "2026-08-27_19-06-04_3"
    for d in (run_dir_1, run_dir_2, run_dir_3):
        assert d.exists()


def test_create_run_output_dir_defaults_to_real_now(tmp_path):
    before = datetime.datetime.now()
    run_dir = create_run_output_dir(tmp_path)
    after = datetime.datetime.now()

    parsed = datetime.datetime.strptime(run_dir.name, "%Y-%m-%d_%H-%M-%S")
    # Allow a 1-second window on either side for real-clock timing slop.
    assert before.replace(microsecond=0) <= parsed <= after.replace(microsecond=0) + datetime.timedelta(seconds=1)


def test_create_run_output_dir_different_seconds_no_suffix(tmp_path):
    run_dir_1 = create_run_output_dir(tmp_path, now=datetime.datetime(2026, 1, 1, 12, 0, 0))
    run_dir_2 = create_run_output_dir(tmp_path, now=datetime.datetime(2026, 1, 1, 12, 0, 1))
    assert run_dir_1.name == "2026-01-01_12-00-00"
    assert run_dir_2.name == "2026-01-01_12-00-01"
