from __future__ import annotations

import pytest
from pydantic import ValidationError

from deskpilot.application.task_loop_reducer import (
    TaskLoopReducer,
    TaskLoopReducerNode,
    TaskLoopReducerProofError,
    TaskLoopReducerSnapshot,
)

TASK_ID = f"tsk_{'1' * 32}"
EXECUTION_ID = f"tlx_{'2' * 64}"


def _node(
    suffix: str,
    *,
    local_key: str,
    channel: str,
    status: str,
    depends_on: tuple[str, ...] = (),
    verified_dependencies: tuple[str, ...] = (),
    candidate: bool = False,
    result: bool = False,
) -> TaskLoopReducerNode:
    return TaskLoopReducerNode.model_validate(
        {
            "node_id": f"pnd_{suffix * 64}",
            "local_key": local_key,
            "channel": channel,
            "status": status,
            "depends_on": depends_on,
            "verified_dependency_node_ids": verified_dependencies,
            "candidate_present": candidate,
            "verified_result_present": result,
            "attempt_count": 1 if status != "pending" else 0,
            "max_attempts": 2,
        }
    )


def _snapshot(
    *nodes: TaskLoopReducerNode,
    execution_status: str = "active",
    active_claim_count: int = 0,
    no_progress_count: int = 0,
    budget_exhausted: bool = False,
    deadline_exceeded: bool = False,
    pending_user_revision: int | None = None,
) -> TaskLoopReducerSnapshot:
    return TaskLoopReducerSnapshot.build(
        task_id=TASK_ID,
        execution_id=EXECUTION_ID,
        execution_status=execution_status,
        execution_revision=3,
        nodes=nodes,
        active_claim_count=active_claim_count,
        no_progress_count=no_progress_count,
        budget_exhausted=budget_exhausted,
        deadline_exceeded=deadline_exceeded,
        pending_user_revision=pending_user_revision,
    )


def test_planned_snapshot_can_only_activate() -> None:
    snapshot = TaskLoopReducerSnapshot.build(
        task_id=TASK_ID,
        execution_id=None,
        execution_status="planned",
        execution_revision=0,
    )

    command = TaskLoopReducer().decide(snapshot)

    assert command.kind == "activate_plan"
    assert command.node_id is None


def test_capability_candidate_is_verified_before_any_downstream_node() -> None:
    source = _node(
        "3",
        local_key="s01_knowledge",
        channel="capability",
        status="awaiting_verification",
        candidate=True,
    )
    target = _node(
        "4",
        local_key="s02_mcp",
        channel="capability",
        status="pending",
        depends_on=(source.node_id,),
    )

    command = TaskLoopReducer().decide(_snapshot(source, target))

    assert command.kind == "verify_candidate"
    assert command.node_id == source.node_id


def test_verified_result_unlocks_exact_ready_capability() -> None:
    source = _node(
        "3",
        local_key="s01_knowledge",
        channel="capability",
        status="verified",
        result=True,
    )
    target = _node(
        "4",
        local_key="s02_mcp",
        channel="capability",
        status="ready",
        depends_on=(source.node_id,),
        verified_dependencies=(source.node_id,),
    )

    command = TaskLoopReducer().decide(_snapshot(source, target))

    assert command.kind == "execute_capability"
    assert command.node_id == target.node_id


def test_ready_node_without_verified_dependency_fails_closed() -> None:
    source = _node(
        "3",
        local_key="s01_knowledge",
        channel="capability",
        status="verified",
        result=True,
    )
    target = _node(
        "4",
        local_key="s02_mcp",
        channel="capability",
        status="ready",
        depends_on=(source.node_id,),
    )

    with pytest.raises(TaskLoopReducerProofError):
        TaskLoopReducer().decide(_snapshot(source, target))


def test_active_claim_prevents_parallel_dispatch_from_one_reducer() -> None:
    running = _node(
        "3",
        local_key="s01_knowledge",
        channel="capability",
        status="running",
    )
    ready = _node(
        "4",
        local_key="s02_mcp",
        channel="capability",
        status="ready",
    )

    command = TaskLoopReducer().decide(
        _snapshot(running, ready, active_claim_count=1)
    )

    assert command.kind == "noop"
    assert command.reason_code == "NODE_EXECUTION_IN_FLIGHT"


def test_waiting_user_and_budget_are_stable_commands() -> None:
    waiting = _node(
        "3",
        local_key="s01_agent",
        channel="agent",
        status="waiting_user",
    )
    wait_command = TaskLoopReducer().decide(
        _snapshot(
            waiting,
            execution_status="awaiting_user",
            pending_user_revision=2,
        )
    )
    budget_command = TaskLoopReducer().decide(
        _snapshot(
            _node(
                "4",
                local_key="s02_capability",
                channel="capability",
                status="ready",
            ),
            budget_exhausted=True,
        )
    )

    assert wait_command.kind == "wait_user"
    assert wait_command.node_id == waiting.node_id
    assert budget_command.kind == "terminate_budget_exhausted"


def test_no_progress_requires_three_stable_observations() -> None:
    pending = _node(
        "3",
        local_key="s01_pending",
        channel="agent",
        status="pending",
    )

    before = TaskLoopReducer().decide(_snapshot(pending, no_progress_count=2))
    terminal = TaskLoopReducer().decide(_snapshot(pending, no_progress_count=3))

    assert before.kind == "record_no_progress"
    assert terminal.kind == "terminate_no_progress"


def test_all_verified_nodes_terminate_successfully() -> None:
    source = _node(
        "3",
        local_key="s01_agent",
        channel="agent",
        status="verified",
        result=True,
    )
    control = _node(
        "4",
        local_key="delivery",
        channel="control",
        status="verified",
        depends_on=(source.node_id,),
        verified_dependencies=(source.node_id,),
    )

    command = TaskLoopReducer().decide(_snapshot(source, control))

    assert command.kind == "terminate_success"


def test_snapshot_rejects_cross_plan_edges_and_cycles() -> None:
    first = _node(
        "3",
        local_key="a",
        channel="agent",
        status="pending",
        depends_on=(f"pnd_{'4' * 64}",),
    )
    second = _node(
        "4",
        local_key="b",
        channel="agent",
        status="pending",
        depends_on=(first.node_id,),
    )

    with pytest.raises(ValidationError):
        _snapshot(first, second)


def test_semantic_progress_digest_rejects_tampering() -> None:
    pending = _node(
        "3",
        local_key="s01_pending",
        channel="agent",
        status="pending",
    )
    snapshot = _snapshot(pending)

    with pytest.raises(ValidationError):
        TaskLoopReducerSnapshot.model_validate(
            {
                **snapshot.model_dump(mode="json"),
                "semantic_progress_digest": "f" * 64,
            }
        )
