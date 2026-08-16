"""PostgreSQL-native claim statements using row locks, SKIP LOCKED, and RETURNING."""

from datetime import datetime

from sqlalchemy import Select, Update, exists, func, select, update
from sqlalchemy.orm import aliased

from deskpilot.domain.effect_graph import EffectNodeStatus
from deskpilot.infrastructure.models import (
    OutboxMessageRecord,
    ToolEffectGraphControlRecord,
    ToolEffectGraphRecord,
    ToolEffectNodeRecord,
)


def build_postgresql_graph_control_claim_statement(
    *,
    owner_id: str,
    database_now: datetime,
    expires_at: datetime,
    batch_size: int,
) -> Update:
    """Claim live owner/fence-bound graph controls in one PostgreSQL UPDATE."""
    locked = (
        select(
            ToolEffectGraphControlRecord.control_id,
            ToolEffectGraphControlRecord.graph_id,
            ToolEffectGraphControlRecord.target_fencing_token,
        )
        .where(
            ToolEffectGraphControlRecord.status == "pending",
            ToolEffectGraphControlRecord.target_owner_id == owner_id,
            ToolEffectGraphControlRecord.available_at <= database_now,
        )
        .order_by(
            ToolEffectGraphControlRecord.available_at,
            ToolEffectGraphControlRecord.created_at,
            ToolEffectGraphControlRecord.control_id,
        )
        .limit(batch_size)
        .with_for_update(skip_locked=True, of=ToolEffectGraphControlRecord)
        .cte("locked_graph_controls")
    )
    live_target_graph_id = (
        select(ToolEffectGraphRecord.graph_id)
        .where(
            ToolEffectGraphRecord.graph_id == locked.c.graph_id,
            ToolEffectGraphRecord.lease_owner_id == owner_id,
            ToolEffectGraphRecord.lease_expires_at > database_now,
            ToolEffectGraphRecord.fencing_token == locked.c.target_fencing_token,
            ToolEffectGraphRecord.fencing_token >= 1,
        )
        .correlate(locked)
        .scalar_subquery()
    )
    candidates = (
        select(locked.c.control_id)
        .where(locked.c.graph_id == live_target_graph_id)
        .cte("claimable_graph_controls")
    )
    return (
        update(ToolEffectGraphControlRecord)
        .where(ToolEffectGraphControlRecord.control_id.in_(select(candidates.c.control_id)))
        .values(
            status="processing",
            revision=ToolEffectGraphControlRecord.revision + 1,
            attempt_count=ToolEffectGraphControlRecord.attempt_count + 1,
            claim_owner_id=owner_id,
            claim_acquired_at=database_now,
            claim_expires_at=expires_at,
            claim_fencing_token=ToolEffectGraphControlRecord.claim_fencing_token + 1,
            last_error_code=None,
            updated_at=database_now,
        )
        .returning(ToolEffectGraphControlRecord)
        .execution_options(synchronize_session=False)
    )


