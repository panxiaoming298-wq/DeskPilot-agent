from pathlib import Path

import pytest
from sqlalchemy import update

from deskpilot.application.task_service import (
    EffectBranchDecisionConflictError,
    EffectBranchDecisionProofRejectedError,
    EffectReadySetProofRejectedError,
    TaskService,
)
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.effect_graph import (
    CompensationStrategy,
    EffectDagBranchCondition,
    EffectDagNodeDefinition,
    EffectGraphStatus,
    EffectNodeStatus,
)
from deskpilot.domain.schemas import TaskCreate
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import ToolEffectBranchDecisionRecord
from deskpilot.tools.computer import DISK_USAGE_CONTRACT


def _node(
    node_key: str,
    *,
    depends_on: tuple[str, ...] = (),
    when: EffectDagBranchCondition | None = None,
) -> EffectDagNodeDefinition:
    return EffectDagNodeDefinition(
        node_key=node_key,
        step_id=node_key,
        tool_name=DISK_USAGE_CONTRACT.name,
        tool_version=DISK_USAGE_CONTRACT.version,
        contract_digest=DISK_USAGE_CONTRACT.digest,
        compensation_strategy=CompensationStrategy.NONE,
        depends_on=depends_on,
        conditional_depends_on=(() if when is None else (when,)),
    )


def _route(outcome: str) -> EffectDagBranchCondition:
    return EffectDagBranchCondition(
        predecessor_key="evaluate",
        decision_key="route",
        expected_outcome=outcome,
    )


async def _succeed_ready_node(
    service: TaskService,
    task_id: str,
    node_key: str,
    *,
    lease_owner_id: str,
    fencing_token: int,
) -> None:
    ready = await service.checkpoint_effect_dag_ready_set(
        task_id,
        lease_owner_id=lease_owner_id,
        fencing_token=fencing_token,
    )
    proof = next(item for item in ready.ready_nodes if item.node_key == node_key)
    claim = (
        await service.claim_effect_dag_nodes(
            task_id,
            (proof.node_id,),
            ready_proof_digest=ready.proof_digest,
            claim_owner_id=f"worker_{node_key}",
            claim_ttl_seconds=30,
            lease_owner_id=lease_owner_id,
            fencing_token=fencing_token,
        )
    )[0]
    await service.transition_claimed_effect_node(
        task_id,
        claim.node_id,
        expected_statuses=frozenset({EffectNodeStatus.ACTIVE}),
        target_status=EffectNodeStatus.SUCCEEDED,
        transition_kind="test_succeeded",
        event_type="effect.node.succeeded",
        claim_owner_id=claim.owner_id,
        node_fencing_token=claim.fencing_token,
        lease_owner_id=lease_owner_id,
        fencing_token=fencing_token,
    )


