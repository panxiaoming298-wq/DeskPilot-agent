"""Two-step exact-path Artifact export with immutable receipts."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path, PurePosixPath
from typing import Literal, cast

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.artifact_runtime import (
    DeliveryManifestRead,
    PatchReceiptRead,
    PdfRenderVerificationRead,
)
from deskpilot.domain.task_plans import TaskContract
from deskpilot.domain.task_workbench import (
    ArtifactExportRead,
    artifact_export_receipt_digest,
)
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import (
    ArtifactExportRecord,
    ArtifactPatchReceiptRecord,
    ArtifactRecord,
    ArtifactRevisionRecord,
    DeliveryManifestRecord,
    TaskArtifactWorkspaceRecord,
    TaskContractVersionRecord,
    TaskPlanningStateRecord,
    utc_now,
)


class ArtifactExportError(RuntimeError):
    code = "ARTIFACT_EXPORT_ERROR"


class ArtifactExportNotFoundError(ArtifactExportError):
    code = "ARTIFACT_EXPORT_NOT_FOUND"


class ArtifactExportConflictError(ArtifactExportError):
    code = "ARTIFACT_EXPORT_CONFLICT"


class ArtifactExportProofRejectedError(ArtifactExportError):
    code = "ARTIFACT_EXPORT_PROOF_REJECTED"


class ArtifactExportPathRejectedError(ArtifactExportError):
    code = "ARTIFACT_EXPORT_PATH_REJECTED"


class ArtifactExportRuntime:
    def __init__(self, database: Database, workspace_root: str) -> None:
        self._database = database
        self._workspace_root = Path(workspace_root).expanduser().resolve()

    async def prepare(
        self,
        delivery_id: str,
        target_path: str,
        idempotency_key: str,
        *,
        artifact_id: str | None = None,
    ) -> ArtifactExportRead:
        key_digest = self._key_digest(idempotency_key)
        try:
            async with self._database.session() as session, session.begin():
                delivery, _, artifact, revision, source_path = await self._resolve_source(
                    session, delivery_id, artifact_id=artifact_id
                )
                target = self._target_path(target_path, require_absent=True)
                self._assert_target_media_type(target, revision.media_type)
                replay = await session.scalar(
                    select(ArtifactExportRecord).where(
                        ArtifactExportRecord.prepare_key_digest == key_digest
                    )
                )
                if replay is not None:
                    if (
                        replay.delivery_id != delivery_id
                        or replay.artifact_id != artifact.artifact_id
                        or replay.target_path != str(target)
                    ):
                        raise ArtifactExportConflictError(
                            "Idempotency key is bound to another export preview"
                        )
                    return self._read(replay)

                request_material = {
                    "schema_version": "deskpilot.artifact-export-request.v1",
                    "delivery_id": delivery.delivery_id,
                    "task_id": delivery.task_id,
                    "artifact_id": artifact.artifact_id,
                    "revision_id": revision.revision_id,
                    "target_path": str(target),
                    "conflict_policy": "fail_if_exists",
                    "source_digest": revision.content_digest,
                    "byte_count": revision.byte_count,
                }
                request_digest = sha256_digest(request_material)
                confirmation_digest = sha256_digest(
                    {
                        "kind": "workspace.export.confirm.v1",
                        "request_digest": request_digest,
                        "target_absent": True,
                    }
                )
                export_id = f"xpt_{sha256_digest({'request_digest': request_digest})}"
                record = ArtifactExportRecord(
                    export_id=export_id,
                    delivery_id=delivery.delivery_id,
                    task_id=delivery.task_id,
                    artifact_id=artifact.artifact_id,
                    revision_id=revision.revision_id,
                    target_path=str(target),
                    conflict_policy="fail_if_exists",
                    status="prepared",
                    source_digest=revision.content_digest,
                    request_digest=request_digest,
                    confirmation_digest=confirmation_digest,
                    prepare_key_digest=key_digest,
                    commit_key_digest=None,
                    receipt_digest=None,
                    byte_count=revision.byte_count,
                    error_code=None,
                    requested_at=utc_now(),
                    committed_at=None,
                    updated_at=utc_now(),
                )
                if self._file_digest(source_path) != revision.content_digest:
                    raise ArtifactExportProofRejectedError("Artifact blob digest drifted")
                session.add(record)
                await session.flush()
                return self._read(record)
        except IntegrityError as error:
            raise ArtifactExportConflictError("Export preview changed concurrently") from error

    async def commit(
        self,
        export_id: str,
        confirmation_digest: str,
        idempotency_key: str,
    ) -> ArtifactExportRead:
        key_digest = self._key_digest(idempotency_key)
        async with self._database.session() as session, session.begin():
            record = await session.scalar(
                select(ArtifactExportRecord)
                .where(ArtifactExportRecord.export_id == export_id)
                .with_for_update()
            )
            if record is None:
                raise ArtifactExportNotFoundError("Artifact export does not exist")
            self._assert_record(record)
            if confirmation_digest != record.confirmation_digest:
                raise ArtifactExportProofRejectedError("Export confirmation digest is stale")
            if record.commit_key_digest not in {None, key_digest}:
                raise ArtifactExportConflictError(
                    "Export commit is bound to another idempotency key"
                )
            target = Path(record.target_path)
            if record.status == "committed":
                self._verify_committed_file(record, target)
                return self._read(record)
            if record.status == "failed":
                if (
                    record.error_code == "TARGET_WRITE_FAILED"
                    and record.commit_key_digest == key_digest
                    and not self._target_exists(target)
                ):
                    record.status = "prepared"
                else:
                    raise ArtifactExportConflictError(
                        "Failed export cannot be retried without removing the conflict"
                    )
            _, _, artifact, revision, source_path = await self._resolve_source(
                session, record.delivery_id, artifact_id=record.artifact_id
            )
            if (
                artifact.artifact_id != record.artifact_id
                or revision.revision_id != record.revision_id
                or revision.content_digest != record.source_digest
                or revision.byte_count != record.byte_count
                or self._file_digest(source_path) != record.source_digest
            ):
                raise ArtifactExportProofRejectedError("Artifact export source drifted")
            if record.status == "prepared":
                target = self._target_path(record.target_path, require_absent=True)
                self._assert_target_media_type(target, revision.media_type)
                record.status = "committing"
                record.commit_key_digest = key_digest
                record.error_code = None
                record.updated_at = utc_now()
            content = source_path.read_bytes()

        target = Path(record.target_path)
        try:
            await asyncio.to_thread(self._write_exclusive, target, content)
        except FileExistsError:
            if not target.is_file() or self._file_digest(target) != record.source_digest:
                await self._mark_failed(export_id, "TARGET_ALREADY_EXISTS")
                raise ArtifactExportConflictError(
                    "Export target appeared after confirmation; overwrite is forbidden"
                ) from None
        except OSError as error:
            await self._mark_failed(export_id, "TARGET_WRITE_FAILED")
            raise ArtifactExportConflictError("Artifact export write failed") from error

        async with self._database.session() as session, session.begin():
            record = await session.scalar(
                select(ArtifactExportRecord)
                .where(ArtifactExportRecord.export_id == export_id)
                .with_for_update()
            )
            if record is None:
                raise ArtifactExportNotFoundError("Artifact export disappeared")
            if record.status == "committed":
                self._verify_committed_file(record, target)
                return self._read(record)
            if record.status != "committing" or record.commit_key_digest != key_digest:
                raise ArtifactExportConflictError("Artifact export state changed concurrently")
            self._verify_committed_file(record, target, require_receipt=False)
            committed_at = utc_now()
            record.status = "committed"
            record.receipt_digest = artifact_export_receipt_digest(
                export_id=record.export_id,
                delivery_id=record.delivery_id,
                task_id=record.task_id,
                artifact_id=record.artifact_id,
                revision_id=record.revision_id,
                target_path=record.target_path,
                source_digest=record.source_digest,
                byte_count=record.byte_count,
                committed_at=committed_at,
            )
            record.committed_at = committed_at
            record.error_code = None
            record.updated_at = committed_at
            await session.flush()
            return self._read(record)

    async def get(self, export_id: str) -> ArtifactExportRead:
        async with self._database.session() as session:
            record = await session.get(ArtifactExportRecord, export_id)
            if record is None:
                raise ArtifactExportNotFoundError("Artifact export does not exist")
            if record.status == "committed":
                self._verify_committed_file(record, Path(record.target_path))
            return self._read(record)

    async def list_for_task(self, task_id: str) -> tuple[ArtifactExportRead, ...]:
        async with self._database.session() as session:
            records = tuple(
                (
                    await session.scalars(
                        select(ArtifactExportRecord)
                        .where(ArtifactExportRecord.task_id == task_id)
                        .order_by(ArtifactExportRecord.requested_at)
                    )
                ).all()
            )
            for item in records:
                if item.status == "committed":
                    self._verify_committed_file(item, Path(item.target_path))
            return tuple(self._read(item) for item in records)

    async def _resolve_source(
        self,
        session: AsyncSession,
        delivery_id: str,
        *,
        artifact_id: str | None,
    ) -> tuple[
        DeliveryManifestRead,
        TaskArtifactWorkspaceRecord,
        ArtifactRecord,
        ArtifactRevisionRecord,
        Path,
    ]:
        delivery_record = await session.get(DeliveryManifestRecord, delivery_id)
        if delivery_record is None:
            raise ArtifactExportNotFoundError("Verified delivery does not exist")
        try:
            delivery = DeliveryManifestRead.model_validate(delivery_record.manifest)
        except ValueError as error:
            raise ArtifactExportProofRejectedError("Delivery proof is invalid") from error
        if delivery.manifest_digest != delivery_record.manifest_digest:
            raise ArtifactExportProofRejectedError("Delivery digest drifted")
        state = await session.get(TaskPlanningStateRecord, delivery.task_id)
        if state is None:
            raise ArtifactExportProofRejectedError("Active Task Contract is missing")
        contract_record = await session.get(
            TaskContractVersionRecord,
            (delivery.task_id, state.active_contract_version),
        )
        if contract_record is None:
            raise ArtifactExportProofRejectedError("Active Task Contract is missing")
        contract = TaskContract.model_validate(contract_record.manifest)
        if (
            contract.digest != contract_record.contract_digest
            or state.active_contract_digest != contract.digest
            or contract.workspace is None
            or not contract.workspace.allow_user_path_export
        ):
            raise ArtifactExportProofRejectedError(
                "Active Task Contract does not authorize user-path export"
            )
        workspace = await session.get(TaskArtifactWorkspaceRecord, delivery.workspace_id)
        selected_artifact_id = artifact_id or delivery.artifact_id
        artifact = await session.get(ArtifactRecord, selected_artifact_id)
        revision = (
            await session.get(ArtifactRevisionRecord, artifact.active_revision_id)
            if artifact is not None and artifact.active_revision_id is not None
            else None
        )
        receipt = (
            await session.get(ArtifactPatchReceiptRecord, revision.patch_receipt_id)
            if revision is not None
            else None
        )
        if (
            workspace is None
            or artifact is None
            or revision is None
            or receipt is None
            or workspace.task_id != delivery.task_id
            or workspace.run_id != delivery.run_id
            or workspace.status != "delivered"
            or artifact.workspace_id != workspace.workspace_id
            or artifact.active_revision_id != revision.revision_id
            or revision.artifact_id != artifact.artifact_id
            or (
                artifact.artifact_id == delivery.artifact_id
                and revision.revision_id != delivery.revision_id
            )
            or receipt.workspace_id != workspace.workspace_id
            or receipt.artifact_id != artifact.artifact_id
            or receipt.new_revision_id != revision.revision_id
            or receipt.new_digest != revision.content_digest
            or receipt.byte_count != revision.byte_count
        ):
            raise ArtifactExportProofRejectedError("Artifact delivery lineage drifted")
        try:
            PatchReceiptRead(
                patch_receipt_id=receipt.patch_receipt_id,
                artifact_id=receipt.artifact_id,
                operation=cast(Literal["create", "replace"], receipt.operation),
                relative_path=receipt.relative_path,
                base_revision_id=receipt.base_revision_id,
                new_revision_id=receipt.new_revision_id,
                base_digest=receipt.base_digest,
                new_digest=receipt.new_digest,
                byte_count=receipt.byte_count,
                receipt_digest=receipt.receipt_digest,
                created_at=receipt.created_at,
            )
        except ValueError as error:
            raise ArtifactExportProofRejectedError("PatchReceipt proof drifted") from error
        self._assert_pdf_render_proof(revision)
        source_path = self._blob_path(workspace.workspace_id, revision.blob_name)
        self._assert_blob_media_type(source_path, revision.media_type)
        return delivery, workspace, artifact, revision, source_path

    async def _mark_failed(self, export_id: str, error_code: str) -> None:
        async with self._database.session() as session, session.begin():
            record = await session.get(ArtifactExportRecord, export_id)
            if record is not None and record.status != "committed":
                record.status = "failed"
                record.error_code = error_code
                record.receipt_digest = None
                record.committed_at = None
                record.updated_at = utc_now()

    def _blob_path(self, workspace_id: str, blob_name: str) -> Path:
        if (
            PurePosixPath(blob_name).name != blob_name
            or Path(blob_name).suffix.lower() not in {".html", ".md", ".pdf"}
        ):
            raise ArtifactExportProofRejectedError("Artifact blob name is invalid")
        try:
            path = (self._workspace_root / workspace_id / "blobs" / blob_name).resolve(
                strict=True
            )
        except OSError as error:
            raise ArtifactExportProofRejectedError("Artifact blob is missing") from error
        if self._workspace_root not in path.parents or self._is_link(path):
            raise ArtifactExportProofRejectedError("Artifact blob escaped its Workspace")
        return path

    def _target_path(self, value: str, *, require_absent: bool) -> Path:
        raw = Path(value).expanduser()
        if not raw.is_absolute() or raw.name in {"", ".", ".."}:
            raise ArtifactExportPathRejectedError("Export target must be an absolute file path")
        if raw.suffix.lower() not in {".html", ".md", ".pdf"} or ":" in raw.name:
            raise ArtifactExportPathRejectedError(
                "Only an exact .html, .md, or .pdf target is allowed"
            )
        try:
            parent = raw.parent.resolve(strict=True)
        except OSError as error:
            raise ArtifactExportPathRejectedError(
                "Export parent directory does not exist"
            ) from error
        if not parent.is_dir() or self._has_link_component(raw.parent):
            raise ArtifactExportPathRejectedError("Export path links and junctions are forbidden")
        target = parent / raw.name
        if require_absent and target.exists():
            raise ArtifactExportConflictError(
                "Export target already exists; overwrite is forbidden"
            )
        return target

    @staticmethod
    def _assert_target_media_type(target: Path, media_type: str) -> None:
        expected = ArtifactExportRuntime._suffix_for_media_type(media_type)
        if target.suffix.lower() != expected:
            raise ArtifactExportPathRejectedError(
                f"Selected Artifact requires an exact {expected} target"
            )

    @staticmethod
    def _assert_blob_media_type(path: Path, media_type: str) -> None:
        expected = ArtifactExportRuntime._suffix_for_media_type(media_type)
        if path.suffix.lower() != expected:
            raise ArtifactExportProofRejectedError("Artifact media type and blob suffix drifted")

    @staticmethod
    def _assert_pdf_render_proof(revision: ArtifactRevisionRecord) -> None:
        if revision.media_type != "application/pdf":
            if revision.render_evidence is not None or revision.render_evidence_digest is not None:
                raise ArtifactExportProofRejectedError(
                    "Non-PDF Artifact carries unexpected render evidence"
                )
            return
        if revision.render_evidence is None or revision.render_evidence_digest is None:
            raise ArtifactExportProofRejectedError("PDF render proof is missing")
        try:
            proof = PdfRenderVerificationRead.model_validate(revision.render_evidence)
        except ValueError as error:
            raise ArtifactExportProofRejectedError("PDF render proof drifted") from error
        if (
            proof.evidence_digest != revision.render_evidence_digest
            or proof.source_digest != revision.content_digest
        ):
            raise ArtifactExportProofRejectedError("PDF render proof source drifted")

    @staticmethod
    def _suffix_for_media_type(media_type: str) -> Literal[".html", ".md", ".pdf"]:
        if media_type == "application/pdf":
            return ".pdf"
        if media_type == "text/html":
            return ".html"
        if media_type == "text/markdown":
            return ".md"
        raise ArtifactExportProofRejectedError("Artifact media type is not exportable")

    @staticmethod
    def _has_link_component(path: Path) -> bool:
        current = path
        while current != current.parent:
            if ArtifactExportRuntime._is_link(current):
                return True
            current = current.parent
        return False

    @staticmethod
    def _is_link(path: Path) -> bool:
        is_junction = getattr(os.path, "isjunction", lambda _: False)
        return path.is_symlink() or bool(is_junction(path))

    @staticmethod
    def _write_exclusive(path: Path, content: bytes) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        descriptor = os.open(path, flags, 0o600)
        try:
            view = memoryview(content)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _target_exists(path: Path) -> bool:
        return path.exists()

    @classmethod
    def _verify_committed_file(
        cls,
        record: ArtifactExportRecord,
        target: Path,
        *,
        require_receipt: bool = True,
    ) -> None:
        if not target.is_file() or cls._is_link(target):
            raise ArtifactExportProofRejectedError("Exported file is missing or linked")
        if cls._file_digest(target) != record.source_digest:
            raise ArtifactExportProofRejectedError("Exported file digest drifted")
        if require_receipt:
            cls._read(record)

    @staticmethod
    def _key_digest(value: str) -> str:
        normalized = value.strip()
        if not 8 <= len(normalized) <= 200:
            raise ArtifactExportConflictError("Idempotency key length is invalid")
        return sha256_digest({"idempotency_key": normalized})

    @staticmethod
    def _bytes_digest(content: bytes) -> str:
        return sha256_digest({"bytes_hex": content.hex()})

    @classmethod
    def _file_digest(cls, path: Path) -> str:
        return cls._bytes_digest(path.read_bytes())

    @staticmethod
    def _assert_record(record: ArtifactExportRecord) -> None:
        request_material = {
            "schema_version": "deskpilot.artifact-export-request.v1",
            "delivery_id": record.delivery_id,
            "task_id": record.task_id,
            "artifact_id": record.artifact_id,
            "revision_id": record.revision_id,
            "target_path": record.target_path,
            "conflict_policy": record.conflict_policy,
            "source_digest": record.source_digest,
            "byte_count": record.byte_count,
        }
        request_digest = sha256_digest(request_material)
        expected_confirmation = sha256_digest(
            {
                "kind": "workspace.export.confirm.v1",
                "request_digest": request_digest,
                "target_absent": True,
            }
        )
        if (
            record.conflict_policy != "fail_if_exists"
            or record.request_digest != request_digest
            or record.confirmation_digest != expected_confirmation
        ):
            raise ArtifactExportProofRejectedError("Artifact export preview drifted")

    @staticmethod
    def _read(record: ArtifactExportRecord) -> ArtifactExportRead:
        ArtifactExportRuntime._assert_record(record)
        return ArtifactExportRead(
            export_id=record.export_id,
            delivery_id=record.delivery_id,
            task_id=record.task_id,
            artifact_id=record.artifact_id,
            revision_id=record.revision_id,
            target_path=record.target_path,
            conflict_policy="fail_if_exists",
            status=cast(
                Literal["prepared", "committing", "committed", "failed"],
                record.status,
            ),
            source_digest=record.source_digest,
            request_digest=record.request_digest,
            confirmation_digest=record.confirmation_digest,
            receipt_digest=record.receipt_digest,
            byte_count=record.byte_count,
            error_code=record.error_code,
            requested_at=record.requested_at,
            committed_at=record.committed_at,
        )
