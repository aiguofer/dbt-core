import json
from pathlib import Path

import pytest

from tests.parity.manifest_diff import (
    Allowlist,
    diff_manifests,
    load_allowlist,
    _main,
)


def _write(tmp_path: Path, name: str, data: dict) -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(data))
    return p


def _manifest(nodes=None, sources=None, **extra):
    base = {
        "metadata": {"dbt_schema_version": "v12", "dbt_version": "0.0.0"},
        "nodes": nodes or {},
        "sources": sources or {},
    }
    base.update(extra)
    return base


class TestEqualManifests:
    def test_identical_is_empty(self, tmp_path: Path):
        m = _manifest(nodes={"model.x.a": {"name": "a", "config": {"materialized": "view"}}})
        a = _write(tmp_path, "core.json", m)
        b = _write(tmp_path, "fs.json", m)
        report = diff_manifests(core_path=a, fs_path=b)
        assert report.is_empty()


class TestIgnoredFields:
    def test_volatile_per_item_fields_ignored(self, tmp_path: Path):
        core = _manifest(
            nodes={"m.x.a": {"name": "a", "raw_code": "select 1", "checksum": {"name": "sha256"}}}
        )
        fs = _manifest(
            nodes={"m.x.a": {"name": "a", "raw_code": "SELECT 1", "checksum": {"name": "none"}}}
        )
        report = diff_manifests(
            core_path=_write(tmp_path, "c.json", core),
            fs_path=_write(tmp_path, "f.json", fs),
        )
        assert report.is_empty()

    def test_null_vs_empty_dict_ignored(self, tmp_path: Path):
        core = _manifest(nodes={"m.x.a": {"name": "a", "meta": None}})
        fs = _manifest(nodes={"m.x.a": {"name": "a", "meta": {}}})
        report = diff_manifests(
            core_path=_write(tmp_path, "c.json", core),
            fs_path=_write(tmp_path, "f.json", fs),
        )
        assert report.is_empty()


class TestRealDiffs:
    def test_only_fs_id_detected(self, tmp_path: Path):
        core = _manifest(nodes={"m.x.a": {"name": "a"}})
        fs = _manifest(nodes={"m.x.a": {"name": "a"}, "m.x.b": {"name": "b"}})
        report = diff_manifests(
            core_path=_write(tmp_path, "c.json", core),
            fs_path=_write(tmp_path, "f.json", fs),
        )
        assert not report.is_empty()
        nodes = next(c for c in report.collections if c.name == "nodes")
        assert nodes.only_fs_ids == ["m.x.b"]

    def test_field_only_in_fs(self, tmp_path: Path):
        core = _manifest(nodes={"m.x.a": {"name": "a"}})
        fs = _manifest(nodes={"m.x.a": {"name": "a", "unrendered_config": {"x": 1}}})
        report = diff_manifests(
            core_path=_write(tmp_path, "c.json", core),
            fs_path=_write(tmp_path, "f.json", fs),
        )
        nodes = next(c for c in report.collections if c.name == "nodes")
        assert "unrendered_config" in nodes.fields_only_in_fs

    def test_value_mismatch_collected(self, tmp_path: Path):
        core = _manifest(nodes={"m.x.a": {"name": "a", "alias": "core_alias"}})
        fs = _manifest(nodes={"m.x.a": {"name": "a", "alias": "fs_alias"}})
        report = diff_manifests(
            core_path=_write(tmp_path, "c.json", core),
            fs_path=_write(tmp_path, "f.json", fs),
        )
        nodes = next(c for c in report.collections if c.name == "nodes")
        assert nodes.value_mismatches.get("alias") == 1


class TestAllowlist:
    def test_allowlist_suppresses_only_in_fs(self, tmp_path: Path):
        core = _manifest(nodes={"m.x.a": {"name": "a"}})
        fs = _manifest(nodes={"m.x.a": {"name": "a", "unrendered_config": {"x": 1}}})
        allowlist = Allowlist(
            by_collection={"nodes": {"fields_only_in_fs": {"unrendered_config"}}}
        )
        report = diff_manifests(
            core_path=_write(tmp_path, "c.json", core),
            fs_path=_write(tmp_path, "f.json", fs),
            allowlist=allowlist,
        )
        assert report.is_empty()

    def test_load_allowlist_from_file(self, tmp_path: Path):
        path = tmp_path / "allow.json"
        path.write_text(json.dumps({"nodes": {"value_mismatches": ["alias"]}}))
        loaded = load_allowlist(str(path))
        assert loaded is not None
        assert loaded.is_allowed("nodes", "value_mismatches", "alias")
        assert not loaded.is_allowed("nodes", "value_mismatches", "name")

    def test_load_allowlist_none(self):
        assert load_allowlist(None) is None


class TestReportSerialization:
    def test_to_json_roundtrip(self, tmp_path: Path):
        core = _manifest(nodes={"m.x.a": {"name": "a"}})
        fs = _manifest(nodes={"m.x.b": {"name": "b"}})
        report = diff_manifests(
            core_path=_write(tmp_path, "c.json", core),
            fs_path=_write(tmp_path, "f.json", fs),
        )
        parsed = json.loads(report.to_json())
        assert "collections" in parsed
        assert parsed["collections"][0]["name"] == "nodes"


class TestCli:
    def test_strict_exit_on_diff(self, tmp_path: Path, capsys):
        core = _write(tmp_path, "c.json", _manifest(nodes={"a": {"name": "a"}}))
        fs = _write(tmp_path, "f.json", _manifest(nodes={"b": {"name": "b"}}))
        rc = _main(["--core", str(core), "--fs", str(fs), "--strict"])
        assert rc == 1

    def test_strict_zero_when_clean(self, tmp_path: Path):
        m = _manifest(nodes={"a": {"name": "a"}})
        core = _write(tmp_path, "c.json", m)
        fs = _write(tmp_path, "f.json", m)
        rc = _main(["--core", str(core), "--fs", str(fs), "--strict"])
        assert rc == 0

    def test_json_output(self, tmp_path: Path, capsys):
        m = _manifest(nodes={"a": {"name": "a"}})
        core = _write(tmp_path, "c.json", m)
        fs = _write(tmp_path, "f.json", m)
        _main(["--core", str(core), "--fs", str(fs), "--json"])
        out = capsys.readouterr().out
        assert "collections" in json.loads(out)
