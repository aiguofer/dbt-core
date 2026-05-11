from types import SimpleNamespace
from unittest import mock

import pytest

from dbt.cli import requires
from dbt.exceptions import DbtProjectError


def _ctx(flags, manifest=None):
    """Minimal click-like Context with the keys setup_manifest reads."""
    runtime_config = SimpleNamespace(project_target_path="target")
    project = SimpleNamespace(project_name="test_project")
    profile = SimpleNamespace()
    obj = {
        "flags": flags,
        "profile": profile,
        "project": project,
        "runtime_config": runtime_config,
        "manifest": manifest,
    }
    return SimpleNamespace(obj=obj)


def _flags(use_fusion: bool = False, require_fusion: bool = False):
    return SimpleNamespace(
        USE_FUSION_PARSER=use_fusion,
        REQUIRE_FUSION_PARSER=require_fusion,
        FUSION_PARSER_COMMAND="fs parse",
        PROJECT_DIR="/proj",
        VARS={},
        write_json=True,
    )


class TestSetupManifestFusionBranch:
    def _common_patches(self):
        # Stub adapter wiring + catalog loading + deferred events.
        return mock.patch.multiple(
            "dbt.cli.requires",
            load_catalogs=mock.DEFAULT,
            get_active_write_integration=mock.DEFAULT,
            _wire_adapter_for_external_manifest=mock.DEFAULT,
            get_adapter=mock.DEFAULT,
            fire_deferred_events=mock.DEFAULT,
        )

    def test_use_fusion_calls_parse_with_fusion(self):
        ctx = _ctx(_flags(use_fusion=True))
        with self._common_patches() as patches, mock.patch(
            "dbt.parser.fusion.parse_with_fusion"
        ) as parse_with_fusion, mock.patch("dbt.cli.requires.parse_manifest") as parse_manifest:
            patches["load_catalogs"].return_value = []
            parse_with_fusion.return_value = SimpleNamespace()

            requires.setup_manifest(ctx)

            parse_with_fusion.assert_called_once()
            parse_manifest.assert_not_called()
            patches["_wire_adapter_for_external_manifest"].assert_called_once()

    def test_default_calls_parse_manifest(self):
        ctx = _ctx(_flags(use_fusion=False))
        with self._common_patches() as patches, mock.patch(
            "dbt.parser.fusion.parse_with_fusion"
        ) as parse_with_fusion, mock.patch("dbt.cli.requires.parse_manifest") as parse_manifest:
            patches["load_catalogs"].return_value = []
            parse_manifest.return_value = SimpleNamespace()

            requires.setup_manifest(ctx)

            parse_manifest.assert_called_once()
            parse_with_fusion.assert_not_called()
            patches["_wire_adapter_for_external_manifest"].assert_not_called()

    def test_require_fusion_without_use_fusion_raises(self):
        ctx = _ctx(_flags(use_fusion=False, require_fusion=True))
        with self._common_patches() as patches:
            patches["load_catalogs"].return_value = []
            with pytest.raises(DbtProjectError, match="require_fusion_parser"):
                requires.setup_manifest(ctx)

    def test_require_fusion_with_use_fusion_proceeds(self):
        ctx = _ctx(_flags(use_fusion=True, require_fusion=True))
        with self._common_patches() as patches, mock.patch(
            "dbt.parser.fusion.parse_with_fusion"
        ) as parse_with_fusion:
            patches["load_catalogs"].return_value = []
            parse_with_fusion.return_value = SimpleNamespace()

            requires.setup_manifest(ctx)

            parse_with_fusion.assert_called_once()

    def test_pre_set_manifest_skips_both_parsers(self):
        existing = SimpleNamespace()
        ctx = _ctx(_flags(use_fusion=True), manifest=existing)
        with self._common_patches() as patches, mock.patch(
            "dbt.parser.fusion.parse_with_fusion"
        ) as parse_with_fusion, mock.patch("dbt.cli.requires.parse_manifest") as parse_manifest:
            patches["load_catalogs"].return_value = []

            requires.setup_manifest(ctx)

            parse_with_fusion.assert_not_called()
            parse_manifest.assert_not_called()
            patches["_wire_adapter_for_external_manifest"].assert_called_once()
            assert ctx.obj["manifest"] is existing
