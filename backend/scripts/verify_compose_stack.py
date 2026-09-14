import json
import os
import platform
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from agentlens.tool_grant_repository import issue_tool_grant, revoke_tool_grant


class VerificationError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise VerificationError(message)


def request_json(client, method, path, expected_status, payload=None):
    response = client.request(method, path, json=payload)
    require(
        response.status_code == expected_status,
        f"{method} {path} returned {response.status_code}: {response.text[:300]}",
    )
    result = response.json()
    require(isinstance(result, dict), f"{method} {path} did not return an object")
    return result


def verify_candidate_readiness(client):
    result = request_json(
        client,
        "POST",
        "/api/v1/candidates/coding-assistant-v1/test",
        200,
    )
    require(result.get("ok") is True, "candidate readiness probe did not pass")
    status = result.get("status")
    candidate = result.get("candidate")
    require(isinstance(status, dict), "candidate readiness has no runtime status")
    require(isinstance(candidate, dict), "candidate readiness did not return a candidate")
    require(status.get("status") == "ready", "candidate runtime is not ready")
    require(
        status.get("provider") in {"fake", "openai", "deepseek"},
        "candidate runtime provider is invalid",
    )
    require(
        isinstance(status.get("model"), str)
        and status["model"] not in {"", "unverified-runtime-model"},
        "candidate runtime model was not resolved",
    )
    identity = status.get("identity")
    require(isinstance(identity, dict), "candidate readiness has no identity fingerprint")
    for field in ("prompt_hash", "scaffold_version", "tool_schema_hash"):
        require(
            isinstance(identity.get(field), str) and identity[field],
            f"candidate identity has no {field}",
        )
    require(
        candidate.get("model") == status.get("model"),
        "candidate identity was not synchronized",
    )
    return {
        "provider": status["provider"],
        "model": status["model"],
        "identity_synchronized": True,
    }


def validate_tool_grant_health(health):
    storage = health.get("storage")
    require(isinstance(storage, dict), "health has no storage audit")
    report = storage.get("tool_grants")
    require(isinstance(report, dict), "health has no tool grant retention audit")
    count_fields = (
        "active",
        "expired",
        "revoked",
        "other",
        "total",
        "expired_marked",
        "purged",
    )
    for field in count_fields:
        require(
            type(report.get(field)) is int and report[field] >= 0,
            f"tool grant health field {field} is invalid",
        )
    require(
        report["total"]
        == report["active"] + report["expired"] + report["revoked"] + report["other"],
        "tool grant status counts do not add up",
    )
    require(
        type(report.get("retention_seconds")) is int
        and report["retention_seconds"] >= 3_600,
        "tool grant retention is invalid",
    )
    require(
        type(report.get("cleanup_batch_size")) is int
        and 1 <= report["cleanup_batch_size"] <= 10_000,
        "tool grant cleanup batch size is invalid",
    )
    return report


def verify_tool_grant_retention(client, initial_health):
    baseline = validate_tool_grant_health(initial_health)
    current_time = datetime.now(UTC)
    token = issue_tool_grant(
        run_id="compose-tool-grant-retention",
        experiment_id="compose-verification",
        cassette_id="customer-tools-v2",
        cassette_content_sha256="a" * 64,
        allowed_tools=[],
        max_calls=0,
        ttl_seconds=60,
        now=current_time
        - timedelta(seconds=baseline["retention_seconds"] + 120),
    )
    try:
        require(
            revoke_tool_grant(
                token,
                now=current_time
                - timedelta(seconds=baseline["retention_seconds"] + 1),
            ),
            "old tool grant could not be prepared for retention verification",
        )
        health = request_json(client, "GET", "/health", 200)
        report = validate_tool_grant_health(health)
        require(report["purged"] >= 1, "health did not purge an old terminal tool grant")
        return {
            "retention_seconds": report["retention_seconds"],
            "cleanup_batch_size": report["cleanup_batch_size"],
            "purged": report["purged"],
            "status_counts": {
                field: report[field]
                for field in ("active", "expired", "revoked", "other", "total")
            },
        }
    finally:
        revoke_tool_grant(token)


