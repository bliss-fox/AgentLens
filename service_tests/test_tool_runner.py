import asyncio

import pytest
from fastapi.testclient import TestClient

from tool_runner.app import MAX_OUTPUT_BYTES, app, execute_python

client = TestClient(app)


def test_static_analysis_returns_structured_findings():
    response = client.post(
        "/invoke",
        json={
            "tool": "analyze_code",
            "arguments": {"code": "try:\n    pass\nexcept:\n    pass"},
        },
    )
    assert response.status_code == 200
    assert response.json()["finding_codes"] == ["BARE_EXCEPT"]


def test_forbidden_import_is_rejected_before_execution():
    response = client.post(
        "/invoke",
        json={"tool": "execute_python", "arguments": {"code": "import os"}},
    )
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "rejected"
    assert result["findings"][0]["code"] == "IMPORT_FORBIDDEN"


@pytest.mark.asyncio
async def test_execution_returns_stdout_and_exit_code():
    result = await execute_python("print(sum([1, 2, 3]))", timeout=2)
    assert result["ok"] is True
    assert result["stdout"].splitlines() == ["6"]
    assert result["exit_code"] == 0


@pytest.mark.asyncio
async def test_infinite_loop_is_killed_on_timeout():
    before = {task for task in asyncio.all_tasks() if not task.done()}
    result = await execute_python("while True:\n    pass", timeout=1)
    await asyncio.sleep(0)
    after = {task for task in asyncio.all_tasks() if not task.done()}
    assert result["status"] == "timeout"
    assert result["ok"] is False
    assert after <= before | {asyncio.current_task()}


@pytest.mark.asyncio
async def test_output_is_bounded():
    result = await execute_python(
        f"print('x' * {MAX_OUTPUT_BYTES + 10_000})",
        timeout=2,
    )
    assert result["ok"] is False
    assert result["truncated"] is True
    assert len(result["stdout"].encode("utf-8")) <= MAX_OUTPUT_BYTES
