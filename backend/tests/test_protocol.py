import asyncio
import time
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from threading import Event, get_ident

import httpx
import pytest

import agentlens.demo as demo_module
import agentlens.store as store_module
import agentlens.worker as worker_module
from agentlens.agent_client import AgentProtocolError, parse_sse_lines, run_http_agent
from agentlens.cassette import CassetteMissError, ToolCassette
from agentlens.database import (
    claim_experiment,
    load_execution_state,
    mark_stale_experiment,
    persist_experiment,
    session_factory,
    update_experiment_progress,
)
from agentlens.exceptions import ExperimentCancelled
from agentlens.models import ExperimentRecord
from agentlens.schemas import (
    AgentRunRequest,
    Budget,
    EventStreamValidator,
    EventType,
    ExperimentRequest,
    TraceEvent,
)
from agentlens.store import ExperimentStore
from agentlens.tool_gateway import registry


def event(seq, kind, **payload):
    return TraceEvent(run_id="run-1", seq=seq, type=kind, payload=payload)


def test_valid_complete_event_stream():
    validator = EventStreamValidator("run-1")
    for item in [
        event(0, EventType.RUN_STARTED),
        event(1, EventType.FINAL),
        event(2, EventType.RUN_COMPLETED),
    ]:
        validator.accept(item)
    validator.finish()


@pytest.mark.parametrize(
    "items",
    [
        [event(1, EventType.RUN_STARTED)],
        [event(0, EventType.RUN_STARTED), event(2, EventType.FINAL)],
        [event(0, EventType.RUN_STARTED), event(1, EventType.RUN_COMPLETED)],
        [
            event(0, EventType.RUN_STARTED),
            event(1, EventType.FINAL),
            event(2, EventType.MESSAGE),
            event(3, EventType.RUN_COMPLETED),
        ],
        [
            event(0, EventType.RUN_STARTED),
            event(1, EventType.FINAL),
            event(2, EventType.ERROR),
            event(3, EventType.RUN_COMPLETED),
        ],
    ],
)
def test_invalid_streams_fail(items):
    validator = EventStreamValidator("run-1")
    with pytest.raises(ValueError):
        for item in items:
            validator.accept(item)
        validator.finish()


def test_replay_cassette_fails_closed_and_record_is_reusable():
    cassette = ToolCassette(mode="record")
    assert cassette.invoke("lookup", {"id": 1}, lambda: {"name": "Ada"}) == {"name": "Ada"}
    cassette.mode = "replay"
    assert cassette.invoke("lookup", {"id": 1}) == {"name": "Ada"}
    with pytest.raises(CassetteMissError):
        cassette.invoke("lookup", {"id": 2})


def test_cassette_content_digest_is_stable_and_covers_responses():
    first = ToolCassette(
        mode="replay",
        records={"lookup:fixed": {"name": "Ada", "active": True}},
    )
    same_content = ToolCassette(
        mode="replay",
        records={"lookup:fixed": {"active": True, "name": "Ada"}},
    )
    changed_response = ToolCassette(
        mode="replay",
        records={"lookup:fixed": {"name": "Grace", "active": True}},
    )

    assert first.content_sha256() == same_content.content_sha256()
    assert first.content_sha256() != changed_response.content_sha256()
    assert len(first.content_sha256()) == 64


async def lines(values):
    for value in values:
        yield value


@pytest.mark.asyncio
async def test_chunked_sse_parser_validates_complete_stream():
    values = []
    for item in [
        event(0, EventType.RUN_STARTED),
        event(1, EventType.FINAL),
        event(2, EventType.RUN_COMPLETED),
    ]:
        values.extend(["event: trace", f"data: {item.model_dump_json()}", ""])
    parsed = await parse_sse_lines(lines(values), "run-1")
    assert [item.seq for item in parsed] == [0, 1, 2]


@pytest.mark.asyncio
async def test_sse_parser_rejects_missing_terminal_event():
    values = ["data: " + event(0, EventType.RUN_STARTED).model_dump_json(), ""]
    with pytest.raises(AgentProtocolError):
        await parse_sse_lines(lines(values), "run-1")


