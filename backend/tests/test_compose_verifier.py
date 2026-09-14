import importlib.util
import json
from pathlib import Path

import httpx
import pytest


def load_verifier():
    path = Path(__file__).parents[1] / "scripts" / "verify_compose_stack.py"
    spec = importlib.util.spec_from_file_location("compose_verifier", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def sse(name, data):
    body = {"type": name, **data}
    return f"event: {name}\ndata: {json.dumps(body)}\n\n"


def test_compose_verifier_accepts_completion_cancellation_and_audit_deltas():
    verifier = load_verifier()
    state = {
        "completed": False,
        "posts": 0,
        "health_failures": 2,
        "authorized_tool_calls": 0,
        "judge_attempts": 0,
        "successful_health_calls": 0,
        "experiment_payloads": [],
    }
    failure = {
        "category": "tool_misuse",
        "label": "tool misuse",
        "severity": "high",
        "event_range": [1, 1],
        "rule": "tool.allowlist_or_schema",
        "explanation": "deterministic explanation",
        "judge_verdict": "not_run",
        "confidence": "high",
        "judge_explanation": None,
        "judge_evidence_sequences": [],
    }
    completed_experiment = {
        "id": "exp-completed",
        "status": "completed",
        "runs": [
            {"run_id": "run-failed", "failures": [failure]},
            *[{} for _ in range(5)],
        ],
        "baseline_runs": [],
    }

    def handler(request):
        path = request.url.path
        if request.method == "GET" and path == "/health":
            if state["health_failures"]:
                state["health_failures"] -= 1
                raise httpx.ConnectError("API is starting", request=request)
            state["successful_health_calls"] += 1
            storage = (
                {"experiments": 2, "runs": 126, "events": 986}
                if state["completed"]
                else {"experiments": 1, "runs": 120, "events": 966}
            )
            storage["tool_grants"] = {
                "active": 0,
                "expired": 0,
                "revoked": 0,
                "other": 0,
                "total": 0,
                "expired_marked": 0,
                "purged": 1 if state["successful_health_calls"] == 2 else 0,
                "retention_seconds": 604800,
                "cleanup_batch_size": 1000,
            }
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "execution_backend": "arq",
                    "database_status": "ok",
                    "schema_revision": "0005_evaluation_assets",
                    "queue_status": "ok",
                    "worker_status": "ok",
                    "storage": storage,
                },
            )
        if request.method == "GET" and path == "/api/v1/cassettes/customer-tools-v2":
            return httpx.Response(
                200,
                json={
                    "id": "customer-tools-v2",
                    "mode": "replay",
                    "entries": 3,
                    "keys": [],
                    "content_sha256": "a" * 64,
                },
            )
        if request.method == "POST" and path == "/api/v1/candidates/coding-assistant-v1/test":
            identity = {
                "model": "fake",
                "prompt_hash": "sha256:prompt",
                "scaffold_version": "agentlens-openai-compat@0.1.0",
                "tool_schema_hash": "sha256:tools",
            }
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "status": {"status": "ready", "provider": "fake", "model": "fake", "identity": identity},
                    "candidate": {"id": "coding-assistant-v1", "model": "fake"},
                },
            )
        if request.method == "POST" and path == "/api/v1/tools/invoke":
            payload = json.loads(request.content)
            contract_matches = (
                payload["run_id"] == "compose-tool-grant-verification"
                and payload["cassette_content_sha256"] == "a" * 64
            )
            if contract_matches and state["authorized_tool_calls"] == 0:
                state["authorized_tool_calls"] += 1
                return httpx.Response(
                    424,
                    json={
                        "detail": {
                            "category": "environment_error",
                            "message": "frozen cassette has no response",
                        }
                    },
                )
            return httpx.Response(
                403,
                json={
                    "detail": {
                        "category": "tool_authorization_error",
                        "message": "tool grant is invalid or inactive",
                        "retryable": False,
                    }
                },
            )
        if request.method == "POST" and path == "/api/v1/experiments":
            state["posts"] += 1
            state["experiment_payloads"].append(json.loads(request.content))
            suffix = "completed" if state["posts"] == 1 else "cancelled"
            return httpx.Response(202, json={"id": f"exp-{suffix}", "status": "queued"})
        if request.method == "GET" and path == "/api/v1/experiments/exp-completed/events":
            state["completed"] = True
            body = sse(
                "experiment.progress",
                {"completed": 6, "total": 6, "status": "completed"},
            ) + sse("experiment.result", {"experiment": completed_experiment})
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text=body,
            )
        if request.method == "POST" and path.endswith("/failures/0/review"):
            state["judge_attempts"] += 1
            return httpx.Response(
                503,
                json={
                    "detail": {
                        "category": "judge_unavailable",
                        "message": "semantic judge is not configured",
                        "retryable": False,
                    }
                },
            )
        if request.method == "GET" and path == "/api/v1/experiments/exp-completed":
            return httpx.Response(200, json=completed_experiment)
        if request.method == "POST" and path.endswith("/exp-cancelled/cancel"):
            return httpx.Response(200, json={"id": "exp-cancelled", "status": "cancelled"})
        if request.method == "GET" and path.endswith("/exp-cancelled/events"):
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text=sse(
                    "experiment.cancelled",
                    {"experiment_id": "exp-cancelled", "status": "cancelled"},
                ),
            )
        if request.method == "GET" and path.endswith("/exp-cancelled"):
            return httpx.Response(200, json={"id": "exp-cancelled", "status": "cancelled"})
        return httpx.Response(404)

    result = verifier.verify_stack(
        "http://agentlens.test",
        transport=httpx.MockTransport(handler),
    )
    assert result["schema_version"] == "agentlens-compose-verification/v7"
    assert result["status"] == "passed"
    assert result["environment"]["python_version"]
    assert result["environment"]["platform"]
    assert result["execution_backend"] == "arq"
    assert result["database_status"] == "ok"
    assert result["schema_revision"] == "0005_evaluation_assets"
    assert result["queue_status"] == "ok"
    assert result["worker_status"] == "ok"
    assert result["candidate_readiness"] == {
        "provider": "fake",
        "model": "fake",
        "identity_synchronized": True,
    }
    assert result["tool_grant_retention"] == {
        "retention_seconds": 604800,
        "cleanup_batch_size": 1000,
        "purged": 1,
        "status_counts": {
            "active": 0,
            "expired": 0,
            "revoked": 0,
            "other": 0,
            "total": 0,
        },
    }
    assert result["fail_closed_tool_gateway"] == {
        "status_code": 424,
        "category": "environment_error",
        "content_binding": True,
        "run_scoped_authorization": True,
        "revocation_enforced": True,
        "content_sha256": "a" * 64,
    }
    assert state["health_failures"] == 0
    assert result["completed"]["storage_delta"] == {
        "experiments": 1,
        "runs": 6,
        "events": 20,
    }
    assert result["completed"]["offline_judge"] == {
        "attempts": 2,
        "status_code": 503,
        "category": "judge_unavailable",
        "claim_released": True,
        "evidence_unchanged": True,
    }
    assert state["judge_attempts"] == 2
    assert len(state["experiment_payloads"]) == 2
    assert all(payload["execution_mode"] == "http" for payload in state["experiment_payloads"])
    assert all(payload["candidate_id"] == "coding-assistant-v1" for payload in state["experiment_payloads"])
    assert all(payload["baseline_candidate_id"] is None for payload in state["experiment_payloads"])
    assert result["cancelled"]["experiment_id"] == "exp-cancelled"


