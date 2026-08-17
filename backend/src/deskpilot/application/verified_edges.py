"""Single trusted reducer for the runtime's verified-edge unlock rule."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from deskpilot.domain.agent_runtime import ExecutionNodeStatus, ExecutionRunStatus
from deskpilot.infrastructure.models import (
    TaskExecutionEdgeRecord,
    TaskExecutionNodeRecord,
    TaskExecutionRunRecord,
    utc_now,
)


class VerifiedEdgeProofError(RuntimeError):
    code = "VERIFIED_EDGE_PROOF_REJECTED"


async def mark_verified_and_unlock(
    session: AsyncSession,
    run: TaskExecutionRunRecord,
    node: TaskExecutionNodeRecord,
) -> None:
    """Verify one node and unlock targets only when every incoming edge is verified."""

    now = utc_now()
    node.status = ExecutionNodeStatus.VERIFIED.value
    node.revision += 1
    node.updated_at = now
    outgoing = tuple(
        (
            await session.scalars(
                select(TaskExecutionEdgeRecord).where(
                    TaskExecutionEdgeRecord.run_id == run.run_id,
                    TaskExecutionEdgeRecord.from_node_id == node.node_id,
                )
            )
        ).all()
    )
    for edge in outgoing:
        if edge.requirement != "verified":
            raise VerifiedEdgeProofError("Unsupported edge requirement")
        target = await session.scalar(
            select(TaskExecutionNodeRecord)
            .where(TaskExecutionNodeRecord.node_id == edge.to_node_id)
            .with_for_update()
        )
        if target is None or target.status != ExecutionNodeStatus.PENDING.value:
            continue
        source_ids = tuple(
            item.from_node_id
            for item in (
                await session.scalars(
                    select(TaskExecutionEdgeRecord).where(
                        TaskExecutionEdgeRecord.run_id == run.run_id,
                        TaskExecutionEdgeRecord.to_node_id == target.node_id,
                        TaskExecutionEdgeRecord.requirement == "verified",
                    )
                )
            ).all()
        )
        verified_count = await session.scalar(
            select(func.count())
            .select_from(TaskExecutionNodeRecord)
            .where(
                TaskExecutionNodeRecord.node_id.in_(source_ids),
                TaskExecutionNodeRecord.status == ExecutionNodeStatus.VERIFIED.value,
            )
        )
        if source_ids and int(verified_count or 0) == len(source_ids):
            target.status = ExecutionNodeStatus.READY.value
            target.revision += 1
            target.updated_at = now
    run.status = ExecutionRunStatus.ACTIVE.value
    run.revision += 1
    run.updated_at = now
