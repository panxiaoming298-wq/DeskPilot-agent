"""Content-addressed, source-verified local text knowledge base."""

import asyncio
import hashlib
import re
import stat
from datetime import UTC
from pathlib import Path

from sqlalchemy import select

from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.knowledge import (
    KnowledgeCitationRead,
    KnowledgeSearchRead,
    KnowledgeSourceRead,
)
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import (
    KnowledgeArtifactRecord,
    KnowledgeChunkRecord,
    KnowledgeSourceRecord,
    utc_now,
)

PARSER_VERSION = "deskpilot.plain-text.v1"
CHUNKER_VERSION = "deskpilot.line-window.v1"
MAX_SOURCE_BYTES = 1_048_576
MAX_CHUNK_CHARS = 1_200


class KnowledgeSourceError(ValueError):
    code = "KNOWLEDGE_SOURCE_INVALID"


class KnowledgeProofRejectedError(RuntimeError):
    code = "KNOWLEDGE_PROOF_REJECTED"


class LocalKnowledgeBase:
    def __init__(self, database: Database) -> None:
        self._database = database
        self._lock = asyncio.Lock()

    async def import_file(self, raw_path: str) -> KnowledgeSourceRead:
        material = await asyncio.to_thread(self._read_source, raw_path)
        canonical_path, payload, text, source_version = material
        content_digest = hashlib.sha256(payload).hexdigest()
        artifact_id = f"art_{content_digest}"
        chunks = self._chunks(text, artifact_id)
        manifest_digest = self._manifest_digest(artifact_id, content_digest, chunks)
        source_id = f"ksr_{sha256_digest({'canonical_path': canonical_path})}"
        now = utc_now()
        async with self._lock:
            async with self._database.session() as session:
                async with session.begin():
                    artifact = await session.get(KnowledgeArtifactRecord, artifact_id)
                    if artifact is None:
                        artifact = KnowledgeArtifactRecord(
                            artifact_id=artifact_id,
                            content_digest=content_digest,
                            byte_size=len(payload),
                            parser_version=PARSER_VERSION,
                            chunker_version=CHUNKER_VERSION,
                            extracted_text=text,
                            chunk_count=len(chunks),
                            manifest_digest=manifest_digest,
                            created_at=now,
                        )
                        session.add(artifact)
                        session.add_all(
                            KnowledgeChunkRecord(
                                chunk_id=chunk_id,
                                artifact_id=artifact_id,
                                ordinal=ordinal,
                                locator=locator,
                                text=chunk_text,
                                text_digest=text_digest,
                                proof_digest=proof_digest,
                            )
                            for (
                                chunk_id,
                                ordinal,
                                locator,
                                chunk_text,
                                text_digest,
                                proof_digest,
                            ) in chunks
                        )
                    else:
                        self._verify_artifact(artifact, manifest_digest, len(chunks))
                    source = await session.get(KnowledgeSourceRecord, source_id)
                    if source is None:
                        source = KnowledgeSourceRecord(
                            source_id=source_id,
                            canonical_path=canonical_path,
                            artifact_id=artifact_id,
                            source_version=source_version,
                            imported_at=now,
                            updated_at=now,
                        )
                        session.add(source)
                    else:
                        if source.canonical_path != canonical_path:
                            raise KnowledgeProofRejectedError("Source identity path mismatch")
                        source.artifact_id = artifact_id
                        source.source_version = source_version
                        source.updated_at = now
                    await session.flush()
                    return self._source_read(source, artifact)

    async def list_sources(self) -> tuple[KnowledgeSourceRead, ...]:
        async with self._database.session() as session:
            rows = (
                await session.execute(
                    select(KnowledgeSourceRecord, KnowledgeArtifactRecord)
                    .join(KnowledgeArtifactRecord)
                    .order_by(KnowledgeSourceRecord.updated_at.desc())
                )
            ).all()
            return tuple(self._source_read(source, artifact) for source, artifact in rows)

    async def search(self, query: str, limit: int) -> KnowledgeSearchRead:
        normalized = " ".join(query.casefold().split())
        if not normalized:
            raise KnowledgeSourceError("Knowledge query is empty")
        query_digest = sha256_digest({"query": normalized})
        async with self._database.session() as session:
            source_rows = (
                await session.execute(
                    select(KnowledgeSourceRecord, KnowledgeArtifactRecord)
                    .join(
                        KnowledgeArtifactRecord,
                        KnowledgeArtifactRecord.artifact_id == KnowledgeSourceRecord.artifact_id,
                    )
                    .order_by(KnowledgeSourceRecord.source_id)
                )
            ).all()
            chunks = (
                (
                    await session.execute(
                        select(KnowledgeChunkRecord).order_by(
                            KnowledgeChunkRecord.artifact_id,
                            KnowledgeChunkRecord.ordinal,
                        )
                    )
                )
                .scalars()
                .all()
            )
        chunks_by_artifact: dict[str, list[KnowledgeChunkRecord]] = {}
        for chunk in chunks:
            chunks_by_artifact.setdefault(chunk.artifact_id, []).append(chunk)
        artifacts = {artifact.artifact_id: artifact for _, artifact in source_rows}
        for artifact in artifacts.values():
            expected_chunks = self._chunks(artifact.extracted_text, artifact.artifact_id)
            expected_manifest = self._manifest_digest(
                artifact.artifact_id,
                artifact.content_digest,
                expected_chunks,
            )
            self._verify_artifact(artifact, expected_manifest, len(expected_chunks))
            actual_chunks = chunks_by_artifact.get(artifact.artifact_id, [])
            actual_material = [
                (
                    chunk.chunk_id,
                    chunk.ordinal,
                    chunk.locator,
                    chunk.text,
                    chunk.text_digest,
                    chunk.proof_digest,
                )
                for chunk in actual_chunks
            ]
            if actual_material != expected_chunks:
                raise KnowledgeProofRejectedError("Knowledge chunk set is incomplete")
            for chunk in actual_chunks:
                self._verify_chunk(chunk)
        sources = {source.source_id: source for source, _ in source_rows}
        stale: set[str] = set()
        for source in sources.values():
            try:
                current = await asyncio.to_thread(self._read_source, source.canonical_path)
            except KnowledgeSourceError:
                stale.add(source.source_id)
                continue
            if current[3] != source.source_version:
                stale.add(source.source_id)
        terms = tuple(dict.fromkeys(re.findall(r"\w+", normalized, flags=re.UNICODE))) or (
            normalized,
        )
        matches: list[KnowledgeCitationRead] = []
        for source, artifact in source_rows:
            if source.source_id in stale:
                continue
            for chunk in chunks_by_artifact[artifact.artifact_id]:
                folded = chunk.text.casefold()
                hits = sum(folded.count(term) for term in terms)
                if hits == 0 and normalized not in folded:
                    continue
                score = float(hits + (2 if normalized in folded else 0))
                retrieval_digest = sha256_digest(
                    {
                        "schema_version": "deskpilot.knowledge-retrieval-proof.v1",
                        "query_digest": query_digest,
                        "source_id": source.source_id,
                        "source_version": source.source_version,
                        "artifact_id": artifact.artifact_id,
                        "manifest_digest": artifact.manifest_digest,
                        "chunk_id": chunk.chunk_id,
                        "chunk_proof_digest": chunk.proof_digest,
                        "score": score,
                    }
                )
                matches.append(
                    KnowledgeCitationRead(
                        source_id=source.source_id,
                        artifact_id=artifact.artifact_id,
                        chunk_id=chunk.chunk_id,
                        canonical_path=source.canonical_path,
                        locator=chunk.locator,
                        snippet=chunk.text[:500],
                        score=score,
                        text_digest=chunk.text_digest,
                        chunk_proof_digest=chunk.proof_digest,
                        retrieval_proof_digest=retrieval_digest,
                    )
                )
        citations = tuple(sorted(matches, key=lambda item: (-item.score, item.chunk_id))[:limit])
        return KnowledgeSearchRead(
            query_digest=query_digest,
            citations=citations,
            searched_sources=len(sources) - len(stale),
            stale_source_ids=tuple(sorted(stale)),
            result_digest=sha256_digest(
                {
                    "query_digest": query_digest,
                    "citations": [item.model_dump(mode="json") for item in citations],
                    "searched_sources": len(sources) - len(stale),
                    "stale_source_ids": sorted(stale),
                }
            ),
        )

    @staticmethod
    def _read_source(raw_path: str) -> tuple[str, bytes, str, str]:
        candidate = Path(raw_path).expanduser()
        try:
            metadata = candidate.lstat()
        except OSError as error:
            raise KnowledgeSourceError("Knowledge source is unavailable") from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise KnowledgeSourceError("Knowledge source must be a regular non-symlink file")
        resolved = candidate.resolve(strict=True)
        if resolved.suffix.casefold() not in {".txt", ".md"}:
            raise KnowledgeSourceError("Only UTF-8 .txt and .md sources are supported")
        if metadata.st_size > MAX_SOURCE_BYTES:
            raise KnowledgeSourceError("Knowledge source exceeds the 1 MiB limit")
        try:
            payload = resolved.read_bytes()
            text = payload.decode("utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise KnowledgeSourceError("Knowledge source must be readable UTF-8 text") from error
        if not text.strip() or "\x00" in text:
            raise KnowledgeSourceError("Knowledge source is empty or binary")
        after = resolved.stat()
        if (metadata.st_size, metadata.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise KnowledgeSourceError("Knowledge source changed while it was being read")
        content_digest = hashlib.sha256(payload).hexdigest()
        source_version = sha256_digest(
            {
                "canonical_path": str(resolved),
                "content_digest": content_digest,
                "size": len(payload),
                "modified_ns": after.st_mtime_ns,
            }
        )
        return str(resolved), payload, text, source_version

    @staticmethod
    def _chunks(text: str, artifact_id: str) -> list[tuple[str, int, str, str, str, str]]:
        lines = text.splitlines()
        result: list[tuple[str, int, str, str, str, str]] = []
        start = 0
        while start < len(lines):
            end = start
            size = 0
            while end < len(lines) and (
                end == start or size + len(lines[end]) + 1 <= MAX_CHUNK_CHARS
            ):
                size += len(lines[end]) + 1
                end += 1
            chunk_text = "\n".join(lines[start:end]).strip()
            if chunk_text:
                ordinal = len(result)
                locator = f"L{start + 1}-L{end}"
                text_digest = sha256_digest({"text": chunk_text})
                proof = sha256_digest(
                    {
                        "artifact_id": artifact_id,
                        "ordinal": ordinal,
                        "locator": locator,
                        "text_digest": text_digest,
                    }
                )
                result.append((f"kch_{proof}", ordinal, locator, chunk_text, text_digest, proof))
            start = end
        if not result:
            raise KnowledgeSourceError("Knowledge source produced no chunks")
        return result

    @staticmethod
    def _verify_chunk(chunk: KnowledgeChunkRecord) -> None:
        text_digest = sha256_digest({"text": chunk.text})
        proof = sha256_digest(
            {
                "artifact_id": chunk.artifact_id,
                "ordinal": chunk.ordinal,
                "locator": chunk.locator,
                "text_digest": text_digest,
            }
        )
        if (
            text_digest != chunk.text_digest
            or proof != chunk.proof_digest
            or chunk.chunk_id != f"kch_{proof}"
        ):
            raise KnowledgeProofRejectedError("Knowledge chunk proof is invalid")

    @staticmethod
    def _verify_artifact(
        artifact: KnowledgeArtifactRecord, manifest_digest: str, chunk_count: int
    ) -> None:
        content_digest = hashlib.sha256(artifact.extracted_text.encode("utf-8")).hexdigest()
        if (
            artifact.content_digest != content_digest
            or artifact.artifact_id != f"art_{content_digest}"
            or artifact.byte_size != len(artifact.extracted_text.encode("utf-8"))
            or artifact.parser_version != PARSER_VERSION
            or artifact.chunker_version != CHUNKER_VERSION
            or artifact.manifest_digest != manifest_digest
            or artifact.chunk_count != chunk_count
        ):
            raise KnowledgeProofRejectedError("Knowledge artifact manifest is invalid")

    @staticmethod
    def _manifest_digest(
        artifact_id: str,
        content_digest: str,
        chunks: list[tuple[str, int, str, str, str, str]],
    ) -> str:
        return sha256_digest(
            {
                "schema_version": "deskpilot.knowledge-manifest.v1",
                "artifact_id": artifact_id,
                "content_digest": content_digest,
                "parser_version": PARSER_VERSION,
                "chunker_version": CHUNKER_VERSION,
                "chunks": [
                    {"chunk_id": item[0], "locator": item[2], "proof_digest": item[5]}
                    for item in chunks
                ],
            }
        )

    @staticmethod
    def _source_read(
        source: KnowledgeSourceRecord, artifact: KnowledgeArtifactRecord
    ) -> KnowledgeSourceRead:
        imported = (
            source.imported_at.replace(tzinfo=UTC)
            if source.imported_at.tzinfo is None
            else source.imported_at
        )
        updated = (
            source.updated_at.replace(tzinfo=UTC)
            if source.updated_at.tzinfo is None
            else source.updated_at
        )
        return KnowledgeSourceRead(
            source_id=source.source_id,
            canonical_path=source.canonical_path,
            artifact_id=artifact.artifact_id,
            source_version=source.source_version,
            content_digest=artifact.content_digest,
            byte_size=artifact.byte_size,
            chunk_count=artifact.chunk_count,
            manifest_digest=artifact.manifest_digest,
            imported_at=imported,
            updated_at=updated,
        )