def build_postgresql_outbox_claim_statement(
    *,
    owner_id: str,
    database_now: datetime,
    expires_at: datetime,
    batch_size: int,
) -> Update:
    """Claim one ordered Outbox batch in a single PostgreSQL UPDATE."""
    earlier = aliased(OutboxMessageRecord)
    candidates = (
        select(OutboxMessageRecord.message_id)
        .where(
            OutboxMessageRecord.published_at.is_(None),
            OutboxMessageRecord.dead_lettered_at.is_(None),
            OutboxMessageRecord.available_at <= database_now,
            (
                OutboxMessageRecord.claim_owner_id.is_(None)
                | OutboxMessageRecord.claim_expires_at.is_(None)
                | (OutboxMessageRecord.claim_expires_at <= database_now)
            ),
            ~exists(
                select(1).where(
                    earlier.task_id == OutboxMessageRecord.task_id,
                    earlier.published_at.is_(None),
                    earlier.dead_lettered_at.is_(None),
                    earlier.event_seq < OutboxMessageRecord.event_seq,
                )
            ),
        )
        .order_by(
            OutboxMessageRecord.created_at,
            OutboxMessageRecord.task_id,
            OutboxMessageRecord.event_seq,
        )
        .limit(batch_size)
        .with_for_update(skip_locked=True, of=OutboxMessageRecord)
        .cte("claimable_outbox")
    )
    next_fence = OutboxMessageRecord.claim_fencing_token + 1
    delivery_id = func.concat(
        "dlv_",
        func.md5(
            func.concat(
                OutboxMessageRecord.message_id,
                ":",
                next_fence,
                ":",
                owner_id,
            )
        ),
    )
    return (
        update(OutboxMessageRecord)
        .where(OutboxMessageRecord.message_id.in_(select(candidates.c.message_id)))
        .values(
            claim_owner_id=owner_id,
            claim_acquired_at=database_now,
            claim_expires_at=expires_at,
            claim_fencing_token=next_fence,
            delivery_id=delivery_id,
            delivery_attempted_at=database_now,
        )
        .returning(
            OutboxMessageRecord.message_id,
            OutboxMessageRecord.topic,
            OutboxMessageRecord.payload,
            OutboxMessageRecord.attempt_count,
            OutboxMessageRecord.claim_fencing_token,
            OutboxMessageRecord.delivery_id,
            OutboxMessageRecord.delivery_attempted_at,
        )
    )


def build_postgresql_node_lock_statement(
    *,
    graph_id: str,
    node_ids: tuple[str, ...],
    database_now: datetime,
) -> Select[tuple[str]]:
    """Lock a proven node subset without waiting behind another dispatcher."""
    return (
        select(ToolEffectNodeRecord.node_id)
        .where(
            ToolEffectNodeRecord.graph_id == graph_id,
            ToolEffectNodeRecord.node_id.in_(node_ids),
            ToolEffectNodeRecord.status.in_(
                (EffectNodeStatus.PENDING.value, EffectNodeStatus.ACTIVE.value)
            ),
            (
                ToolEffectNodeRecord.claim_owner_id.is_(None)
                | ToolEffectNodeRecord.claim_expires_at.is_(None)
                | (ToolEffectNodeRecord.claim_expires_at <= database_now)
            ),
        )
        .order_by(ToolEffectNodeRecord.node_id)
        .with_for_update(skip_locked=True, of=ToolEffectNodeRecord)
    )


def build_postgresql_node_claim_statement(
    *,
    graph_id: str,
    node_ids: tuple[str, ...],
    owner_id: str,
    database_now: datetime,
    expires_at: datetime,
) -> Update:
    """Update every pre-locked node and return its newly issued fence."""
    return (
        update(ToolEffectNodeRecord)
        .where(
            ToolEffectNodeRecord.graph_id == graph_id,
            ToolEffectNodeRecord.node_id.in_(node_ids),
            ToolEffectNodeRecord.status.in_(
                (EffectNodeStatus.PENDING.value, EffectNodeStatus.ACTIVE.value)
            ),
            (
                ToolEffectNodeRecord.claim_owner_id.is_(None)
                | ToolEffectNodeRecord.claim_expires_at.is_(None)
                | (ToolEffectNodeRecord.claim_expires_at <= database_now)
            ),
        )
        .values(
            status=EffectNodeStatus.ACTIVE.value,
            revision=ToolEffectNodeRecord.revision + 1,
            claim_owner_id=owner_id,
            claim_acquired_at=database_now,
            claim_heartbeat_at=database_now,
            claim_expires_at=expires_at,
            claim_fencing_token=ToolEffectNodeRecord.claim_fencing_token + 1,
            updated_at=database_now,
        )
        .returning(
            ToolEffectNodeRecord.node_id,
            ToolEffectNodeRecord.revision,
            ToolEffectNodeRecord.claim_fencing_token,
        )
        .execution_options(synchronize_session=False)
    )
