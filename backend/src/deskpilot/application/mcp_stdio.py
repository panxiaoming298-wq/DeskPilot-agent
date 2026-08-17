"""Strict, short-lived MCP stdio client for trusted built-in servers."""

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from deskpilot.core.canonical_json import canonical_json_bytes

MCP_PROTOCOL_VERSION = "2025-11-25"
MAX_MCP_FRAME_BYTES = 65_536


class McpStdioError(RuntimeError):
    code = "MCP_PROTOCOL_REJECTED"


class McpStdioTimeoutError(McpStdioError):
    code = "MCP_REQUEST_TIMEOUT"


class McpBundleRejectedError(McpStdioError):
    code = "MCP_SERVER_BUNDLE_REJECTED"


@dataclass(frozen=True, slots=True)
class McpStdioResult:
    protocol_version: str
    tools: tuple[dict[str, Any], ...]
    structured_content: dict[str, Any]


class McpStdioHost:
    def __init__(self, server_script: Path, *, timeout_seconds: float = 3.0) -> None:
        self._server_script = server_script.resolve(strict=True)
        self._bundle_digest = hashlib.sha256(self._server_script.read_bytes()).hexdigest()
        self._timeout_seconds = timeout_seconds

    @property
    def bundle_digest(self) -> str:
        return self._bundle_digest

    async def invoke(self, tool_name: str, arguments: dict[str, Any]) -> McpStdioResult:
        current_digest = await asyncio.to_thread(
            lambda: hashlib.sha256(self._server_script.read_bytes()).hexdigest()
        )
        if current_digest != self._bundle_digest:
            raise McpBundleRejectedError("MCP server bundle changed after registration")
        creation: dict[str, Any] = {}
        if os.name == "nt":
            creation["creationflags"] = subprocess.CREATE_NO_WINDOW
        else:
            creation["start_new_session"] = True
        with tempfile.TemporaryDirectory(prefix="deskpilot-mcp-") as working_directory:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                str(self._server_script),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=working_directory,
                env=self._isolated_environment(),
                limit=MAX_MCP_FRAME_BYTES,
                **creation,
            )
            if process.stderr is None:
                await self._shutdown(process)
                raise McpStdioError("MCP stderr is unavailable")
            stderr_task = asyncio.create_task(process.stderr.read(MAX_MCP_FRAME_BYTES + 1))
            active_request = [1]
            try:
                async with asyncio.timeout(self._timeout_seconds):
                    return await self._exchange(process, tool_name, arguments, active_request)
            except TimeoutError as error:
                await self._send_cancellation(process, active_request[0])
                raise McpStdioTimeoutError("MCP request exceeded its deadline") from error
            finally:
                await self._shutdown(process)
                await stderr_task

    @staticmethod
    def _isolated_environment() -> dict[str, str]:
        allowed = ("SystemRoot", "WINDIR", "COMSPEC", "TEMP", "TMP")
        environment = {key: os.environ[key] for key in allowed if key in os.environ}
        environment["PATH"] = str(Path(sys.executable).parent)
        return environment

    async def _exchange(
        self,
        process: asyncio.subprocess.Process,
        tool_name: str,
        arguments: dict[str, Any],
        active_request: list[int],
    ) -> McpStdioResult:
        await self._send(
            process,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "deskpilot", "version": "0.1.0"},
                },
            },
        )
        initialized = await self._response(process, 1)
        if initialized.get("protocolVersion") != MCP_PROTOCOL_VERSION:
            raise McpStdioError("MCP protocol version mismatch")
        capabilities = initialized.get("capabilities")
        if not isinstance(capabilities, dict) or not isinstance(capabilities.get("tools"), dict):
            raise McpStdioError("MCP server did not negotiate tools")
        await self._send(
            process,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        active_request[0] = 2
        await self._send(
            process,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        listed = await self._response(process, 2)
        tools = listed.get("tools")
        if not isinstance(tools, list) or any(not isinstance(tool, dict) for tool in tools):
            raise McpStdioError("MCP tool list is invalid")
        active_request[0] = 3
        await self._send(
            process,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            },
        )
        called = await self._response(process, 3)
        if called.get("isError") is True:
            raise McpStdioError("MCP tool returned an execution error")
        content = called.get("structuredContent")
        if not isinstance(content, dict):
            raise McpStdioError("MCP tool omitted structured content")
        if len(canonical_json_bytes(content)) > MAX_MCP_FRAME_BYTES:
            raise McpStdioError("MCP structured content exceeds the limit")
        return McpStdioResult(
            protocol_version=MCP_PROTOCOL_VERSION,
            tools=tuple(tools),
            structured_content=content,
        )

    @staticmethod
    async def _send(process: asyncio.subprocess.Process, payload: dict[str, Any]) -> None:
        if process.stdin is None:
            raise McpStdioError("MCP stdin is unavailable")
        encoded = canonical_json_bytes(payload) + b"\n"
        if len(encoded) > MAX_MCP_FRAME_BYTES:
            raise McpStdioError("MCP request exceeds the frame limit")
        process.stdin.write(encoded)
        try:
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as error:
            raise McpStdioError("MCP server closed its input") from error

    @staticmethod
    async def _response(process: asyncio.subprocess.Process, request_id: int) -> dict[str, Any]:
        if process.stdout is None:
            raise McpStdioError("MCP stdout is unavailable")
        try:
            raw = await process.stdout.readline()
        except (ValueError, asyncio.LimitOverrunError) as error:
            raise McpStdioError("MCP response exceeds the frame limit") from error
        if not raw or len(raw) > MAX_MCP_FRAME_BYTES or not raw.endswith(b"\n"):
            raise McpStdioError("MCP response frame is incomplete")
        try:
            message = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise McpStdioError("MCP response is not valid JSON") from error
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise McpStdioError("MCP response is not JSON-RPC 2.0")
        if message.get("id") != request_id:
            raise McpStdioError("MCP response ID mismatch")
        if "error" in message or not isinstance(message.get("result"), dict):
            raise McpStdioError("MCP server returned a protocol error")
        return cast(dict[str, Any], message["result"])

    @classmethod
    async def _send_cancellation(
        cls,
        process: asyncio.subprocess.Process,
        request_id: int,
    ) -> None:
        try:
            await cls._send(
                process,
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/cancelled",
                    "params": {"requestId": request_id, "reason": "DeskPilot deadline"},
                },
            )
        except McpStdioError:
            pass

    @staticmethod
    async def _shutdown(process: asyncio.subprocess.Process) -> None:
        if process.stdin is not None:
            process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), timeout=0.5)
            return
        except TimeoutError:
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=0.5)
        except TimeoutError:
            process.kill()
            await process.wait()
