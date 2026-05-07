"""Fusion parser integration.

Phase 0 skeleton — defines the public surface and argv translation, but does not
yet invoke the subprocess. All entry points raise NotImplementedError so the
flag can land safely without exposing a half-finished code path.

See docs/arch/fusion_parser_design.md for the full design and rollout plan.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import TYPE_CHECKING, List

from dbt.exceptions import FusionParserError

if TYPE_CHECKING:
    from dbt.cli.flags import Flags
    from dbt.config import RuntimeConfig
    from dbt.contracts.graph.manifest import Manifest


def parse_with_fusion(flags: "Flags", runtime_config: "RuntimeConfig") -> "Manifest":
    """Invoke fs parse, load the resulting manifest.json, return runtime Manifest.

    Phase 0: not yet wired up. Raises NotImplementedError.

    Phase 1 will:
      1. Build argv from flags via _build_argv.
      2. Run subprocess; surface stderr as structured events.
      3. Load <target>/manifest.json via WritableManifest.read_and_check_versions.
      4. Convert to runtime Manifest via Manifest.from_writable_manifest.
      5. Delete stale partial_parse.msgpack.
    """
    raise NotImplementedError(
        "Fusion parser integration is not yet implemented. "
        "Set --no-use-fusion-parser (default) to use dbt-core's own parser."
    )


def _build_argv(flags: "Flags") -> List[str]:
    """Translate dbt-core flags into fs CLI args.

    The base command is taken from flags.FUSION_PARSER_COMMAND (default 'fs parse')
    and split with shlex so users can configure subcommands or wrappers.

    Forwarded flags (must affect manifest output):
      --project-dir, --profiles-dir, --profile, --target,
      --target-path, --vars, --packages-install-path

    Open question: does fs accept dbt-core's flag names verbatim? v1 assumes yes.
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
    """Remove partial_parse.msgpack on first fusion run.

    The msgpack cache is owned by dbt-core's parser; in fusion mode it would
    be both stale (no longer written) and potentially misleading (different
    file_id mappings). Deleting it on entry to fusion mode is harmless if
    absent and unambiguous if present.
    """
    msgpack = target_path / "partial_parse.msgpack"
    if msgpack.exists():
        msgpack.unlink()


__all__ = ["parse_with_fusion", "FusionParserError"]
