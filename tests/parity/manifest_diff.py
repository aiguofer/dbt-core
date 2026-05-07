"""Structured diff between an fs-produced manifest.json and a core-produced one.

Importable library plus CLI. Vendored from the internal-analytics
diff_manifests.py prototype, restructured around a DiffReport object so
tests can assert is_empty() and CI can dump JSON.

Usage as library:

    from tests.parity.manifest_diff import diff_manifests, load_allowlist

    report = diff_manifests(
        core_path="target-core/manifest.json",
        fs_path="target-fs/manifest.json",
        allowlist=load_allowlist("tests/parity/allowlists/known_drift.json"),
    )
    assert report.is_empty(), report.summary()

CLI:

    python -m tests.parity.manifest_diff \\
        --core target-core/manifest.json \\
        --fs target-fs/manifest.json \\
        --allowlist tests/parity/allowlists/known_drift.json \\
        --json
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple


# --- Field policies --------------------------------------------------------

# Top-level metadata fields whose values are inherently volatile and meaningless
# to compare. Always ignored regardless of allowlist.
IGNORE_LEAF_PATHS: Set[str] = {
    "metadata.generated_at",
    "metadata.invocation_id",
    "metadata.invocation_started_at",
    "metadata.run_started_at",
    "metadata.dbt_version",
    "metadata.dbt_schema_version",
    "metadata.user_id",
    "metadata.project_id",
}

# Per-item fields that legitimately drift between parsers (compile-stage outputs
# or run-time artifacts that have nothing to do with parse fidelity).
IGNORE_ITEM_FIELDS: Set[str] = {
    "created_at",
    "raw_code",
    "compiled_code",
    "checksum",
    "extra_ctes_injected",
    "extra_ctes",
    "build_path",
    "compiled_path",
    "patch_path",
    "_event_status",
}

# Top-level mapping collections we know about. Anything not listed here is
# compared as opaque equality by the metadata pass.
DICT_COLLECTIONS: Tuple[str, ...] = (
    "nodes",
    "sources",
    "macros",
    "docs",
    "exposures",
    "metrics",
    "groups",
    "selectors",
    "saved_queries",
    "semantic_models",
    "unit_tests",
    "functions",
    "disabled",
)


# --- Report types ----------------------------------------------------------

@dataclasses.dataclass
class CollectionDiff:
    name: str
    fs_count: int
    core_count: int
    only_fs_ids: List[str]
    only_core_ids: List[str]
    fields_only_in_fs: Dict[str, int]
    fields_only_in_core: Dict[str, int]
    type_mismatches: Dict[str, int]
    value_mismatches: Dict[str, int]
    type_samples: Dict[str, List[str]]
    value_samples: Dict[str, List[str]]

    def is_empty(self) -> bool:
        return not (
            self.only_fs_ids
            or self.only_core_ids
            or self.fields_only_in_fs
            or self.fields_only_in_core
            or self.type_mismatches
            or self.value_mismatches
        )


@dataclasses.dataclass
class DiffReport:
    top_level_only_fs: List[str]
    top_level_only_core: List[str]
    collections: List[CollectionDiff]

    def is_empty(self) -> bool:
        return (
            not self.top_level_only_fs
            and not self.top_level_only_core
            and all(c.is_empty() for c in self.collections)
        )

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(dataclasses.asdict(self), indent=indent, default=str)

    def summary(self) -> str:
        lines: List[str] = []
        if self.top_level_only_fs:
            lines.append(f"top-level keys only in fs: {self.top_level_only_fs}")
        if self.top_level_only_core:
            lines.append(f"top-level keys only in core: {self.top_level_only_core}")
        for c in self.collections:
            if c.is_empty():
                continue
            lines.append(
                f"=== {c.name} === "
                f"fs={c.fs_count} core={c.core_count} "
                f"only_fs={len(c.only_fs_ids)} only_core={len(c.only_core_ids)}"
            )
            if c.fields_only_in_fs:
                lines.append(f"  fields only in fs: {dict(_top(c.fields_only_in_fs))}")
            if c.fields_only_in_core:
                lines.append(f"  fields only in core: {dict(_top(c.fields_only_in_core))}")
            if c.type_mismatches:
                lines.append(f"  type mismatches: {dict(_top(c.type_mismatches))}")
                for path, samples in c.type_samples.items():
                    for s in samples[:1]:
                        lines.append(f"    e.g. {path}: {s}")
            if c.value_mismatches:
                lines.append(f"  value mismatches: {dict(_top(c.value_mismatches))}")
                for path, samples in c.value_samples.items():
                    for s in samples[:1]:
                        lines.append(f"    e.g. {path}: {s}")
        return "\n".join(lines) if lines else "no differences"


def _top(counter_like: Dict[str, int], n: int = 10) -> List[Tuple[str, int]]:
    return Counter(counter_like).most_common(n)


# --- Allowlist -------------------------------------------------------------

@dataclasses.dataclass
class Allowlist:
    """Per-collection sets of allowed field paths.

    Schema (json):
        {
          "nodes": {
            "fields_only_in_fs": ["unrendered_config", ...],
            "fields_only_in_core": [...],
            "type_mismatches": [...],
            "value_mismatches": [...]
          },
          ...
        }
    """

    by_collection: Dict[str, Dict[str, Set[str]]] = dataclasses.field(default_factory=dict)

    def is_allowed(self, collection: str, bucket: str, path: str) -> bool:
        """Allowlist entries match the path or any of its dotted ancestors:
        an entry of `unrendered_config` suppresses `unrendered_config` and
        every `unrendered_config.<...>` descendant.
        """
        coll = self.by_collection.get(collection) or {}
        allowed = coll.get(bucket) or set()
        if path in allowed:
            return True
        parts = path.split(".")
        for i in range(1, len(parts)):
            if ".".join(parts[:i]) in allowed:
                return True
        return False


def load_allowlist(path: Optional[str]) -> Optional[Allowlist]:
    if path is None:
        return None
    raw = json.loads(Path(path).read_text())
    by_collection: Dict[str, Dict[str, Set[str]]] = {}
    for coll, buckets in raw.items():
        # Allow `_`-prefixed keys (e.g. _doc) to embed comments in the JSON.
        if coll.startswith("_") or not isinstance(buckets, dict):
            continue
        by_collection[coll] = {bucket: set(paths) for bucket, paths in buckets.items()}
    return Allowlist(by_collection=by_collection)


# --- Helpers ---------------------------------------------------------------

def _type_name(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        return "str"
    if isinstance(v, list):
        return "list"
    if isinstance(v, dict):
        return "dict"
    return type(v).__name__


def _field_paths(d: Any, prefix: str = "") -> Set[str]:
    out: Set[str] = set()
    if isinstance(d, dict):
        for k, v in d.items():
            path = f"{prefix}.{k}" if prefix else k
            out.add(path)
            if isinstance(v, dict):
                out |= _field_paths(v, path)
    return out


def _get_path(d: Any, path: str) -> Any:
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _normalize(v: Any) -> Any:
    """Treat `meta: None` and `meta: {}` as equal; same for tags None vs []."""
    if v is None:
        return None
    if isinstance(v, dict):
        return {k: _normalize(val) for k, val in v.items() if val not in (None, {}, [])}
    if isinstance(v, list):
        return [_normalize(x) for x in v]
    return v


def _truncate(v: Any, n: int = 60) -> str:
    s = json.dumps(v, default=str) if not isinstance(v, str) else v
    if len(s) > n:
        s = s[: n - 1] + "…"
    return s


def _is_empty_collection_sentinel(ft: str, ct: str, fv: Any, cv: Any) -> bool:
    """null vs empty dict/list is noise — treat as equal."""
    if (ft, ct) in (("null", "dict"), ("dict", "null")):
        if (isinstance(fv, dict) and not fv) or (isinstance(cv, dict) and not cv):
            return True
    if (ft, ct) in (("null", "list"), ("list", "null")):
        if (isinstance(fv, list) and not fv) or (isinstance(cv, list) and not cv):
            return True
    return False


# --- Core diff -------------------------------------------------------------

def _diff_collection(
    name: str,
    fs_items: Dict[str, Any],
    core_items: Dict[str, Any],
    *,
    max_samples: int,
    field_filter: Optional[str],
    allowlist: Optional[Allowlist],
    max_id_samples: int,
) -> CollectionDiff:
    fs_keys = set(fs_items)
    core_keys = set(core_items)
    only_fs = sorted(fs_keys - core_keys)[:max_id_samples]
    only_core = sorted(core_keys - fs_keys)[:max_id_samples]
    shared = fs_keys & core_keys

    only_fs_field_count: Counter = Counter()
    only_core_field_count: Counter = Counter()
    type_mismatch: Counter = Counter()
    value_mismatch: Counter = Counter()
    type_samples: Dict[str, List[str]] = defaultdict(list)
    value_samples: Dict[str, List[str]] = defaultdict(list)

    def _allowed(bucket: str, path: str) -> bool:
        return bool(allowlist and allowlist.is_allowed(name, bucket, path))

    for uid in shared:
        fa = fs_items[uid]
        ca = core_items[uid]
        if not isinstance(fa, dict) or not isinstance(ca, dict):
            continue
        fs_paths = _field_paths(fa)
        core_paths = _field_paths(ca)

        for p in fs_paths - core_paths:
            top = p.split(".")[0]
            if top in IGNORE_ITEM_FIELDS:
                continue
            if field_filter and not p.startswith(field_filter):
                continue
            if _allowed("fields_only_in_fs", p):
                continue
            only_fs_field_count[p] += 1
        for p in core_paths - fs_paths:
            top = p.split(".")[0]
            if top in IGNORE_ITEM_FIELDS:
                continue
            if field_filter and not p.startswith(field_filter):
                continue
            if _allowed("fields_only_in_core", p):
                continue
            only_core_field_count[p] += 1

        for p in fs_paths & core_paths:
            top = p.split(".")[0]
            if top in IGNORE_ITEM_FIELDS:
                continue
            if field_filter and not p.startswith(field_filter):
                continue
            fv = _get_path(fa, p)
            cv = _get_path(ca, p)
            ft, ct = _type_name(fv), _type_name(cv)
            if ft != ct:
                if _is_empty_collection_sentinel(ft, ct, fv, cv):
                    continue
                if _allowed("type_mismatches", p):
                    continue
                type_mismatch[p] += 1
                if len(type_samples[p]) < max_samples:
                    type_samples[p].append(
                        f"{uid}: fs={ft}({_truncate(fv)}) core={ct}({_truncate(cv)})"
                    )
            elif ft not in ("dict", "list") and _normalize(fv) != _normalize(cv):
                if _allowed("value_mismatches", p):
                    continue
                value_mismatch[p] += 1
                if len(value_samples[p]) < max_samples:
                    value_samples[p].append(
                        f"{uid}: fs={_truncate(fv)} core={_truncate(cv)}"
                    )

    return CollectionDiff(
        name=name,
        fs_count=len(fs_keys),
        core_count=len(core_keys),
        only_fs_ids=only_fs,
        only_core_ids=only_core,
        fields_only_in_fs=dict(only_fs_field_count),
        fields_only_in_core=dict(only_core_field_count),
        type_mismatches=dict(type_mismatch),
        value_mismatches=dict(value_mismatch),
        type_samples=dict(type_samples),
        value_samples=dict(value_samples),
    )


def diff_manifests(
    core_path: str | Path,
    fs_path: str | Path,
    *,
    collections: Optional[Iterable[str]] = None,
    field_filter: Optional[str] = None,
    max_samples: int = 3,
    max_id_samples: int = 5,
    allowlist: Optional[Allowlist] = None,
) -> DiffReport:
    """Compare two manifest.json files and return a structured DiffReport.

    Volatile metadata + compile-stage fields are unconditionally ignored
    (see IGNORE_LEAF_PATHS, IGNORE_ITEM_FIELDS). The allowlist suppresses
    additional documented drift on a per-collection-and-bucket basis.
    """
    fs = json.loads(Path(fs_path).read_text())
    core = json.loads(Path(core_path).read_text())

    targets = list(collections) if collections else list(DICT_COLLECTIONS)

    coll_diffs: List[CollectionDiff] = []
    for c in targets:
        if c not in fs and c not in core:
            continue
        coll_diffs.append(
            _diff_collection(
                c,
                fs.get(c, {}) or {},
                core.get(c, {}) or {},
                max_samples=max_samples,
                field_filter=field_filter,
                allowlist=allowlist,
                max_id_samples=max_id_samples,
            )
        )

    return DiffReport(
        top_level_only_fs=sorted(set(fs) - set(core)),
        top_level_only_core=sorted(set(core) - set(fs)),
        collections=coll_diffs,
    )


# --- CLI -------------------------------------------------------------------

def _main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Diff fs vs core manifest.json")
    ap.add_argument("--core", required=True, help="path to core manifest.json")
    ap.add_argument("--fs", required=True, help="path to fs manifest.json")
    ap.add_argument("--collection", help="limit to one collection")
    ap.add_argument("--field", help="limit field-level diff to this dot-path")
    ap.add_argument("--max-samples", type=int, default=3)
    ap.add_argument("--allowlist", help="JSON allowlist of expected drift")
    ap.add_argument("--json", action="store_true", help="emit JSON report")
    ap.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero if any unallowed diff is found",
    )
    args = ap.parse_args(argv)

    report = diff_manifests(
        core_path=args.core,
        fs_path=args.fs,
        collections=[args.collection] if args.collection else None,
        field_filter=args.field,
        max_samples=args.max_samples,
        allowlist=load_allowlist(args.allowlist),
    )

    if args.json:
        print(report.to_json())
    else:
        print(report.summary())

    return 1 if (args.strict and not report.is_empty()) else 0


if __name__ == "__main__":
    sys.exit(_main())
