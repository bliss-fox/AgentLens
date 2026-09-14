from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request, status

from agentlens.application_dependencies import experiment_store_for
from agentlens.config import JUDGE_REVIEW_LEASE_GRACE_SECONDS, get_settings
from agentlens.exceptions import FailureReviewStateError
from agentlens.judge import (
    JudgeReviewError,
    apply_judge_review,
    sanitize_judge_payload,
    semantic_failure_review,
)
from agentlens.review_repository import (
    claim_failure_review,
    persist_failure_review,
    release_failure_review_claim,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["reviews"])


@router.post("/api/v1/experiments/{experiment_id}/runs/{run_id}/failures/{failure_index}/review")
async def review_failure(
    experiment_id: str,
    run_id: str,
    failure_index: int,
    request: Request,
    force: bool = False,
) -> dict:
    experiment_store = experiment_store_for(request.app)
    experiment = await asyncio.to_thread(experiment_store.get, experiment_id)
    if experiment is None:
        raise HTTPException(404, "experiment not found")
    if experiment.status != "completed":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {
                "category": "review_not_ready",
                "message": "only completed experiments can be reviewed",
                "retryable": True,
            },
        )
    run = next(
        (item for item in (*experiment.runs, *experiment.baseline_runs) if item.run_id == run_id),
        None,
    )
    if run is None:
        raise HTTPException(404, "run not found")
    if failure_index < 0 or failure_index >= len(run.failures):
        raise HTTPException(404, "failure not found")

    try:
        claim = await asyncio.to_thread(
            claim_failure_review,
            experiment_id,
            run_id,
            failure_index,
            force=force,
            lease_seconds=(get_settings().judge_timeout_seconds + JUDGE_REVIEW_LEASE_GRACE_SECONDS),
        )
    except LookupError as error:
        raise HTTPException(404, "review target not found") from error
    except FailureReviewStateError as error:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {
                "category": error.category,
                "message": error.public_message,
                "retryable": error.retryable,
            },
        ) from error

    failure = claim.failure
    attributed_events = claim.events
    payload = sanitize_judge_payload(
        {
            "experiment_id": experiment.id,
            "run_id": run_id,
            "task_id": claim.task_id,
            "candidate_id": claim.candidate_id,
            "failure": failure.model_dump(mode="json"),
            "events": [event.model_dump(mode="json") for event in attributed_events],
        }
    )
    claim_token = claim.token
    try:
        try:
            verdict = await semantic_failure_review(payload)
        except JudgeReviewError as error:
            status_code = {
                "judge_context_too_large": status.HTTP_413_CONTENT_TOO_LARGE,
                "judge_invalid_request": status.HTTP_422_UNPROCESSABLE_CONTENT,
                "judge_invalid_response": status.HTTP_502_BAD_GATEWAY,
                "judge_timeout": status.HTTP_504_GATEWAY_TIMEOUT,
                "judge_unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
            }.get(error.category, status.HTTP_503_SERVICE_UNAVAILABLE)
            raise HTTPException(
                status_code,
                {
                    "category": error.category,
                    "message": error.public_message,
                    "retryable": error.retryable,
                },
            ) from error
        if verdict is None:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                {
                    "category": "judge_unavailable",
                    "message": "semantic judge is not configured",
                    "retryable": False,
                },
            )
        try:
            reviewed_failure = apply_judge_review(
                failure,
                verdict,
                {event.seq for event in attributed_events},
            )
        except ValueError as error:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                {
                    "category": "judge_invalid_response",
                    "message": "semantic judge returned invalid evidence citations",
                    "retryable": False,
                },
            ) from error
        try:
            updated_experiment = await asyncio.to_thread(
                persist_failure_review,
                experiment_id,
                run_id,
                failure_index,
                reviewed_failure,
                claim_token,
            )
        except LookupError as error:
            raise HTTPException(404, "review target not found") from error
        except FailureReviewStateError as error:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                {
                    "category": error.category,
                    "message": error.public_message,
                    "retryable": error.retryable,
                },
            ) from error
        except ValueError as error:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                {
                    "category": "review_conflict",
                    "message": "failure evidence changed before review persistence",
                    "retryable": True,
                },
            ) from error
        claim_token = None
        return {
            "experiment": updated_experiment.model_dump(mode="json"),
            "failure": reviewed_failure.model_dump(mode="json"),
            "review": verdict.model_dump(mode="json"),
        }
    finally:
        if claim_token is not None:
            try:
                await asyncio.to_thread(
                    release_failure_review_claim,
                    experiment_id,
                    run_id,
                    failure_index,
                    claim_token,
                )
            except Exception:  # noqa: BLE001 - log safely; lease expiry recovers.
                logger.warning("failure review claim release failed; lease expiry will recover")
