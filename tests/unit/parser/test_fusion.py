import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from dbt.exceptions import (
    FusionParserError,
    FusionParserMissingError,
    FusionParserSchemaError,
    FusionParserVersionError,
)
from dbt.parser.fusion import (
    _build_argv,
    _delete_stale_partial_parse,
    _serialize_vars,
    parse_with_fusion,
)


def _flags(**overrides):
    base = {
        "FUSION_PARSER_COMMAND": "fs parse",
        "PROJECT_DIR": None,
        "PROFILES_DIR": None,
        "PROFILE": None,
        "TARGET": None,
        "TARGET_PATH": None,
        "PACKAGES_INSTALL_PATH": None,
        "VARS": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class TestBuildArgv:
    def test_default_command_no_forwards(self):
        assert _build_argv(_flags()) == ["fs", "parse"]

    def test_forwards_all_known_flags(self):
        argv = _build_argv(
            _flags(
                PROJECT_DIR="/proj",
                PROFILES_DIR="/profiles",
                PROFILE="my_profile",
                TARGET="dev",
                TARGET_PATH="target",
                PACKAGES_INSTALL_PATH="dbt_packages",
                VARS={"k": "v"},
            )
        )
        assert argv[:2] == ["fs", "parse"]
        for pair in [
            ("--project-dir", "/proj"),
            ("--profiles-dir", "/profiles"),
            ("--profile", "my_profile"),
            ("--target", "dev"),
            ("--target-path", "target"),
            ("--packages-install-path", "dbt_packages"),
        ]:
            i = argv.index(pair[0])
            assert argv[i + 1] == pair[1]
        i = argv.index("--vars")
        assert "k" in argv[i + 1] and "v" in argv[i + 1]

    def test_custom_command_split_with_shlex(self):
        argv = _build_argv(_flags(FUSION_PARSER_COMMAND="uv run fs parse"))
        assert argv == ["uv", "run", "fs", "parse"]


class TestSerializeVars:
    def test_dict_to_yaml(self):
        out = _serialize_vars({"a": 1, "b": "two"})
        assert "a:" in out and "b:" in out

    def test_passthrough_string(self):
        assert _serialize_vars("a: 1") == "a: 1"


class TestDeleteStalePartialParse:
    def test_deletes_when_present(self, tmp_path: Path):
        msgpack = tmp_path / "partial_parse.msgpack"
        msgpack.write_bytes(b"stale")
        _delete_stale_partial_parse(tmp_path)
        assert not msgpack.exists()

    def test_noop_when_absent(self, tmp_path: Path):
        _delete_stale_partial_parse(tmp_path)


class TestParseWithFusion:
    def _runtime_config(self, target_path: Path):
        return SimpleNamespace(project_target_path=str(target_path))

    def test_missing_binary_raises_typed_error(self, tmp_path: Path):
        flags = _flags(FUSION_PARSER_COMMAND="definitely-not-a-real-binary-xyz")
        with mock.patch(
            "dbt.parser.fusion.subprocess.run", side_effect=FileNotFoundError()
        ):
            with pytest.raises(FusionParserMissingError):
                parse_with_fusion(flags, self._runtime_config(tmp_path))

    def test_nonzero_exit_raises_with_stderr(self, tmp_path: Path):
        flags = _flags()
        completed = subprocess.CompletedProcess(
            args=["fs", "parse"], returncode=2, stdout="", stderr="bad project"
        )
        with mock.patch("dbt.parser.fusion.subprocess.run", return_value=completed):
            with pytest.raises(FusionParserError, match="bad project"):
                parse_with_fusion(flags, self._runtime_config(tmp_path))

    def test_missing_manifest_after_success_raises(self, tmp_path: Path):
        flags = _flags()
        completed = subprocess.CompletedProcess(
            args=["fs", "parse"], returncode=0, stdout="", stderr=""
        )
        with mock.patch("dbt.parser.fusion.subprocess.run", return_value=completed):
            with pytest.raises(FusionParserError, match="manifest.json was not produced|was not produced"):
                parse_with_fusion(flags, self._runtime_config(tmp_path))

    def test_invalid_json_raises_schema_error(self, tmp_path: Path):
        (tmp_path / "manifest.json").write_text("{ not valid json")
        flags = _flags()
        completed = subprocess.CompletedProcess(
            args=["fs", "parse"], returncode=0, stdout="", stderr=""
        )
        with mock.patch("dbt.parser.fusion.subprocess.run", return_value=completed):
            with pytest.raises(FusionParserSchemaError):
                parse_with_fusion(flags, self._runtime_config(tmp_path))

    def test_incompatible_schema_version_raises_version_error(self, tmp_path: Path):
        (tmp_path / "manifest.json").write_text(
            json.dumps(
                {
                    "metadata": {
                        "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v1.json"
                    }
                }
            )
        )
        flags = _flags()
        completed = subprocess.CompletedProcess(
            args=["fs", "parse"], returncode=0, stdout="", stderr=""
        )
        with mock.patch("dbt.parser.fusion.subprocess.run", return_value=completed):
            with pytest.raises(FusionParserVersionError):
                parse_with_fusion(flags, self._runtime_config(tmp_path))
