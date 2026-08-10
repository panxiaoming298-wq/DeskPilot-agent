"""Versioned, signed messages exchanged with an isolated Tool Runner."""

import base64
import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    model_validator,
)

from deskpilot.core.canonical_json import canonical_json_bytes
from deskpilot.domain.policy import ToolAuthorizationGrant
from deskpilot.domain.tool_commit import ToolCommitReceipt
from deskpilot.domain.tool_contracts import SEMVER_PATTERN, TOOL_NAME_PATTERN

PROTOCOL_VERSION: Literal["deskpilot.runner.v1"] = "deskpilot.runner.v1"
SIGNATURE_ALGORITHM: Literal["HMAC-SHA256"] = "HMAC-SHA256"
DEFAULT_MAX_COMMAND_TTL = timedelta(seconds=60)
DEFAULT_CLOCK_SKEW = timedelta(seconds=5)

IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"
SIGNATURE_PATTERN = r"^[A-Za-z0-9_-]{43}$"
SECRET_PATTERN = r"^[A-Za-z0-9_-]{43,172}$"


def _ensure_timezone_aware(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError("IPC timestamps must be timezone-aware")
    return value


AwareDateTime = Annotated[datetime, AfterValidator(_ensure_timezone_aware)]


class RunnerBootstrap(BaseModel):
    """Unsigned first frame delivered over the inherited private stdin pipe."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    message_type: Literal["runner.bootstrap"] = "runner.bootstrap"
    protocol_version: Literal["deskpilot.runner.v1"] = PROTOCOL_VERSION
    key_id: str = Field(pattern=IDENTIFIER_PATTERN)
    secret: str = Field(pattern=SECRET_PATTERN, repr=False)
    startup_nonce: str = Field(min_length=16, max_length=128)
    heartbeat_interval_seconds: float = Field(default=0.5, ge=0.1, le=60)
    require_windows_sandbox: bool = False
    require_network_isolation: bool = False
    worker_runtime_root: str = Field(
        default="./data/worker-runtime",
        min_length=1,
        max_length=32_767,
    )
    worker_runtime_bundle: str | None = Field(
        default=None,
        min_length=1,
        max_length=32_767,
    )
    appcontainer_profile_journal_path: str = Field(
        default="./data/runner/appcontainer-profiles.json",
        min_length=1,
        max_length=32_767,
    )
    commit_receipt_database_path: str = Field(
        default="./data/runner/commit-receipts.db",
        min_length=1,
        max_length=32_767,
    )
    worker_memory_limit_bytes: int = Field(
        default=268_435_456,
        ge=67_108_864,
        le=2_147_483_648,
    )
    worker_active_process_limit: int = Field(default=1, ge=1, le=16)


class IpcProtocolError(RuntimeError):
    """Stable base error for protocol failures at the process boundary."""

    code = "IPC_PROTOCOL_ERROR"


class InvalidSignatureError(IpcProtocolError):
    code = "IPC_SIGNATURE_INVALID"


class UnknownKeyError(IpcProtocolError):
    code = "IPC_KEY_UNKNOWN"


class StartupNonceMismatchError(IpcProtocolError):
    code = "IPC_STARTUP_NONCE_MISMATCH"


class MessageExpiredError(IpcProtocolError):
    code = "IPC_MESSAGE_EXPIRED"


class MessageIssuedInFutureError(IpcProtocolError):
    code = "IPC_MESSAGE_ISSUED_IN_FUTURE"


class MessageTtlExceededError(IpcProtocolError):
    code = "IPC_MESSAGE_TTL_EXCEEDED"


class ReplayDetectedError(IpcProtocolError):
    code = "IPC_REPLAY_DETECTED"


class UnexpectedMessageError(IpcProtocolError):
    code = "IPC_UNEXPECTED_MESSAGE"


class TimedRunnerCommand(BaseModel):
    """Fields every control-plane command must bind into its signature."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    issued_at: AwareDateTime
    expires_at: AwareDateTime
    nonce: str = Field(min_length=16, max_length=128)
    startup_nonce: str = Field(min_length=16, max_length=128)

    @model_validator(mode="after")
    def validate_time_window(self) -> Self:
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at must be later than issued_at")
        return self


class ToolCallRequest(TimedRunnerCommand):
    message_type: Literal["tool.call"] = "tool.call"
    call_id: str = Field(pattern=IDENTIFIER_PATTERN)
    task_id: str = Field(pattern=IDENTIFIER_PATTERN)
    step_id: str = Field(pattern=IDENTIFIER_PATTERN)
    tool_name: str = Field(pattern=TOOL_NAME_PATTERN)
    tool_version: str = Field(pattern=SEMVER_PATTERN)
    contract_digest: str = Field(pattern=SHA256_PATTERN)
    arguments: dict[str, JsonValue]
    actor: str = Field(min_length=1, max_length=200)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)
    expected_resource_versions: dict[str, str] = Field(default_factory=dict)
    authorization: ToolAuthorizationGrant


