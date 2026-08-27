import pytest
import yaml

from analysis.src.config import load_config, animals_by_role


def _write_yaml(tmp_path, content: dict, filename="config.yaml"):
    path = tmp_path / filename
    with open(path, "w") as f:
        yaml.safe_dump(content, f)
    return path


VALID_CONFIG = {
    "output_dir": "outputs",
    "occupancy_timeline_bin_seconds": 60,
    "proximity_contact_threshold": None,
    "animals": {
        101: {"role": "dam", "gap_fill_sqlite": "a101.sqlite"},
        102: {"role": "dam", "gap_fill_sqlite": "a102.sqlite"},
        103: {"role": "babysitter", "gap_fill_sqlite": "a103.sqlite"},
        104: {"role": "babysitter", "gap_fill_sqlite": "a104.sqlite"},
    },
}


def test_load_config_valid(tmp_path):
    path = _write_yaml(tmp_path, VALID_CONFIG)
    config = load_config(path)
    assert len(config["animals"]) == 4
    # Paths must be resolved to absolute paths relative to the config file.
    assert config["animals"][101]["gap_fill_sqlite"].is_absolute()
    assert config["output_dir"].is_absolute()


def test_load_config_missing_top_level_key(tmp_path):
    bad = dict(VALID_CONFIG)
    del bad["output_dir"]
    path = _write_yaml(tmp_path, bad)
    with pytest.raises(ValueError, match="output_dir"):
        load_config(path)


def test_load_config_missing_animal_key(tmp_path):
    import copy
    bad = copy.deepcopy(VALID_CONFIG)
    del bad["animals"][101]["role"]
    path = _write_yaml(tmp_path, bad)
    with pytest.raises(ValueError, match="role"):
        load_config(path)


def test_load_config_invalid_role(tmp_path):
    import copy
    bad = copy.deepcopy(VALID_CONFIG)
    bad["animals"][101]["role"] = "grandparent"
    path = _write_yaml(tmp_path, bad)
    with pytest.raises(ValueError, match="role"):
        load_config(path)


def test_load_config_empty_animals(tmp_path):
    import copy
    bad = copy.deepcopy(VALID_CONFIG)
    bad["animals"] = {}
    path = _write_yaml(tmp_path, bad)
    with pytest.raises(ValueError, match="animals"):
        load_config(path)


def test_load_config_empty_file(tmp_path):
    path = tmp_path / "empty.yaml"
    path.write_text("")
    with pytest.raises(ValueError, match="empty"):
        load_config(path)


def test_animals_by_role(tmp_path):
    path = _write_yaml(tmp_path, VALID_CONFIG)
    config = load_config(path)
    assert sorted(animals_by_role(config, "dam")) == [101, 102]
    assert sorted(animals_by_role(config, "babysitter")) == [103, 104]


def test_animals_by_role_none_of_that_role(tmp_path):
    import copy
    only_dams = copy.deepcopy(VALID_CONFIG)
    only_dams["animals"] = {101: {"role": "dam", "gap_fill_sqlite": "a101.sqlite"}}
    path = _write_yaml(tmp_path, only_dams)
    config = load_config(path)
    assert animals_by_role(config, "babysitter") == []
