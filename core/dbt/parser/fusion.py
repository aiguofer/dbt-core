"""Fusion parser integration.

Delegates parsing to an external `fs parse` subprocess that produces a
manifest.json on disk. dbt-core then loads that manifest and converts it
to a runtime Manifest, bypassing its own parser entirely.

See docs/arch/fusion_parser_design.md for the full design and rollout plan.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, List

from dbt.artifacts.exceptions import IncompatibleSchemaError
from dbt.artifacts.schemas.manifest import WritableManifest
from dbt.contracts.graph.manifest import Manifest
from dbt.exceptions import (
    FusionParserError,
    FusionParserMissingError,
    FusionParserSchemaError,
    FusionParserVersionError,
)

if TYPE_CHECKING:
    from dbt.cli.flags import Flags
    from dbt.config import RuntimeConfig


def parse_with_fusion(flags: "Flags", runtime_config: "RuntimeConfig") -> Manifest:
    """Invoke fs parse, load the resulting manifest.json, return runtime Manifest.

    Steps:
      1. Build argv from flags.
      2. Run subprocess; raise typed errors on missing binary or non-zero exit.
      3. Load <target>/manifest.json via WritableManifest.read_and_check_versions.
      4. Convert to runtime Manifest via Manifest.from_writable_manifest.
      5. Delete stale partial_parse.msgpack so a later non-fusion run reparses.
    """
    argv = _build_argv(flags)
    target_path = Path(runtime_config.project_target_path)
    manifest_path = target_path / "manifest.json"

    _run_fusion(argv)

    if not manifest_path.exists():
        raise FusionParserError(
            f"fs parse completed but {manifest_path} was not produced."
        )

    writable = _load_writable_manifest(manifest_path)
    manifest = Manifest.from_writable_manifest(writable)
    # build_flat_graph is normally called by ManifestLoader.get_full_manifest;
    # the fusion path bypasses that loader, so populate flat_graph here to
    # power the `graph` context variable (graph.nodes, graph.sources, ...).
    manifest.build_flat_graph()

    _delete_stale_partial_parse(target_path)

    return manifest


def _build_argv(flags: "Flags") -> List[str]:
    """Translate dbt-core flags into fs CLI args.

    The base command is taken from flags.FUSION_PARSER_COMMAND (default 'fs parse')
    and split with shlex so users can configure subcommands or wrappers.

    Forwarded flags (must affect manifest output):
      --project-dir, --profiles-dir, --profile, --target,
      --target-path, --vars, --packages-install-path
    """
    base = shlex.split(getattr(flags, "FUSION_PARSER_COMMAND", "fs parse"))
    forwarded: List[str] = []

    project_dir = getattr(flags, "PROJECT_DIR", None)
    if project_dir:
        forwarded += ["--project-dir", str(project_dir)]

    profiles_dir = getattr(flags, "PROFILES_DIR", None)
    if profiles_dir:
        forwarded += ["--profiles-dir", str(profiles_dir)]

    profile = getattr(flags, "PROFILE", None)
    if profile:
        forwarded += ["--profile", profile]

    target = getattr(flags, "TARGET", None)
    if target:
        forwarded += ["--target", target]

    target_path = getattr(flags, "TARGET_PATH", None)
    if target_path:
        forwarded += ["--target-path", str(target_path)]

    packages_install_path = getattr(flags, "PACKAGES_INSTALL_PATH", None)
    if packages_install_path:
        forwarded += ["--packages-install-path", str(packages_install_path)]

    cli_vars = getattr(flags, "VARS", None)
    if cli_vars:
        forwarded += ["--vars", _serialize_vars(cli_vars)]

    return base + forwarded


def _run_fusion(argv: List[str]) -> None:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, check=False)
    except FileNotFoundError as e:
        raise FusionParserMissingError(
            f"Fusion parser command not found: {argv[0]!r}. "
            f"Ensure 'fs' is installed and on PATH, or set --fusion-parser-command."
        ) from e

    if result.returncode != 0:
        stderr = (result.stderr or "").strip() or "(no stderr)"
        raise FusionParserError(
            f"Fusion parser failed (exit {result.returncode}): {stderr}"
        )


def _load_writable_manifest(path: Path) -> WritableManifest:
    try:
        return WritableManifest.read_and_check_versions(str(path))
    except IncompatibleSchemaError as e:
        raise FusionParserVersionError(
            f"Fusion-produced manifest at {path} has an incompatible schema "
            f"version: expected {e.expected}, found {e.found}."
        ) from e
    except Exception as e:
        raise FusionParserSchemaError(
            f"Could not load fusion-produced manifest at {path}: {e}"
        ) from e


def _serialize_vars(cli_vars) -> str:
    """Serialize the resolved --vars dict to a YAML string for fs.

    dbt-core's --vars is parsed into a dict by click via the YAML param type
    (cli/params.py vars). Forward as a compact YAML string so fs receives a
    single canonical value rather than re-resolving env vars or layered configs.
    """
    import yaml

    if isinstance(cli_vars, str):
        return cli_vars
    return yaml.safe_dump(cli_vars, default_flow_style=True).strip()


def _delete_stale_partial_parse(target_path: Path) -> None:
    """Remove partial_parse.msgpack written by a prior non-fusion run.

    The msgpack cache is owned by dbt-core's parser; in fusion mode it is no
    longer written, and a later non-fusion run would load a cache whose
    file_id mappings predate any fusion-era source changes. Deleting on
    fusion entry is harmless if absent and unambiguous if present.
    """
    msgpack = target_path / "partial_parse.msgpack"
    if msgpack.exists():
        msgpack.unlink()


__all__ = ["parse_with_fusion", "FusionParserError"]
