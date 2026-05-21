"""Import smoke tests.

Imports every Bedrock Python module to catch:
- syntax errors
- broken imports (renamed/moved modules)
- circular import problems
- missing dependencies

Runs in ~1 second on the dev box. Should be the first thing run after
any refactor that moves/renames modules.

This catches the failure mode the e2e suite catches only after ~10 min
of VM boot — and catches it for ALL modules, not just the ones the
e2e happens to exercise.
"""
from __future__ import annotations

import importlib
import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "installer"))


def _module_paths():
    """Every importable .py under installer/lib + mgmt, expressed as
    dotted module paths. Skips test files and the iso-build staged
    copies (those mirror installer/lib and would double-test)."""
    roots = [
        ("installer.lib", REPO_ROOT / "installer" / "lib"),
        ("mgmt", REPO_ROOT / "mgmt"),
        ("bedrock_d", REPO_ROOT / "bedrock_d"),
    ]
    out: list[str] = []
    for prefix, root in roots:
        if not root.exists():
            continue
        for py in sorted(root.rglob("*.py")):
            rel = py.relative_to(root)
            parts = list(rel.with_suffix("").parts)
            # Skip __pycache__ and dunder modules
            if any(p.startswith("__") for p in parts if p != "__init__"):
                continue
            if parts[-1] == "__init__":
                parts = parts[:-1]
            if not parts:
                continue
            dotted = ".".join([prefix] + parts) if parts else prefix
            out.append(dotted)
    return out


# Modules with heavy side-effects on import we want to skip. Empty for
# now; add here if we find one (e.g. a module that opens a socket at
# import-time — those are bugs anyway).
_SKIP: set[str] = set()

# Third-party deps that bedrock-d nodes have installed but a bare dev
# box may not. If a module fails to import because one of these is
# missing, we SKIP — that's a dev-box-not-fully-provisioned condition,
# not a code regression. Anything outside this list is a real failure.
_OPTIONAL_DEPS = frozenset({
    "fastapi", "uvicorn", "pydantic", "paramiko", "libvirt",
    "cryptography", "msgpack", "httpx",
})


@pytest.mark.parametrize("module_name", _module_paths())
def test_module_imports(module_name: str):
    """Every Bedrock .py file imports without exception (or skips
    cleanly when a server-side third-party dep isn't on the dev box)."""
    if module_name in _SKIP:
        pytest.skip(f"explicitly skipped: {module_name}")
    try:
        importlib.import_module(module_name)
    except ModuleNotFoundError as e:
        # Walk the missing-module name (its first dotted segment) and
        # skip if it matches a known dev-box-optional dep. Otherwise
        # re-raise — that's a real bug.
        missing = (e.name or "").split(".")[0]
        if missing and missing in _OPTIONAL_DEPS:
            pytest.skip(f"dev-box-optional dep missing: {missing}")
        raise


def test_at_least_one_module_found():
    """Sanity: the discovery itself works."""
    mods = _module_paths()
    assert len(mods) > 10, (
        f"expected to discover >10 modules, found {len(mods)}: {mods}"
    )


def test_no_duplicate_modules():
    """Same module discovered twice would silently shadow."""
    mods = _module_paths()
    assert len(mods) == len(set(mods)), "duplicates: " + str(
        [m for m in mods if mods.count(m) > 1]
    )