class ToolCancelRequest(TimedRunnerCommand):
    message_type: Literal["tool.cancel"] = "tool.cancel"
    call_id: str = Field(pattern=IDENTIFIER_PATTERN)
    reason: str = Field(min_length=1, max_length=500)


class ToolCommitReceiptRequest(TimedRunnerCommand):
    message_type: Literal["tool.commit_receipt.get"] = "tool.commit_receipt.get"
    call_id: str = Field(pattern=IDENTIFIER_PATTERN)


class RunnerHello(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    message_type: Literal["runner.hello"] = "runner.hello"
    runner_id: str = Field(pattern=IDENTIFIER_PATTERN)
    startup_nonce: str = Field(min_length=16, max_length=128)
    supported_protocols: tuple[str, ...]
    isolation_mode: Literal[
        "windows_restricted",
        "windows_appcontainer",
        "process_only",
    ] = "process_only"
    network_isolation_mode: Literal["none", "appcontainer"] = "none"
    per_call_process_isolation: bool = False
    occurred_at: AwareDateTime


class RunnerHeartbeat(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    message_type: Literal["runner.heartbeat"] = "runner.heartbeat"
    runner_id: str = Field(pattern=IDENTIFIER_PATTERN)
    startup_nonce: str = Field(min_length=16, max_length=128)
    occurred_at: AwareDateTime
    active_call_ids: tuple[str, ...] = ()


class ToolProgress(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    message_type: Literal["tool.progress"] = "tool.progress"
    runner_id: str = Field(pattern=IDENTIFIER_PATTERN)
    startup_nonce: str = Field(min_length=16, max_length=128)
    call_id: str = Field(pattern=IDENTIFIER_PATTERN)
    sequence: int = Field(ge=0)
    message: str = Field(min_length=1, max_length=500)
    percent: float | None = Field(default=None, ge=0, le=100)
    occurred_at: AwareDateTime


class ToolError(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,99}$")
    message: str = Field(min_length=1, max_length=1_000)
    retryable: bool = False
    details: dict[str, JsonValue] = Field(default_factory=dict)


class ToolCallResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    message_type: Literal["tool.result"] = "tool.result"
    runner_id: str = Field(pattern=IDENTIFIER_PATTERN)
    startup_nonce: str = Field(min_length=16, max_length=128)
    call_id: str = Field(pattern=IDENTIFIER_PATTERN)
    status: Literal["succeeded", "failed", "cancelled", "unknown"]
    output: dict[str, JsonValue] | None = None
    error: ToolError | None = None
    started_at: AwareDateTime
    finished_at: AwareDateTime

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must not be earlier than started_at")
        if self.status == "succeeded" and (self.output is None or self.error is not None):
            raise ValueError("a succeeded result requires output and forbids error")
        if self.status != "succeeded" and self.error is None:
            raise ValueError("a non-succeeded result requires error")
        if self.status != "succeeded" and self.output is not None:
            raise ValueError("only a succeeded result may contain output")
        return self


class ToolCommitReceiptResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    message_type: Literal[
        "tool.commit_receipt.result"
    ] = "tool.commit_receipt.result"
    runner_id: str = Field(pattern=IDENTIFIER_PATTERN)
    startup_nonce: str = Field(min_length=16, max_length=128)
    call_id: str = Field(pattern=IDENTIFIER_PATTERN)
    receipt: ToolCommitReceipt | None = None
    occurred_at: AwareDateTime


IpcPayload = Annotated[
    ToolCallRequest
    | ToolCancelRequest
    | ToolCommitReceiptRequest
    | RunnerHello
    | RunnerHeartbeat
    | ToolProgress
    | ToolCallResult
    | ToolCommitReceiptResult,
    Field(discriminator="message_type"),
]


class SignedIpcEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal["deskpilot.runner.v1"] = PROTOCOL_VERSION
    key_id: str = Field(pattern=IDENTIFIER_PATTERN)
    algorithm: Literal["HMAC-SHA256"] = SIGNATURE_ALGORITHM
    payload: IpcPayload
    signature: str = Field(pattern=SIGNATURE_PATTERN)


def _signature_content(*, key_id: str, payload: IpcPayload) -> dict[str, JsonValue]:
    return {
        "algorithm": SIGNATURE_ALGORITHM,
        "key_id": key_id,
        "payload": payload.model_dump(mode="json"),
        "protocol_version": PROTOCOL_VERSION,
    }


class IpcSigner:
    """Signs complete semantic messages; the signature field itself is excluded."""

    def __init__(self, *, key_id: str, secret: bytes) -> None:
        if len(secret) < 32:
            raise ValueError("IPC HMAC secret must contain at least 32 bytes")
        self.key_id = key_id
        self._secret = secret

    def signature_for(self, payload: IpcPayload) -> str:
        digest = hmac.new(
            self._secret,
            canonical_json_bytes(_signature_content(key_id=self.key_id, payload=payload)),
            hashlib.sha256,
        ).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    def sign(self, payload: IpcPayload) -> SignedIpcEnvelope:
        return SignedIpcEnvelope(
            key_id=self.key_id,
            payload=payload,
            signature=self.signature_for(payload),
        )


class InMemoryReplayGuard:
    """Atomically consumes command nonces for one Runner process lifetime."""

    def __init__(self) -> None:
        self._expires_by_nonce: dict[str, datetime] = {}
        self._lock = Lock()

    def consume(self, nonce: str, *, expires_at: datetime, now: datetime) -> None:
        with self._lock:
            self._expires_by_nonce = {
                stored_nonce: expiry
                for stored_nonce, expiry in self._expires_by_nonce.items()
                if expiry > now
            }
            if nonce in self._expires_by_nonce:
                raise ReplayDetectedError("IPC command nonce has already been consumed")
            self._expires_by_nonce[nonce] = expires_at


class IpcVerifier:
    """Authenticates an envelope and enforces Runner-session command freshness."""

    def __init__(
        self,
        *,
        key_id: str,
        secret: bytes,
        startup_nonce: str,
        replay_guard: InMemoryReplayGuard | None = None,
        max_command_ttl: timedelta = DEFAULT_MAX_COMMAND_TTL,
        clock_skew: timedelta = DEFAULT_CLOCK_SKEW,
    ) -> None:
        self._signer = IpcSigner(key_id=key_id, secret=secret)
        self._startup_nonce = startup_nonce
        self._replay_guard = replay_guard or InMemoryReplayGuard()
        self._max_command_ttl = max_command_ttl
        self._clock_skew = clock_skew

    def verify(
        self,
        envelope: SignedIpcEnvelope,
        *,
        now: datetime | None = None,
    ) -> IpcPayload:
        if envelope.key_id != self._signer.key_id:
            raise UnknownKeyError("IPC envelope refers to an unknown key")
        expected = self._signer.signature_for(envelope.payload)
        if not hmac.compare_digest(envelope.signature, expected):
            raise InvalidSignatureError("IPC envelope signature is invalid")
        if envelope.payload.startup_nonce != self._startup_nonce:
            raise StartupNonceMismatchError("IPC message belongs to another Runner session")

        if isinstance(envelope.payload, TimedRunnerCommand):
            current_time = now or datetime.now(UTC)
            if envelope.payload.issued_at > current_time + self._clock_skew:
                raise MessageIssuedInFutureError("IPC command was issued too far in the future")
            if envelope.payload.expires_at <= current_time:
                raise MessageExpiredError("IPC command has expired")
            if envelope.payload.expires_at - envelope.payload.issued_at > self._max_command_ttl:
                raise MessageTtlExceededError("IPC command TTL exceeds the configured maximum")
            self._replay_guard.consume(
                envelope.payload.nonce,
                expires_at=envelope.payload.expires_at,
                now=current_time,
            )
        return envelope.payload
