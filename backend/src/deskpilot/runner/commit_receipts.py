"""Runner-owned SQLite journal for controlled-commit evidence."""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Literal

from pydantic import JsonValue

from deskpilot.core.canonical_json import canonical_json_bytes
from deskpilot.domain.tool_commit import ToolCommitReceipt

CommitAttemptState = Literal["prepared", "committing", "committed", "no_effect", "unknown"]


class CommitReceiptStoreError(RuntimeError):
    code = "TOOL_COMMIT_RECEIPT_STORE_FAILED"


class CommitReceiptBindingError(CommitReceiptStoreError):
    code = "TOOL_COMMIT_RECEIPT_CONFLICT"


@dataclass(frozen=True, slots=True)
class PreparedCommitRecord:
    receipt_id: str
    call_id: str
    tool_name: str
    tool_version: str
    authorization_id: str
    approval_id: str
    preview_hash: str
    prepare_digest: str
    idempotency_key_digest: str
    binding_digest: str
    state: CommitAttemptState
    prepared_payload: dict[str, JsonValue]
    commit_started_at: datetime | None
    receipt: ToolCommitReceipt | None
    created_at: datetime
    updated_at: datetime


class CommitReceiptStore:
    """Serializes commit ownership and evidence independently of Runner lifetime."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve(strict=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_lock = Lock()
        self._initialized = False
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=30000")
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._initialize_lock:
            if self._initialized:
                return
            with self._connection() as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS controlled_commit_attempts (
                        receipt_id TEXT PRIMARY KEY,
                        call_id TEXT NOT NULL UNIQUE,
                        tool_name TEXT NOT NULL,
                        tool_version TEXT NOT NULL,
                        authorization_id TEXT NOT NULL,
                        approval_id TEXT NOT NULL,
                        preview_hash TEXT NOT NULL,
                        prepare_digest TEXT NOT NULL,
                        idempotency_key_digest TEXT NOT NULL,
                        binding_digest TEXT NOT NULL,
                        state TEXT NOT NULL CHECK (
                            state IN (
                                'prepared', 'committing', 'committed',
                                'no_effect', 'unknown'
                            )
                        ),
                        prepared_json TEXT NOT NULL,
                        commit_started_at TEXT,
                        receipt_json TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE (tool_name, tool_version, idempotency_key_digest)
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS ix_controlled_commit_attempts_state
                    ON controlled_commit_attempts (state, updated_at)
                    """
                )
            self._initialized = True

    def stage(self, record: PreparedCommitRecord) -> PreparedCommitRecord:
        if record.state != "prepared" or record.receipt is not None:
            raise ValueError("A new controlled commit must be staged as prepared")
        prepared_json = canonical_json_bytes(record.prepared_payload).decode("utf-8")
        timestamp = record.created_at.astimezone(UTC).isoformat()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    """
                    SELECT * FROM controlled_commit_attempts
                    WHERE call_id = ? OR (
                        tool_name = ? AND tool_version = ?
                        AND idempotency_key_digest = ?
                    )
                    ORDER BY CASE WHEN call_id = ? THEN 0 ELSE 1 END
                    LIMIT 1
                    """,
                    (
                        record.call_id,
                        record.tool_name,
                        record.tool_version,
                        record.idempotency_key_digest,
                        record.call_id,
                    ),
                ).fetchone()
                if existing is not None:
                    loaded = self._from_row(existing)
                    self._validate_binding(loaded, record)
                    connection.execute("COMMIT")
                    return loaded
                connection.execute(
                    """
                    INSERT INTO controlled_commit_attempts (
                        receipt_id, call_id, tool_name, tool_version,
                        authorization_id, approval_id, preview_hash,
                        prepare_digest, idempotency_key_digest, binding_digest,
                        state, prepared_json, commit_started_at, receipt_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
                    """,
                    (
                        record.receipt_id,
                        record.call_id,
                        record.tool_name,
                        record.tool_version,
                        record.authorization_id,
                        record.approval_id,
                        record.preview_hash,
                        record.prepare_digest,
                        record.idempotency_key_digest,
                        record.binding_digest,
                        record.state,
                        prepared_json,
                        timestamp,
                        timestamp,
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return record

    def mark_committing(
        self,
        receipt_id: str,
        *,
        commit_started_at: datetime,
    ) -> PreparedCommitRecord:
        return self._transition(
            receipt_id,
            from_states=("prepared",),
            state="committing",
            commit_started_at=commit_started_at,
        )

    def mark_committed(self, receipt: ToolCommitReceipt) -> PreparedCommitRecord:
        return self._transition(
            receipt.receipt_id,
            from_states=("committing",),
            state="committed",
            receipt=receipt,
        )

    def mark_recovered_committed(self, receipt: ToolCommitReceipt) -> PreparedCommitRecord:
        return self._transition(
            receipt.receipt_id,
            from_states=("prepared", "committing"),
            state="committed",
            receipt=receipt,
        )

    def mark_no_effect(self, receipt_id: str) -> PreparedCommitRecord:
        return self._transition(
            receipt_id,
            from_states=("prepared", "committing"),
            state="no_effect",
        )

    def mark_unknown(self, receipt_id: str) -> PreparedCommitRecord:
        return self._transition(
            receipt_id,
            from_states=("prepared", "committing"),
            state="unknown",
        )

    def _transition(
        self,
        receipt_id: str,
        *,
        from_states: tuple[CommitAttemptState, ...],
        state: CommitAttemptState,
        commit_started_at: datetime | None = None,
        receipt: ToolCommitReceipt | None = None,
    ) -> PreparedCommitRecord:
        timestamp = datetime.now(UTC).isoformat()
        receipt_json = (
            canonical_json_bytes(receipt).decode("utf-8") if receipt is not None else None
        )
        placeholders = ", ".join("?" for _ in from_states)
        assignments = ["state = ?", "updated_at = ?"]
        parameters: list[object] = [state, timestamp]
        if commit_started_at is not None:
            assignments.append("commit_started_at = ?")
            parameters.append(commit_started_at.astimezone(UTC).isoformat())
        if receipt_json is not None:
            assignments.append("receipt_json = ?")
            parameters.append(receipt_json)
        parameters.extend((receipt_id, *from_states))
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    f"""
                    UPDATE controlled_commit_attempts
                    SET {", ".join(assignments)}
                    WHERE receipt_id = ? AND state IN ({placeholders})
                    """,
                    parameters,
                )
                if cursor.rowcount != 1:
                    existing = connection.execute(
                        "SELECT * FROM controlled_commit_attempts WHERE receipt_id = ?",
                        (receipt_id,),
                    ).fetchone()
                    if existing is None:
                        raise CommitReceiptStoreError("Controlled commit receipt is missing")
                    loaded = self._from_row(existing)
                    if loaded.state == state:
                        connection.execute("COMMIT")
                        return loaded
                    raise CommitReceiptStoreError(
                        "Controlled commit receipt has an invalid state transition"
                    )
                row = connection.execute(
                    "SELECT * FROM controlled_commit_attempts WHERE receipt_id = ?",
                    (receipt_id,),
                ).fetchone()
                if row is None:
                    raise CommitReceiptStoreError("Controlled commit receipt disappeared")
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._from_row(row)

    def get_for_call(self, call_id: str) -> PreparedCommitRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM controlled_commit_attempts WHERE call_id = ?",
                (call_id,),
            ).fetchone()
        return None if row is None else self._from_row(row)

    def list_incomplete(
        self,
        tool_name: str,
        tool_version: str,
    ) -> tuple[PreparedCommitRecord, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM controlled_commit_attempts
                WHERE tool_name = ? AND tool_version = ?
                  AND state IN ('prepared', 'committing')
                ORDER BY created_at, receipt_id
                """,
                (tool_name, tool_version),
            ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    @staticmethod
    def _validate_binding(
        existing: PreparedCommitRecord,
        candidate: PreparedCommitRecord,
    ) -> None:
        if (
            existing.call_id != candidate.call_id
            or existing.receipt_id != candidate.receipt_id
            or existing.binding_digest != candidate.binding_digest
            or existing.authorization_id != candidate.authorization_id
            or existing.prepare_digest != candidate.prepare_digest
        ):
            raise CommitReceiptBindingError(
                "Controlled commit idempotency key is already bound to another request"
            )

    @staticmethod
    def _from_row(row: sqlite3.Row) -> PreparedCommitRecord:
        try:
            prepared = json.loads(str(row["prepared_json"]))
            receipt_raw = (
                json.loads(str(row["receipt_json"]))
                if row["receipt_json"] is not None
                else None
            )
            if not isinstance(prepared, dict):
                raise TypeError("prepared payload is not an object")
            receipt = (
                ToolCommitReceipt.model_validate(receipt_raw)
                if receipt_raw is not None
                else None
            )
            return PreparedCommitRecord(
                receipt_id=str(row["receipt_id"]),
                call_id=str(row["call_id"]),
                tool_name=str(row["tool_name"]),
                tool_version=str(row["tool_version"]),
                authorization_id=str(row["authorization_id"]),
                approval_id=str(row["approval_id"]),
                preview_hash=str(row["preview_hash"]),
                prepare_digest=str(row["prepare_digest"]),
                idempotency_key_digest=str(row["idempotency_key_digest"]),
                binding_digest=str(row["binding_digest"]),
                state=str(row["state"]),  # type: ignore[arg-type]
                prepared_payload=prepared,
                commit_started_at=(
                    datetime.fromisoformat(str(row["commit_started_at"]))
                    if row["commit_started_at"] is not None
                    else None
                ),
                receipt=receipt,
                created_at=datetime.fromisoformat(str(row["created_at"])),
                updated_at=datetime.fromisoformat(str(row["updated_at"])),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise CommitReceiptStoreError(
                "Controlled commit receipt journal is corrupt"
            ) from error


__all__ = [
    "CommitReceiptBindingError",
    "CommitReceiptStore",
    "CommitReceiptStoreError",
    "PreparedCommitRecord",
]
