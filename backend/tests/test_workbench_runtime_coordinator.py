from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from deskpilot.application.workbench_runtime_coordinator import (
    WorkbenchRuntimeFenceRejectedError,
    WorkbenchRuntimeStore,
)
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import TaskRecord, WorkbenchRuntimeItemRecord


async def _create_task(database: Database, task_id: str) -> None:
    now = datetime.now(UTC)
    async with database.session() as session, session.begin():
        session.add(
            TaskRecord(
                task_id=task_id,
                goal="durable Workbench runtime test",
                status="created",
                mode="fake",
                privacy_mode="local_preferred",
                constraints=[],
                last_event_seq=0,
                created_at=now,
                updated_at=now,
            )
        )


@pytest.mark.asyncio
async def test_workbench_runtime_recovers_expired_claim_and_fences_cancel(
    tmp_path: Path,
) -> None:
    database = Database(
        f"sqlite+aiosqlite:///{(tmp_path / 'workbench-runtime.db').as_posix()}"
    )
    await database.migrate()
    store = WorkbenchRuntimeStore(database)
    try:
        recoverable_task_id = "tsk_11111111111111111111111111111111"
        await _create_task(database, recoverable_task_id)
        await store.enqueue(recoverable_task_id, "1" * 64)
        first = (
            await store.claim("runtime-a", ttl_seconds=30, limit=1)
        )[0]

        async with database.session() as session, session.begin():
            record = await session.get(
                WorkbenchRuntimeItemRecord,
                first.work_item_id,
            )
            assert record is not None
            record.claim_expires_at = datetime(2000, 1, 1, tzinfo=UTC)

        recovered = (
            await store.claim("runtime-b", ttl_seconds=30, limit=1)
        )[0]
        assert recovered.work_item_id == first.work_item_id
        assert recovered.claim_fencing_token == first.claim_fencing_token + 1
        with pytest.raises(WorkbenchRuntimeFenceRejectedError):
            await store.complete(
                first,
                projection_digest="2" * 64,
                requeue=False,
            )
        assert (
            await store.complete(
                recovered,
                projection_digest="2" * 64,
                requeue=False,
            )
            == "applied"
        )

        cancelled_task_id = "tsk_22222222222222222222222222222222"
        await _create_task(database, cancelled_task_id)
        await store.enqueue(cancelled_task_id, "3" * 64)
        cancelled_claim = (
            await store.claim("runtime-c", ttl_seconds=30, limit=1)
        )[0]
        assert await store.cancel(cancelled_task_id)
        with pytest.raises(WorkbenchRuntimeFenceRejectedError):
            await store.complete(
                cancelled_claim,
                projection_digest="4" * 64,
                requeue=False,
            )

        async with database.session() as session:
            statuses = tuple(
                (
                    await session.scalars(
                        select(WorkbenchRuntimeItemRecord.status).order_by(
                            WorkbenchRuntimeItemRecord.task_id
                        )
                    )
                ).all()
            )
        assert statuses == ("applied", "cancelled")
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_workbench_runtime_claims_full_wave_when_task_loop_progress_is_equal(
    tmp_path: Path,
) -> None:
    database = Database(
        f"sqlite+aiosqlite:///{(tmp_path / 'workbench-runtime-fairness.db').as_posix()}"
    )
    await database.migrate()
    store = WorkbenchRuntimeStore(database)
    task_ids = tuple(f"tsk_{index:032x}" for index in range(1, 4))
    try:
        for task_id in task_ids:
            await _create_task(database, task_id)
            await store.enqueue(task_id, task_id.removeprefix("tsk_") * 2)

        async with database.session() as session, session.begin():
            records = tuple(
                (
                    await session.scalars(
                        select(WorkbenchRuntimeItemRecord).order_by(
                            WorkbenchRuntimeItemRecord.task_id
                        )
                    )
                ).all()
            )
            for record, workbench_attempt_count in zip(
                records,
                (5, 2, 4),
                strict=True,
            ):
                record.attempt_count = workbench_attempt_count

        first_wave = await store.claim("runtime-a", ttl_seconds=30, limit=2)
        assert len(first_wave) == 2
        assert {claim.attempt_count for claim in first_wave} == {3, 6}
    finally:
        await database.dispose()
