# Fusion Parser Integration — dbt-core Design

Design for the dbt-core-side changes to support delegating parsing to an external subcommand (`fs parse`). Companion to `fusion_parser_status.md` (which lists the fs-side blockers F1-F13 we depend on but do not own).

This doc is the implementation plan for what dbt-core ships, broken into landable PR-sized chunks.

## Design Principles

1. **Hidden flag first.** Land the integration behind an undocumented `--use-fusion-parser` flag so we can iterate without committing to public surface area. Mirrors how `--use-experimental-parser` (`core/dbt/cli/params.py:778`) shipped.
2. **Single branch point.** All conditional fusion behavior lives in `setup_manifest()` (`core/dbt/cli/requires.py:418`). No fusion-aware code scattered through parsers, tasks, or compilation.
3. **Preserve existing flow.** The non-fusion path must be byte-identical to today. No refactors that could regress current behavior.
4. **Defensive on load.** dbt-core does not trust fs's manifest blindly — version-check, schema-validate, and (optionally) normalize known-divergent fields on load. Fail loudly on incompatibility.
5. **Plugin-respectful.** Plugin `get_manifest_artifacts` (read-only enrichment) re-runs after load. Plugin `get_nodes` (mid-parse injection) is **unsupported in v1**; fail-fast if a registered plugin advertises it.
6. **Reuse, don't reinvent.** Manifest deserialization (`WritableManifest.read_and_check_versions`), `Manifest.from_writable_manifest`, plugin manager — all already exist.

## Component Map

| # | File / module | Purpose | Status |
|---|---|---|---|
| C1 | `core/dbt/cli/params.py` | New flag definitions | New |
| C2 | `core/dbt/contracts/project.py` | `ProjectFlags` additions for `flags:` block | New |
| C3 | `core/dbt/cli/main.py` | Wire flags into `global_flags` | Modify |
| C4 | `core/dbt/parser/fusion.py` | Subprocess invocation + load + post-processing | **New module** |
| C5 | `core/dbt/cli/requires.py` | Branch in `setup_manifest()` | Modify |
| C6 | `core/dbt/parser/manifest.py` | Partial-parse skip + plugin enrichment hooks | Modify |
| C7 | `core/dbt/events/types.py` | New event types for fusion lifecycle | New |
| C8 | `core/dbt/exceptions.py` | New exception classes | New |
| C9 | `tests/parity/` | Parity test infrastructure | **New tree** |
| C10 | `tests/functional/fusion_parser/` | Functional tests | **New tree** |

## Phasing — What Lands When

Each phase is a coherent, mergeable set of changes. Earlier phases must be on `main` before later ones.

### Phase 0 — Plumbing (no behavior change)

Lands the flag, `Flags` resolution, and a no-op fusion module. No user-visible changes; safe to merge before fs is ready.

- C1, C2, C3 — flag definitions
- C4 — module skeleton (raises `NotImplementedError` on invocation)
- C7, C8 — event + exception types
- Unit tests for flag resolution

### Phase 1 — Functional integration (hidden flag)

Hooks fusion into `setup_manifest`, runs end-to-end on a real project. Behind a hidden flag (`--use-fusion-parser`). Requires fs to be present and (mostly) producing valid output — but not yet meeting full fidelity.

- C4 — full implementation
- C5 — branch in `setup_manifest`
- C6 — partial-parse skip + plugin re-run
- Functional tests with a mocked fs binary

### Phase 2 — Test infrastructure

Parity gate that can run against any fs build.

- C9 — `tests/parity/manifest_diff.py` based on `internal-analytics/scripts/diff_manifests.py`
- C10 — fixture matrix
- CI job (gated; fs binary on the runner)

### Phase 3 — UX polish & rollout

Documentation, error messages, behavior flag for default-on rollout. Gated on fs-side blockers F1-F13 being resolved.

- Help text, docs site
- Behavior flag `require_fusion_parser` in `ProjectFlags`
- Telemetry hooks
- Migration guide

## Component Designs

### C1 — Flag definitions (`cli/params.py`)

Added near the existing parser flags (`:721`, `:778`). Two new options:

```python
use_fusion_parser = _create_option_and_track_env_var(
    "--use-fusion-parser/--no-use-fusion-parser",
    envvar="DBT_USE_FUSION_PARSER",
    help="Delegate parsing to the fusion parser (fs). Hidden in v1.",
    default=False,
    hidden=True,  # remove in Phase 3
)

fusion_parser_command = _create_option_and_track_env_var(
    "--fusion-parser-command",
    envvar="DBT_FUSION_PARSER_COMMAND",
    help="Command to invoke for the fusion parser. Defaults to 'fs parse'.",
    default="fs parse",
    hidden=True,
)
```

Hidden in v1 (`hidden=True`). Default `False`. Resolution chain CLI > env > project flags handled by existing `cli/flags.py:277-308` machinery — no new code needed.

### C2 — `ProjectFlags` additions (`contracts/project.py:333-377`)

Two additions to make the flag settable in `dbt_project.yml`:

```python
@dataclass
class ProjectFlags(ExtensibleDbtClassMixin):
    # ... existing fields ...
    use_fusion_parser: Optional[bool] = None
    fusion_parser_command: Optional[str] = None
```

Goes alongside `static_parser` (`:346`) and `use_experimental_parser` (`:349`).

A future Phase 3 addition:

```python
    require_fusion_parser: bool = False  # behavior flag for default-on rollout
```

### C3 — Wire into `global_flags` (`cli/main.py:130-153`)

Add to the decorator stack alongside `@p.use_experimental_parser` (`:147`):

```python
@p.use_experimental_parser
@p.use_fusion_parser
@p.fusion_parser_command
```

### C4 — Fusion module (`core/dbt/parser/fusion.py`)

**New module.** Owns subprocess invocation, manifest loading, and post-processing. Single public entry point used by `setup_manifest`.

```python
# core/dbt/parser/fusion.py

import os
import shlex
import subprocess
from pathlib import Path
from typing import List, Optional

from dbt.artifacts.schemas.manifest import WritableManifest
from dbt.config import RuntimeConfig
from dbt.contracts.graph.manifest import Manifest
from dbt.cli.flags import Flags
from dbt.exceptions import (
    FusionParserMissingError,
    FusionParserError,
    FusionParserSchemaError,
)
from dbt.events.types import (
    FusionParserStarted,
    FusionParserCompleted,
    FusionParserSubprocessFailed,
)
from dbt_common.events.functions import fire_event


def parse_with_fusion(flags: Flags, runtime_config: RuntimeConfig) -> Manifest:
    """Invoke fs parse, load the resulting manifest.json, return runtime Manifest.

    Branch entry from setup_manifest. Behavior:
      1. Build argv from flags.
      2. Run subprocess; surface stderr as structured events.
      3. Load <target>/manifest.json via WritableManifest.read_and_check_versions.
      4. Convert to runtime Manifest via Manifest.from_writable_manifest.
      5. Post-process (plugin enrichment hook called by setup_manifest, not here).
    """
    argv = _build_argv(flags)
    target_path = Path(runtime_config.project_target_path)
    manifest_path = target_path / "manifest.json"

    fire_event(FusionParserStarted(command=" ".join(argv)))

    _run_fusion(argv)

    if not manifest_path.exists():
        raise FusionParserError(
            f"fs parse completed but {manifest_path} was not produced."
        )

    writable = _load_writable_manifest(manifest_path)
    manifest = Manifest.from_writable_manifest(writable)

    fire_event(FusionParserCompleted(node_count=len(manifest.nodes)))
    return manifest


def _build_argv(flags: Flags) -> List[str]:
    """Translate dbt-core flags into fs CLI args.

    Open question: does fs accept dbt-core's flag names verbatim,
    or do we need a translation table? v1 assumes verbatim.
    """
    base = shlex.split(flags.FUSION_PARSER_COMMAND)
    forwarded = []

    if flags.PROJECT_DIR:
        forwarded += ["--project-dir", str(flags.PROJECT_DIR)]
    if flags.PROFILES_DIR:
        forwarded += ["--profiles-dir", str(flags.PROFILES_DIR)]
    if flags.PROFILE:
        forwarded += ["--profile", flags.PROFILE]
    if flags.TARGET:
        forwarded += ["--target", flags.TARGET]
    if flags.TARGET_PATH:
        forwarded += ["--target-path", str(flags.TARGET_PATH)]
    if flags.VARS:
        # canonical: dbt-core resolves --vars; pass YAML string forward
        forwarded += ["--vars", _serialize_vars(flags.VARS)]

    return base + forwarded


def _run_fusion(argv: List[str]) -> None:
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as e:
        raise FusionParserMissingError(argv[0]) from e

    if result.returncode != 0:
        fire_event(
            FusionParserSubprocessFailed(
                returncode=result.returncode,
                stderr=result.stderr,
            )
        )
        raise FusionParserError(
            f"fs parse failed (exit {result.returncode}): {result.stderr}"
        )

    # forward fs's stdout/stderr as events for log fidelity
    _forward_fs_output(result.stdout, result.stderr)


def _load_writable_manifest(path: Path) -> WritableManifest:
    try:
        return WritableManifest.read_and_check_versions(str(path))
    except Exception as e:
        raise FusionParserSchemaError(
            f"Could not load fusion-produced manifest at {path}: {e}"
        ) from e


def _serialize_vars(vars_dict) -> str:
    import yaml
    return yaml.safe_dump(vars_dict, default_flow_style=True).strip()


def _forward_fs_output(stdout: str, stderr: str) -> None:
    """Future: parse JSON-lines from stderr into typed events.
    v1: emit as plain log events.
    """
    # ... event dispatch ...
    pass
```

