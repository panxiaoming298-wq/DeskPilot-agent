from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import delete, event, select, update

from deskpilot.application.task_service import EffectReadySetProofRejectedError, TaskService
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.effect_graph import (
    CompensationStrategy,
    EffectDagBranchCondition,
    EffectDagNodeDefinition,
    EffectNodeStatus,
)
from deskpilot.domain.schemas import TaskCreate
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import (
    ToolEffectDagReadyNodeRecord,
    ToolEffectDagReadyStateRecord,
    ToolEffectNodeRecord,
)
from deskpilot.tools.computer import DISK_USAGE_CONTRACT


def _node(
    node_key: str,
    *,
    depends_on: tuple[str, ...] = (),
    condition: EffectDagBranchCondition | None = None,
) -> EffectDagNodeDefinition:
    return EffectDagNodeDefinition(
        node_key=node_key,
        step_id=node_key,
        tool_name=DISK_USAGE_CONTRACT.name,
        tool_version=DISK_USAGE_CONTRACT.version,
        contract_digest=DISK_USAGE_CONTRACT.digest,
        compensation_strategy=CompensationStrategy.NONE,
        depends_on=depends_on,
        conditional_depends_on=(() if condition is None else (condition,)),
    )


async def _succeed(
    service: TaskService,
    task_id: str,
    node_key: str,
    *,
    owner_id: str,
    fencing_token: int,
) -> None:
    ready = await service.checkpoint_effect_dag_ready_set(
        task_id,
        lease_owner_id=owner_id,
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
            lease_owner_id=owner_id,
            fencing_token=fencing_token,
        )
    )[0]
    await service.transition_claimed_effect_node(
        task_id,
        claim.node_id,
        expected_statuses=frozenset({EffectNodeStatus.ACTIVE}),
        target_status=EffectNodeStatus.SUCCEEDED,
        transition_kind="projection_test_succeeded",
        event_type="effect.node.succeeded",
        claim_owner_id=claim.owner_id,
        node_fencing_token=claim.fencing_token,
        lease_owner_id=owner_id,
        fencing_token=fencing_token,
    )


