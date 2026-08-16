import asyncio
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from deskpilot.application.event_broker import EventBroker
from deskpilot.application.outbox_publisher import OutboxPublisher
from deskpilot.application.task_service import TaskService
from deskpilot.domain.schemas import TaskCreate, TaskEventRead, TaskStatus
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import (
    OutboxMessageRecord,
    TaskEventRecord,
    TaskRecord,
    utc_now,
)


class FailingOnceBroker(EventBroker):
    def __init__(self) -> None:
        super().__init__()
        self.should_fail = True
        self.published: list[TaskEventRead] = []

    async def publish(self, event: TaskEventRead) -> None:
        if self.should_fail:
            self.should_fail = False
            raise RuntimeError("temporary broker failure")
        self.published.append(event)


class RecordingBroker(EventBroker):
    def __init__(self, expected_count: int) -> None:
        super().__init__()
        self.expected_count = expected_count
        self.published: list[TaskEventRead] = []
        self.expected_messages_published = asyncio.Event()

    async def publish(self, event: TaskEventRead) -> None:
        self.published.append(event)
        if len(self.published) >= self.expected_count:
            self.expected_messages_published.set()


def _database_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


@pytest.mark.asyncio
async def test_task_event_and_outbox_commit_together(tmp_path: Path) -> None:
    database = Database(_database_url(tmp_path / "atomic.db"))
    await database.migrate()
    notifications = 0

    def notify() -> None:
        nonlocal notifications
        notifications += 1

    service = TaskService(database, "/api/v1", outbox_notify=notify)
    task = await service.create_task(TaskCreate(goal="验证事务 Outbox"))

    async with database.session() as session:
        event = await session.scalar(
            select(TaskEventRecord).where(TaskEventRecord.task_id == task.task_id)
        )
        message = await session.scalar(
            select(OutboxMessageRecord).where(OutboxMessageRecord.task_id == task.task_id)
        )

    assert event is not None
    assert message is not None
    assert message.event_id == event.event_id
    assert message.event_seq == event.seq
    assert message.payload["event_id"] == event.event_id
    assert message.published_at is None
    assert notifications == 1
    await database.dispose()


@pytest.mark.asyncio
async def test_outbox_failure_rolls_back_task_and_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(_database_url(tmp_path / "rollback.db"))
    await database.migrate()
    service = TaskService(database, "/api/v1")
    create_outbox = TaskService._to_outbox

    def create_invalid_outbox(event: TaskEventRead) -> OutboxMessageRecord:
        message = create_outbox(event)
        message.event_id = "evt_missing"
        return message

    monkeypatch.setattr(TaskService, "_to_outbox", staticmethod(create_invalid_outbox))

    with pytest.raises(IntegrityError):
        await service.create_task(TaskCreate(goal="事务应整体回滚"))

    async with database.session() as session:
        task_count = await session.scalar(select(func.count()).select_from(TaskRecord))
        event_count = await session.scalar(select(func.count()).select_from(TaskEventRecord))
        outbox_count = await session.scalar(
            select(func.count()).select_from(OutboxMessageRecord)
        )

    assert (task_count, event_count, outbox_count) == (0, 0, 0)
    await database.dispose()


@pytest.mark.asyncio
async def test_publisher_retries_then_marks_message_published(tmp_path: Path) -> None:
    database = Database(_database_url(tmp_path / "retry.db"))
    await database.migrate()
    service = TaskService(database, "/api/v1")
    task = await service.create_task(TaskCreate(goal="验证 Outbox 重试"))
    broker = FailingOnceBroker()
    publisher = OutboxPublisher(
        database,
        broker,
        retry_base_seconds=0,
        retry_max_seconds=0,
    )

    first = await publisher.publish_pending()
    assert (first.attempted, first.published, first.failed) == (1, 0, 1)

    async with database.session() as session:
        failed_message = await session.scalar(
            select(OutboxMessageRecord).where(OutboxMessageRecord.task_id == task.task_id)
        )
        assert failed_message is not None
        assert failed_message.attempt_count == 1
        assert failed_message.published_at is None
        assert failed_message.last_error == "RuntimeError: temporary broker failure"

    second = await publisher.publish_pending()
    assert (second.attempted, second.published, second.failed) == (1, 1, 0)
    assert [event.type for event in broker.published] == ["task.created"]

    async with database.session() as session:
        published_message = await session.scalar(
            select(OutboxMessageRecord).where(OutboxMessageRecord.task_id == task.task_id)
        )
        assert published_message is not None
        assert published_message.attempt_count == 2
        assert published_message.published_at is not None
        assert published_message.last_error is None
    await database.dispose()