@pytest.mark.asyncio
async def test_branch_decision_selects_one_path_and_survives_restart(
    tmp_path: Path,
) -> None:
    database = Database(
        f"sqlite+aiosqlite:///{(tmp_path / 'branch-restart.db').as_posix()}"
    )
    await database.migrate()
    first = TaskService(database, "/api/v1")
    restarted = TaskService(database, "/api/v1")
    try:
        task = await first.create_task(TaskCreate(goal="durable conditional branch"))
        graph = await first.create_effect_dag(
            task.task_id,
            (
                _node("evaluate"),
                _node("fast", when=_route("fast")),
                _node("safe", when=_route("safe")),
                _node("safe_child", depends_on=("safe",)),
            ),
        )
        assert len(graph.edges) == 6
        lease = await first.acquire_effect_graph_lease(
            task.task_id,
            owner_id="branch_scheduler",
            ttl_seconds=30,
        )
        await _succeed_ready_node(
            first,
            task.task_id,
            "evaluate",
            lease_owner_id="branch_scheduler",
            fencing_token=lease.fencing_token,
        )

        unresolved = await first.checkpoint_effect_dag_ready_set(
            task.task_id,
            lease_owner_id="branch_scheduler",
            fencing_token=lease.fencing_token,
        )
        assert unresolved.ready_nodes == ()
        evidence_digest = sha256_digest({"trusted_route": "fast"})
        decision = await restarted.record_effect_dag_branch_decision(
            task.task_id,
            graph.nodes[0].node_id,
            decision_key="route",
            outcome="fast",
            evidence_digest=evidence_digest,
            lease_owner_id="branch_scheduler",
            fencing_token=lease.fencing_token,
        )
        assert decision.decision_id == f"tbd_{decision.proof_digest}"
        assert decision.source_node_key == "evaluate"

        event_count = len(await restarted.list_events(task.task_id))
        graph_revision = (await restarted.get_effect_graph(task.task_id)).revision
        retried = await restarted.record_effect_dag_branch_decision(
            task.task_id,
            graph.nodes[0].node_id,
            decision_key="route",
            outcome="fast",
            evidence_digest=evidence_digest,
            lease_owner_id="branch_scheduler",
            fencing_token=lease.fencing_token,
        )
        assert retried == decision
        assert len(await restarted.list_events(task.task_id)) == event_count
        assert (await restarted.get_effect_graph(task.task_id)).revision == graph_revision

        with pytest.raises(EffectBranchDecisionConflictError):
            await restarted.record_effect_dag_branch_decision(
                task.task_id,
                graph.nodes[0].node_id,
                decision_key="route",
                outcome="safe",
                evidence_digest=sha256_digest({"trusted_route": "safe"}),
                lease_owner_id="branch_scheduler",
                fencing_token=lease.fencing_token,
            )
        with pytest.raises(EffectReadySetProofRejectedError):
            await restarted.claim_effect_dag_nodes(
                task.task_id,
                (graph.nodes[1].node_id,),
                ready_proof_digest=unresolved.proof_digest,
                claim_owner_id="stale_branch_worker",
                claim_ttl_seconds=30,
                lease_owner_id="branch_scheduler",
                fencing_token=lease.fencing_token,
            )

        reduced = await restarted.reduce_effect_dag(
            task.task_id,
            lease_owner_id="branch_scheduler",
            fencing_token=lease.fencing_token,
        )
        status_by_key = {node.node_key: node.status for node in reduced.nodes}
        assert status_by_key["safe"] is EffectNodeStatus.SKIPPED
        assert status_by_key["safe_child"] is EffectNodeStatus.SKIPPED
        assert status_by_key["fast"] is EffectNodeStatus.PENDING

        selected = await restarted.checkpoint_effect_dag_ready_set(
            task.task_id,
            lease_owner_id="branch_scheduler",
            fencing_token=lease.fencing_token,
        )
        assert [proof.node_key for proof in selected.ready_nodes] == ["fast"]
        assert selected.ready_nodes[0].branch_decisions[0].decision_id == (
            decision.decision_id
        )
        recovered_graph = await restarted.get_effect_graph(task.task_id)
        assert recovered_graph.branch_decisions == (decision,)

        await _succeed_ready_node(
            restarted,
            task.task_id,
            "fast",
            lease_owner_id="branch_scheduler",
            fencing_token=lease.fencing_token,
        )
        terminal = await restarted.reduce_effect_dag(
            task.task_id,
            lease_owner_id="branch_scheduler",
            fencing_token=lease.fencing_token,
        )
        assert terminal.status is EffectGraphStatus.SUCCEEDED
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_branch_decision_proof_tampering_is_rejected(tmp_path: Path) -> None:
    database = Database(
        f"sqlite+aiosqlite:///{(tmp_path / 'branch-tamper.db').as_posix()}"
    )
    await database.migrate()
    service = TaskService(database, "/api/v1")
    try:
        task = await service.create_task(TaskCreate(goal="reject branch tampering"))
        graph = await service.create_effect_dag(
            task.task_id,
            (_node("evaluate"), _node("fast", when=_route("fast"))),
        )
        lease = await service.acquire_effect_graph_lease(
            task.task_id, owner_id="tamper_scheduler", ttl_seconds=30
        )
        await _succeed_ready_node(
            service,
            task.task_id,
            "evaluate",
            lease_owner_id="tamper_scheduler",
            fencing_token=lease.fencing_token,
        )
        await service.record_effect_dag_branch_decision(
            task.task_id,
            graph.nodes[0].node_id,
            decision_key="route",
            outcome="fast",
            evidence_digest=sha256_digest({"trusted_route": "fast"}),
            lease_owner_id="tamper_scheduler",
            fencing_token=lease.fencing_token,
        )
        async with database.session() as session:
            async with session.begin():
                await session.execute(
                    update(ToolEffectBranchDecisionRecord).values(
                        proof_digest="0" * 64
                    )
                )

        with pytest.raises(EffectBranchDecisionProofRejectedError):
            await service.checkpoint_effect_dag_ready_set(
                task.task_id,
                lease_owner_id="tamper_scheduler",
                fencing_token=lease.fencing_token,
            )
    finally:
        await database.dispose()


def test_conditional_dependencies_participate_in_cycle_validation() -> None:
    with pytest.raises(ValueError, match="cycle"):
        TaskService._validate_effect_dag(
            (
                _node(
                    "left",
                    when=EffectDagBranchCondition(
                        predecessor_key="right",
                        decision_key="route",
                        expected_outcome="left",
                    ),
                ),
                _node("right", depends_on=("left",)),
            )
        )
