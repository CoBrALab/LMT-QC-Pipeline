"""
analysis/src/run_utils.py

Shared helper for every analysis/scripts/run_*.py CLI: each execution
writes its outputs into a freshly created, timestamped subdirectory of a
parent output directory, rather than directly into the parent -- so
repeated runs never silently overwrite a previous run's results, and
every run's outputs are self-contained and individually identifiable by
when they were produced.

Why this exists
----------------
Before this module, every CLI script wrote directly into its configured
output_dir. Two runs against the same config (e.g. re-running after a
config tweak, or a batch of sessions sharing one output_dir) would
silently overwrite each other's files with no record that this happened.
This module is the one place that decides the run-directory naming
scheme, so every script gets the same behavior rather than each
reimplementing (and potentially disagreeing on) it.
"""

import datetime
import pathlib

TIMESTAMP_FORMAT = "%Y-%m-%d_%H-%M-%S"


def create_run_output_dir(parent_dir, now: datetime.datetime = None) -> pathlib.Path:
    """
    What it does
    ------------
    Creates and returns a new, timestamped subdirectory of `parent_dir`
    (e.g. `<parent_dir>/2026-08-27_19-06-04/`), creating `parent_dir`
    itself if it doesn't already exist.

    Why it exists
    -------------
    The single implementation of this repository's "every run gets its
    own timestamped output folder" convention (see the module docstring)
    -- every analysis/scripts/run_*.py CLI calls this instead of
    creating its output directory directly.

    Inputs
    ------
    parent_dir : str or pathlib.Path
        The PARENT directory a new run folder is created inside --
        never written into directly (see module docstring). This is
        typically either a config file's own configured output
        directory, or a user-supplied --output-dir CLI override; the
        caller decides which, this function only creates the run folder
        inside whichever it's given.
    now : datetime.datetime, optional
        The timestamp to name the run folder after. Defaults to the
        real current time (datetime.datetime.now()). Exposed as a
        parameter specifically so tests can pass a fixed value and
        assert on an exact expected folder name, rather than needing to
        parse whatever the real clock happened to read.

    Outputs
    -------
    pathlib.Path to the newly created run directory. The directory is
    guaranteed to exist when this function returns (already created via
    mkdir), and to be freshly created BY THIS CALL -- see Logic for the
    collision-avoidance guarantee.

    Logic
    -----
    The base folder name is `now.strftime(TIMESTAMP_FORMAT)`, i.e.
    second precision (e.g. "2026-08-27_19-06-04"), matching this
    project's own naming convention. Second precision is usually
    sufficient to avoid collisions between separate runs, but two runs
    genuinely starting within the same second ARE possible (e.g. two
    scripts launched back-to-back in a shell loop, or on a fast test
    machine) -- rather than silently reusing (and overwriting the
    contents of) an existing same-second folder, this function detects
    that collision and appends a numeric suffix (_2, _3, ...) until it
    finds a name that doesn't already exist, guaranteeing every call
    gets its own fresh directory regardless of clock resolution.

    Assumptions
    -----------
    parent_dir is writable. No assumption is made about parent_dir
    already existing -- it's created (parents=True) if not.

    Failure modes
    -------------
    None beyond normal filesystem permission errors. A parent_dir that
    already contains a huge number of same-second collision folders
    (extremely unlikely in practice) would iterate the numeric suffix
    correspondingly further before succeeding -- not a realistic
    concern at this pipeline's scale.

    Validation
    ----------
    See analysis/tests/test_run_utils.py: checks the exact folder name
    format against a fixed timestamp, and checks that two calls in
    immediate succession (same real clock second, in a fast test) never
    collide.

    Integration
    -----------
    Called once, near the top of every analysis/scripts/run_*.py CLI's
    main(), after resolving which parent directory to use (config
    default vs. a --output-dir override) but before writing any output
    file.
    """
    parent_dir = pathlib.Path(parent_dir)
    parent_dir.mkdir(parents=True, exist_ok=True)

    now = now or datetime.datetime.now()
    base_name = now.strftime(TIMESTAMP_FORMAT)

    candidate = parent_dir / base_name
    suffix = 2
    while candidate.exists():
        candidate = parent_dir / f"{base_name}_{suffix}"
        suffix += 1

    candidate.mkdir(parents=True, exist_ok=False)
    return candidate
