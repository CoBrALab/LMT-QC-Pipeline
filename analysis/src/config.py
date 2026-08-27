"""
analysis/src/config.py

Loads and validates analysis/config/analysis_config.yaml.

Why this exists
----------------
Every experiment-specific value (which SQLite file belongs to which
animal, which animals are dams vs. babysitters, bin widths, contact
thresholds, where outputs should be written) is external configuration,
not a hard-coded constant in any analysis script -- so the same code runs
unmodified against a different cage, session, or animal-ID mapping. This
module is the single place that reads and validates that configuration;
analysis/scripts/run_analysis.py never parses YAML itself.

This repository has no existing config-file mechanism (every existing
script takes 100% of its inputs as CLI arguments), so this introduces one
new dependency (PyYAML) for the analysis layer specifically. See
analysis/README.md for the `uv add pyyaml` instruction.
"""

import pathlib

import yaml

REQUIRED_TOP_LEVEL_KEYS = ["output_dir", "animals"]
REQUIRED_ANIMAL_KEYS = ["role", "gap_fill_sqlite"]
VALID_ROLES = {"dam", "babysitter"}


def load_config(config_path) -> dict:
    """
    What it does
    ------------
    Reads analysis_config.yaml and validates its structure before any
    analysis code runs.

    Why it exists
    -------------
    Failing fast and clearly on a malformed/incomplete config (missing
    animal, bad role name, unresolvable path) is much cheaper to debug
    than a cryptic KeyError deep inside a metric function halfway through
    a multi-animal run.

    Inputs
    ------
    config_path : str or pathlib.Path
        Path to a YAML file. See analysis/config/analysis_config.yaml for
        the expected shape.

    Outputs
    -------
    dict: the parsed YAML, with two additions:
        - every animal's "gap_fill_sqlite" value is resolved to an
          absolute pathlib.Path (relative paths in the YAML are resolved
          relative to the config file's own directory, not the current
          working directory the script happens to be run from).
        - "output_dir" is likewise resolved to an absolute pathlib.Path.

    Logic
    -----
    1. Parse YAML.
    2. Check every key in REQUIRED_TOP_LEVEL_KEYS is present.
    3. For every entry in "animals", check every key in
       REQUIRED_ANIMAL_KEYS is present and "role" is one of VALID_ROLES.
    4. Resolve every path field relative to the config file's directory.

    Assumptions
    -----------
    animal IDs (the keys under "animals") are used verbatim as labels
    throughout the analysis layer (e.g. in co-occupancy output columns
    IN_NEST_<id>) -- keep them as the same integer ANIMALID values used in
    the SQLite files themselves, not an arbitrary nickname, so cross-
    referencing a result back to a specific SQLite file stays
    unambiguous.

    Failure modes
    -------------
    - Missing required key (top-level or per-animal): raises ValueError
      naming exactly which key and which animal.
    - Invalid role string: raises ValueError listing the valid set.
    - config file not found / invalid YAML: raises the underlying
      FileNotFoundError / yaml.YAMLError, not swallowed.
    - A gap_fill_sqlite path that doesn't exist on disk is NOT checked
      here (this function only validates the config's STRUCTURE, not
      that referenced files exist) -- that failure surfaces naturally
      and clearly from analysis.src.io.load_gap_fill_analysis() when the
      script actually tries to read it.

    Validation
    ----------
    Run analysis/tests/test_config.py, which exercises every failure mode
    above against small synthetic YAML strings.

    Integration
    -----------
    Called once, at the top of analysis/scripts/run_analysis.py.
    """
    config_path = pathlib.Path(config_path)
    with open(config_path, "r") as f:
        config = yaml.safe_load(f)

    if config is None:
        raise ValueError(f"{config_path} is empty or not valid YAML.")

    missing_top = [k for k in REQUIRED_TOP_LEVEL_KEYS if k not in config]
    if missing_top:
        raise ValueError(f"{config_path}: missing required key(s): {missing_top}")

    if not config["animals"]:
        raise ValueError(f"{config_path}: 'animals' must contain at least one entry.")

    config_dir = config_path.resolve().parent

    for animal_id, animal_cfg in config["animals"].items():
        missing = [k for k in REQUIRED_ANIMAL_KEYS if k not in animal_cfg]
        if missing:
            raise ValueError(
                f"{config_path}: animal {animal_id} missing required key(s): {missing}"
            )
        if animal_cfg["role"] not in VALID_ROLES:
            raise ValueError(
                f"{config_path}: animal {animal_id} has role "
                f"'{animal_cfg['role']}', must be one of {sorted(VALID_ROLES)}."
            )
        animal_cfg["gap_fill_sqlite"] = (
            config_dir / animal_cfg["gap_fill_sqlite"]
        ).resolve()

    config["output_dir"] = (config_dir / config["output_dir"]).resolve()

    if config.get("processed_detection_sqlite"):
        config["processed_detection_sqlite"] = (
            config_dir / config["processed_detection_sqlite"]
        ).resolve()

    return config


def animals_by_role(config: dict, role: str) -> list:
    """
    What it does
    ------------
    Returns the list of animal IDs (as they appear in the config's
    "animals" mapping) matching a given role.

    Why it exists
    -------------
    analysis/scripts/run_analysis.py needs "the two dams" and "the
    two babysitters" as label lists to pass to
    analysis.src.co_occupancy.co_occupancy_seconds() -- deriving them from
    role, once, here, avoids hard-coding specific animal IDs into the
    script itself.

    Inputs
    ------
    config : output of load_config().
    role : "dam" or "babysitter".

    Outputs
    -------
    list of animal IDs (same type as the YAML keys -- typically int).

    Failure modes
    -------------
    Returns an empty list (not an error) if no animal has that role --
    the calling script should check for this before attempting a
    dam-dam or babysitter-babysitter comparison that needs at least 2.
    """
    return [aid for aid, cfg in config["animals"].items() if cfg["role"] == role]