def collect_experiment_events(client, experiment_id):
    events = []
    event_name = None
    first_event_seconds = None
    started = time.perf_counter()
    with client.stream("GET", f"/api/v1/experiments/{experiment_id}/events") as response:
        require(response.status_code == 200, f"SSE returned HTTP {response.status_code}")
        require(
            response.headers.get("content-type", "").startswith("text/event-stream"),
            "wrong SSE content type",
        )
        for line in response.iter_lines():
            if line.startswith("event:"):
                event_name = line.removeprefix("event:").strip()
            elif line.startswith("data:"):
                require(event_name is not None, "SSE data has no event name")
                data = json.loads(line.removeprefix("data:").strip())
                require(data.get("type", event_name) == event_name, "SSE event/data type mismatch")
                events.append({"name": event_name, "data": data})
                first_event_seconds = first_event_seconds or time.perf_counter() - started
                require(len(events) <= 5_000, "too many SSE events")
    require(events, "SSE ended without events")
    terminal = [
        item
        for item in events
        if item["name"] in {"experiment.result", "experiment.cancelled", "experiment.error"}
    ]
    require(len(terminal) == 1, "SSE must have one terminal event")
    require(events[-1] is terminal[0], "SSE terminal event must be last")
    return events, first_event_seconds or 0.0


def verify_fail_closed_tool_gateway(client):
    snapshot = request_json(
        client,
        "GET",
        "/api/v1/cassettes/customer-tools-v2",
        200,
    )
    cassette_sha256 = snapshot.get("content_sha256")
    require(
        isinstance(cassette_sha256, str)
        and len(cassette_sha256) == 64
        and all(character in "0123456789abcdef" for character in cassette_sha256),
        "cassette snapshot has no valid content digest",
    )
    run_id = "compose-tool-grant-verification"
    token = issue_tool_grant(
        run_id=run_id,
        experiment_id="compose-verification",
        cassette_id="customer-tools-v2",
        cassette_content_sha256=cassette_sha256,
        allowed_tools=["search_customer"],
        max_calls=1,
        ttl_seconds=60,
    )
    invocation = {
        "run_id": run_id,
        "tool_grant_token": token,
        "cassette_id": "customer-tools-v2",
        "cassette_content_sha256": cassette_sha256,
        "mode": "replay",
        "tool": "search_customer",
        "arguments": {"customer_id": "compose-missing"},
    }
    try:
        result = request_json(
            client,
            "POST",
            "/api/v1/tools/invoke",
            424,
            invocation,
        )
        detail = result.get("detail")
        require(isinstance(detail, dict), "tool failure has no structured detail")
        require(
            detail.get("category") == "environment_error",
            "tool failure category changed",
        )
        require(
            "traceback" not in json.dumps(detail, ensure_ascii=False).lower(),
            "tool failure leaked a traceback",
        )

        stale_digest = "0" * 64 if cassette_sha256 != "0" * 64 else "1" * 64
        drifted = request_json(
            client,
            "POST",
            "/api/v1/tools/invoke",
            403,
            {**invocation, "cassette_content_sha256": stale_digest},
        )
        drifted_detail = drifted.get("detail")
        require(
            isinstance(drifted_detail, dict),
            "digest mismatch has no structured detail",
        )
        require(
            drifted_detail.get("category") == "tool_authorization_error",
            "digest mismatch authorization category changed",
        )

        cross_run = request_json(
            client,
            "POST",
            "/api/v1/tools/invoke",
            403,
            {**invocation, "run_id": "compose-cross-run"},
        )
        cross_run_detail = cross_run.get("detail")
        require(
            isinstance(cross_run_detail, dict)
            and cross_run_detail.get("category") == "tool_authorization_error",
            "cross-run grant reuse was not rejected",
        )
    finally:
        require(revoke_tool_grant(token), "tool grant could not be revoked")

    revoked = request_json(
        client,
        "POST",
        "/api/v1/tools/invoke",
        403,
        invocation,
    )
    revoked_detail = revoked.get("detail")
    require(
        isinstance(revoked_detail, dict)
        and revoked_detail.get("category") == "tool_authorization_error",
        "revoked tool grant was accepted",
    )
    serialized = json.dumps(
        [drifted_detail, cross_run_detail, revoked_detail],
        ensure_ascii=False,
    ).lower()
    require("traceback" not in serialized, "tool authorization leaked a traceback")
    require(token not in serialized, "tool authorization leaked the raw grant token")
    return {
        "status_code": 424,
        "category": detail["category"],
        "content_binding": True,
        "run_scoped_authorization": True,
        "revocation_enforced": True,
        "content_sha256": cassette_sha256,
    }

