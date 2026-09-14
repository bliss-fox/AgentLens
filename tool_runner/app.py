from __future__ import annotations

import ast
import asyncio
import os
import signal
import sys
import time
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

MAX_CODE_BYTES = 50_000
MAX_OUTPUT_BYTES = 65_536
MAX_TIMEOUT_SECONDS = 20
FORBIDDEN_CALLS = {
    "__import__",
    "breakpoint",
    "compile",
    "eval",
    "exec",
    "globals",
    "help",
    "input",
    "locals",
    "open",
    "vars",
}
FORBIDDEN_ATTRIBUTES = {
    "fork",
    "kill",
    "popen",
    "remove",
    "rename",
    "replace",
    "rmdir",
    "spawn",
    "system",
    "unlink",
}


class ToolRequest(BaseModel):
    tool: Literal["execute_python", "analyze_code"]
    arguments: dict[str, Any] = Field(default_factory=dict)


def _source(arguments: dict[str, Any]) -> str:
    source = arguments.get("code")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("code must be a non-empty string")
    if len(source.encode("utf-8")) > MAX_CODE_BYTES:
        raise ValueError("code exceeds the 50000-byte limit")
    return source


def policy_findings(source: str) -> list[dict[str, Any]]:
    try:
        tree = ast.parse(source, mode="exec")
    except SyntaxError as error:
        return [
            {
                "code": "SYNTAX_ERROR",
                "severity": "error",
                "line": error.lineno or 1,
                "message": error.msg,
            }
        ]
    findings: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            findings.append(
                {
                    "code": "IMPORT_FORBIDDEN",
                    "severity": "error",
                    "line": node.lineno,
                    "message": "imports are disabled in the constrained runner",
                }
            )
        if isinstance(node, ast.ExceptHandler) and node.type is None:
            findings.append(
                {
                    "code": "BARE_EXCEPT",
                    "severity": "warning",
                    "line": node.lineno,
                    "message": "bare except catches process-control exceptions",
                }
            )
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and len(node.body) > 50:
            findings.append(
                {
                    "code": "LONG_FUNCTION",
                    "severity": "warning",
                    "line": node.lineno,
                    "message": f"function {node.name} has more than 50 top-level statements",
                }
            )
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else None
            if name in FORBIDDEN_CALLS:
                findings.append(
                    {
                        "code": "CALL_FORBIDDEN",
                        "severity": "error",
                        "line": node.lineno,
                        "message": f"call to {name} is disabled",
                    }
                )
            attribute = node.func.attr if isinstance(node.func, ast.Attribute) else None
            if attribute in FORBIDDEN_ATTRIBUTES:
                findings.append(
                    {
                        "code": "ATTRIBUTE_FORBIDDEN",
                        "severity": "error",
                        "line": node.lineno,
                        "message": f"call to .{attribute} is disabled",
                    }
                )
    return findings


def analyze_code(source: str) -> dict[str, Any]:
    findings = policy_findings(source)
    return {
        "ok": not any(item["severity"] == "error" for item in findings),
        "parseable": not any(item["code"] == "SYNTAX_ERROR" for item in findings),
        "findings": findings,
        "finding_codes": [item["code"] for item in findings],
    }


def _child_limits(timeout: int) -> None:
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (timeout, timeout + 1))
        resource.setrlimit(resource.RLIMIT_AS, (128 * 1024 * 1024, 128 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024, 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_NPROC, (16, 16))
    except (ImportError, ValueError, OSError):
        # Compose memory/PID limits remain the outer boundary on unsupported hosts.
        return


async def _terminate(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (AttributeError, ProcessLookupError, PermissionError):
        process.kill()
    await process.wait()


async def _read_limited(stream: asyncio.StreamReader) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            return b"".join(chunks), False
        remaining = MAX_OUTPUT_BYTES - total
        if remaining > 0:
            chunks.append(chunk[:remaining])
        total += len(chunk)
        if total > MAX_OUTPUT_BYTES:
            return b"".join(chunks), True


async def execute_python(source: str, timeout: int) -> dict[str, Any]:
    findings = policy_findings(source)
    blocking = [item for item in findings if item["severity"] == "error"]
    if blocking:
        return {
            "ok": False,
            "status": "rejected",
            "exit_code": None,
            "stdout": "",
            "stderr": "policy rejected the source",
            "findings": blocking,
            "truncated": False,
        }
    started = time.monotonic()
    process_options: dict[str, Any] = {}
    if os.name != "nt":
        process_options.update(
            start_new_session=True,
            preexec_fn=lambda: _child_limits(timeout),
        )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-c",
        source,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONIOENCODING": "utf-8"},
        **process_options,
    )
    assert process.stdout is not None and process.stderr is not None
    stdout_task = asyncio.create_task(_read_limited(process.stdout))
    stderr_task = asyncio.create_task(_read_limited(process.stderr))
    timed_out = False
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except TimeoutError:
        timed_out = True
        await _terminate(process)
    stdout_result, stderr_result = await asyncio.gather(stdout_task, stderr_task)
    stdout, stdout_truncated = stdout_result
    stderr, stderr_truncated = stderr_result
    truncated = stdout_truncated or stderr_truncated
    status = "timeout" if timed_out else "output_limit" if truncated else "completed"
    return {
        "ok": process.returncode == 0 and not timed_out and not truncated,
        "status": status,
        "exit_code": process.returncode,
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
        "duration_seconds": round(time.monotonic() - started, 4),
        "findings": findings,
        "truncated": truncated,
    }


app = FastAPI(title="AgentLens constrained tool runner", version="0.1.0")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/invoke")
async def invoke(body: ToolRequest) -> dict[str, Any]:
    try:
        source = _source(body.arguments)
        if body.tool == "analyze_code":
            return analyze_code(source)
        requested_timeout = body.arguments.get("timeout", 10)
        if isinstance(requested_timeout, bool) or not isinstance(requested_timeout, int):
            raise TypeError("timeout must be an integer")
        timeout = max(1, min(requested_timeout, MAX_TIMEOUT_SECONDS))
        return await execute_python(source, timeout)
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
