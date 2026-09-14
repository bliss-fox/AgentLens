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

from agentlens.config import (
    BENCHMARK_ID,
    CALIBRATION_VERSION,
    EVALUATOR_VERSION,
    PRICE_TABLE_VERSION,
    TASK_SET_VERSION,
    get_settings,
)
from agentlens.demo import TASKS, build_demo_experiment
from agentlens.execution_contract import build_experiment_snapshot
from agentlens.experiment_repository import persist_experiment, persisted_counts
from agentlens.tool_gateway import registry


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
    environment_snapshot = get_settings().environment_snapshot
    snapshot = build_experiment_snapshot(
        experiment,
        TASKS,
        environment_snapshot=environment_snapshot,
        cassette_contract=registry.environment_contract(environment_snapshot),
    )
    persist_experiment(experiment, snapshot=snapshot)
    elapsed = perf_counter() - started
    task_snapshot = [task.model_dump(mode="json") for task in TASKS]
    task_snapshot_digest = hashlib.sha256(
        json.dumps(
            task_snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    stable_runs = [
        run.model_dump(mode="json", exclude={"events"})
        for run in experiment.runs
    ]
    run_digest = hashlib.sha256(
        json.dumps(stable_runs, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    candidate_run_count = len(experiment.runs)
    baseline_run_count = len(experiment.baseline_runs)
    successes = sum(run.success for run in experiment.runs)
    baseline_successes = sum(run.success for run in experiment.baseline_runs)
    baseline_metrics = experiment.comparison["baseline_metrics"]
    cassette_contract = registry.environment_contract(environment_snapshot)
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
            "benchmark_id": BENCHMARK_ID,
            "task_set_version": TASK_SET_VERSION,
            "task_snapshot_sha256": task_snapshot_digest,
            "calibration_version": CALIBRATION_VERSION,
            "evaluator_version": EVALUATOR_VERSION,
            "price_table_version": PRICE_TABLE_VERSION,
            "environment_snapshot": environment_snapshot,
            "cassette_contract": cassette_contract,
            "tasks": len(task_snapshot),
            "repetitions_per_task": 10,
            "candidate_runs": candidate_run_count,
            "seeds": list(range(1, 11)),
            "bootstrap_seed": 2026,
            "bootstrap_samples": 1500,
        },
        "candidate": {
            "id": experiment.candidate.id,
            "successes": successes,
            "failures": candidate_run_count - successes,
            "metrics": experiment.metrics,
        },
        "baseline": {
            "id": experiment.baseline.id,
            "successes": baseline_successes,
            "failures": baseline_run_count - baseline_successes,
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