def find_failure_target(experiment):
    runs = experiment.get("runs", [])
    baseline_runs = experiment.get("baseline_runs", [])
    require(isinstance(runs, list), "candidate runs are not a list")
    require(isinstance(baseline_runs, list), "baseline runs are not a list")
    for run in (*runs, *baseline_runs):
        if not isinstance(run, dict):
            continue
        failures = run.get("failures")
        if not isinstance(failures, list) or not failures:
            continue
        run_id = run.get("run_id")
        require(isinstance(run_id, str) and run_id, "failed run has no run_id")
        require(isinstance(failures[0], dict), "failure evidence is not an object")
        return run_id, 0, failures[0]
    raise VerificationError("completed experiment has no failure evidence for Judge check")


def verify_offline_judge_review(client, experiment):
    experiment_id = experiment.get("id")
    require(isinstance(experiment_id, str) and experiment_id, "result has no experiment id")
    run_id, failure_index, original_failure = find_failure_target(experiment)
    path = (
        f"/api/v1/experiments/{experiment_id}/runs/{run_id}/"
        f"failures/{failure_index}/review"
    )
    for _ in range(2):
        result = request_json(client, "POST", path, 503)
        detail = result.get("detail")
        require(isinstance(detail, dict), "Judge failure has no structured detail")
        require(
            detail.get("category") == "judge_unavailable",
            "offline Judge failure category changed",
        )
        require(detail.get("retryable") is False, "offline Judge must not be retryable")
        serialized = json.dumps(detail, ensure_ascii=False).lower()
        require("traceback" not in serialized, "Judge failure leaked a traceback")

    current = request_json(client, "GET", f"/api/v1/experiments/{experiment_id}", 200)
    current_runs = (*current.get("runs", []), *current.get("baseline_runs", []))
    current_run = next(
        (
            run
            for run in current_runs
            if isinstance(run, dict) and run.get("run_id") == run_id
        ),
        None,
    )
    require(current_run is not None, "reviewed run disappeared from experiment")
    failures = current_run.get("failures")
    require(
        isinstance(failures, list) and failure_index < len(failures),
        "reviewed failure disappeared from run",
    )
    require(failures[failure_index] == original_failure, "offline Judge changed evidence")
    return {
        "attempts": 2,
        "status_code": 503,
        "category": "judge_unavailable",
        "claim_released": True,
        "evidence_unchanged": True,
    }


def verify_completed_experiment(client):
    before = request_json(client, "GET", "/health", 200)["storage"]
    started = time.perf_counter()
    queued = request_json(
        client,
        "POST",
        "/api/v1/experiments",
        202,
        {
            "prompt": "Compose ARQ HTTP completion verification",
            "candidate_id": "coding-assistant-v1",
            "baseline_candidate_id": None,
            "benchmark_id": "coding-agent-core-v1",
            "execution_mode": "http",
            "repetitions": 1,
        },
    )
    require(queued["status"] == "queued", "POST did not return queued")
    events, first_event_seconds = collect_experiment_events(client, queued["id"])
    terminal = events[-1]
    require(terminal["name"] == "experiment.result", "experiment did not complete")
    experiment = terminal["data"]["experiment"]
    require(experiment["status"] == "completed", "result is not completed")
    require(len(experiment["runs"]) == 6, "candidate run count is not 6")
    require(len(experiment["baseline_runs"]) == 0, "single-candidate run returned baseline data")
    offline_judge = verify_offline_judge_review(client, experiment)
    after = request_json(client, "GET", "/health", 200)["storage"]
    require(after["experiments"] >= before["experiments"] + 1, "experiment was not persisted")
    require(after["runs"] >= before["runs"] + 6, "runs were not persisted")
    require(after["events"] > before["events"], "events were not persisted")
    return {
        "experiment_id": queued["id"],
        "first_event_seconds": round(first_event_seconds, 4),
        "completed_seconds": round(time.perf_counter() - started, 4),
        "sse_events": len(events),
        "offline_judge": offline_judge,
        "storage_delta": {
            key: after[key] - before[key] for key in ("experiments", "runs", "events")
        },
    }


