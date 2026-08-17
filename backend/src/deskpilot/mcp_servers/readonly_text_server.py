"""Minimal stdio MCP server with one deterministic, side-effect-free text tool."""

import hashlib
import json
import re
import sys
from typing import Any, cast

PROTOCOL_VERSION = "2025-11-25"
TOOL_NAME = "deskpilot.text.metrics"
INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"text": {"type": "string", "minLength": 1, "maxLength": 4096}},
    "required": ["text"],
    "additionalProperties": False,
}
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "character_count": {"type": "integer", "minimum": 1},
        "line_count": {"type": "integer", "minimum": 1},
        "word_count": {"type": "integer", "minimum": 0},
        "text_digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    },
    "required": ["character_count", "line_count", "word_count", "text_digest"],
    "additionalProperties": False,
}
TOOL = {
    "name": TOOL_NAME,
    "title": "本地文本指标",
    "description": "在隔离的本地 stdio Server 中计算文本长度、行数、词数和摘要。",
    "inputSchema": INPUT_SCHEMA,
    "outputSchema": OUTPUT_SCHEMA,
    "annotations": {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "execution": {"taskSupport": "forbidden"},
}


def _reply(
    request_id: object, *, result: dict[str, Any] | None = None, error: dict[str, Any] | None = None
) -> None:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result or {}
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _tool_result(arguments: object) -> dict[str, Any]:
    if not isinstance(arguments, dict) or set(arguments) != {"text"}:
        return {"content": [{"type": "text", "text": "Invalid text input"}], "isError": True}
    text = arguments.get("text")
    if not isinstance(text, str) or not 1 <= len(text) <= 4096:
        return {"content": [{"type": "text", "text": "Invalid text input"}], "isError": True}
    structured = {
        "character_count": len(text),
        "line_count": text.count("\n") + 1,
        "word_count": len(re.findall(r"\w+", text, flags=re.UNICODE)),
        "text_digest": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(structured, ensure_ascii=False, separators=(",", ":")),
            }
        ],
        "structuredContent": structured,
        "isError": False,
    }


def main() -> int:
    cast(Any, sys.stdin).reconfigure(encoding="utf-8")
    cast(Any, sys.stdout).reconfigure(encoding="utf-8")
    initialized = False
    ready = False
    for raw_line in sys.stdin:
        try:
            message = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            continue
        method = message.get("method")
        request_id = message.get("id")
        if method == "initialize" and request_id is not None and not initialized:
            params = message.get("params")
            if not isinstance(params, dict) or params.get("protocolVersion") != PROTOCOL_VERSION:
                _reply(
                    request_id, error={"code": -32602, "message": "Unsupported protocol version"}
                )
                continue
            initialized = True
            _reply(
                request_id,
                result={
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {
                        "name": "deskpilot.readonly-text",
                        "title": "DeskPilot Read-only Text",
                        "version": "1.0.0",
                    },
                },
            )
        elif method == "notifications/initialized" and initialized and request_id is None:
            ready = True
        elif method == "tools/list" and request_id is not None and ready:
            _reply(request_id, result={"tools": [TOOL]})
        elif method == "tools/call" and request_id is not None and ready:
            params = message.get("params")
            if not isinstance(params, dict) or params.get("name") != TOOL_NAME:
                _reply(request_id, error={"code": -32602, "message": "Unknown tool"})
            else:
                _reply(request_id, result=_tool_result(params.get("arguments", {})))
        elif request_id is not None:
            _reply(request_id, error={"code": -32601, "message": "Method not found"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
