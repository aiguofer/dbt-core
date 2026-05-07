"""Parity tests: parse the same project with core and with fs, then assert
manifest_diff is empty modulo the documented allowlist.

Skips automatically when fs is not on PATH (see fs_binary fixture).

Each class covers a fidelity area from docs/arch/fusion_parser_status.md
F1-F13. As fs closes blockers, the corresponding allowlist entries in
tests/parity/allowlists/known_drift.json are pruned and these tests
tighten naturally.
"""

from __future__ import annotations

import pytest


# --- Plain models ----------------------------------------------------------

PLAIN__MODEL_A_SQL = "select 1 as id"
PLAIN__MODEL_B_SQL = "select * from {{ ref('plain_a') }}"


class TestParityPlainModels:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "plain_a.sql": PLAIN__MODEL_A_SQL,
            "plain_b.sql": PLAIN__MODEL_B_SQL,
        }

    def test_parity(self, core_manifest, fs_manifest, parity_diff):
        report = parity_diff(core_manifest, fs_manifest)
        assert report.is_empty(), report.summary()


# --- Generic tests with kwargs / long names (F4) --------------------------

GENERIC__MODEL_SQL = "select 1 as id, 'a' as kind"
GENERIC__SCHEMA_YML = """
version: 2
models:
  - name: gtest_model
    columns:
      - name: id
        data_tests:
          - unique
          - not_null
      - name: kind
        data_tests:
          - accepted_values:
              values: ['a', 'b']
              quote: true
"""


class TestParityGenericTests:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "gtest_model.sql": GENERIC__MODEL_SQL,
            "schema.yml": GENERIC__SCHEMA_YML,
        }

    def test_parity(self, core_manifest, fs_manifest, parity_diff):
        report = parity_diff(core_manifest, fs_manifest)
        assert report.is_empty(), report.summary()


# --- Multi-snapshot file (F1, F2) -----------------------------------------

MULTI_SNAP__SQL = """
{% snapshot snap_a %}
{{ config(target_schema=schema, strategy='check', unique_key='id', check_cols=['v']) }}
select 1 as id, 1 as v
{% endsnapshot %}

{% snapshot snap_b %}
{{ config(target_schema=schema, strategy='check', unique_key='id', check_cols=['v']) }}
select 2 as id, 2 as v
{% endsnapshot %}
"""


class TestParityMultiSnapshot:
    @pytest.fixture(scope="class")
    def snapshots(self):
        return {"snaps.sql": MULTI_SNAP__SQL}

    def test_parity(self, core_manifest, fs_manifest, parity_diff):
        report = parity_diff(core_manifest, fs_manifest)
        assert report.is_empty(), report.summary()


# --- on-run-start / on-run-end hooks (F3) ---------------------------------

HOOKS__PROJECT_OVERRIDES = {
    "on-run-start": ["select 1 as start_hook"],
    "on-run-end": ["select 2 as end_hook"],
}
HOOKS__MODEL_SQL = "select 1 as id"


class TestParityHooks:
    @pytest.fixture(scope="class")
    def project_config_update(self):
        return HOOKS__PROJECT_OVERRIDES

    @pytest.fixture(scope="class")
    def models(self):
        return {"hooks_model.sql": HOOKS__MODEL_SQL}

    def test_parity(self, core_manifest, fs_manifest, parity_diff):
        report = parity_diff(core_manifest, fs_manifest)
        assert report.is_empty(), report.summary()


# --- Custom search paths (F5) ---------------------------------------------

CUSTOM_PATHS__MODEL_SQL = "select 1 as id"


class TestParityCustomSearchPaths:
    @pytest.fixture(scope="class")
    def project_config_update(self):
        return {"model-paths": ["analysis_models"]}

    @pytest.fixture(scope="class")
    def models(self):
        # Files placed under the renamed path. The dbt test framework
        # writes `models/` by default, so we rely on `model-paths` above
        # to redirect — but we still seed via the standard `models`
        # fixture key, which will land in models/.
        return {"custom_path_model.sql": CUSTOM_PATHS__MODEL_SQL}

    def test_parity(self, core_manifest, fs_manifest, parity_diff):
        # Note: this primarily exercises the *path* fields. Allowlist
        # currently tolerates path drift; this test will tighten as F5
        # closes.
        report = parity_diff(core_manifest, fs_manifest)
        assert report.is_empty(), report.summary()
