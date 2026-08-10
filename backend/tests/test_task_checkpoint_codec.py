import pytest

from deskpilot.application.task_checkpoint_codec import (
    TaskCheckpointCodec,
    TaskCheckpointInvalidError,
)
from deskpilot.domain.task_checkpoints import (
    TaskCheckpointPayload,
    initial_tool_call_id,
)


class RecordingProtector:
    scheme = "test_xor_v1"

    def __init__(self) -> None:
        self.protect_buffer: bytearray | None = None
        self.unprotect_buffer: bytearray | None = None
        self.contexts: list[str] = []

    def protect(self, plaintext: bytearray, *, context: str) -> bytes:
        self.protect_buffer = plaintext
        self.contexts.append(context)
        return bytes(value ^ 0xA5 for value in plaintext)

    def unprotect(self, payload: bytes, *, context: str) -> bytearray:
        self.contexts.append(context)
        plaintext = bytearray(value ^ 0xA5 for value in payload)
        self.unprotect_buffer = plaintext
        return plaintext


def _checkpoint(task_id: str = "tsk_0123456789abcdef0123456789abcdef") -> TaskCheckpointPayload:
    return TaskCheckpointPayload(
        task_id=task_id,
        next_stage=0,
        tool_call_id=initial_tool_call_id(task_id),
    )


def test_checkpoint_codec_binds_context_and_zeroes_working_buffers() -> None:
    protector = RecordingProtector()
    codec = TaskCheckpointCodec(protector)
    checkpoint = _checkpoint()

    protected = codec.encode(checkpoint)
    decoded = codec.decode(
        task_id=checkpoint.task_id,
        scheme=protected.scheme,
        payload=protected.payload,
    )

    context = f"DeskPilot/TaskCheckpoint/{checkpoint.task_id}/v1"
    assert decoded == checkpoint
    assert protector.contexts == [context, context]
    assert protector.protect_buffer is not None
    assert not any(protector.protect_buffer)
    assert protector.unprotect_buffer is not None
    assert not any(protector.unprotect_buffer)
    assert checkpoint.task_id.encode() not in protected.payload


def test_checkpoint_codec_rejects_mismatched_record_identity() -> None:
    codec = TaskCheckpointCodec(RecordingProtector())
    checkpoint = _checkpoint()
    protected = codec.encode(checkpoint)

    with pytest.raises(TaskCheckpointInvalidError, match="identity"):
        codec.decode(
            task_id="tsk_abcdef0123456789abcdef0123456789",
            scheme=protected.scheme,
            payload=protected.payload,
        )


def test_checkpoint_codec_rejects_unknown_protection_scheme() -> None:
    codec = TaskCheckpointCodec(RecordingProtector())
    checkpoint = _checkpoint()
    protected = codec.encode(checkpoint)

    with pytest.raises(TaskCheckpointInvalidError, match="unsupported"):
        codec.decode(
            task_id=checkpoint.task_id,
            scheme="unknown-protection-v1",
            payload=protected.payload,
        )