@pytest.mark.asyncio
async def test_new_publisher_recovers_pending_messages_in_sequence(tmp_path: Path) -> None:
    database_path = tmp_path / "restart.db"
    first_database = Database(_database_url(database_path))
    await first_database.migrate()
    service = TaskService(first_database, "/api/v1")
    task = await service.create_task(TaskCreate(goal="验证重启恢复"))
    await service.append_event(task.task_id, "plan.proposed", {"steps": []})
    await first_database.dispose()

    restarted_database = Database(_database_url(database_path))
    await restarted_database.migrate()
    broker = RecordingBroker(expected_count=2)
    publisher = OutboxPublisher(restarted_database, broker, poll_interval_seconds=0.01)
    publisher.start()
    await asyncio.wait_for(broker.expected_messages_published.wait(), timeout=1)
    await publisher.shutdown()

    assert [event.seq for event in broker.published] == [1, 2]
    assert [event.type for event in broker.published] == ["task.created", "plan.proposed"]
    await restarted_database.dispose()


@pytest.mark.asyncio
async def test_publisher_fans_out_committed_event_to_live_subscriber(tmp_path: Path) -> None:
    database = Database(_database_url(tmp_path / "fanout.db"))
    await database.migrate()
    broker = EventBroker()
    publisher = OutboxPublisher(database, broker, poll_interval_seconds=0.01)
    service = TaskService(database, "/api/v1", outbox_notify=publisher.notify)
    task = await service.create_task(TaskCreate(goal="验证实时事件投递"))

    async with broker.subscribe(task.task_id) as queue:
        publisher.start()
        event = await asyncio.wait_for(queue.get(), timeout=1)
        await publisher.shutdown()

    assert event.task_id == task.task_id
    assert event.seq == 1
    assert event.type == "task.created"
    await database.dispose()


@pytest.mark.asyncio
async def test_control_transition_writes_event_and_outbox_atomically(tmp_path: Path) -> None:
    database = Database(_database_url(tmp_path / "control-outbox.db"))
    await database.migrate()
    service = TaskService(database, "/api/v1")
    task = await service.create_task(TaskCreate(goal="控制命令 Outbox"))

    cancelled = await service.transition_task(
        task.task_id,
        TaskStatus.CANCELLED,
        command="cancel",
    )

    async with database.session() as session:
        events = list(
            (
                await session.scalars(
                    select(TaskEventRecord)
                    .where(TaskEventRecord.task_id == task.task_id)
                    .order_by(TaskEventRecord.seq)
                )
            ).all()
        )
        messages = list(
            (
                await session.scalars(
                    select(OutboxMessageRecord)
                    .where(OutboxMessageRecord.task_id == task.task_id)
                    .order_by(OutboxMessageRecord.event_seq)
                )
            ).all()
        )

    assert cancelled.status is TaskStatus.CANCELLED
    assert [event.type for event in events] == ["task.created", "task.cancelled"]
    assert [message.event_id for message in messages] == [event.event_id for event in events]
    await database.dispose()


@pytest.mark.asyncio
async def test_two_publishers_claim_one_message_only_once(tmp_path: Path) -> None:
    database = Database(_database_url(tmp_path / "multi-instance.db"))
    await database.migrate()
    service = TaskService(database, "/api/v1")
    task = await service.create_task(TaskCreate(goal="multi-instance outbox claim"))
    broker = RecordingBroker(expected_count=1)
    first = OutboxPublisher(database, broker, instance_id="publisher_first")
    second = OutboxPublisher(database, broker, instance_id="publisher_second")

    results = await asyncio.gather(
        first.publish_pending(),
        second.publish_pending(),
    )

    assert sum(result.attempted for result in results) == 1
    assert sum(result.published for result in results) == 1
    assert [event.task_id for event in broker.published] == [task.task_id]
    await database.dispose()


@pytest.mark.asyncio
async def test_expired_outbox_claim_is_reclaimed_and_stale_fence_cannot_ack(
    tmp_path: Path,
) -> None:
    database = Database(_database_url(tmp_path / "outbox-fence.db"))
    await database.migrate()
    service = TaskService(database, "/api/v1")
    task = await service.create_task(TaskCreate(goal="outbox fence takeover"))
    broker = RecordingBroker(expected_count=1)
    stale = OutboxPublisher(database, broker, instance_id="publisher_stale")
    current = OutboxPublisher(database, broker, instance_id="publisher_current")

    stale_claims = await stale._claim_batch()
    assert len(stale_claims) == 1
    async with database.session() as session:
        async with session.begin():
            await session.execute(
                update(OutboxMessageRecord)
                .where(OutboxMessageRecord.task_id == task.task_id)
                .values(claim_expires_at=utc_now() - timedelta(seconds=1))
            )

    result = await current.publish_pending()
    assert (result.attempted, result.published, result.fenced) == (1, 1, 0)
    assert not await stale._mark_published(stale_claims[0])

    async with database.session() as session:
        message = await session.scalar(
            select(OutboxMessageRecord).where(
                OutboxMessageRecord.task_id == task.task_id
            )
        )
        assert message is not None
        assert message.published_at is not None
        assert message.claim_fencing_token == 2
    await database.dispose()
