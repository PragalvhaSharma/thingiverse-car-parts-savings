"""Shared paths and loaders for the tests.

Run from the repo root:  python -m unittest discover -s tests -v
The tests need only the standard library and beautifulsoup4; the few that need
trimesh, requests or pydantic skip themselves when those are not installed.
"""

import functools
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SEP2 = ROOT / "data" / "sep2" / "cars9k_final.csv.gz"
DEPLOYED = ROOT / "data" / "sep2" / "deployed_sha256.json"
ROMAIN = ROOT / "data" / "inputs" / "validated_matches_2026-08-13.csv"
PRIUS = ROOT / "data" / "inputs" / "toyota_prius_parts.csv"
EXPECTED = ROOT / "examples" / "expected_sep2.json"


def has(module) -> bool:
    return importlib.util.find_spec(module) is not None


@functools.cache
def _sep2():
    from carsavings.schema import read_rows

    return tuple(read_rows(SEP2))


def sep2_rows() -> list:
    """The 9,190 Sep 2 rows, as fresh dicts (nested lists are shared, so do not change them)."""
    return [dict(r) for r in _sep2()]


def load_script(name):
    """Import script/<name>.py as a module."""
    path = ROOT / "script" / ("%s.py" % name)
    spec = importlib.util.spec_from_file_location("script_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
