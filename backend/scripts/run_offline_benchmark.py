"""Run and save the fixed AgentLens 6x10 offline benchmark evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

from agentlens.database import persist_experiment, persisted_counts
from agentlens.demo import build_demo_experiment


def parse_args() -> argparse.Namespace:
    default_output = Path(__file__).resolve().parents[2] / "evidence" / "offline-benchmark.json"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=default_output)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    started = perf_counter()
    experiment = build_demo_experiment(
        "benchmark-offline-6x10",
        "Fixed offline portfolio benchmark: 6 tasks x 10 repetitions",
    )
    persist_experiment(experiment)
    elapsed = perf_counter() - started
    stable_runs = [
        run.model_dump(mode="json", exclude={"events"})
        for run in experiment.runs
    ]
    run_digest = hashlib.sha256(
        json.dumps(stable_runs, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    successes = sum(run.success for run in experiment.runs)
    baseline_metrics = experiment.comparison["baseline_metrics"]
    evidence = {
        "schema_version": "agentlens-benchmark-evidence/v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "command": "cd backend && python scripts/run_offline_benchmark.py",
        "environment": {
            "os": platform.platform(),
            "python": sys.version.split()[0],
            "mode": "deterministic_offline",
            "external_api_calls": 0,
        },
        "protocol": {
            "tasks": 6,
            "repetitions_per_task": 10,
            "candidate_runs": experiment.total_runs,
            "seeds": list(range(1, 11)),
            "bootstrap_seed": 2026,
            "bootstrap_samples": 1500,
        },
        "candidate": {
            "id": experiment.candidate.id,
            "successes": successes,
            "failures": experiment.total_runs - successes,
            "metrics": experiment.metrics,
        },
        "baseline": {
            "id": experiment.baseline.id,
            "successes": round(baseline_metrics["success_rate"] * experiment.total_runs),
            "failures": experiment.total_runs
            - round(baseline_metrics["success_rate"] * experiment.total_runs),
            "metrics": baseline_metrics,
        },
        "comparison": {
            key: value
            for key, value in experiment.comparison.items()
            if key != "baseline_metrics"
        },
        "failure_categories": experiment.failures,
        "judge_calibration": experiment.judge_calibration,
        "persistence": persisted_counts(),
        "run_digest_sha256": run_digest,
        "elapsed_seconds": round(elapsed, 4),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    print(f"evidence_file={args.output.resolve()}")


if __name__ == "__main__":
    main()
