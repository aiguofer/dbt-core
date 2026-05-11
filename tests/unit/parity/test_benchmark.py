import json
from pathlib import Path
from unittest import mock

from tests.parity.benchmark import _main, _stats, benchmark


class TestStats:
    def test_basic(self):
        s = _stats("core", [1.0, 2.0, 3.0, 4.0, 5.0])
        assert s.runs == 5
        assert s.median_seconds == 3.0
        assert s.min_seconds == 1.0
        assert s.max_seconds == 5.0
        # p95 of 5 sorted samples = the 5th sample
        assert s.p95_seconds == 5.0


class TestBenchmark:
    def test_runs_core_only_when_fs_omitted(self):
        with mock.patch(
            "tests.parity.benchmark._time_subprocess", side_effect=[0.1, 0.2, 0.3]
        ):
            results = benchmark("/proj", runs=3)
        assert "fs" not in results
        assert results["core"]["runs"] == 3

    def test_runs_both_when_fs_provided(self):
        # 3 core + 3 fs = 6 calls
        with mock.patch(
            "tests.parity.benchmark._time_subprocess",
            side_effect=[0.1, 0.2, 0.3, 0.05, 0.05, 0.05],
        ):
            results = benchmark("/proj", runs=3, fs_binary="fs")
        assert results["core"]["runs"] == 3
        assert results["fs"]["runs"] == 3
        assert results["fs"]["median_seconds"] == 0.05


class TestCli:
    def test_writes_json_file(self, tmp_path: Path):
        out = tmp_path / "bench.json"
        with mock.patch(
            "tests.parity.benchmark._time_subprocess", side_effect=[0.1, 0.1]
        ):
            rc = _main(
                [
                    "--project-dir",
                    "/proj",
                    "--runs",
                    "2",
                    "--json",
                    str(out),
                ]
            )
        assert rc == 0
        assert "core" in json.loads(out.read_text())
