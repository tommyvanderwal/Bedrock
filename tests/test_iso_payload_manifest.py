"""Guard against ISO-payload drift (lesson_iso_payload_drift).

install.sh fetches lib/ files for an HTTP/network install from an explicit
``LIB_FILES=(...)`` list. A new module added under lib/ but forgotten
in LIB_FILES is silently missing on a network install — and the completeness
backstop in install.sh only runs in file:// (offline-ISO) mode, so the network
path has NO runtime guard. witness_file.py drifted exactly this way.

This test IS the missing CI check: LIB_FILES must equal the set of *.py/*.sql
files actually under lib/. If you add or remove a lib module, update
LIB_FILES in the same change and this test keeps you honest."""
from __future__ import annotations

import os
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO / "installer" / "install.sh"
LIB_DIR = REPO / "lib"


def _listed_lib_files() -> set[str]:
    src = INSTALL_SH.read_text()
    m = re.search(r"LIB_FILES=\((.*?)\)", src, re.S)
    assert m, "LIB_FILES=( ... ) array not found in install.sh"
    return {tok.strip() for tok in m.group(1).split() if tok.strip()}


def _actual_lib_files() -> set[str]:
    return {f for f in os.listdir(LIB_DIR)
            if f.endswith(".py") or f.endswith(".sql")}


def test_lib_files_manifest_matches_source():
    listed = _listed_lib_files()
    actual = _actual_lib_files()
    missing = actual - listed          # in source, not fetched on HTTP install
    stale = listed - actual            # fetched but no longer in source
    assert not missing, (
        f"lib modules missing from install.sh LIB_FILES (a network install "
        f"would ship without them → ModuleNotFoundError): {sorted(missing)}")
    assert not stale, (
        f"install.sh LIB_FILES lists files not in lib/ "
        f"(install.sh would 404 fetching them): {sorted(stale)}")


def test_witness_file_is_in_the_manifest():
    # Regression pin for the specific drift that motivated this test.
    assert "witness_file.py" in _listed_lib_files()