@pytest.mark.asyncio
async def test_sse_parser_rejects_malformed_json():
    with pytest.raises(AgentProtocolError):
        await parse_sse_lines(lines(["event: trace", "data: {not-json}"]), "run-1")


@pytest.mark.asyncio
async def test_sse_parser_requires_trace_event_name():
    values = ["event: message", "data: " + event(0, EventType.RUN_STARTED).model_dump_json(), ""]
    with pytest.raises(AgentProtocolError, match="event name"):
        await parse_sse_lines(lines(values), "run-1")


@pytest.mark.asyncio
async def test_sse_parser_enforces_tool_and_token_budgets():
    tool_values = []
    for item in [
        event(0, EventType.RUN_STARTED),
        event(1, EventType.TOOL_CALL, name="lookup", arguments={}),
    ]:
        tool_values.extend(["event: trace", f"data: {item.model_dump_json()}", ""])
    with pytest.raises(AgentProtocolError) as tool_error:
        await parse_sse_lines(lines(tool_values), "run-1", Budget(max_tool_calls=0))
    assert tool_error.value.category == "budget_exhausted"

    token_values = []
    for item in [
        event(0, EventType.RUN_STARTED),
        event(1, EventType.USAGE, input_tokens=8, output_tokens=4),
    ]:
        token_values.extend(["event: trace", f"data: {item.model_dump_json()}", ""])
    with pytest.raises(AgentProtocolError) as token_error:
        await parse_sse_lines(lines(token_values), "run-1", Budget(max_tokens=10))
    assert token_error.value.category == "budget_exhausted"


@pytest.mark.asyncio
async def test_sse_parser_does_not_charge_cached_input_twice():
    values = []
    for item in [
        event(0, EventType.RUN_STARTED),
        event(
            1,
            EventType.USAGE,
            input_tokens=1_000,
            output_tokens=100,
            cached_tokens=200,
        ),
        event(2, EventType.FINAL),
        event(3, EventType.RUN_COMPLETED),
    ]:
        values.extend(["event: trace", f"data: {item.model_dump_json()}", ""])

    parsed = await parse_sse_lines(
        lines(values),
        "run-1",
        Budget(max_cost_cny=0.018),
    )

    assert len(parsed) == 4


@pytest.mark.asyncio
async def test_sse_parser_rejects_oversized_event_payload():
    oversized = event(0, EventType.RUN_STARTED, value="x" * 500)
    values = ["event: trace", f"data: {oversized.model_dump_json()}", ""]
    with pytest.raises(AgentProtocolError) as caught:
        await parse_sse_lines(
            lines(values),
            "run-1",
            Budget(max_event_bytes=256),
        )
    assert caught.value.category == "budget_exhausted"


def agent_request() -> AgentRunRequest:
    return AgentRunRequest(
        run_id="run-1",
        task_id="task-1",
        task_input={},
        seed=1,
        environment_snapshot="cassette:v1",
        cassette_content_sha256="0" * 64,
        tool_grant_token="a" * 43,
        tool_gateway_url="http://gateway",
        budget=Budget(max_seconds=1),
    )


@pytest.mark.asyncio
async def test_http_agent_rejects_wrong_content_type():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))
    with pytest.raises(AgentProtocolError, match="expected text/event-stream"):
        await run_http_agent("http://agent", agent_request(), transport=transport)


@pytest.mark.asyncio
async def test_http_agent_accepts_case_insensitive_content_type_with_parameters():
    items = [
        event(0, EventType.RUN_STARTED),
        event(1, EventType.FINAL),
        event(2, EventType.RUN_COMPLETED),
    ]
    body = "".join(f"event: trace\ndata: {item.model_dump_json()}\n\n" for item in items)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "Text/Event-Stream; Charset=utf-8"},
            text=body,
        )
    )

    result = await run_http_agent("http://agent", agent_request(), transport=transport)

    assert [item.type for item in result] == [
        EventType.RUN_STARTED,
        EventType.FINAL,
        EventType.RUN_COMPLETED,
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content_type",
    ["text/event-streaming", "application/text/event-stream", "text/event-stream-malicious"],
)
async def test_http_agent_rejects_deceptive_content_type(content_type):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, headers={"content-type": content_type}, text="")
    )
    with pytest.raises(AgentProtocolError, match="expected text/event-stream"):
        await run_http_agent("http://agent", agent_request(), transport=transport)