def test_compose_verifier_rejects_missing_tool_grant_retention_audit():
    verifier = load_verifier()
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={"storage": {"experiments": 1, "runs": 1, "events": 1}},
        )
    )
    with (
        httpx.Client(
            base_url="http://agentlens.test",
            transport=transport,
        ) as client,
        pytest.raises(verifier.VerificationError, match="retention audit"),
    ):
        verifier.verify_tool_grant_retention(
            client,
            {"storage": {"experiments": 1, "runs": 1, "events": 1}},
        )


def test_compose_verifier_rejects_tool_gateway_that_does_not_fail_closed():
    verifier = load_verifier()
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"content_sha256": "a" * 64})
        return httpx.Response(200, json={"result": {"id": "unexpected-live-result"}})

    transport = httpx.MockTransport(handler)
    with (
        httpx.Client(base_url="http://agentlens.test", transport=transport) as client,
        pytest.raises(verifier.VerificationError, match="returned 200"),
    ):
        verifier.verify_fail_closed_tool_gateway(client)


def test_compose_verifier_rejects_offline_judge_claim_leak():
    verifier = load_verifier()
    experiment = {
        "id": "exp-completed",
        "status": "completed",
        "runs": [
            {
                "run_id": "run-failed",
                "failures": [
                    {
                        "judge_verdict": "not_run",
                        "judge_explanation": None,
                        "judge_evidence_sequences": [],
                    }
                ],
            }
        ],
        "baseline_runs": [],
    }
    attempts = 0

    def handler(request):
        nonlocal attempts
        if request.method == "POST":
            attempts += 1
            status_code = 503 if attempts == 1 else 409
            category = "judge_unavailable" if attempts == 1 else "review_in_progress"
            return httpx.Response(
                status_code,
                json={"detail": {"category": category, "retryable": False}},
            )
        return httpx.Response(200, json=experiment)

    with (
        httpx.Client(
            base_url="http://agentlens.test",
            transport=httpx.MockTransport(handler),
        ) as client,
        pytest.raises(verifier.VerificationError, match="returned 409"),
    ):
        verifier.verify_offline_judge_review(client, experiment)


