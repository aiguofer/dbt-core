"""Shared fixtures for the fusion-parser parity matrix.

Each fixture project lives in a subdirectory. A test parses with both
core and a real fs binary, then asserts the manifest_diff is empty modulo
the documented allowlist. Tests skip cleanly when fs is not on PATH.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from dbt.tests.util import run_dbt

# Pull in fs_binary / parity_diff / parity_allowlist from the parity package.
pytest_plugins = ["tests.parity.conftest"]


@pytest.fixture
def core_manifest(project) -> Path:
    """Run core's parser and return the produced manifest.json path."""
    run_dbt(["parse"])
    path = Path(project.project_root) / "target" / "manifest.json"
    assert path.exists(), "core parse did not produce manifest.json"
    return path


@pytest.fixture
def fs_manifest(project, fs_binary, core_manifest) -> Path:
    """Run fs against the same project and return its manifest.json path.

    Uses a separate target directory so the core artifact above isn't
    clobbered.
    """
    fs_target = Path(project.project_root) / "target-fs"
    fs_target.mkdir(exist_ok=True)
    result = subprocess.run(
        [
            fs_binary,
            "parse",
            "--project-dir",
            str(project.project_root),
            "--target-path",
            str(fs_target),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(
            f"fs parse failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    fs_manifest = fs_target / "manifest.json"
    assert fs_manifest.exists(), "fs parse did not produce manifest.json"
    return fs_manifest