@pytest.mark.asyncio
async def test_http_agent_maps_timeout_to_structured_category():
    def timeout(request):
        raise httpx.ReadTimeout("scripted timeout", request=request)

    with pytest.raises(AgentProtocolError) as caught:
        await run_http_agent(
            "http://agent", agent_request(), transport=httpx.MockTransport(timeout)
        )
    assert caught.value.category == "timeout"
    assert "exceeded" in str(caught.value)


@pytest.mark.asyncio
async def test_http_agent_cancellation_aborts_in_flight_request_promptly():
    async def slow_response(request):
        await asyncio.sleep(5)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text="")

    started = time.perf_counter()
    with pytest.raises(AgentProtocolError) as caught:
        await run_http_agent(
            "http://agent",
            agent_request(),
            transport=httpx.MockTransport(slow_response),
            is_cancelled=lambda: True,
            cancellation_poll_seconds=0.01,
        )
    assert caught.value.category == "cancelled"
    assert time.perf_counter() - started < 0.5


@pytest.mark.asyncio
async def test_cancel_during_stream_emits_terminal_cancellation():
    experiment_store = ExperimentStore()
    experiment = experiment_store.create_queued(ExperimentRequest(prompt="cancel stream"))
    stream = experiment_store.progress_events(experiment.id, delay_seconds=0)
    first = await anext(stream)
    assert first["type"] == "experiment.progress"
    experiment_store.cancel(experiment.id)
    terminal = await anext(stream)
    assert terminal["type"] == "experiment.cancelled"
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


@pytest.mark.asyncio
async def test_progress_polling_offloads_database_reads(monkeypatch):
    event_loop_thread = get_ident()
    database_threads = []

    def fake_load_execution_state(experiment_id):
        database_threads.append(get_ident())
        return {
            "status": "cancelled",
            "progress": {"completed": 0, "total": 12},
        }

    monkeypatch.setattr(store_module, "load_execution_state", fake_load_execution_state)
    events = [
        event
        async for event in ExperimentStore().progress_events(
            "exp-thread-check",
            delay_seconds=0,
        )
    ]

    assert [event["type"] for event in events] == ["experiment.cancelled"]
    assert database_threads
    assert all(thread_id != event_loop_thread for thread_id in database_threads)


@pytest.mark.asyncio
async def test_client_disconnect_stops_stream_without_result_event():
    experiment_store = ExperimentStore()
    experiment = experiment_store.create_queued(ExperimentRequest(prompt="disconnect stream"))
    checks = iter([False, True])

    async def disconnected() -> bool:
        return next(checks)

    events = [
        item
        async for item in experiment_store.progress_events(
            experiment.id, is_disconnected=disconnected, delay_seconds=0
        )
    ]
    assert [event["type"] for event in events] == ["experiment.progress"]


def test_experiment_is_readable_across_store_instances():
    first = ExperimentStore()
    queued = first.create_queued(ExperimentRequest(prompt="cross store", repetitions=1))
    second = ExperimentStore()
    assert second.get(queued.id).status == "queued"
    second.execute(queued.id)
    assert first.get(queued.id).status == "completed"


def test_guarded_completion_does_not_recreate_missing_experiment():
    experiment_id = "exp-missing-guarded-completion"
    completed = demo_module.build_demo_experiment(
        experiment_id,
        "guarded completion",
        repetitions=1,
    )

    assert (
        persist_experiment(
            completed,
            expected_statuses={"running"},
        )
        is False
    )
    assert load_execution_state(experiment_id) is None