def test_compose_verifier_writes_machine_readable_result_atomically(tmp_path):
    verifier = load_verifier()
    destination = tmp_path / "nested" / "compose-verification.json"
    result = {
        "schema_version": "agentlens-compose-verification/v7",
        "status": "passed",
    }

    verifier.write_verification_result(destination, result)

    assert json.loads(destination.read_text(encoding="utf-8")) == result
    assert not (destination.parent / f".{destination.name}.tmp").exists()


def test_compose_ci_runs_real_stack_and_always_tears_down():
    root = Path(__file__).parents[2]
    workflow = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    dockerfile = (root / "backend" / "Dockerfile").read_text(encoding="utf-8")
    start = "docker compose -p agentlens_ci up -d --build postgres redis api worker"
    verify_step = (
        "- name: Verify cross-process completion, cancellation, offline Judge, and failure"
    )
    copy_step = "- name: Copy Compose verification evidence"
    upload_step = "- name: Upload Compose verification evidence"
    teardown = "docker compose -p agentlens_ci down -v --remove-orphans"

    assert "name: Compose Linux E2E" in workflow
    assert "Verify deterministic run, task, and cassette digests" in workflow
    assert "86cbc7e5495a16860f4c84ac42e1f3a3aaf4d9ab27cd1ea65fc50b0a7992508b" in workflow
    assert "customer-tools-v2@2026-08-12" in workflow
    assert "scripted-calibration@2026-08-12" in workflow
    assert workflow.index(start) < workflow.index(verify_step)
    assert workflow.index(verify_step) < workflow.index(copy_step)
    assert workflow.index(copy_step) < workflow.index(upload_step)
    assert workflow.index(upload_step) < workflow.index("actions/upload-artifact@v4")
    assert "AGENTLENS_VERIFY_OUTPUT=/tmp/agentlens-compose-verification.json" in workflow
    assert "api:/tmp/agentlens-compose-verification.json" in workflow
    assert "evidence/compose-verification.json" in workflow
    assert "name: agentlens-compose-verification" in workflow
    assert "path: evidence/compose-verification.json" in workflow
    assert "if: failure()" in workflow
    assert "if: always()" in workflow
    assert teardown in workflow
    assert "COPY scripts/verify_compose_stack.py scripts/verify_compose_stack.py" in dockerfile


def test_compose_verifier_rejects_unmigrated_database():
    verifier = load_verifier()
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "execution_backend": "arq",
                "database_status": "ok",
                "schema_revision": None,
                "storage": {"experiments": 0, "runs": 0, "events": 0},
            },
        )
    )
    with pytest.raises(verifier.VerificationError, match="Alembic"):
        verifier.verify_stack("http://agentlens.test", transport=transport)


def test_compose_verifier_rejects_non_arq_api():
    verifier = load_verifier()
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "execution_backend": "local",
                "storage": {"experiments": 0, "runs": 0, "events": 0},
            },
        )
    )
    with pytest.raises(verifier.VerificationError, match="ARQ"):
        verifier.verify_stack("http://agentlens.test", transport=transport)
