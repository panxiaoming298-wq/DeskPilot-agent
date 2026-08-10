"""Strict one-message-per-line codec for Runner stdin/stdout transport."""

import json
from typing import Any

from pydantic import ValidationError

from deskpilot.core.canonical_json import canonical_json_bytes
from deskpilot.runner.ipc_protocol import (
    IpcProtocolError,
    RunnerBootstrap,
    SignedIpcEnvelope,
)

DEFAULT_MAX_FRAME_BYTES = 1_048_576


class IpcFrameError(IpcProtocolError):
    code = "IPC_FRAME_INVALID"


class IpcFrameTooLargeError(IpcFrameError):
    code = "IPC_FRAME_TOO_LARGE"


class DuplicateJsonKeyError(IpcFrameError):
    code = "IPC_DUPLICATE_JSON_KEY"


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJsonKeyError(f"Duplicate JSON key is forbidden: {key}")
        result[key] = value
    return result


def _reject_nonstandard_number(value: str) -> None:
    raise ValueError(f"Non-standard JSON number is forbidden: {value}")


class NdjsonIpcCodec:
    def __init__(self, *, max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES) -> None:
        if max_frame_bytes < 1_024:
            raise ValueError("max_frame_bytes must be at least 1024")
        self._max_frame_bytes = max_frame_bytes

    def encode(self, envelope: SignedIpcEnvelope) -> bytes:
        frame = canonical_json_bytes(envelope) + b"\n"
        if len(frame) > self._max_frame_bytes:
            raise IpcFrameTooLargeError("Encoded IPC frame exceeds the configured limit")
        return frame

    def decode(self, frame: bytes) -> SignedIpcEnvelope:
        if len(frame) > self._max_frame_bytes:
            raise IpcFrameTooLargeError("IPC frame exceeds the configured limit")
        if not frame.endswith(b"\n") or frame.count(b"\n") != 1:
            raise IpcFrameError("IPC transport requires exactly one newline-terminated frame")
        try:
            decoded = frame[:-1].decode("utf-8", errors="strict")
            value = json.loads(
                decoded,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_nonstandard_number,
            )
            return SignedIpcEnvelope.model_validate(value)
        except DuplicateJsonKeyError:
            raise
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValidationError,
            ValueError,
        ) as error:
            raise IpcFrameError("IPC frame is not a valid signed envelope") from error


class BootstrapCodec:
    """Strict codec for the one unsigned frame required before signing is possible."""

    def __init__(self, *, max_frame_bytes: int = 4_096) -> None:
        if max_frame_bytes < 1_024:
            raise ValueError("max_frame_bytes must be at least 1024")
        self._max_frame_bytes = max_frame_bytes

    def encode(self, bootstrap: RunnerBootstrap) -> bytes:
        frame = canonical_json_bytes(bootstrap) + b"\n"
        if len(frame) > self._max_frame_bytes:
            raise IpcFrameTooLargeError("Runner bootstrap frame exceeds the configured limit")
        return frame

    def decode(self, frame: bytes) -> RunnerBootstrap:
        if len(frame) > self._max_frame_bytes:
            raise IpcFrameTooLargeError("Runner bootstrap frame exceeds the configured limit")
        if not frame.endswith(b"\n") or frame.count(b"\n") != 1:
            raise IpcFrameError("Runner bootstrap must be exactly one newline-terminated frame")
        try:
            decoded = frame[:-1].decode("utf-8", errors="strict")
            value = json.loads(
                decoded,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_nonstandard_number,
            )
            return RunnerBootstrap.model_validate(value)
        except DuplicateJsonKeyError:
            raise
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValidationError,
            ValueError,
        ) as error:
            raise IpcFrameError("Runner bootstrap frame is invalid") from error