@pytest.mark.asyncio
async def test_runner_failure_is_persisted_and_streamed_without_traceback():
    def failing_runner(*args, **kwargs):
        raise RuntimeError("sensitive internal traceback")

    experiment_store = ExperimentStore(runner=failing_runner)
    queued = experiment_store.create_queued(ExperimentRequest(prompt="failure", repetitions=1))
    result = experiment_store.execute(queued.id)
    assert result.status == "failed"
    events = [event async for event in experiment_store.progress_events(queued.id, delay_seconds=0)]
    assert [event["type"] for event in events] == ["experiment.error"]
    assert events[0]["category"] == "internal_error"
    assert events[0]["experiment"]["status"] == "failed"
    assert "sensitive" not in events[0]["message"]


@pytest.mark.parametrize(
    "corruption",
    [
        "request",
        "snapshot",
        "tasks",
        "contract",
        "cassette_contract",
        "tool_gateway_url",
    ],
)
@pytest.mark.asyncio
async def test_invalid_persisted_execution_state_fails_closed(corruption):
    runner_calls = []

    def runner(*args, **kwargs):
        runner_calls.append((args, kwargs))
        raise AssertionError("invalid state reached the runner")

    experiment_store = ExperimentStore(runner=runner)
    queued = experiment_store.create_queued(
        ExperimentRequest(prompt="validate persisted state", repetitions=1)
    )
    with session_factory()() as session, session.begin():
        record = session.get(ExperimentRecord, queued.id)
        assert record is not None
        if corruption == "request":
            payload = dict(record.metrics or {})
            payload["request"] = {"prompt": ""}
            record.metrics = payload
        else:
            snapshot = dict(record.snapshot)
            if corruption == "snapshot":
                snapshot["candidate"] = {"id": "broken"}
            elif corruption == "tasks":
                snapshot["tasks"] = snapshot["tasks"][:1]
            elif corruption == "contract":
                snapshot["evaluator"] = "agentlens-evaluator@future"
            elif corruption == "cassette_contract":
                snapshot["cassette_contract"] = {"content_sha256": "tampered"}
            else:
                snapshot["tool_gateway_url"] = "https://user:secret@gateway.test/invoke"
            record.snapshot = snapshot

    result = experiment_store.execute(queued.id)
    events = [
        event
        async for event in experiment_store.progress_events(
            queued.id,
            delay_seconds=0,
        )
    ]

    assert result.status == "failed"
    assert not runner_calls
    assert [event["type"] for event in events] == ["experiment.error"]
    assert events[0]["category"] == "invalid_execution_state"
    assert events[0]["message"] == "persisted experiment state is invalid"


def test_running_cancellation_propagates_to_runner_boundary():
    started = Event()
    release = Event()

    def cancellable_runner(*args, on_progress, is_cancelled, **kwargs):
        on_progress(1, 12)
        started.set()
        assert release.wait(2)
        if is_cancelled():
            raise ExperimentCancelled
        raise AssertionError("runner did not observe cancellation")

    experiment_store = ExperimentStore(runner=cancellable_runner)
    queued = experiment_store.create_queued(
        ExperimentRequest(prompt="cancel running", repetitions=1)
    )
    thread = experiment_store.start_local(
        queued.id, ExperimentRequest(prompt="cancel running", repetitions=1)
    )
    assert started.wait(2)
    cancelled = experiment_store.cancel(queued.id)
    assert cancelled is not None
    release.set()
    thread.join(2)
    assert not thread.is_alive()
    assert experiment_store.get(queued.id).status == "cancelled"


def test_local_store_shutdown_cancels_and_joins_active_thread():
    started = Event()

    def cancellable_runner(*args, is_cancelled, **kwargs):
        started.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if is_cancelled():
                raise ExperimentCancelled
            time.sleep(0.01)
        raise AssertionError("shutdown cancellation did not reach the runner")

    experiment_store = ExperimentStore(runner=cancellable_runner)
    queued = experiment_store.create_queued(
        ExperimentRequest(prompt="shutdown local worker", repetitions=1)
    )
    thread = experiment_store.start_local(queued.id)
    assert started.wait(2)

    remaining = experiment_store.shutdown(wait_seconds=1)

    assert remaining == []
    assert not thread.is_alive()
    assert experiment_store.get(queued.id).status == "cancelled"


