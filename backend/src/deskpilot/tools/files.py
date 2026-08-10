"""Brokered, reversible local file operations."""

import hashlib
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from threading import Event

from pydantic import BaseModel, ConfigDict, Field

from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.policy import PolicyResource
from deskpilot.domain.tool_commit import ToolCommitReceipt
from deskpilot.domain.tool_contracts import (
    ToolCommitProtocol,
    ToolContract,
    ToolExecutionContract,
    ToolIdempotency,
    ToolRiskLevel,
    ToolSecurityContract,
)
from deskpilot.runner.authorization import AuthorizedToolCall
from deskpilot.runner.commit_receipts import CommitReceiptStore, PreparedCommitRecord
from deskpilot.runner.controlled_commit import ControlledCommitBoundary
from deskpilot.runner.executor import (
    ToolExecutionCancelledError,
    ToolExecutionContext,
    ToolExecutorError,
)
from deskpilot.runner.worker_protocol import BrokeredFileMove, BrokeredResource

FILE_MOVE_SOURCE_CAPABILITY = "filesystem.file.move_source"
FILE_MOVE_DESTINATION_CAPABILITY = "filesystem.file.move_destination"
FILE_MOVE_EXPECTED_VERSION_KEYS = frozenset({"destination", "source"})


class FileMoveInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str = Field(min_length=1, max_length=32_767)
    destination: str = Field(min_length=1, max_length=32_767)