@pytest.mark.asyncio
async def test_ready_v6_checkpoint_and_claim_do_not_reload_the_full_graph(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'ready-projection-page.db').as_posix()}")
    await database.migrate()
    service = TaskService(database, "/api/v1")
    try:
        task = await service.create_task(TaskCreate(goal="bounded projected ready page"))
        graph = await service.create_effect_dag(
            task.task_id,
            tuple(_node(f"root_{index}") for index in range(512)),
        )
        lease = await service.acquire_effect_graph_lease(
            task.task_id,
            owner_id="projection_scheduler",
            ttl_seconds=30,
        )

        async def reject_full_graph_load(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("steady-state ready paging must not load the full graph")

        monkeypatch.setattr(
            TaskService,
            "_load_effect_nodes_and_edges",
            staticmethod(reject_full_graph_load),
        )
        checkpoint = await service.checkpoint_effect_dag_ready_set(
            task.task_id,
            lease_owner_id="projection_scheduler",
            fencing_token=lease.fencing_token,
            page_size=7,
        )
        assert checkpoint.total_ready == 512
        assert len(checkpoint.ready_nodes) == 7
        assert checkpoint.projection_revision == 1
        assert len(checkpoint.projection_digest) == 64

        claim = await service.claim_effect_dag_nodes(
            task.task_id,
            (checkpoint.ready_nodes[0].node_id,),
            ready_proof_digest=checkpoint.proof_digest,
            claim_owner_id="projected_worker",
            claim_ttl_seconds=30,
            lease_owner_id="projection_scheduler",
            fencing_token=lease.fencing_token,
        )
        assert len(claim) == 1
        next_page = await service.checkpoint_effect_dag_ready_set(
            task.task_id,
            lease_owner_id="projection_scheduler",
            fencing_token=lease.fencing_token,
            page_size=7,
        )
        assert next_page.total_ready == 511
        assert next_page.projection_revision == 2

        async with database.session() as session:
            state = await session.get(ToolEffectDagReadyStateRecord, graph.graph_id)
            projected_count = len(
                (
                    await session.scalars(
                        select(ToolEffectDagReadyNodeRecord).where(
                            ToolEffectDagReadyNodeRecord.graph_id == graph.graph_id
                        )
                    )
                ).all()
            )
        assert state is not None
        assert state.revision == 2
        assert state.membership_version == 1
        assert state.projected_node_count == 512
        assert state.ready_node_count == 511
        assert projected_count == 512
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_ready_membership_reconciles_expired_claim_by_database_time(
    tmp_path: Path,
) -> None:
    database = Database(
        f"sqlite+aiosqlite:///{(tmp_path / 'ready-membership-expiry.db').as_posix()}"
    )
    await database.migrate()
    service = TaskService(database, "/api/v1")
    try:
        task = await service.create_task(TaskCreate(goal="reconcile expired ready claim"))
        graph = await service.create_effect_dag(
            task.task_id,
            (_node("first"), _node("second")),
        )
        lease = await service.acquire_effect_graph_lease(
            task.task_id,
            owner_id="expiry_scheduler",
            ttl_seconds=30,
        )
        ready = await service.checkpoint_effect_dag_ready_set(
            task.task_id,
            lease_owner_id="expiry_scheduler",
            fencing_token=lease.fencing_token,
        )
        claimed_node_id = ready.ready_nodes[0].node_id
        await service.claim_effect_dag_nodes(
            task.task_id,
            (claimed_node_id,),
            ready_proof_digest=ready.proof_digest,
            claim_owner_id="expired_worker",
            claim_ttl_seconds=30,
            lease_owner_id="expiry_scheduler",
            fencing_token=lease.fencing_token,
        )
        async with database.session() as session:
            async with session.begin():
                await session.execute(
                    update(ToolEffectNodeRecord)
                    .where(ToolEffectNodeRecord.node_id == claimed_node_id)
                    .values(claim_expires_at=datetime.now(UTC) - timedelta(seconds=1))
                )

        reclaimed = await service.checkpoint_effect_dag_ready_set(
            task.task_id,
            lease_owner_id="expiry_scheduler",
            fencing_token=lease.fencing_token,
        )
        assert reclaimed.total_ready == 2
        assert {item.node_id for item in reclaimed.ready_nodes} == {
            node.node_id for node in graph.nodes
        }
        async with database.session() as session:
            state = await session.get(ToolEffectDagReadyStateRecord, graph.graph_id)
            membership = await session.get(ToolEffectDagReadyNodeRecord, claimed_node_id)
        assert state is not None
        assert state.ready_node_count == 2
        assert state.revision == 3
        assert membership is not None and membership.membership_ready
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_ready_membership_count_and_row_drift_fail_closed(tmp_path: Path) -> None:
    database = Database(
        f"sqlite+aiosqlite:///{(tmp_path / 'ready-membership-drift.db').as_posix()}"
    )
    await database.migrate()
    service = TaskService(database, "/api/v1")
    try:
        task = await service.create_task(TaskCreate(goal="reject ready count drift"))
        graph = await service.create_effect_dag(
            task.task_id,
            (_node("first"), _node("second")),
        )
        lease = await service.acquire_effect_graph_lease(
            task.task_id,
            owner_id="drift_scheduler",
            ttl_seconds=30,
        )
        async with database.session() as session:
            async with session.begin():
                await session.execute(
                    update(ToolEffectDagReadyStateRecord)
                    .where(ToolEffectDagReadyStateRecord.graph_id == graph.graph_id)
                    .values(ready_node_count=1)
                )
        with pytest.raises(EffectReadySetProofRejectedError):
            await service.checkpoint_effect_dag_ready_set(
                task.task_id,
                lease_owner_id="drift_scheduler",
                fencing_token=lease.fencing_token,
            )

        async with database.session() as session:
            async with session.begin():
                await session.execute(
                    update(ToolEffectDagReadyStateRecord)
                    .where(ToolEffectDagReadyStateRecord.graph_id == graph.graph_id)
                    .values(ready_node_count=2)
                )
                await session.execute(
                    update(ToolEffectDagReadyNodeRecord)
                    .where(ToolEffectDagReadyNodeRecord.node_id == graph.nodes[0].node_id)
                    .values(membership_ready=False)
                )
        with pytest.raises(EffectReadySetProofRejectedError):
            await service.checkpoint_effect_dag_ready_set(
                task.task_id,
                lease_owner_id="drift_scheduler",
                fencing_token=lease.fencing_token,
            )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_steady_ready_checkpoint_executes_no_count_query(tmp_path: Path) -> None:
    database = Database(
        f"sqlite+aiosqlite:///{(tmp_path / 'ready-membership-no-count.db').as_posix()}"
    )
    await database.migrate()
    service = TaskService(database, "/api/v1")
    statements: list[str] = []

    def capture_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: object,
    ) -> None:
        statements.append(statement)

    try:
        task = await service.create_task(TaskCreate(goal="avoid steady ready count scan"))
        await service.create_effect_dag(
            task.task_id,
            tuple(_node(f"root_{index}") for index in range(32)),
        )
        lease = await service.acquire_effect_graph_lease(
            task.task_id,
            owner_id="no_count_scheduler",
            ttl_seconds=30,
        )
        event.listen(database.engine.sync_engine, "before_cursor_execute", capture_statement)
        await service.checkpoint_effect_dag_ready_set(
            task.task_id,
            lease_owner_id="no_count_scheduler",
            fencing_token=lease.fencing_token,
            page_size=7,
        )
        assert not any("count(" in statement.lower() for statement in statements)
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture_statement)
        await database.dispose()