def test_local_thread_registration_rolls_back_when_start_fails(monkeypatch):
    class FailedThread:
        def __init__(self, *, target, name, daemon):
            self.target = target
            self.name = name
            self.daemon = daemon

        def start(self):
            raise RuntimeError("thread scheduler unavailable")

        def join(self, timeout):
            raise AssertionError("unstarted thread was retained for shutdown")

        def is_alive(self):
            return False

    monkeypatch.setattr(store_module, "Thread", FailedThread)
    experiment_store = ExperimentStore()
    queued = experiment_store.create_queued(
        ExperimentRequest(prompt="thread registration rollback", repetitions=1)
    )

    with pytest.raises(RuntimeError, match="scheduler unavailable"):
        experiment_store.start_local(queued.id)

    assert experiment_store.shutdown(wait_seconds=0) == []
    assert experiment_store.get(queued.id).status == "queued"


def test_local_thread_ignores_legacy_request_and_uses_persisted_request():
    captured = {}

    def capture_runner(*args, **kwargs):
        captured["prompt"] = args[1]
        captured["repetitions"] = args[4]
        captured["execution_mode"] = kwargs["execution_mode"]
        raise ExperimentCancelled

    experiment_store = ExperimentStore(runner=capture_runner)
    durable_request = ExperimentRequest(prompt="durable local request", repetitions=1)
    queued = experiment_store.create_queued(durable_request)
    legacy_request = durable_request.model_copy(
        update={"prompt": "tampered", "repetitions": 10, "execution_mode": "http"}
    )

    thread = experiment_store.start_local(queued.id, legacy_request)
    thread.join(2)

    assert not thread.is_alive()
    assert captured == {
        "prompt": "durable local request",
        "repetitions": 1,
        "execution_mode": "scripted",
    }
    assert experiment_store.get(queued.id).status == "cancelled"


def test_persisted_progress_counts_candidate_and_baseline_work():
    experiment_store = ExperimentStore()
    queued = experiment_store.create_queued(
        ExperimentRequest(prompt="combined progress", repetitions=1)
    )
    assert queued.completed_runs == 0
    assert queued.total_runs == 12
    assert claim_experiment(queued.id)
    assert update_experiment_progress(queued.id, 7, 12)

    running = experiment_store.get(queued.id)
    assert running is not None
    assert running.status == "running"
    assert running.completed_runs == 7
    assert running.total_runs == 12
    trials = next(step for step in running.plan if step.key == "trials")
    assert trials.status == "active"
    assert trials.detail == "7 / 12"


def test_progress_updates_reject_invalid_regressive_or_changed_totals():
    for completed, total in [(-1, 12), (13, 12), (0, 13)]:
        experiment_store = ExperimentStore()
        queued = experiment_store.create_queued(
            ExperimentRequest(prompt=f"invalid progress {completed}/{total}", repetitions=1)
        )
        assert claim_experiment(queued.id)
        with pytest.raises(ValueError, match="progress"):
            update_experiment_progress(queued.id, completed, total)
        assert load_execution_state(queued.id)["progress"] == {"completed": 0, "total": 12}

    experiment_store = ExperimentStore()
    queued = experiment_store.create_queued(
        ExperimentRequest(prompt="regressive progress", repetitions=1)
    )
    assert claim_experiment(queued.id)
    assert update_experiment_progress(queued.id, 7, 12)
    with pytest.raises(ValueError, match="progress"):
        update_experiment_progress(queued.id, 6, 12)
    assert load_execution_state(queued.id)["progress"] == {"completed": 7, "total": 12}