**Open in this module:**
- `_build_argv` flag translation: confirm fs accepts our flag names (open question #1 from status doc).
- `_forward_fs_output`: fs error format TBD (open question #2). v1 uses plain text; v2 parses JSON-lines.

### C5 — Branch in `setup_manifest` (`cli/requires.py:401-437`)

Minimal modification. The existing short-circuit for pre-populated manifest stays untouched. New branch sits between flags-check and `parse_manifest()` call:

```python
def setup_manifest(ctx: Context, write: bool = True, write_perf_info: bool = False):
    """Load the manifest and add it to the context."""
    req_strs = ["flags", "profile", "project", "runtime_config"]
    reqs = [ctx.obj.get(dep) for dep in req_strs]

    if None in reqs:
        raise DbtProjectError("flags, profile, project, and runtime_config required for manifest")

    runtime_config = ctx.obj["runtime_config"]
    flags = ctx.obj["flags"]
    catalogs = load_catalogs(flags.PROJECT_DIR, ctx.obj["project"].project_name, flags.VARS)
    active_integrations = [get_active_write_integration(catalog) for catalog in catalogs]
    ctx.obj["catalogs"] = catalogs

    if ctx.obj.get("manifest") is None:
        if flags.USE_FUSION_PARSER:
            from dbt.parser.fusion import parse_with_fusion
            ctx.obj["manifest"] = parse_with_fusion(flags, runtime_config)
            _enrich_with_plugins(ctx.obj["manifest"])  # post-load hook (C6)
            adapter = get_adapter(runtime_config)
        else:
            ctx.obj["manifest"] = parse_manifest(
                runtime_config,
                write_perf_info,
                write,
                ctx.obj["flags"].write_json,
                active_integrations,
            )
            adapter = get_adapter(runtime_config)
    else:
        register_adapter(runtime_config, get_mp_context())
        adapter = get_adapter(runtime_config)
        adapter.set_macro_context_generator(generate_runtime_macro_context)
        adapter.set_macro_resolver(ctx.obj["manifest"])
        query_header_context = generate_query_header_context(adapter.config, ctx.obj["manifest"])
        adapter.connections.set_query_header(query_header_context)
        for integration in active_integrations:
            adapter.add_catalog_integration(integration)

    fire_deferred_events(event_group_type=EventGroupType.PARSE)
```

**Note:** the fusion branch still needs the adapter wiring at `:430-433` — should be hoisted to a helper and called from both branches. Sketch above doesn't show that hoist for clarity; the real change is:

```python
if ctx.obj.get("manifest") is None:
    if flags.USE_FUSION_PARSER:
        ctx.obj["manifest"] = parse_with_fusion(flags, runtime_config)
        _enrich_with_plugins(ctx.obj["manifest"])
    else:
        ctx.obj["manifest"] = parse_manifest(...)
    _wire_adapter(runtime_config, ctx.obj["manifest"], active_integrations)
else:
    _wire_adapter(runtime_config, ctx.obj["manifest"], active_integrations)
```

Where `_wire_adapter` encapsulates today's `:428-435`.

### C6 — Partial parse skip + plugin enrichment (`parser/manifest.py`)

**Partial parse:** add a guard at the top of `read_manifest_for_partial_parse()` (`:1026`) and `write_manifest_for_partial_parse()` (`:904`):

```python
def read_manifest_for_partial_parse(self) -> Optional[Manifest]:
    if get_flags().USE_FUSION_PARSER:
        return None
    # ... existing body ...

def write_manifest_for_partial_parse(self):
    if get_flags().USE_FUSION_PARSER:
        return
    # ... existing body ...
```

Plus a one-time stale-cache cleanup in `parse_with_fusion` itself:

```python
def _delete_stale_partial_parse(target_path: Path) -> None:
    msgpack = target_path / "partial_parse.msgpack"
    if msgpack.exists():
        msgpack.unlink()
```

**Plugin enrichment:** new helper in `parser/manifest.py`, called from `setup_manifest`:

```python
def enrich_manifest_with_plugin_artifacts(manifest: Manifest) -> None:
    """Re-runs the read-only plugin enrichment hook against a fusion-loaded
    manifest. Fail-fast if any registered plugin uses get_nodes (mid-parse
    injection) — unsupported in v1.
    """
    pm = plugins.get_plugin_manager()
    for plugin in pm.plugins:
        if plugin.has_get_nodes():
            raise DbtPluginError(
                f"Plugin {plugin.name} uses get_nodes(), which is not supported "
                f"in fusion parser mode in v1."
            )
    pm.get_manifest_artifacts(manifest)
```

**Semantic manifest:** in fusion mode, skip `write_semantic_manifest()` (called from `parser/manifest.py:2535`). fs already produced one. Add a flag check at the call site.

### C7 — Events (`events/types.py`)

New proto events — follow the existing pattern (`MainReportArgs` etc). Names:

- `FusionParserStarted(command: str)` — info
- `FusionParserCompleted(node_count: int, elapsed_ms: int)` — info
- `FusionParserSubprocessFailed(returncode: int, stderr: str)` — error
- `FusionParserSchemaIncompatible(version_found: str, version_expected: str)` — error
- `FusionParserStatePartialDisabled` — info (when partial-parse silently overridden)
- `FusionParserMixedStateWarning(prior_parser: str, current_parser: str)` — warn

### C8 — Exceptions (`exceptions.py`)

```python
class FusionParserError(DbtRuntimeError):
    """Generic fusion parser failure."""

class FusionParserMissingError(FusionParserError):
    """fs binary not on PATH or not executable."""

class FusionParserSchemaError(FusionParserError):
    """fs-produced manifest fails schema validation."""

class FusionParserVersionError(FusionParserError):
    """fs version incompatible with this dbt-core."""
```

All inherit from `DbtRuntimeError` so existing error handling in CLI catches them cleanly.

### C9 — Parity test infrastructure (`tests/parity/`)

New directory. Vendored from `internal-analytics/scripts/diff_manifests.py`. Two artifacts:

```
tests/parity/
├── __init__.py
├── manifest_diff.py        # importable diff library
├── allowlists/
│   └── known_drift.json    # documented expected drift, by field path
└── conftest.py             # pytest fixtures
```

**`manifest_diff.py`** API:

```python
def diff_manifests(
    core_path: Path,
    fs_path: Path,
    *,
    strict: bool = False,
    allowlist: Optional[dict] = None,
    normalize_paths: bool = True,
) -> DiffReport: ...

class DiffReport:
    def is_empty(self) -> bool: ...
    def to_json(self) -> str: ...
    def summary(self) -> str: ...
```

CLI wrapper for ad-hoc invocation:

```bash
python -m tests.parity.manifest_diff \
    --core target-core/manifest.json \
    --fs target-fs/manifest.json \
    --strict --json --allowlist tests/parity/allowlists/known_drift.json
```

Exit non-zero on any unallowed diff.

### C10 — Functional tests (`tests/functional/fusion_parser/`)

Pattern mirrors `tests/functional/experimental_parser/test_all_experimental_parser.py`.

Three layers:

1. **Mocked fs (fast).** A pytest fixture that builds a fake fs binary writing a known-good `manifest.json` from a fixture file. Tests dbt-core's load + branch logic in isolation.
2. **Real fs (slow, gated).** Runs against real `fs` if available; skipped otherwise. Asserts parity for each fixture.
3. **Edge cases.** Each fs blocker (F1-F13) gets a regression fixture. Allowlist tracks "still broken on fs side."

Fixture matrix:
- Plain models
- Generic tests with kwargs, long names, vars
- Multi-snapshot files
- Custom search paths in `dbt_project.yml`
- Packages (deps installed)
- `on-run-start` / `on-run-end` hooks
- Mixed-parser `--state` directories

## Conflict Resolution

Implemented in `parse_with_fusion` and `setup_manifest`:

| Combination | Behavior | Where |
|---|---|---|
| `USE_FUSION_PARSER=True`, fs not on PATH | `FusionParserMissingError` | `_run_fusion` |
| `USE_FUSION_PARSER=True`, fs version unsupported | `FusionParserVersionError` (Phase 2+) | `_load_writable_manifest` after metadata read |
| `USE_FUSION_PARSER=True`, `PARTIAL_PARSE=True` | Silent override + `FusionParserStatePartialDisabled` event | `read/write_manifest_for_partial_parse` |
| `USE_FUSION_PARSER=True`, `--state path/` produced by core | Warn + `FusionParserMixedStateWarning` | `PreviousState.__init__` (read `metadata.generated_by_parser` once available; v1: skip) |
| `USE_FUSION_PARSER=True`, plugin uses `get_nodes` | `DbtPluginError` | `enrich_manifest_with_plugin_artifacts` |
| `USE_FUSION_PARSER=False`, fs on PATH | No-op | n/a |

## Performance Considerations

- **Subprocess overhead.** Process startup + JSON serialization+deserialization round-trip. For large projects, the deserialize side is the long pole. Measured: ~1.4s on internal-analytics (5,000 nodes) for `read_and_check_versions` + `from_writable_manifest`. Subprocess startup negligible.
- **No partial-parse cache.** Every fusion run is full reparse from dbt-core's perspective. fs's cold-parse time must beat core's warm partial-parse time (0.86s msgpack load on internal-analytics) for fusion mode to be a perf win. **This is the rollout gate to Phase 3.**
- **`perf_info.json`.** Replace dbt-core's parse-stage timings with fusion-shaped timings: `subprocess_elapsed`, `manifest_load_elapsed`, `plugin_enrichment_elapsed`. Same file, different schema fields. Or: leave `perf_info.json` empty/skipped in fusion mode and document.

## Open Questions Carried Forward

These are not blockers for Phase 0/1 but need answers before Phase 2/3:

1. **fs CLI flag names** — verbatim match or translation layer? Affects `_build_argv` (C4).
2. **fs error format** — JSON-lines on stderr (preferred) vs exit code only? Affects `_forward_fs_output` (C4).
3. **fs binary distribution** — bundled with dbt-core, separate package, user-installed? Affects preflight UX.
4. **Plugin `get_nodes` audit** — list real plugins using this hook before we declare it unsupported.
5. **`metadata.generated_by_parser` field** — when does v13 schema bump happen? Mixed-state detection currently degrades to "we can't tell" without it.
6. **`unrendered_config` semantics** — once fs fixes F9, confirm parity end-to-end with `state:modified.config`.
7. **`fusion_parser_command` security** — accepts arbitrary shell command. Lock down to allowlist? Subprocess uses `shlex.split` (no shell), so injection risk is bounded but still a foot-gun.

## Definition of Done — Phase 1

- `--use-fusion-parser` flag exists, hidden, defaults `False`.
- With flag on: dbt-core invokes fs, loads manifest, runs `dbt run` end-to-end on internal-analytics with fs blockers F1-F13 not yet resolved (i.e., expected failures documented).
- With flag off: zero behavior change vs main.
- Mocked-fs functional tests pass in CI.
- Plugin `get_nodes` fail-fast verified.
- Stale `partial_parse.msgpack` deleted on first fusion run.
- Adapter wiring helper extracted (`_wire_adapter`).

## Definition of Done — Phase 2

- Real-fs CI job green against internal-analytics with documented allowlist.
- Parity diff library importable from tests + as CLI.
- Fixture matrix covers every blocker F1-F13 with a regression test.
- Performance benchmark suite in place; baselines recorded.

## Definition of Done — Phase 3

- F1-F13 closed on fs side; allowlist mostly empty.
- Flag becomes public; help text un-hidden.
- `require_fusion_parser` behavior flag added.
- Migration guide published.
- Telemetry tracking adoption + failure rate.
