"""Pytest fixtures for fusion parity tests.

These fixtures are shared by tests/functional/fusion_parser/parity/. The
real fs binary is gated behind a marker — when DBT_FS_BIN points at an
executable, real-fs tests run; otherwise they skip.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from tests.parity.manifest_diff import (
    Allowlist,
    DiffReport,
    diff_manifests,
    load_allowlist,
)


_DEFAULT_ALLOWLIST = (
    Path(__file__).resolve().parent / "allowlists" / "known_drift.json"
)


@pytest.fixture(scope="session")
def parity_allowlist() -> Allowlist:
    return load_allowlist(str(_DEFAULT_ALLOWLIST))  # type: ignore[return-value]


@pytest.fixture(scope="session")
def fs_binary() -> str:
    """Path to a real fs binary, or skip the test.

    Resolution order:
      1. $DBT_FS_BIN (explicit path, may be absolute or PATH-resolvable).
      2. `fs` on PATH.
    Tests that depend on this fixture are auto-skipped when neither resolves.
    """
    candidate = os.environ.get("DBT_FS_BIN") or "fs"
    resolved = shutil.which(candidate)
    if not resolved:
        pytest.skip(f"fs binary not found (looked for {candidate!r}); set DBT_FS_BIN to enable parity")
    return resolved


@pytest.fixture
def parity_diff(parity_allowlist):
    """Returns a callable that diffs two manifest paths against the allowlist."""

    def _diff(core_path: Path, fs_path: Path) -> DiffReport:
        return diff_manifests(
            core_path=core_path,
            fs_path=fs_path,
            allowlist=parity_allowlist,
        )

    return _diff
