"""Bounded effect-ready projection queries shared by runtime and verification."""

from sqlalchemy import Select, select
from sqlalchemy.sql.elements import ColumnElement

from deskpilot.infrastructure.models import (
    ToolEffectDagReadyNodeRecord,
    ToolEffectNodeRecord,
)


def _effect_ready_conditions(
    *,
    graph_id: str,
) -> tuple[ColumnElement[bool], ...]:
    return (
        ToolEffectDagReadyNodeRecord.graph_id == graph_id,
        ToolEffectDagReadyNodeRecord.membership_ready.is_(True),
    )


def build_effect_ready_page_statement(
    *,
    graph_id: str,
    page_size: int,
    after_ordinal: int | None,
) -> Select[tuple[ToolEffectNodeRecord, ToolEffectDagReadyNodeRecord]]:
    """Read one ordinal keyset page plus a bounded has-more sentinel row."""
    conditions = list(
        _effect_ready_conditions(
            graph_id=graph_id,
        )
    )
    if after_ordinal is not None:
        conditions.append(ToolEffectDagReadyNodeRecord.ordinal > after_ordinal)
    return (
        select(ToolEffectNodeRecord, ToolEffectDagReadyNodeRecord)
        .join(
            ToolEffectDagReadyNodeRecord,
            ToolEffectDagReadyNodeRecord.node_id == ToolEffectNodeRecord.node_id,
        )
        .where(*conditions)
        .order_by(ToolEffectDagReadyNodeRecord.ordinal)
        .limit(page_size + 1)
    )
