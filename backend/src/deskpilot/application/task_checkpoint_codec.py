"""Encode trusted task runtime checkpoints into current-user protected payloads."""

from dataclasses import dataclass

from pydantic import ValidationError

from deskpilot.application.provider_runtime_store import RuntimeConfigProtector
from deskpilot.core.canonical_json import canonical_json_bytes
from deskpilot.domain.task_checkpoints import TaskCheckpointPayload

_MAX_PLAINTEXT_BYTES = 512 * 1024


class TaskCheckpointInvalidError(RuntimeError):
    code = "TASK_CHECKPOINT_INVALID"


@dataclass(frozen=True, slots=True)
class ProtectedTaskCheckpoint:
    scheme: str
    payload: bytes


class TaskCheckpointCodec:
    def __init__(self, protector: RuntimeConfigProtector) -> None:
        self._protector = protector

    def encode(self, checkpoint: TaskCheckpointPayload) -> ProtectedTaskCheckpoint:
        plaintext = bytearray(canonical_json_bytes(checkpoint))
        try:
            self._validate_size(plaintext)
            return ProtectedTaskCheckpoint(
                scheme=self._protector.scheme,
                payload=self._protector.protect(
                    plaintext,
                    context=self._context(checkpoint.task_id),
                ),
            )
        finally:
            self._zero(plaintext)

    def decode(
        self,
        *,
        task_id: str,
        scheme: str,
        payload: bytes,
    ) -> TaskCheckpointPayload:
        if scheme != self._protector.scheme:
            raise TaskCheckpointInvalidError(
                "Task checkpoint protection scheme is unsupported"
            )
        plaintext = self._protector.unprotect(
            payload,
            context=self._context(task_id),
        )
        try:
            self._validate_size(plaintext)
            try:
                checkpoint = TaskCheckpointPayload.model_validate_json(plaintext)
            except ValidationError as error:
                raise TaskCheckpointInvalidError(
                    "Task checkpoint payload is invalid"
                ) from error
            if checkpoint.task_id != task_id:
                raise TaskCheckpointInvalidError(
                    "Task checkpoint record identity does not match"
                )
            return checkpoint
        finally:
            self._zero(plaintext)

    @staticmethod
    def _validate_size(payload: bytearray) -> None:
        if not payload or len(payload) > _MAX_PLAINTEXT_BYTES:
            raise TaskCheckpointInvalidError("Task checkpoint has an invalid size")

    @staticmethod
    def _context(task_id: str) -> str:
        return f"DeskPilot/TaskCheckpoint/{task_id}/v1"

    @staticmethod
    def _zero(buffer: bytearray) -> None:
        buffer[:] = b"\x00" * len(buffer)
