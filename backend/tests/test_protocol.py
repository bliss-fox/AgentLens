import httpx
import pytest

from agentlens.agent_client import AgentProtocolError, parse_sse_lines, run_http_agent
from agentlens.cassette import CassetteMissError, ToolCassette
from agentlens.schemas import AgentRunRequest, Budget, EventStreamValidator, EventType, TraceEvent
from agentlens.store import ExperimentStore


def event(seq, kind):
    return TraceEvent(run_id="run-1", seq=seq, type=kind)


def test_valid_complete_event_stream():
    validator = EventStreamValidator("run-1")
    for item in [event(0, EventType.RUN_STARTED), event(1, EventType.FINAL), event(2, EventType.RUN_COMPLETED)]:
        validator.accept(item)
    validator.finish()


@pytest.mark.parametrize("items", [
    [event(1, EventType.RUN_STARTED)],
    [event(0, EventType.RUN_STARTED), event(2, EventType.FINAL)],
    [event(0, EventType.RUN_STARTED), event(1, EventType.RUN_COMPLETED)],
])
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


async def lines(values):
    for value in values:
        yield value


@pytest.mark.asyncio
async def test_chunked_sse_parser_validates_complete_stream():
    values = []
    for item in [event(0, EventType.RUN_STARTED), event(1, EventType.FINAL), event(2, EventType.RUN_COMPLETED)]:
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
        await parse_sse_lines(lines(["data: {not-json}"]), "run-1")


def agent_request() -> AgentRunRequest:
    return AgentRunRequest(
        run_id="run-1", task_id="task-1", task_input={}, seed=1,
        environment_snapshot="cassette:v1", tool_gateway_url="http://gateway",
        budget=Budget(max_seconds=1),
    )


@pytest.mark.asyncio
async def test_http_agent_rejects_wrong_content_type():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))
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
async def test_cancel_during_stream_emits_terminal_cancellation():
    experiment_store = ExperimentStore()
    experiment = experiment_store.latest()
    stream = experiment_store.progress_events(experiment.id, delay_seconds=0)
    first = await anext(stream)
    assert first["type"] == "experiment.progress"
    experiment_store.cancel(experiment.id)
    terminal = await anext(stream)
    assert terminal["type"] == "experiment.cancelled"
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


@pytest.mark.asyncio
async def test_client_disconnect_stops_stream_without_result_event():
    experiment_store = ExperimentStore()
    experiment = experiment_store.latest()
    checks = iter([False, True])

    async def disconnected() -> bool:
        return next(checks)

    events = [
        item async for item in experiment_store.progress_events(
            experiment.id, is_disconnected=disconnected, delay_seconds=0
        )
    ]
    assert [event["type"] for event in events] == ["experiment.progress"]