@pytest.mark.asyncio
async def test_succeeded_node_updates_only_direct_successor_counters(
    tmp_path: Path,
) -> None:
    database = Database(
        f"sqlite+aiosqlite:///{(tmp_path / 'ready-projection-delta.db').as_posix()}"
    )
    await database.migrate()
    service = TaskService(database, "/api/v1")
    try:
        task = await service.create_task(TaskCreate(goal="increment direct successors"))
        graph = await service.create_effect_dag(
            task.task_id,
            (
                _node("root"),
                _node("left", depends_on=("root",)),
                _node("right", depends_on=("root",)),
                _node("join", depends_on=("left", "right")),
            ),
        )
        lease = await service.acquire_effect_graph_lease(
            task.task_id,
            owner_id="delta_scheduler",
            ttl_seconds=30,
        )
        await _succeed(
            service,
            task.task_id,
            "root",
            owner_id="delta_scheduler",
            fencing_token=lease.fencing_token,
        )

        async with database.session() as session:
            rows = tuple(
                (
                    await session.scalars(
                        select(ToolEffectDagReadyNodeRecord)
                        .where(ToolEffectDagReadyNodeRecord.graph_id == graph.graph_id)
                        .order_by(ToolEffectDagReadyNodeRecord.ordinal)
                    )
                ).all()
            )
        by_key = {graph.nodes[row.ordinal].node_key: row for row in rows}
        assert by_key["root"].revision == 2
        assert by_key["left"].remaining_predecessors == 0
        assert by_key["left"].revision == 2
        assert by_key["right"].remaining_predecessors == 0
        assert by_key["right"].revision == 2
        assert by_key["join"].remaining_predecessors == 2
        assert by_key["join"].revision == 1

        ready = await service.checkpoint_effect_dag_ready_set(
            task.task_id,
            lease_owner_id="delta_scheduler",
            fencing_token=lease.fencing_token,
        )
        assert [proof.node_key for proof in ready.ready_nodes] == ["left", "right"]
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_branch_decision_projects_selected_and_rejected_paths(
    tmp_path: Path,
) -> None:
    database = Database(
        f"sqlite+aiosqlite:///{(tmp_path / 'ready-projection-branch.db').as_posix()}"
    )
    await database.migrate()
    service = TaskService(database, "/api/v1")
    try:
        task = await service.create_task(TaskCreate(goal="project one branch"))
        graph = await service.create_effect_dag(
            task.task_id,
            (
                _node("evaluate"),
                _node(
                    "fast",
                    condition=EffectDagBranchCondition(
                        predecessor_key="evaluate",
                        decision_key="route",
                        expected_outcome="fast",
                    ),
                ),
                _node(
                    "safe",
                    condition=EffectDagBranchCondition(
                        predecessor_key="evaluate",
                        decision_key="route",
                        expected_outcome="safe",
                    ),
                ),
            ),
        )
        lease = await service.acquire_effect_graph_lease(
            task.task_id,
            owner_id="branch_projection_scheduler",
            ttl_seconds=30,
        )
        await _succeed(
            service,
            task.task_id,
            "evaluate",
            owner_id="branch_projection_scheduler",
            fencing_token=lease.fencing_token,
        )
        await service.record_effect_dag_branch_decision(
            task.task_id,
            graph.nodes[0].node_id,
            decision_key="route",
            outcome="fast",
            evidence_digest=sha256_digest({"route": "fast"}),
            lease_owner_id="branch_projection_scheduler",
            fencing_token=lease.fencing_token,
        )

        async with database.session() as session:
            rows = tuple(
                (
                    await session.scalars(
                        select(ToolEffectDagReadyNodeRecord)
                        .where(ToolEffectDagReadyNodeRecord.graph_id == graph.graph_id)
                        .order_by(ToolEffectDagReadyNodeRecord.ordinal)
                    )
                ).all()
            )
        fast, safe = rows[1], rows[2]
        assert fast.remaining_predecessors == 0
        assert fast.unresolved_branches == 0
        assert not fast.branch_rejected
        assert safe.remaining_predecessors == 0
        assert safe.unresolved_branches == 0
        assert safe.branch_rejected

        ready = await service.checkpoint_effect_dag_ready_set(
            task.task_id,
            lease_owner_id="branch_projection_scheduler",
            fencing_token=lease.fencing_token,
        )
        assert [proof.node_key for proof in ready.ready_nodes] == ["fast"]
        assert ready.ready_nodes[0].branch_decisions[0].outcome == "fast"
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_missing_projection_rebuilds_once_and_row_tampering_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(
        f"sqlite+aiosqlite:///{(tmp_path / 'ready-projection-rebuild.db').as_posix()}"
    )
    await database.migrate()
    service = TaskService(database, "/api/v1")
    try:
        task = await service.create_task(TaskCreate(goal="repair ready projection"))
        graph = await service.create_effect_dag(task.task_id, (_node("root"),))
        lease = await service.acquire_effect_graph_lease(
            task.task_id,
            owner_id="repair_scheduler",
            ttl_seconds=30,
        )
        async with database.session() as session:
            async with session.begin():
                await session.execute(
                    delete(ToolEffectDagReadyNodeRecord).where(
                        ToolEffectDagReadyNodeRecord.graph_id == graph.graph_id
                    )
                )
                await session.execute(
                    delete(ToolEffectDagReadyStateRecord).where(
                        ToolEffectDagReadyStateRecord.graph_id == graph.graph_id
                    )
                )

        original = TaskService._load_effect_nodes_and_edges
        rebuild_count = 0

        async def count_rebuild(*args: object, **kwargs: object) -> object:
            nonlocal rebuild_count
            rebuild_count += 1
            return await original(*args, **kwargs)

        monkeypatch.setattr(
            TaskService,
            "_load_effect_nodes_and_edges",
            staticmethod(count_rebuild),
        )
        first = await service.checkpoint_effect_dag_ready_set(
            task.task_id,
            lease_owner_id="repair_scheduler",
            fencing_token=lease.fencing_token,
        )
        second = await service.checkpoint_effect_dag_ready_set(
            task.task_id,
            lease_owner_id="repair_scheduler",
            fencing_token=lease.fencing_token,
        )
        assert first.ready_set_digest == second.ready_set_digest
        assert rebuild_count == 1

        async with database.session() as session:
            async with session.begin():
                await session.execute(
                    update(ToolEffectDagReadyStateRecord)
                    .where(ToolEffectDagReadyStateRecord.graph_id == graph.graph_id)
                    .values(event_seq=999)
                )
        repaired = await service.checkpoint_effect_dag_ready_set(
            task.task_id,
            lease_owner_id="repair_scheduler",
            fencing_token=lease.fencing_token,
        )
        assert repaired.ready_nodes[0].node_key == "root"
        assert rebuild_count == 2

        async with database.session() as session:
            async with session.begin():
                await session.execute(
                    update(ToolEffectDagReadyNodeRecord)
                    .where(ToolEffectDagReadyNodeRecord.graph_id == graph.graph_id)
                    .values(proof_digest="0" * 64)
                )
        with pytest.raises(EffectReadySetProofRejectedError):
            await service.checkpoint_effect_dag_ready_set(
                task.task_id,
                lease_owner_id="repair_scheduler",
                fencing_token=lease.fencing_token,
            )
    finally:
        await database.dispose()
