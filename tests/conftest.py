import importlib.util
import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).parent.parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _load_module(filename, module_name):
    """
    Load one of the pipeline's numerically-prefixed scripts as an importable
    module. Standard `import` can't reach these filenames (they aren't valid
    Python identifiers), so we load by path instead.
    """
    path = REPO_ROOT / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def binary_search_module():
    # Safe to import directly: GUI bootstrap is already __main__-guarded.
    return _load_module("2.lmt_binary_search.py", "lmt_binary_search")


@pytest.fixture(scope="session")
def gap_fill_module():
    # Requires the __main__ guard added in this change; without it this
    # import attempts to open a Tk window.
    return _load_module("1.lmt_gap_fill.py", "lmt_gap_fill")


@pytest.fixture(scope="session")
def preprocessing_module():
    # Safe to import directly: CLI entry point is __main__-guarded.
    return _load_module("0.Preprocessing.py", "preprocessing")