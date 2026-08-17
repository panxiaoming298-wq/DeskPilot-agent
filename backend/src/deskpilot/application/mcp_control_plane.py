"""Trusted MCP registry, local risk floor, invocation and value-free audit."""

import asyncio
from datetime import UTC
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from deskpilot.application.mcp_stdio import McpStdioError, McpStdioHost
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.mcp import (
    McpAuditEventRead,
    McpAuditPage,
    McpServerMutationRead,
    McpServerRead,
    McpTextMetricsInput,
    McpTextMetricsOutput,
    McpToolCallRead,
    McpToolRead,
)
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import (
    McpAuditEventRecord,
    McpAuditStateRecord,
    McpServerStateRecord,
    utc_now,
)
from deskpilot.mcp_servers.readonly_text_server import (
    INPUT_SCHEMA,
    OUTPUT_SCHEMA,
    PROTOCOL_VERSION,
    TOOL,
    TOOL_NAME,
)
from deskpilot.observability import TelemetryFacade

SERVER_ID = "deskpilot.readonly-text"
SERVER_TITLE = "DeskPilot 只读文本 Server"
COMMAND_PREVIEW = ("python", "-I", "<bundled>/readonly_text_server.py")


class McpControlError(RuntimeError):
    code = "MCP_CONTROL_REJECTED"


class McpServerDisabledError(McpControlError):
    code = "MCP_SERVER_DISABLED"


class McpToolRejectedError(McpControlError):
    code = "MCP_TOOL_REJECTED"


class McpAuditRejectedError(McpControlError):
    code = "MCP_AUDIT_REJECTED"