class FileMovePrepare(BaseModel):
    """Side-effect-free worker proposal consumed only by the parent Runner."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: str = Field(default="file.move", pattern=r"^file\.move$")
    source: str = Field(min_length=1, max_length=32_767)
    destination: str = Field(min_length=1, max_length=32_767)
    source_version: str = Field(pattern=r"^[0-9a-f]{64}$")
    destination_version: str = Field(default="absent", pattern=r"^absent$")


class FileMoveOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str
    destination: str
    source_version_before: str = Field(pattern=r"^[0-9a-f]{64}$")
    destination_version_after: str = Field(pattern=r"^[0-9a-f]{64}$")
    reversible: bool = True
    commit_receipt: ToolCommitReceipt


FILE_MOVE_CONTRACT = ToolContract.from_models(
    name="file.move",
    version="1.0.0",
    description="Move one existing regular file to a new path without overwriting.",
    input_model=FileMoveInput,
    output_model=FileMoveOutput,
    risk_level=ToolRiskLevel.R1,
    side_effects=("filesystem_write",),
    reversible=True,
    execution=ToolExecutionContract(
        timeout_seconds=30,
        idempotency=ToolIdempotency.KEY_REQUIRED,
        max_output_bytes=32_768,
        resource_locks=("file:{source}", "dir:{destination_parent}"),
        commit_protocol=ToolCommitProtocol.BROKERED,
    ),
    security=ToolSecurityContract(
        capabilities=(
            FILE_MOVE_DESTINATION_CAPABILITY,
            FILE_MOVE_SOURCE_CAPABILITY,
        ),
        supports_dry_run=True,
    ),
)


class FileMoveValidationError(ToolExecutorError):
    code = "TOOL_FILE_MOVE_INVALID"


class FileMoveStaleResourceError(ToolExecutorError):
    code = "TOOL_RESOURCE_VERSION_MISMATCH"


class FileMoveDestinationConflictError(ToolExecutorError):
    code = "TOOL_FILE_MOVE_DESTINATION_EXISTS"


class FileMoveCommitUnknownError(ToolExecutorError):
    code = "TOOL_COMMIT_OUTCOME_UNKNOWN"


class FileMoveNoEffectError(ToolExecutorError):
    code = "TOOL_COMMIT_CONFIRMED_NO_EFFECT"


def _canonical_source(path: str) -> Path:
    candidate = Path(path).expanduser()
    try:
        original_stat = candidate.lstat()
    except OSError as error:
        raise FileMoveValidationError("file.move source is unavailable") from error
    if stat.S_ISLNK(original_stat.st_mode):
        raise FileMoveValidationError("file.move does not accept a symbolic-link source")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_file():
        raise FileMoveValidationError("file.move source must be a regular file")
    return resolved


def _canonical_destination(path: str) -> Path:
    candidate = Path(path).expanduser()
    if candidate.name in {"", ".", ".."}:
        raise FileMoveValidationError("file.move destination must name a file")
    try:
        parent = candidate.parent.resolve(strict=True)
    except OSError as error:
        raise FileMoveValidationError("file.move destination parent is unavailable") from error
    if not parent.is_dir():
        raise FileMoveValidationError("file.move destination parent must be a directory")
    resolved = parent / candidate.name
    if resolved.exists() or resolved.is_symlink():
        raise FileMoveDestinationConflictError("file.move destination already exists")
    return resolved


def _same_volume(source: Path, destination: Path) -> bool:
    if os.name == "nt":
        return os.path.splitdrive(str(source))[0].casefold() == os.path.splitdrive(
            str(destination)
        )[0].casefold()
    return source.stat().st_dev == destination.parent.stat().st_dev


def read_file_version(path: str | Path) -> str:
    """Hash stable file identity, metadata, and contents through one descriptor."""
    resolved = _canonical_source(str(path))
    descriptor = os.open(resolved, os.O_RDONLY)
    try:
        before = os.fstat(descriptor)
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1_048_576)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity_before != identity_after:
        raise FileMoveStaleResourceError("file.move source changed during versioning")
    return sha256_digest(
        {
            "content_sha256": digest.hexdigest(),
            "device": before.st_dev,
            "file_id": before.st_ino,
            "modified_ns": before.st_mtime_ns,
            "size": before.st_size,
        }
    )


def expected_file_move_versions(arguments: FileMoveInput) -> dict[str, str]:
    return {
        "destination": "absent",
        "source": read_file_version(arguments.source),
    }


def normalize_file_move_input(arguments: FileMoveInput) -> FileMoveInput:
    """Resolve one explicit request without calculating its approval version yet."""
    source = _canonical_source(arguments.source)
    destination = _canonical_destination(arguments.destination)
    if source == destination:
        raise FileMoveValidationError("file.move source and destination must differ")
    if not _same_volume(source, destination):
        raise FileMoveValidationError("file.move requires a same-volume destination")
    return FileMoveInput(source=str(source), destination=str(destination))


def project_file_move_resources(arguments: BaseModel) -> tuple[PolicyResource, ...]:
    if not isinstance(arguments, FileMoveInput):
        raise TypeError("file.move received an unexpected input model")
    normalized = normalize_file_move_input(arguments)
    source = Path(normalized.source)
    destination = Path(normalized.destination)
    source_version = read_file_version(source)
    return (
        PolicyResource(
            kind="filesystem_path",
            identifier=str(destination),
            operations=(FILE_MOVE_DESTINATION_CAPABILITY,),
            display_name=str(destination),
        ),
        PolicyResource(
            kind="filesystem_path",
            identifier=str(source),
            operations=(FILE_MOVE_SOURCE_CAPABILITY,),
            version_digest=source_version,
            display_name=str(source),
        ),
    )


def prepare_file_move(
    arguments: BaseModel,
    cancellation: Event,
    context: ToolExecutionContext,
) -> BaseModel:
    if not isinstance(arguments, FileMoveInput):
        raise TypeError("file.move received an unexpected input model")
    if cancellation.is_set():
        raise ToolExecutionCancelledError("file.move was cancelled before prepare")
    facts = context.require_file_move()
    return FileMovePrepare(
        source=facts.source_identifier,
        destination=facts.destination_identifier,
        source_version=facts.source_version,
        destination_version=facts.destination_version,
    )


def _move_no_replace(source: Path, destination: Path) -> None:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        move_file = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
        move_file.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        move_file.restype = wintypes.BOOL
        movefile_write_through = 0x00000008
        if not move_file(str(source), str(destination), movefile_write_through):
            error = ctypes.get_last_error()
            if error in {80, 183}:
                raise FileMoveDestinationConflictError(
                    "file.move destination appeared before commit"
                )
            raise OSError(error, "MoveFileExW failed")
        return
    if destination.exists() or destination.is_symlink():
        raise FileMoveDestinationConflictError("file.move destination appeared before commit")
    os.rename(source, destination)


class FileMoveCommitProvider:
    """Trusted parent-side provider for exactly one no-overwrite file move."""

    contract_key = FILE_MOVE_CONTRACT.key

    def recover(self, store: CommitReceiptStore) -> None:
        for record in store.list_incomplete(*self.contract_key):
            self._recover_record(store, record)

    def commit(
        self,
        call: AuthorizedToolCall,
        prepared: BaseModel,
        resources: tuple[BrokeredResource, ...],
        cancellation: Event,
        boundary: ControlledCommitBoundary,
        store: CommitReceiptStore,
    ) -> BaseModel:
        if not isinstance(prepared, FileMovePrepare):
            raise TypeError("file.move commit provider received an invalid prepare")
        facts = self._require_facts(resources)
        self._validate_prepare(call, prepared, facts)
        authorization = call.request.authorization
        if (
            authorization.approval_id is None
            or authorization.preview_hash is None
            or call.request.idempotency_key is None
        ):
            raise FileMoveValidationError(
                "file.move requires an exact approval preview and idempotency key"
            )
        prepare_digest = sha256_digest(
            {
                "authorization_id": authorization.authorization_id,
                "expected_resource_versions": call.request.expected_resource_versions,
                "prepare": prepared.model_dump(mode="json"),
                "preview_hash": authorization.preview_hash,
            }
        )
        idempotency_key_digest = hashlib.sha256(
            call.request.idempotency_key.encode("utf-8")
        ).hexdigest()
        receipt_id = f"cmt_{sha256_digest(
            {'call_id': call.request.call_id, 'prepare': prepare_digest}
        )}"
        now = datetime.now(UTC)
        staged = PreparedCommitRecord(
            receipt_id=receipt_id,
            call_id=call.request.call_id,
            tool_name=FILE_MOVE_CONTRACT.name,
            tool_version=FILE_MOVE_CONTRACT.version,
            authorization_id=authorization.authorization_id,
            approval_id=authorization.approval_id,
            preview_hash=authorization.preview_hash,
            prepare_digest=prepare_digest,
            idempotency_key_digest=idempotency_key_digest,
            binding_digest=sha256_digest(
                {
                    "arguments_digest": authorization.arguments_digest,
                    "authorization_id": authorization.authorization_id,
                    "idempotency_key_digest": idempotency_key_digest,
                    "prepare_digest": prepare_digest,
                }
            ),
            state="prepared",
            prepared_payload=prepared.model_dump(mode="json"),
            commit_started_at=None,
            receipt=None,
            created_at=now,
            updated_at=now,
        )
        existing = store.stage(staged)
        if existing.state == "committed" and existing.receipt is not None:
            output = self._output(prepared, existing.receipt)
            boundary.mark_committing()
            boundary.mark_committed(output, existing.receipt)
            return output
        if existing.state == "no_effect":
            boundary.mark_no_effect()
            raise FileMoveNoEffectError(
                "A previous file.move attempt is proven to have produced no effect"
            )
        if existing.state == "unknown":
            boundary.mark_unknown()
            raise FileMoveCommitUnknownError(
                "A previous file.move attempt has an unknown external outcome"
            )
        if existing.state == "committing":
            recovered = self._recover_record(store, existing)
            if recovered.state == "committed" and recovered.receipt is not None:
                output = self._output(prepared, recovered.receipt)
                boundary.mark_committing()
                boundary.mark_committed(output, recovered.receipt)
                return output
            if recovered.state == "no_effect":
                boundary.mark_no_effect()
                raise FileMoveNoEffectError(
                    "The earlier file.move commit is proven to have produced no effect"
                )
            boundary.mark_unknown()
            raise FileMoveCommitUnknownError(
                "The earlier file.move commit could not be reconciled"
            )

        if cancellation.is_set():
            store.mark_no_effect(receipt_id)
            boundary.mark_no_effect()
            raise ToolExecutionCancelledError("file.move was cancelled before commit")

        try:
            self._validate_external_state(prepared)
        except Exception:
            store.mark_no_effect(receipt_id)
            boundary.mark_no_effect()
            raise
        if not boundary.try_mark_committing(cancellation):
            store.mark_no_effect(receipt_id)
            raise ToolExecutionCancelledError("file.move was cancelled before commit")
        commit_started_at = datetime.now(UTC)
        try:
            store.mark_committing(receipt_id, commit_started_at=commit_started_at)
        except Exception:
            boundary.mark_no_effect()
            raise
        try:
            _move_no_replace(Path(prepared.source), Path(prepared.destination))
        except Exception as error:
            recovered = self._recover_record(
                store,
                store.get_for_call(call.request.call_id) or staged,
            )
            if recovered.state == "committed" and recovered.receipt is not None:
                output = self._output(prepared, recovered.receipt)
                boundary.mark_committed(output, recovered.receipt)
                return output
            if recovered.state == "no_effect":
                boundary.mark_no_effect()
                raise
            boundary.mark_unknown()
            raise FileMoveCommitUnknownError(
                "file.move failed after crossing the commit boundary"
            ) from error

        destination_version = read_file_version(prepared.destination)
        if destination_version != prepared.source_version:
            store.mark_unknown(receipt_id)
            boundary.mark_unknown()
            raise FileMoveCommitUnknownError(
                "file.move destination version does not match the prepared source"
            )
        receipt = self._receipt(
            staged,
            commit_started_at=commit_started_at,
            recorded_at=datetime.now(UTC),
        )
        store.mark_committed(receipt)
        output = self._output(prepared, receipt)
        boundary.mark_committed(output, receipt)
        return output

    @staticmethod
    def _require_facts(resources: tuple[BrokeredResource, ...]) -> BrokeredFileMove:
        if len(resources) != 1 or not isinstance(resources[0], BrokeredFileMove):
            raise FileMoveValidationError(
                "file.move requires one brokered source/destination fact set"
            )
        return resources[0]

    @staticmethod
    def _validate_prepare(
        call: AuthorizedToolCall,
        prepared: FileMovePrepare,
        facts: BrokeredFileMove,
    ) -> None:
        expected_versions = call.request.expected_resource_versions
        if set(expected_versions) != FILE_MOVE_EXPECTED_VERSION_KEYS:
            raise FileMoveValidationError("file.move expected versions have an invalid shape")
        expected_prepare = FileMovePrepare(
            source=facts.source_identifier,
            destination=facts.destination_identifier,
            source_version=facts.source_version,
            destination_version=facts.destination_version,
        )
        if prepared != expected_prepare:
            raise FileMoveValidationError(
                "file.move worker prepare does not match brokered resource facts"
            )
        if expected_versions != {
            "destination": facts.destination_version,
            "source": facts.source_version,
        }:
            raise FileMoveStaleResourceError(
                "file.move actual resource versions do not match the approved preview"
            )

    @staticmethod
    def _validate_external_state(prepared: FileMovePrepare) -> None:
        source = _canonical_source(prepared.source)
        destination = _canonical_destination(prepared.destination)
        if source != Path(prepared.source) or destination != Path(prepared.destination):
            raise FileMoveValidationError("file.move canonical paths changed after prepare")
        if read_file_version(source) != prepared.source_version:
            raise FileMoveStaleResourceError(
                "file.move source changed after approval"
            )
        if not _same_volume(source, destination):
            raise FileMoveValidationError("file.move destination volume changed")

    def _recover_record(
        self,
        store: CommitReceiptStore,
        record: PreparedCommitRecord,
    ) -> PreparedCommitRecord:
        if record.state == "prepared":
            # The provider persists ``committing`` before invoking the OS move.
            # A merely prepared attempt therefore has a proven no-effect outcome,
            # regardless of unrelated changes to either path after the crash.
            return store.mark_no_effect(record.receipt_id)
        prepared = FileMovePrepare.model_validate(record.prepared_payload)
        source_exists = Path(prepared.source).exists()
        destination_exists = Path(prepared.destination).exists()
        if not source_exists and destination_exists:
            try:
                destination_version = read_file_version(prepared.destination)
            except ToolExecutorError:
                return store.mark_unknown(record.receipt_id)
            if destination_version == prepared.source_version:
                commit_started_at = record.commit_started_at or record.updated_at
                receipt = self._receipt(
                    record,
                    commit_started_at=commit_started_at,
                    recorded_at=datetime.now(UTC),
                )
                return store.mark_recovered_committed(receipt)
            return store.mark_unknown(record.receipt_id)
        if source_exists and not destination_exists:
            try:
                source_version = read_file_version(prepared.source)
            except ToolExecutorError:
                return store.mark_unknown(record.receipt_id)
            if source_version == prepared.source_version:
                return store.mark_no_effect(record.receipt_id)
        return store.mark_unknown(record.receipt_id)

    @staticmethod
    def _receipt(
        record: PreparedCommitRecord,
        *,
        commit_started_at: datetime,
        recorded_at: datetime,
    ) -> ToolCommitReceipt:
        prepared = FileMovePrepare.model_validate(record.prepared_payload)
        return ToolCommitReceipt(
            receipt_id=record.receipt_id,
            call_id=record.call_id,
            tool_name=record.tool_name,
            tool_version=record.tool_version,
            authorization_id=record.authorization_id,
            approval_id=record.approval_id,
            preview_hash=record.preview_hash,
            prepare_digest=record.prepare_digest,
            idempotency_key_digest=record.idempotency_key_digest,
            resource_versions_before={
                "destination": "absent",
                "source": prepared.source_version,
            },
            resource_versions_after={
                "destination": prepared.source_version,
                "source": "absent",
            },
            commit_started_at=commit_started_at,
            receipt_recorded_at=recorded_at,
        )

    @staticmethod
    def _output(
        prepared: FileMovePrepare,
        receipt: ToolCommitReceipt,
    ) -> FileMoveOutput:
        return FileMoveOutput(
            source=prepared.source,
            destination=prepared.destination,
            source_version_before=prepared.source_version,
            destination_version_after=receipt.resource_versions_after["destination"],
            commit_receipt=receipt,
        )


FILE_MOVE_COMMIT_PROVIDER = FileMoveCommitProvider()


__all__ = [
    "FILE_MOVE_COMMIT_PROVIDER",
    "FILE_MOVE_CONTRACT",
    "FileMoveInput",
    "FileMoveOutput",
    "FileMovePrepare",
    "expected_file_move_versions",
    "normalize_file_move_input",
    "prepare_file_move",
    "project_file_move_resources",
    "read_file_version",
]
