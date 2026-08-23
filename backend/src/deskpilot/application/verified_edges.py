"""Trusted reducers for verified and server-adjudicated conditional edges."""

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from deskpilot.domain.agent_runtime import (
    AgentTaskGraphConditionDecision,
    BoundAgentTaskGraphCondition,
    ExecutionNodeStatus,
    ExecutionRunStatus,
)
from deskpilot.infrastructure.models import (
    TaskExecutionEdgeRecord,
    TaskExecutionNodeRecord,
    TaskExecutionRunRecord,
    utc_now,
)


class VerifiedEdgeProofError(RuntimeError):
    code = "VERIFIED_EDGE_PROOF_REJECTED"


async def unlock_if_ready(
    session: AsyncSession,
    run: TaskExecutionRunRecord,
    target: TaskExecutionNodeRecord,
) -> bool:
    """Unlock one target only after every verified/conditional edge proves true."""

    if target.status != ExecutionNodeStatus.PENDING.value:
        return False
    incoming = tuple(
        (
            await session.scalars(
                select(TaskExecutionEdgeRecord).where(
                    TaskExecutionEdgeRecord.run_id == run.run_id,
                    TaskExecutionEdgeRecord.to_node_id == target.node_id,
                )
            )
        ).all()
    )
    if not incoming:
        return False
    for edge in incoming:
        source = await session.get(TaskExecutionNodeRecord, edge.from_node_id)
        if source is None or source.status != ExecutionNodeStatus.VERIFIED.value:
            return False
        condition_fields = (
            edge.condition_manifest,
            edge.condition_digest,
            edge.decision_manifest,
            edge.decision_digest,
        )
        if edge.requirement == "verified":
            if any(item is not None for item in condition_fields):
                raise VerifiedEdgeProofError("Verified edge contains condition state")
            continue
        if edge.requirement != "server_condition":
            raise VerifiedEdgeProofError("Unsupported edge requirement")
        if edge.condition_manifest is None or edge.condition_digest is None:
            raise VerifiedEdgeProofError("Conditional edge binding is incomplete")
        if (edge.decision_manifest is None) != (edge.decision_digest is None):
            raise VerifiedEdgeProofError("Conditional edge decision is incomplete")
        if edge.decision_manifest is None:
            return False
        try:
            condition = BoundAgentTaskGraphCondition.model_validate(edge.condition_manifest)
            decision = AgentTaskGraphConditionDecision.model_validate(edge.decision_manifest)
        except ValidationError as error:
            raise VerifiedEdgeProofError("Conditional edge proof is invalid") from error
        if (
            edge.condition_digest != condition.condition_digest
            or edge.decision_digest != decision.decision_digest
            or condition.source_node_id != edge.from_node_id
            or decision.source_node_id != edge.from_node_id
            or decision.target_node_id != edge.to_node_id
            or decision.predicate != condition.predicate
        ):
            raise VerifiedEdgeProofError("Conditional edge binding changed")
        if not decision.matched:
            return False
    now = utc_now()
    target.status = ExecutionNodeStatus.READY.value
    target.revision += 1
    target.updated_at = now
    return True


async def mark_verified_and_unlock(
    session: AsyncSession,
    run: TaskExecutionRunRecord,
    node: TaskExecutionNodeRecord,
) -> None:
    """Verify one node and unlock targets only when every incoming edge is satisfied."""

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
        target = await session.scalar(
            select(TaskExecutionNodeRecord)
            .where(TaskExecutionNodeRecord.node_id == edge.to_node_id)
            .with_for_update()
        )
        if target is None:
            continue
        await unlock_if_ready(session, run, target)
    run.status = ExecutionRunStatus.ACTIVE.value
    run.revision += 1
    run.updated_at = now
