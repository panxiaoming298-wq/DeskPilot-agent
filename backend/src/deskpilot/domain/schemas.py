"""Public domain schemas used by the API and application layer."""

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deskpilot.domain.model_contracts import PrivacyMode


class TaskStatus(StrEnum):
    CREATED = "created"
    CLASSIFYING = "classifying"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_RECONCILIATION = "waiting_reconciliation"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PAUSED = "paused"

    @property
    def is_terminal(self) -> bool:
        return self in {self.SUCCEEDED, self.FAILED, self.CANCELLED}


class FileMoveTaskRequest(BaseModel):
    """Exact user-selected inputs for the only writable task entry point."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["file_move"] = "file_move"
    source: str = Field(min_length=1, max_length=32_767)
    destination: str = Field(min_length=1, max_length=32_767)

    @model_validator(mode="after")
    def validate_distinct_text(self) -> "FileMoveTaskRequest":
        if self.source == self.destination:
            raise ValueError("file_move source and destination must differ")
        return self


class FileMoveSagaOperation(BaseModel):
    """One explicit, user-selected operation in a bounded file-move saga."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    source: str = Field(min_length=1, max_length=32_767)
    destination: str = Field(min_length=1, max_length=32_767)

    @model_validator(mode="after")
    def validate_distinct_text(self) -> "FileMoveSagaOperation":
        if self.source == self.destination:
            raise ValueError("file_move saga source and destination must differ")
        return self


class FileMoveSagaRequest(BaseModel):
    """Bounded ordered moves whose committed effects form one local saga."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["file_move_saga"] = "file_move_saga"
    operations: tuple[FileMoveSagaOperation, ...] = Field(min_length=2, max_length=10)

    @model_validator(mode="after")
    def validate_operation_identities(self) -> "FileMoveSagaRequest":
        operation_ids = [operation.operation_id for operation in self.operations]
        if len(operation_ids) != len(set(operation_ids)):
            raise ValueError("file_move saga operation_id values must be unique")
        return self


class FileMoveDagOperation(FileMoveSagaOperation):
    """One explicit operation in a trusted, dependency-ordered file-move DAG."""

    depends_on: tuple[str, ...] = Field(default=(), max_length=9)

    @model_validator(mode="after")
    def validate_no_self_dependency(self) -> "FileMoveDagOperation":
        if self.operation_id in self.depends_on:
            raise ValueError("file_move DAG operation cannot depend on itself")
        if len(self.depends_on) != len(set(self.depends_on)):
            raise ValueError("file_move DAG dependencies must be unique")
        return self


class FileMoveDagRequest(BaseModel):
    """Trusted v2 plan whose paths/dependencies come from a protected request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["file_move_dag"] = "file_move_dag"
    operations: tuple[FileMoveDagOperation, ...] = Field(min_length=2, max_length=10)

    @model_validator(mode="after")
    def validate_topological_order(self) -> "FileMoveDagRequest":
        operation_ids = [operation.operation_id for operation in self.operations]
        if len(operation_ids) != len(set(operation_ids)):
            raise ValueError("file_move DAG operation_id values must be unique")
        known: set[str] = set()
        for operation in self.operations:
            if any(dependency not in known for dependency in operation.depends_on):
                raise ValueError("file_move DAG dependencies must reference an earlier operation")
            known.add(operation.operation_id)
        return self


class DiskPressureGuardedFileMoveRequest(BaseModel):
    """Move one file only when the destination disk is below a trusted threshold."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["disk_pressure_guarded_file_move"] = "disk_pressure_guarded_file_move"
    source: str = Field(min_length=1, max_length=32_767)
    destination: str = Field(min_length=1, max_length=32_767)
    maximum_used_percent: float = Field(ge=0, le=100)

    @model_validator(mode="after")
    def validate_distinct_text(self) -> "DiskPressureGuardedFileMoveRequest":
        if self.source == self.destination:
            raise ValueError("guarded file_move source and destination must differ")
        return self


class FileMoveCompensationRequest(BaseModel):
    """Trusted reverse move derived only from an original approval and receipt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["file_move_compensation"] = "file_move_compensation"
    source: str = Field(min_length=1, max_length=32_767)
    destination: str = Field(min_length=1, max_length=32_767)
    reconciliation_id: str = Field(min_length=1, max_length=140)
    original_task_id: str = Field(min_length=1, max_length=40)
    original_call_id: str = Field(min_length=1, max_length=128)
    receipt_id: str = Field(pattern=r"^cmt_[0-9a-f]{64}$")
    expected_source_version: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_distinct_text(self) -> "FileMoveCompensationRequest":
        if self.source == self.destination:
            raise ValueError("file_move compensation source and destination must differ")
        return self


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: str | None = None
    goal: str = Field(min_length=1, max_length=4_000)
    privacy_mode: PrivacyMode = "local_preferred"
    constraints: list[str] = Field(default_factory=list, max_length=50)
    tool_request: (
        FileMoveTaskRequest
        | FileMoveSagaRequest
        | FileMoveDagRequest
        | DiskPressureGuardedFileMoveRequest
        | None
    ) = None


class TaskControlCommand(BaseModel):
    reason: str | None = Field(default=None, min_length=1, max_length=500)


class TaskRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    task_id: str
    conversation_id: str | None
    goal: str
    status: TaskStatus
    mode: str
    privacy_mode: PrivacyMode
    constraints: list[str]
    last_event_seq: int
    event_stream: str
    created_at: datetime
    updated_at: datetime


class TaskHistoryRead(BaseModel):
    """Bounded newest-first task history page for the local control center."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    items: tuple[TaskRead, ...]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)


class TaskEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    event_id: str
    task_id: str
    seq: int
    type: str
    timestamp: datetime
    trace_id: str
    payload: dict[str, Any]


class SessionBootstrapRead(BaseModel):
    access_token: str
    token_type: Literal["Bearer"] = "Bearer"
    websocket_protocol: str