def verify_cancelled_experiment(client):
    queued = request_json(
        client,
        "POST",
        "/api/v1/experiments",
        202,
        {
            "prompt": "Compose ARQ HTTP cancellation verification",
            "candidate_id": "coding-assistant-v1",
            "baseline_candidate_id": None,
            "benchmark_id": "coding-agent-core-v1",
            "execution_mode": "http",
            "repetitions": 50,
        },
    )
    cancelled = request_json(client, "POST", f"/api/v1/experiments/{queued['id']}/cancel", 200)
    require(cancelled["status"] == "cancelled", "cancel was not persisted")
    events, first_event_seconds = collect_experiment_events(client, queued["id"])
    require(events[-1]["name"] == "experiment.cancelled", "SSE did not cancel")
    current = request_json(client, "GET", f"/api/v1/experiments/{queued['id']}", 200)
    require(current["status"] == "cancelled", "cancel was overwritten")
    return {
        "experiment_id": queued["id"],
        "first_event_seconds": round(first_event_seconds, 4),
        "sse_events": len(events),
    }


def wait_for_api(client, timeout_seconds=60):
    deadline = time.monotonic() + timeout_seconds
    last_error = "API did not respond"
    while time.monotonic() < deadline:
        try:
            response = client.get("/health")
            if response.status_code == 200:
                result = response.json()
                if isinstance(result, dict):
                    return result
            last_error = f"health returned HTTP {response.status_code}"
        except httpx.HTTPError as error:
            last_error = str(error)
        time.sleep(0.5)
    raise VerificationError(f"API was not ready within {timeout_seconds}s: {last_error}")


def verify_stack(base_url, transport=None):
    with httpx.Client(base_url=base_url.rstrip("/"), timeout=90, transport=transport) as client:
        health = wait_for_api(client)
        require(health.get("execution_backend") == "arq", "API is not using ARQ")
        require(health.get("database_status") == "ok", "database is unavailable")
        require(
            health.get("schema_revision") == "0005_evaluation_assets",
            "database is not at the expected Alembic revision",
        )
        require(health.get("queue_status") == "ok", "Redis queue is unavailable")
        require(health.get("worker_status") == "ok", "ARQ worker is unavailable")
        grant_retention = verify_tool_grant_retention(client, health)
        return {
            "schema_version": "agentlens-compose-verification/v7",
            "status": "passed",
            "base_url": base_url,
            "environment": {
                "python_version": platform.python_version(),
                "platform": platform.platform(),
            },
            "execution_backend": health["execution_backend"],
            "database_status": health["database_status"],
            "schema_revision": health["schema_revision"],
            "queue_status": health["queue_status"],
            "worker_status": health["worker_status"],
            "candidate_readiness": verify_candidate_readiness(client),
            "tool_grant_retention": grant_retention,
            "fail_closed_tool_gateway": verify_fail_closed_tool_gateway(client),
            "completed": verify_completed_experiment(client),
            "cancelled": verify_cancelled_experiment(client),
        }


def write_verification_result(output_path, result):
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    temporary.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, destination)


def main():
    base_url = os.getenv("AGENTLENS_VERIFY_URL", "http://127.0.0.1:8000")
    try:
        result = verify_stack(base_url)
    except (httpx.HTTPError, VerificationError, ValueError) as error:
        raise SystemExit(f"compose verification failed: {error}") from error
    output_path = os.getenv("AGENTLENS_VERIFY_OUTPUT")
    if output_path:
        write_verification_result(output_path, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
