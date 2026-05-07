"""Wall-clock benchmark harness for fs vs core parse.

Times N invocations of each parser against the same project, reports
median + p95, and emits JSON for downstream baseline comparison.

This is harness only — recording baselines and gating PRs on regressions
is a CI follow-on.

Usage:

    python -m tests.parity.benchmark \\
        --project-dir path/to/project \\
        --runs 5 \\
        --fs-binary fs \\
        --json results.json
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional


@dataclass
class ParseStats:
    parser: str
    runs: int
    samples_seconds: List[float]
    median_seconds: float
    p95_seconds: float
    min_seconds: float
    max_seconds: float


def _stats(parser: str, samples: List[float]) -> ParseStats:
    sorted_samples = sorted(samples)
    p95_idx = max(0, int(round(0.95 * (len(sorted_samples) - 1))))
    return ParseStats(
        parser=parser,
        runs=len(samples),
        samples_seconds=samples,
        median_seconds=statistics.median(samples),
        p95_seconds=sorted_samples[p95_idx],
        min_seconds=min(samples),
        max_seconds=max(samples),
    )


def _time_subprocess(argv: List[str]) -> float:
    start = time.perf_counter()
    result = subprocess.run(argv, capture_output=True, text=True)
    elapsed = time.perf_counter() - start
    if result.returncode != 0:
        raise RuntimeError(
            f"command failed (exit {result.returncode}): {' '.join(argv)}\n{result.stderr}"
        )
    return elapsed


def benchmark(
    project_dir: str,
    runs: int = 5,
    fs_binary: Optional[str] = None,
    dbt_binary: str = "dbt",
) -> dict:
    """Run `dbt parse` and `fs parse` `runs` times each, return stats dict."""
    results: dict = {"project_dir": project_dir, "runs": runs}

    core_samples = [
        _time_subprocess([dbt_binary, "parse", "--project-dir", project_dir])
        for _ in range(runs)
    ]
    results["core"] = asdict(_stats("core", core_samples))

    if fs_binary:
        resolved = shutil.which(fs_binary) or fs_binary
        fs_samples = [
            _time_subprocess([resolved, "parse", "--project-dir", project_dir])
            for _ in range(runs)
        ]
        results["fs"] = asdict(_stats("fs", fs_samples))

    return results


def _main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Benchmark fs vs core parse")
    ap.add_argument("--project-dir", required=True)
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--fs-binary", help="path to fs (omit to skip fs side)")
    ap.add_argument("--dbt-binary", default="dbt")
    ap.add_argument("--json", help="path to write JSON results to")
    args = ap.parse_args(argv)

    results = benchmark(
        project_dir=args.project_dir,
        runs=args.runs,
        fs_binary=args.fs_binary,
        dbt_binary=args.dbt_binary,
    )

    text = json.dumps(results, indent=2)
    if args.json:
        Path(args.json).write_text(text)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
