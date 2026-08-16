from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import update

from deskpilot.application.task_service import (
    EffectNodeFenceRejectedError,
    EffectReadySetProofRejectedError,
    TaskService,
)
from deskpilot.domain.effect_graph import (
    EFFECT_DAG_SCHEMA_VERSION,
    CompensationStrategy,
    EffectDagNodeDefinition,
    EffectNodeStatus,
)
from deskpilot.domain.schemas import TaskCreate
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import (
    ToolEffectNodeRecord,
    ToolEffectReadySetCheckpointRecord,
    utc_now,
)
from deskpilot.tools.computer import DISK_USAGE_CONTRACT


def _node(
    node_key: str,
    *,
    depends_on: tuple[str, ...] = (),
) -> EffectDagNodeDefinition:
    return EffectDagNodeDefinition(
        node_key=node_key,
        step_id=node_key,
        tool_name=DISK_USAGE_CONTRACT.name,
        tool_version=DISK_USAGE_CONTRACT.version,
        contract_digest=DISK_USAGE_CONTRACT.digest,
        compensation_strategy=CompensationStrategy.NONE,
        depends_on=depends_on,
    )


@pytest.mark.asyncio
async def test_parallel_ready_set_survives_restart_and_fences_old_node_owner(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'dag-recovery.db').as_posix()}")
    await database.migrate()
    first = TaskService(database, "/api/v1")
    restarted = TaskService(database, "/api/v1")
    try:
        task = await first.create_task(TaskCreate(goal="parallel DAG recovery"))
        graph = await first.create_effect_dag(
            task.task_id,
            (
                _node("left"),
                _node("right"),
                _node("join", depends_on=("left", "right")),
            ),
        )
        assert graph.schema_version == EFFECT_DAG_SCHEMA_VERSION
        assert graph.current_node_id is None
        assert len(graph.edges) == 4

        lease = await first.acquire_effect_graph_lease(
            task.task_id,
            owner_id="dag_scheduler",
            ttl_seconds=30,
        )
        roots = await first.checkpoint_effect_dag_ready_set(
            task.task_id,
            lease_owner_id="dag_scheduler",
            fencing_token=lease.fencing_token,
        )
        assert [proof.node_key for proof in roots.ready_nodes] == ["left", "right"]
        first_claims = await first.claim_effect_dag_nodes(
            task.task_id,
            tuple(proof.node_id for proof in roots.ready_nodes),
            ready_proof_digest=roots.proof_digest,
            claim_owner_id="worker_before_crash",
            claim_ttl_seconds=30,
            lease_owner_id="dag_scheduler",
            fencing_token=lease.fencing_token,
        )
        assert [claim.fencing_token for claim in first_claims] == [1, 1]

        async with database.session() as session:
            async with session.begin():
                await session.execute(
                    update(ToolEffectNodeRecord)
                    .where(
                        ToolEffectNodeRecord.node_id.in_(
                            tuple(claim.node_id for claim in first_claims)
                        )
                    )
                    .values(claim_expires_at=utc_now() - timedelta(seconds=1))
                )

        recovered_ready = await restarted.checkpoint_effect_dag_ready_set(
            task.task_id,
            lease_owner_id="dag_scheduler",
            fencing_token=lease.fencing_token,
        )
        assert [proof.status for proof in recovered_ready.ready_nodes] == [
            EffectNodeStatus.ACTIVE,
            EffectNodeStatus.ACTIVE,
        ]
        recovered_claims = await restarted.claim_effect_dag_nodes(
            task.task_id,
            tuple(proof.node_id for proof in recovered_ready.ready_nodes),
            ready_proof_digest=recovered_ready.proof_digest,
            claim_owner_id="worker_after_restart",
            claim_ttl_seconds=30,
            lease_owner_id="dag_scheduler",
            fencing_token=lease.fencing_token,
        )
        assert [claim.fencing_token for claim in recovered_claims] == [2, 2]

        with pytest.raises(EffectNodeFenceRejectedError):
            await first.transition_claimed_effect_node(
                task.task_id,
                first_claims[0].node_id,
                expected_statuses=frozenset({EffectNodeStatus.ACTIVE}),
                target_status=EffectNodeStatus.SUCCEEDED,
                transition_kind="stale_worker_finished",
                event_type="effect.node.succeeded",
                claim_owner_id="worker_before_crash",
                node_fencing_token=first_claims[0].fencing_token,
                lease_owner_id="dag_scheduler",
                fencing_token=lease.fencing_token,
            )

        for claim in recovered_claims:
            await restarted.transition_claimed_effect_node(
                task.task_id,
                claim.node_id,
                expected_statuses=frozenset({EffectNodeStatus.ACTIVE}),
                target_status=EffectNodeStatus.SUCCEEDED,
                transition_kind="parallel_node_succeeded",
                event_type="effect.node.succeeded",
                claim_owner_id="worker_after_restart",
                node_fencing_token=claim.fencing_token,
                lease_owner_id="dag_scheduler",
                fencing_token=lease.fencing_token,
            )

        join_ready = await restarted.checkpoint_effect_dag_ready_set(
            task.task_id,
            lease_owner_id="dag_scheduler",
            fencing_token=lease.fencing_token,
        )
        assert [proof.node_key for proof in join_ready.ready_nodes] == ["join"]
        assert [predecessor.node_key for predecessor in join_ready.ready_nodes[0].predecessors] == [
            "left",
            "right",
        ]
        assert all(
            predecessor.status is EffectNodeStatus.SUCCEEDED
            for predecessor in join_ready.ready_nodes[0].predecessors
        )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_ready_set_pages_bind_cursor_membership_and_claim_scope(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'dag-ready-pages.db').as_posix()}")
    await database.migrate()
    service = TaskService(database, "/api/v1")
    task = await service.create_task(TaskCreate(goal="page ready proofs"))
    await service.create_effect_dag(
        task.task_id,
        tuple(_node(f"root_{index}") for index in range(7)),
    )
    lease = await service.acquire_effect_graph_lease(
        task.task_id,
        owner_id="paged_scheduler",
        ttl_seconds=30,
    )
    try:
        pages = []
        cursor: str | None = None
        while True:
            page = await service.checkpoint_effect_dag_ready_set(
                task.task_id,
                lease_owner_id="paged_scheduler",
                fencing_token=lease.fencing_token,
                page_size=3,
                cursor=cursor,
            )
            pages.append(page)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor

        assert [len(page.ready_nodes) for page in pages] == [3, 3, 1]
        assert [page.after_ordinal for page in pages] == [None, 2, 5]
        assert [page.last_ordinal for page in pages] == [2, 5, 6]
        assert [page.cursor for page in pages] == [
            None,
            pages[0].checkpoint_id,
            pages[1].checkpoint_id,
        ]
        assert [page.has_more for page in pages] == [True, True, False]
        assert {page.total_ready for page in pages} == {7}
        assert len({page.ready_set_digest for page in pages}) == 1
        assert len({page.proof_digest for page in pages}) == 3
        assert [proof.node_key for page in pages for proof in page.ready_nodes] == [
            f"root_{index}" for index in range(7)
        ]

        with pytest.raises(EffectReadySetProofRejectedError):
            await service.claim_effect_dag_nodes(
                task.task_id,
                (pages[1].ready_nodes[0].node_id,),
                ready_proof_digest=pages[0].proof_digest,
                claim_owner_id="paged_worker",
                claim_ttl_seconds=30,
                lease_owner_id="paged_scheduler",
                fencing_token=lease.fencing_token,
            )

        claims = await service.claim_effect_dag_nodes(
            task.task_id,
            tuple(proof.node_id for proof in pages[0].ready_nodes),
            ready_proof_digest=pages[0].proof_digest,
            claim_owner_id="paged_worker",
            claim_ttl_seconds=30,
            lease_owner_id="paged_scheduler",
            fencing_token=lease.fencing_token,
        )
        assert len(claims) == 3
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_ready_v5_keyset_cursor_rejects_cross_snapshot_and_tampering(
    tmp_path: Path,
) -> None:
    database = Database(
        f"sqlite+aiosqlite:///{(tmp_path / 'dag-ready-keyset-proof.db').as_posix()}"
    )
    await database.migrate()
    service = TaskService(database, "/api/v1")
    first_task = await service.create_task(TaskCreate(goal="keyset cursor source"))
    second_task = await service.create_task(TaskCreate(goal="keyset cursor target"))
    await service.create_effect_dag(
        first_task.task_id,
        tuple(_node(f"root_{index}") for index in range(5)),
    )
    await service.create_effect_dag(
        second_task.task_id,
        tuple(_node(f"other_{index}") for index in range(5)),
    )
    first_lease = await service.acquire_effect_graph_lease(
        first_task.task_id,
        owner_id="keyset_first",
        ttl_seconds=30,
    )
    second_lease = await service.acquire_effect_graph_lease(
        second_task.task_id,
        owner_id="keyset_second",
        ttl_seconds=30,
    )
    try:
        first_page = await service.checkpoint_effect_dag_ready_set(
            first_task.task_id,
            lease_owner_id="keyset_first",
            fencing_token=first_lease.fencing_token,
            page_size=2,
        )
        assert first_page.next_cursor == first_page.checkpoint_id

        with pytest.raises(EffectReadySetProofRejectedError):
            await service.checkpoint_effect_dag_ready_set(
                second_task.task_id,
                lease_owner_id="keyset_second",
                fencing_token=second_lease.fencing_token,
                page_size=2,
                cursor=first_page.next_cursor,
            )

        forged_checkpoint_id = f"ter_{'f' * 64}"
        async with database.session() as session:
            async with session.begin():
                await session.execute(
                    update(ToolEffectReadySetCheckpointRecord)
                    .where(
                        ToolEffectReadySetCheckpointRecord.checkpoint_id
                        == first_page.checkpoint_id
                    )
                    .values(checkpoint_id=forged_checkpoint_id)
                )

        with pytest.raises(EffectReadySetProofRejectedError):
            await service.checkpoint_effect_dag_ready_set(
                first_task.task_id,
                lease_owner_id="keyset_first",
                fencing_token=first_lease.fencing_token,
                page_size=2,
                cursor=forged_checkpoint_id,
            )

        async with database.session() as session:
            async with session.begin():
                await session.execute(
                    update(ToolEffectReadySetCheckpointRecord)
                    .where(
                        ToolEffectReadySetCheckpointRecord.checkpoint_id
                        == forged_checkpoint_id
                    )
                    .values(checkpoint_id=first_page.checkpoint_id)
                )
                checkpoint = await session.get(
                    ToolEffectReadySetCheckpointRecord,
                    first_page.checkpoint_id,
                )
                assert checkpoint is not None
                checkpoint.predecessor_proof = {
                    **checkpoint.predecessor_proof,
                    "last_ordinal": 99,
                }

        with pytest.raises(EffectReadySetProofRejectedError):
            await service.checkpoint_effect_dag_ready_set(
                first_task.task_id,
                lease_owner_id="keyset_first",
                fencing_token=first_lease.fencing_token,
                page_size=2,
                cursor=first_page.next_cursor,
            )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_effect_dag_rejects_cycles_before_persistence(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'dag-cycle.db').as_posix()}")
    await database.migrate()
    service = TaskService(database, "/api/v1")
    try:
        task = await service.create_task(TaskCreate(goal="reject cyclic DAG"))
        with pytest.raises(ValueError, match="cycle"):
            await service.create_effect_dag(
                task.task_id,
                (
                    _node("left", depends_on=("right",)),
                    _node("right", depends_on=("left",)),
                ),
            )
    finally:
        await database.dispose()