def test_running_heartbeat_is_renewed_and_eventually_expires():
    experiment_store = ExperimentStore()
    queued = experiment_store.create_queued(
        ExperimentRequest(prompt="heartbeat lease", repetitions=1)
    )
    started = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    assert claim_experiment(queued.id, now=started)
    assert not mark_stale_experiment(queued.id, 300, now=started + timedelta(seconds=299))
    assert update_experiment_progress(
        queued.id,
        1,
        12,
        now=started + timedelta(seconds=250),
    )
    assert not mark_stale_experiment(queued.id, 300, now=started + timedelta(seconds=549))
    assert mark_stale_experiment(queued.id, 300, now=started + timedelta(seconds=551))

    state = load_execution_state(queued.id)
    assert state["status"] == "failed"
    assert state["progress"] == {"completed": 1, "total": 12}
    assert state["error"] == {
        "category": "worker_lost",
        "message": "experiment worker heartbeat expired",
    }


@pytest.mark.asyncio
async def test_arq_worker_cancellation_persists_interrupted_terminal_state(monkeypatch):
    started = Event()
    release = Event()
    experiment_store = ExperimentStore()
    queued = experiment_store.create_queued(
        ExperimentRequest(prompt="interrupted worker", repetitions=1)
    )
    assert claim_experiment(queued.id)

    def blocked_execute(experiment_id):
        started.set()
        assert release.wait(2)
        return experiment_store.get(experiment_id)

    blocked_store = type(
        "BlockedStore",
        (),
        {"execute": staticmethod(blocked_execute)},
    )()
    task = asyncio.create_task(
        worker_module.execute_experiment(
            {"experiment_store": blocked_store},
            queued.id,
        )
    )
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    state = load_execution_state(queued.id)
    assert state["status"] == "failed"
    assert state["error"] == {
        "category": "worker_interrupted",
        "message": "experiment worker was interrupted",
    }


@pytest.mark.asyncio
async def test_arq_worker_cancellation_stops_underlying_thread(monkeypatch):
    started = Event()
    cancellation_observed = Event()
    finished = Event()
    release = Event()

    def cooperative_runner(*args, is_cancelled, **kwargs):
        started.set()
        try:
            while not release.is_set():
                if is_cancelled():
                    cancellation_observed.set()
                    raise ExperimentCancelled
                time.sleep(0.01)
            raise ExperimentCancelled
        finally:
            finished.set()

    experiment_store = ExperimentStore(runner=cooperative_runner)
    queued = experiment_store.create_queued(
        ExperimentRequest(prompt="interrupt underlying thread", repetitions=1)
    )
    task = asyncio.create_task(
        worker_module.execute_experiment(
            {"experiment_store": experiment_store},
            queued.id,
        )
    )
    assert await asyncio.to_thread(started.wait, 2)

    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancellation_observed.is_set()
        assert finished.is_set()
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 1)

    state = load_execution_state(queued.id)
    assert state["status"] == "failed"
    assert state["error"]["category"] == "worker_interrupted"


def test_arq_worker_publishes_frequent_health_sentinel():
    settings = worker_module.WorkerSettings
    assert settings.queue_name == "arq:queue"
    assert settings.health_check_key == "agentlens:worker:health"
    assert settings.health_check_interval == 10
    assert settings.job_timeout == 108_600
    assert settings.on_startup is worker_module.startup
    assert settings.on_shutdown is worker_module.shutdown
    assert worker_module.WORKER_INTERRUPT_GRACE_SECONDS == 2.0


@pytest.mark.asyncio
async def test_arq_worker_lifecycle_owns_injected_dependencies(monkeypatch):
    shutdown_calls = []

    class FakeStore:
        def shutdown(self):
            shutdown_calls.append("shutdown")

    fake_store = FakeStore()
    monkeypatch.setattr(
        worker_module,
        "_worker_store_for",
        lambda ctx: ctx.setdefault("experiment_store", fake_store),
    )
    context = {}

    await worker_module.startup(context)
    await worker_module.shutdown(context)

    assert context["experiment_store"] is fake_store
    assert shutdown_calls == ["shutdown"]