class McpControlPlane:
    def __init__(
        self,
        database: Database,
        *,
        host: McpStdioHost | None = None,
        telemetry: TelemetryFacade | None = None,
    ) -> None:
        self._database = database
        self._telemetry = telemetry
        script = Path(__file__).parents[1] / "mcp_servers" / "readonly_text_server.py"
        self._host = host or McpStdioHost(script)
        self._lock = asyncio.Lock()
        self._tool = McpToolRead(
            name=TOOL_NAME,
            title="本地文本指标",
            description="在短生命周期本地进程中计算文本长度、行数、词数和 SHA-256。",
            risk_floor="R0",
            input_schema=INPUT_SCHEMA,
            output_schema=OUTPUT_SCHEMA,
            schema_digest=sha256_digest(
                {"input_schema": INPUT_SCHEMA, "output_schema": OUTPUT_SCHEMA}
            ),
        )
        self._manifest_digest = sha256_digest(
            {
                "server_id": SERVER_ID,
                "transport": "stdio",
                "protocol_version": PROTOCOL_VERSION,
                "command_preview": COMMAND_PREVIEW,
                "network_access": False,
                "filesystem_roots": [],
                "client_capabilities": [],
                "bundle_digest": self._host.bundle_digest,
                "tools": [self._tool.model_dump(mode="json")],
            }
        )

    async def list_servers(self) -> tuple[McpServerRead, ...]:
        async with self._database.session() as session:
            state = await session.get(McpServerStateRecord, SERVER_ID)
            return (self._server_read(state),)

    async def set_enabled(self, enabled: bool) -> McpServerMutationRead:
        async with self._lock:
            async with self._database.session() as session:
                async with session.begin():
                    state = await session.get(McpServerStateRecord, SERVER_ID)
                    if state is None and not enabled:
                        return McpServerMutationRead(
                            server=self._server_read(None),
                            audit_event_id=None,
                        )
                    if (
                        state is not None
                        and state.enabled == enabled
                        and state.manifest_digest == self._manifest_digest
                    ):
                        return McpServerMutationRead(
                            server=self._server_read(state),
                            audit_event_id=None,
                        )
                    now = utc_now()
                    if state is None:
                        state = McpServerStateRecord(
                            server_id=SERVER_ID,
                            manifest_digest=self._manifest_digest,
                            enabled=enabled,
                            revision=1,
                            updated_at=now,
                        )
                        session.add(state)
                    else:
                        state.manifest_digest = self._manifest_digest
                        state.enabled = enabled
                        state.revision += 1
                        state.updated_at = now
                    request_digest = sha256_digest(
                        {
                            "server_id": SERVER_ID,
                            "enabled": enabled,
                            "manifest_digest": self._manifest_digest,
                        }
                    )
                    event = await self._append_audit(
                        session,
                        action="enabled" if enabled else "disabled",
                        request_digest=request_digest,
                        result_digest=sha256_digest(
                            {"enabled": enabled, "revision": state.revision}
                        ),
                        details={"enabled": enabled, "revision": state.revision},
                    )
                    await session.flush()
                    return McpServerMutationRead(
                        server=self._server_read(state),
                        audit_event_id=event.event_id,
                    )

    async def invoke(self, tool_name: str, arguments: dict[str, Any]) -> McpToolCallRead:
        if self._telemetry is None:
            return await self._invoke(tool_name, arguments)
        with self._telemetry.operation(
            "deskpilot.mcp.request",
            "mcp",
            {
                "deskpilot.mcp.operation": "call_tool",
                "deskpilot.tool.class": "readonly_text_metrics",
                "deskpilot.tool.risk": "R0",
            },
        ) as operation:
            result = await self._invoke(tool_name, arguments)
            operation.set_outcome("succeeded")
            return result

    async def _invoke(self, tool_name: str, arguments: dict[str, Any]) -> McpToolCallRead:
        request_digest = sha256_digest(
            {"server_id": SERVER_ID, "tool_name": tool_name, "arguments": arguments}
        )
        try:
            validated_input = self._validate_input(tool_name, arguments)
            await self._require_enabled()
            result = await self._host.invoke(
                tool_name,
                validated_input.model_dump(mode="json"),
            )
            if sha256_digest({"tools": list(result.tools)}) != sha256_digest({"tools": [TOOL]}):
                raise McpToolRejectedError("MCP tool list does not match the trusted manifest")
            validated_output = McpTextMetricsOutput.model_validate(result.structured_content)
        except (McpControlError, McpStdioError, ValueError) as error:
            error_code = getattr(error, "code", "MCP_OUTPUT_SCHEMA_REJECTED")
            await self._audit_call(
                "tool_failed",
                request_digest,
                sha256_digest({"status": "failed", "error_code": error_code}),
                {"tool_name": tool_name, "status": "failed", "error_code": error_code},
            )
            if isinstance(error, McpControlError | McpStdioError):
                raise
            raise McpToolRejectedError("MCP output failed the trusted schema") from error
        content = validated_output.model_dump(mode="json")
        result_digest = sha256_digest(
            {
                "server_id": SERVER_ID,
                "tool_name": tool_name,
                "protocol_version": result.protocol_version,
                "structured_content": content,
            }
        )
        event = await self._audit_call(
            "tool_called",
            request_digest,
            result_digest,
            {"tool_name": tool_name, "status": "succeeded", "risk_floor": "R0"},
        )
        return McpToolCallRead(
            server_id=SERVER_ID,
            tool_name=tool_name,
            protocol_version=result.protocol_version,
            structured_content=content,
            request_digest=request_digest,
            result_digest=result_digest,
            audit_event_id=event.event_id,
        )

    async def list_audit(self, after_sequence: int, limit: int) -> McpAuditPage:
        async with self._database.session() as session:
            records = (
                (
                    await session.execute(
                        select(McpAuditEventRecord).order_by(McpAuditEventRecord.sequence)
                    )
                )
                .scalars()
                .all()
            )
            state = await session.get(McpAuditStateRecord, "mcp")
        if state is None:
            raise McpAuditRejectedError("MCP audit state is missing")
        previous: str | None = None
        for expected_sequence, record in enumerate(records, start=1):
            if record.sequence != expected_sequence:
                raise McpAuditRejectedError("MCP audit sequence is not continuous")
            expected = self._event_digest(
                record.sequence,
                record.event_id,
                record.server_id,
                record.action,
                record.request_digest,
                record.result_digest,
                previous,
                record.details,
                record.occurred_at,
            )
            if record.previous_event_digest != previous or record.event_digest != expected:
                raise McpAuditRejectedError("MCP audit chain is invalid")
            previous = record.event_digest
        if state.next_sequence != len(records) + 1 or state.last_event_digest != previous:
            raise McpAuditRejectedError("MCP audit head does not match the event chain")
        page_records = [record for record in records if record.sequence > after_sequence][:limit]
        return McpAuditPage(
            events=tuple(self._audit_read(record) for record in page_records),
            next_after_sequence=page_records[-1].sequence if page_records else after_sequence,
        )

    def _validate_input(self, tool_name: str, arguments: dict[str, Any]) -> McpTextMetricsInput:
        if tool_name != TOOL_NAME:
            raise McpToolRejectedError("MCP tool is outside the trusted manifest")
        try:
            return McpTextMetricsInput.model_validate(arguments)
        except ValueError as error:
            raise McpToolRejectedError("MCP arguments failed the trusted schema") from error

    async def _require_enabled(self) -> None:
        async with self._database.session() as session:
            state = await session.get(McpServerStateRecord, SERVER_ID)
            if state is None or not state.enabled or state.manifest_digest != self._manifest_digest:
                raise McpServerDisabledError("MCP server is disabled or its manifest changed")

    async def _audit_call(
        self,
        action: Literal["tool_called", "tool_failed"],
        request_digest: str,
        result_digest: str,
        details: dict[str, Any],
    ) -> McpAuditEventRead:
        async with self._lock:
            async with self._database.session() as session:
                async with session.begin():
                    record = await self._append_audit(
                        session,
                        action=action,
                        request_digest=request_digest,
                        result_digest=result_digest,
                        details=details,
                    )
                return self._audit_read(record)

    async def _append_audit(
        self,
        session: AsyncSession,
        *,
        action: Literal["enabled", "disabled", "tool_called", "tool_failed"],
        request_digest: str,
        result_digest: str,
        details: dict[str, Any],
    ) -> McpAuditEventRecord:
        state = await session.scalar(
            select(McpAuditStateRecord)
            .where(McpAuditStateRecord.state_id == "mcp")
            .with_for_update()
        )
        if state is None:
            raise McpAuditRejectedError("MCP audit state is missing")
        last_record = await session.scalar(
            select(McpAuditEventRecord).order_by(McpAuditEventRecord.sequence.desc()).limit(1)
        )
        expected_sequence = 1 if last_record is None else last_record.sequence + 1
        expected_digest = None if last_record is None else last_record.event_digest
        if state.next_sequence != expected_sequence or state.last_event_digest != expected_digest:
            raise McpAuditRejectedError("MCP audit head is inconsistent")
        sequence = state.next_sequence
        event_id = f"mca_{uuid4().hex}"
        occurred_at = utc_now()
        event_digest = self._event_digest(
            sequence,
            event_id,
            SERVER_ID,
            action,
            request_digest,
            result_digest,
            state.last_event_digest,
            details,
            occurred_at,
        )
        record = McpAuditEventRecord(
            sequence=sequence,
            event_id=event_id,
            server_id=SERVER_ID,
            action=action,
            request_digest=request_digest,
            result_digest=result_digest,
            previous_event_digest=state.last_event_digest,
            event_digest=event_digest,
            details=details,
            occurred_at=occurred_at,
        )
        session.add(record)
        state.next_sequence += 1
        state.last_event_digest = event_digest
        await session.flush()
        return record

    @staticmethod
    def _event_digest(
        sequence: int,
        event_id: str,
        server_id: str,
        action: str,
        request_digest: str,
        result_digest: str,
        previous_event_digest: str | None,
        details: dict[str, Any],
        occurred_at: Any,
    ) -> str:
        timestamp = occurred_at
        if getattr(timestamp, "tzinfo", None) is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        return sha256_digest(
            {
                "sequence": sequence,
                "event_id": event_id,
                "server_id": server_id,
                "action": action,
                "request_digest": request_digest,
                "result_digest": result_digest,
                "previous_event_digest": previous_event_digest,
                "details": details,
                "occurred_at": timestamp.isoformat(),
            }
        )

    def _server_read(self, state: McpServerStateRecord | None) -> McpServerRead:
        manifest_current = state is not None and state.manifest_digest == self._manifest_digest
        updated_at = None
        if state is not None:
            updated_at = (
                state.updated_at.replace(tzinfo=UTC)
                if state.updated_at.tzinfo is None
                else state.updated_at
            )
        return McpServerRead(
            server_id=SERVER_ID,
            title=SERVER_TITLE,
            protocol_version=PROTOCOL_VERSION,
            command_preview=COMMAND_PREVIEW,
            enabled=bool(state is not None and state.enabled and manifest_current),
            revision=state.revision if state is not None else 0,
            network_access=False,
            filesystem_roots=(),
            client_capabilities=(),
            tools=(self._tool,),
            bundle_digest=self._host.bundle_digest,
            manifest_digest=self._manifest_digest,
            updated_at=updated_at,
        )

    @staticmethod
    def _audit_read(record: McpAuditEventRecord) -> McpAuditEventRead:
        occurred_at = (
            record.occurred_at.replace(tzinfo=UTC)
            if record.occurred_at.tzinfo is None
            else record.occurred_at
        )
        return McpAuditEventRead(
            event_id=record.event_id,
            sequence=record.sequence,
            server_id=record.server_id,
            action=cast(Any, record.action),
            request_digest=record.request_digest,
            result_digest=record.result_digest,
            previous_event_digest=record.previous_event_digest,
            event_digest=record.event_digest,
            details=record.details,
            occurred_at=occurred_at,
        )
