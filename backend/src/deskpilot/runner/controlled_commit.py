"""Commit-boundary tracking shared by the Runner event loop and providers."""

from dataclasses import dataclass
from enum import StrEnum
from threading import Event, Lock

from pydantic import BaseModel

from deskpilot.domain.tool_commit import ToolCommitReceipt


class ControlledCommitPhase(StrEnum):
    BEFORE_COMMIT = "before_commit"
    COMMITTING = "committing"
    COMMITTED = "committed"
    NO_EFFECT = "no_effect"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ControlledCommitSnapshot:
    phase: ControlledCommitPhase
    output: BaseModel | None
    receipt: ToolCommitReceipt | None


class ControlledCommitBoundary:
    """Thread-safe proof of whether one invocation crossed its commit point."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._phase = ControlledCommitPhase.BEFORE_COMMIT
        self._output: BaseModel | None = None
        self._receipt: ToolCommitReceipt | None = None

    def mark_committing(self) -> None:
        with self._lock:
            if self._phase is not ControlledCommitPhase.BEFORE_COMMIT:
                raise RuntimeError("Controlled commit boundary was crossed more than once")
            self._phase = ControlledCommitPhase.COMMITTING

    def try_mark_committing(self, cancellation: Event) -> bool:
        """Atomically refuse a commit whose cancellation was already published."""
        with self._lock:
            if self._phase is not ControlledCommitPhase.BEFORE_COMMIT:
                raise RuntimeError("Controlled commit boundary was crossed more than once")
            if cancellation.is_set():
                self._phase = ControlledCommitPhase.NO_EFFECT
                return False
            self._phase = ControlledCommitPhase.COMMITTING
            return True

    def mark_committed(self, output: BaseModel, receipt: ToolCommitReceipt) -> None:
        with self._lock:
            if self._phase is not ControlledCommitPhase.COMMITTING:
                raise RuntimeError("Controlled commit completed outside its commit boundary")
            self._phase = ControlledCommitPhase.COMMITTED
            self._output = output
            self._receipt = receipt

    def mark_no_effect(self) -> None:
        with self._lock:
            if self._phase not in {
                ControlledCommitPhase.BEFORE_COMMIT,
                ControlledCommitPhase.COMMITTING,
            }:
                return
            self._phase = ControlledCommitPhase.NO_EFFECT

    def mark_unknown(self) -> None:
        with self._lock:
            if self._phase is ControlledCommitPhase.COMMITTED:
                return
            self._phase = ControlledCommitPhase.UNKNOWN

    def snapshot(self) -> ControlledCommitSnapshot:
        with self._lock:
            return ControlledCommitSnapshot(
                phase=self._phase,
                output=self._output,
                receipt=self._receipt,
            )


__all__ = [
    "ControlledCommitBoundary",
    "ControlledCommitPhase",
    "ControlledCommitSnapshot",
]
