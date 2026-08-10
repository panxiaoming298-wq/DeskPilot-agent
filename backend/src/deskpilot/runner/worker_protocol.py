"""Small, unsigned protocol carried only by one invocation's private pipes."""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from deskpilot.domain.tool_contracts import SEMVER_PATTERN, TOOL_NAME_PATTERN
from deskpilot.runner.ipc_protocol import IDENTIFIER_PATTERN, SHA256_PATTERN

WORKER_PROTOCOL_VERSION: Literal["deskpilot.worker.v1"] = "deskpilot.worker.v1"
MAX_WORKER_FRAME_BYTES = 1_048_576


class BrokeredFilesystemMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Literal["runner.filesystem_metadata.v1"] = "runner.filesystem_metadata.v1"
    kind: Literal["filesystem_path"] = "filesystem_path"
    identifier: str = Field(min_length=1, max_length=32_767)
    operations: tuple[Literal["filesystem.metadata.read"], ...] = (
        "filesystem.metadata.read",
    )
    total_bytes: int = Field(ge=0)
    used_bytes: int = Field(ge=0)
    free_bytes: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_capacity(self) -> Self:
        if self.used_bytes + self.free_bytes != self.total_bytes:
            raise ValueError("Brokered filesystem capacity facts are inconsistent")
        return self


class BrokeredFileMove(BaseModel):
    """Immutable source/destination facts prepared by the trusted parent Runner."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Literal["runner.file_move.v1"] = "runner.file_move.v1"
    kind: Literal["filesystem_move"] = "filesystem_move"
    source_identifier: str = Field(min_length=1, max_length=32_767)
    destination_identifier: str = Field(min_length=1, max_length=32_767)
    operations: tuple[
        Literal[
            "filesystem.file.move_destination",
            "filesystem.file.move_source",
        ],
        ...,
    ] = (
        "filesystem.file.move_destination",
        "filesystem.file.move_source",
    )
    source_version: str = Field(pattern=SHA256_PATTERN)
    destination_version: Literal["absent"] = "absent"

    @model_validator(mode="after")
    def validate_distinct_paths(self) -> Self:
        if self.source_identifier == self.destination_identifier:
            raise ValueError("file.move source and destination must be different")
        if self.operations != (
            "filesystem.file.move_destination",
            "filesystem.file.move_source",
        ):
            raise ValueError("Brokered file.move operations are invalid")
        return self


BrokeredResource = Annotated[
    BrokeredFilesystemMetadata | BrokeredFileMove,
    Field(discriminator="provider"),
]


class WorkerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal["deskpilot.worker.v1"] = WORKER_PROTOCOL_VERSION
    call_id: str = Field(pattern=IDENTIFIER_PATTERN)
    tool_name: str = Field(pattern=TOOL_NAME_PATTERN)
    tool_version: str = Field(pattern=SEMVER_PATTERN)
    contract_digest: str = Field(pattern=SHA256_PATTERN)
    arguments: dict[str, JsonValue]
    resources: tuple[BrokeredResource, ...] = ()


class WorkerError(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,99}$")
    message: str = Field(min_length=1, max_length=1_000)


class WorkerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal["deskpilot.worker.v1"] = WORKER_PROTOCOL_VERSION
    call_id: str = Field(pattern=IDENTIFIER_PATTERN)
    status: Literal["succeeded", "failed"]
    output: dict[str, JsonValue] | None = None
    error: WorkerError | None = None

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.status == "succeeded" and (self.output is None or self.error is not None):
            raise ValueError("a succeeded worker response requires output and forbids error")
        if self.status == "failed" and (self.error is None or self.output is not None):
            raise ValueError("a failed worker response requires error and forbids output")
        return self
