from types import SimpleNamespace
from unittest import mock

import pytest

from dbt.exceptions import dbtPluginError
from dbt.parser.manifest import enrich_manifest_with_plugin_artifacts


def _hook_for(plugin_name: str):
    """Build a callable whose __self__.name matches what PluginManager exposes."""
    plugin_obj = SimpleNamespace(name=plugin_name)
    hook = mock.MagicMock()
    hook.__self__ = plugin_obj
    return hook


class TestEnrichManifestWithPluginArtifacts:
    def test_runs_get_manifest_artifacts_and_writes(self):
        manifest = mock.MagicMock()
        artifact = mock.MagicMock()
        artifact.__class__.__name__ = "FakeArtifact"
        pm = SimpleNamespace(
            hooks={},
            get_manifest_artifacts=mock.MagicMock(return_value={"some/path.json": artifact}),
        )
        with mock.patch(
            "dbt.parser.manifest.plugins.get_plugin_manager", return_value=pm
        ):
            enrich_manifest_with_plugin_artifacts(manifest, "proj")

        pm.get_manifest_artifacts.assert_called_once_with(manifest)
        artifact.write.assert_called_once_with("some/path.json")

    def test_fails_fast_when_plugin_uses_get_nodes(self):
        manifest = mock.MagicMock()
        pm = SimpleNamespace(
            hooks={"get_nodes": [_hook_for("plugin_a"), _hook_for("plugin_b")]},
            get_manifest_artifacts=mock.MagicMock(return_value={}),
        )
        with mock.patch(
            "dbt.parser.manifest.plugins.get_plugin_manager", return_value=pm
        ):
            with pytest.raises(dbtPluginError, match="get_nodes"):
                enrich_manifest_with_plugin_artifacts(manifest, "proj")

        pm.get_manifest_artifacts.assert_not_called()

    def test_no_plugins_no_artifacts(self):
        manifest = mock.MagicMock()
        pm = SimpleNamespace(
            hooks={},
            get_manifest_artifacts=mock.MagicMock(return_value={}),
        )
        with mock.patch(
            "dbt.parser.manifest.plugins.get_plugin_manager", return_value=pm
        ):
            enrich_manifest_with_plugin_artifacts(manifest, "proj")

        pm.get_manifest_artifacts.assert_called_once()