@pytest.mark.asyncio
async def test_arq_worker_executes_persisted_queued_request():
    producer = ExperimentStore()
    request = ExperimentRequest(prompt="worker contract", repetitions=1)
    queued = producer.create_queued(request)
    worker_context = {}
    first_result_id = await worker_module.execute_experiment(worker_context, queued.id)
    second_result_id = await worker_module.execute_experiment(worker_context, queued.id)
    result = producer.get(queued.id)
    assert first_result_id == second_result_id == queued.id
    assert result.status == "completed"
    assert len(result.runs) == 6
    assert len(result.baseline_runs) == 6


@pytest.mark.asyncio
async def test_arq_worker_rejects_unknown_experiment():
    with pytest.raises(LookupError, match="does not exist"):
        await worker_module.execute_experiment({}, "exp-missing")


@pytest.mark.asyncio
async def test_arq_worker_ignores_legacy_payload_and_uses_persisted_request(monkeypatch):
    captured = {}

    def capture_runner(
        experiment_id,
        prompt,
        candidate_id,
        baseline_candidate_id,
        repetitions,
        **kwargs,
    ):
        captured.update(
            {
                "experiment_id": experiment_id,
                "prompt": prompt,
                "candidate_id": candidate_id,
                "baseline_candidate_id": baseline_candidate_id,
                "repetitions": repetitions,
                "execution_mode": kwargs["execution_mode"],
            }
        )
        raise ExperimentCancelled

    request = ExperimentRequest(prompt="durable worker request", repetitions=1)
    queued = ExperimentStore().create_queued(request)
    injected_store = ExperimentStore(runner=capture_runner)
    tampered_payload = request.model_copy(
        update={"prompt": "tampered", "repetitions": 10, "execution_mode": "http"}
    ).model_dump(mode="json")

    result_id = await worker_module.execute_experiment(
        {"experiment_store": injected_store},
        queued.id,
        tampered_payload,
    )

    assert result_id == queued.id
    assert captured["prompt"] == "durable worker request"
    assert captured["repetitions"] == 1
    assert captured["execution_mode"] == "scripted"
    assert injected_store.get(queued.id).status == "cancelled"


def test_worker_uses_durable_candidate_and_environment_snapshot(monkeypatch):
    candidate = demo_module.CANDIDATES["support-v1.4"].model_copy(
        update={
            "id": "snapshot-v2",
            "model": "remote-v2",
            "endpoint": "http://snapshot-v2.test",
        }
    )
    baseline = demo_module.CANDIDATES["support-v1.3"].model_copy(
        update={
            "id": "snapshot-v1",
            "model": "remote-v1",
            "endpoint": "http://snapshot-v1.test",
        }
    )
    catalog = {candidate.id: candidate, baseline.id: baseline}
    monkeypatch.setattr(demo_module, "candidate_catalog", lambda: catalog)
    captured = {}

    def capture_runner(*args, **kwargs):
        captured.update(kwargs)
        raise ExperimentCancelled

    experiment_store = ExperimentStore(runner=capture_runner)
    request = ExperimentRequest(
        prompt="durable snapshot",
        candidate_id=candidate.id,
        baseline_candidate_id=baseline.id,
        execution_mode="http",
        repetitions=1,
    )
    queued = experiment_store.create_queued(request)
    with session_factory()() as session:
        record = session.get(ExperimentRecord, queued.id)
        assert record is not None
        assert record.snapshot["benchmark_id"] == "customer-tools-v2"
        assert record.snapshot["task_set_version"] == "customer-tools-v2@2026-08-12"
        assert record.snapshot["calibration"] == "human-failure-calibration@2026-09-14"
        assert record.snapshot["evaluator"] == "agentlens-evaluator@0.1.0"
        assert record.snapshot["price_table"] == "pricing-cny-2026-08"
        assert record.snapshot["cassette_contract"] == registry.environment_contract(
            record.snapshot["cassette"]
        )
        assert len(record.snapshot["tasks"]) == 6
    monkeypatch.setattr(
        demo_module,
        "candidate_catalog",
        lambda: (_ for _ in ()).throw(AssertionError("worker reread candidate config")),
    )
    result = experiment_store.execute(queued.id, request)

    assert result.status == "cancelled"
    assert captured["candidate_snapshot"].model == "remote-v2"
    assert captured["baseline_snapshot"].endpoint == "http://snapshot-v1.test"
    assert captured["environment_snapshot"] == "customer-tools-v2:cassette-2026-08-12"
    assert captured["cassette_content_sha256"] == registry.snapshot(
        "customer-tools-v2"
    )["content_sha256"]
    assert captured["tool_gateway_url"].endswith("/api/v1/tools/invoke")
    assert [task.id for task in captured["task_snapshot"]] == [
        task.id for task in demo_module.TASKS
    ]


