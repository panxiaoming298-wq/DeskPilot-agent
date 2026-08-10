"""One-shot process entry point for exactly one authorized Tool invocation."""

import importlib
import re
import sys
from threading import Event

from deskpilot.core.canonical_json import canonical_json_bytes
from deskpilot.runner.executor import ToolExecutor
from deskpilot.runner.worker_protocol import (
    MAX_WORKER_FRAME_BYTES,
    WorkerError,
    WorkerRequest,
    WorkerResponse,
)

FACTORY_PATTERN = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*$")


def _load_executor(factory_path: str) -> ToolExecutor:
    if FACTORY_PATTERN.fullmatch(factory_path) is None:
        raise ValueError("Worker executor factory path is invalid")
    module_name, attribute_name = factory_path.split(":", 1)
    module = importlib.import_module(module_name)
    factory = getattr(module, attribute_name)
    if not callable(factory):
        raise TypeError("Worker executor factory is not callable")
    executor = factory()
    if not isinstance(executor, ToolExecutor):
        raise TypeError("Worker executor factory returned an unexpected object")
    return executor


def _failure(call_id: str, error: Exception) -> WorkerResponse:
    code = getattr(error, "code", "TOOL_EXECUTION_FAILED")
    if not isinstance(code, str) or re.fullmatch(r"^[A-Z][A-Z0-9_]{1,99}$", code) is None:
        code = "TOOL_EXECUTION_FAILED"
    return WorkerResponse(
        call_id=call_id,
        status="failed",
        error=WorkerError(
            code=code,
            message=f"Tool worker failed: {type(error).__name__}"[:1_000],
        ),
    )


def run_worker(factory_path: str) -> int:
    frame = sys.stdin.buffer.readline(MAX_WORKER_FRAME_BYTES + 1)
    call_id = "worker-invalid-request"
    try:
        if not frame or len(frame) > MAX_WORKER_FRAME_BYTES or not frame.endswith(b"\n"):
            raise ValueError("Worker request frame is missing or too large")
        request = WorkerRequest.model_validate_json(frame)
        call_id = request.call_id
        executor = _load_executor(factory_path)
        output = executor.execute_worker_request(
            tool_name=request.tool_name,
            tool_version=request.tool_version,
            contract_digest=request.contract_digest,
            arguments=request.arguments,
            resources=request.resources,
            cancellation=Event(),
        )
        response = WorkerResponse(
            call_id=request.call_id,
            status="succeeded",
            output=output.model_dump(mode="json"),
        )
    except Exception as error:
        response = _failure(call_id, error)

    encoded = canonical_json_bytes(response) + b"\n"
    if len(encoded) > MAX_WORKER_FRAME_BYTES:
        response = WorkerResponse(
            call_id=call_id,
            status="failed",
            error=WorkerError(
                code="TOOL_OUTPUT_TOO_LARGE",
                message="Tool worker response exceeded the process boundary limit",
            ),
        )
        encoded = canonical_json_bytes(response) + b"\n"
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()
    return 0


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    return run_worker(sys.argv[1])


if __name__ == "__main__":
    raise SystemExit(main())