def test_worker_accepts_equivalent_cassette_registry_binding(monkeypatch):
    runner_calls = []

    def runner(*args, **kwargs):
        runner_calls.append((args, kwargs))
        raise ExperimentCancelled

    experiment_store = ExperimentStore(runner=runner)
    queued = experiment_store.create_queued(
        ExperimentRequest(prompt="equivalent cassette binding", repetitions=1)
    )
    original = registry._cassettes["customer-tools-v2"]
    equivalent = ToolCassette(mode=original.mode, records=deepcopy(original.records))
    monkeypatch.setitem(registry._cassettes, "customer-tools-v2", equivalent)

    result = experiment_store.execute(queued.id)

    assert result is not None
    assert result.status == "cancelled"
    assert len(runner_calls) == 1


def test_worker_rejects_cassette_response_drift_before_claim(monkeypatch):
    runner_calls = []

    def runner(*args, **kwargs):
        runner_calls.append((args, kwargs))
        raise AssertionError("drifted cassette reached the runner")

    experiment_store = ExperimentStore(runner=runner)
    queued = experiment_store.create_queued(
        ExperimentRequest(prompt="cassette response drift", repetitions=1)
    )
    original = registry._cassettes["customer-tools-v2"]
    drifted = ToolCassette(mode=original.mode, records=deepcopy(original.records))
    first_key = min(drifted.records)
    drifted.records[first_key] = {"drifted": True}
    monkeypatch.setitem(registry._cassettes, "customer-tools-v2", drifted)

    result = experiment_store.execute(queued.id)
    state = load_execution_state(queued.id)

    assert result is not None
    assert result.status == "failed"
    assert state is not None
    assert state["error"]["category"] == "invalid_execution_state"
    assert not runner_calls


def test_worker_rejects_missing_cassette_registry_binding(monkeypatch):
    experiment_store = ExperimentStore(
        runner=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("missing cassette reached the runner")
        )
    )
    queued = experiment_store.create_queued(
        ExperimentRequest(prompt="missing cassette binding", repetitions=1)
    )
    monkeypatch.delitem(registry._cassettes, "customer-tools-v2")

    result = experiment_store.execute(queued.id)
    state = load_execution_state(queued.id)

    assert result is not None
    assert result.status == "failed"
    assert state is not None
    assert state["error"]["category"] == "invalid_execution_state"


def test_worker_executes_frozen_tasks_after_runtime_catalog_changes(monkeypatch):
    experiment_store = ExperimentStore()
    queued = experiment_store.create_queued(
        ExperimentRequest(prompt="frozen task snapshot", repetitions=1)
    )
    original_task_ids = {task.id for task in demo_module.TASKS}
    runtime_only_task = demo_module.TASKS[0].model_copy(
        update={"id": "runtime-only-task", "name": "Runtime-only task"}
    )
    monkeypatch.setattr(demo_module, "TASKS", [runtime_only_task])
    monkeypatch.setattr(store_module, "TASKS", [runtime_only_task])

    result = experiment_store.execute(queued.id)

    assert result is not None
    assert result.status == "completed"
    assert result.total_runs == 12
    assert {run.task_id for run in (*result.runs, *result.baseline_runs)} == original_task_ids
    assert all(run.task_id != "runtime-only-task" for run in result.runs)
