"""Pure deterministic reducer for one persistent model-planner task loop.

The reducer deliberately receives no arguments, paths, executables, environment
variables, authority manifests, or result payloads.  It may select only a
server-persisted node identifier and a bounded command.  The runtime remains
responsible for reloading and validating every proof before carrying out that
command.
"""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_contracts import DIGEST_PATTERN
from deskpilot.domain.task_loop_execution import TASK_LOOP_EXECUTION_ID_PATTERN
from deskpilot.domain.task_plans import PLAN_NODE_ID_PATTERN, TASK_ID_PATTERN

TaskLoopReducerExecutionStatus = Literal[
    "planned",
    "active",
    "paused",
    "awaiting_user",
    "repairing",
    "failed",
    "succeeded",
    "cancelled",
    "budget_exhausted",
    "no_progress",
]
TaskLoopReducerNodeChannel = Literal["agent", "capability", "control"]
TaskLoopReducerNodeStatus = Literal[
    "pending",
    "ready",
    "claimed",
    "running",
    "waiting_user",
    "waiting_children",
    "awaiting_verification",
    "verified",
    "failed",
    "cancelled",
]
TaskLoopReducerCommandKind = Literal[
    "activate_plan",
    "execute_capability",
    "execute_agent",
    "verify_candidate",
    "reduce_control_node",
    "wait_user",
    "record_no_progress",
    "start_repair",
    "terminate_success",
    "terminate_failure",
    "terminate_budget_exhausted",
    "terminate_no_progress",
    "noop",
]

_TERMINAL_EXECUTION_STATUSES = frozenset(
    {"failed", "succeeded", "cancelled", "budget_exhausted", "no_progress"}
)
_ACTIVE_NODE_STATUSES = frozenset({"claimed", "running"})


class TaskLoopReducerProofError(RuntimeError):
    code = "TASK_LOOP_REDUCER_PROOF_REJECTED"


class TaskLoopReducerNode(BaseModel):
    """Payload-free node state assembled from persisted execution truth."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str = Field(pattern=PLAN_NODE_ID_PATTERN)
    local_key: str = Field(min_length=1, max_length=64)
    channel: TaskLoopReducerNodeChannel
    status: TaskLoopReducerNodeStatus
    depends_on: tuple[str, ...] = Field(default=(), max_length=20)
    verified_dependency_node_ids: tuple[str, ...] = Field(default=(), max_length=20)
    candidate_present: bool = False
    verified_result_present: bool = False
    attempt_count: int = Field(default=0, ge=0, le=16)
    max_attempts: int = Field(default=1, ge=1, le=16)

    @model_validator(mode="after")
    def lifecycle_matches(self) -> Self:
        if self.node_id in self.depends_on:
            raise ValueError("Task-loop reducer node depends on itself")
        if len(self.depends_on) != len(set(self.depends_on)):
            raise ValueError("Task-loop reducer node dependencies must be unique")
        if tuple(sorted(self.depends_on)) != self.depends_on:
            raise ValueError("Task-loop reducer node dependencies must be canonical")
        if len(self.verified_dependency_node_ids) != len(
            set(self.verified_dependency_node_ids)
        ):
            raise ValueError("Verified dependency nodes must be unique")
        if tuple(sorted(self.verified_dependency_node_ids)) != (
            self.verified_dependency_node_ids
        ):
            raise ValueError("Verified dependency nodes must be canonical")
        if not set(self.verified_dependency_node_ids).issubset(self.depends_on):
            raise ValueError("Verified dependency proof is not a declared Plan edge")
        if self.candidate_present != (self.status == "awaiting_verification"):
            raise ValueError("Candidate presence does not match node verification state")
        if self.status == "verified" and self.channel != "control":
            if not self.verified_result_present:
                raise ValueError("Verified business node has no persistent ResultRef")
        elif self.verified_result_present:
            raise ValueError("Only a verified business node may expose a ResultRef")
        if self.attempt_count > self.max_attempts:
            raise ValueError("Task-loop node attempt budget was exceeded")
        return self

    @property
    def dependencies_verified(self) -> bool:
        return self.verified_dependency_node_ids == self.depends_on


class TaskLoopReducerSnapshot(BaseModel):
    """Canonical, payload-free snapshot used for one reducer decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.task-loop-reducer-snapshot.v1"] = (
        "deskpilot.task-loop-reducer-snapshot.v1"
    )
    task_id: str = Field(pattern=TASK_ID_PATTERN)
    execution_id: str | None = Field(default=None, pattern=TASK_LOOP_EXECUTION_ID_PATTERN)
    execution_status: TaskLoopReducerExecutionStatus
    execution_revision: int = Field(ge=0)
    nodes: tuple[TaskLoopReducerNode, ...] = Field(default=(), max_length=20)
    active_claim_count: int = Field(default=0, ge=0, le=20)
    no_progress_count: int = Field(default=0, ge=0, le=3)
    repair_count: int = Field(default=0, ge=0, le=2)
    repair_available: bool = False
    budget_exhausted: bool = False
    deadline_exceeded: bool = False
    pending_user_revision: int | None = Field(default=None, ge=1)
    semantic_progress_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def scope_and_progress_match(self) -> Self:
        activated = self.execution_status != "planned"
        if activated != (self.execution_id is not None):
            raise ValueError("Reducer activation state and execution id differ")
        if self.execution_status == "planned" and (
            self.execution_revision != 0
            or self.nodes
            or self.active_claim_count != 0
            or self.repair_count != 0
            or self.repair_available
        ):
            raise ValueError("Planned reducer snapshot cannot contain execution state")
        node_ids = tuple(item.node_id for item in self.nodes)
        local_keys = tuple(item.local_key for item in self.nodes)
        if len(node_ids) != len(set(node_ids)) or len(local_keys) != len(set(local_keys)):
            raise ValueError("Reducer snapshot contains duplicate execution nodes")
        known = set(node_ids)
        if any(not set(item.depends_on).issubset(known) for item in self.nodes):
            raise ValueError("Reducer snapshot contains a cross-Plan dependency")
        self._reject_cycles()
        actual_active = sum(item.status in _ACTIVE_NODE_STATUSES for item in self.nodes)
        if actual_active != self.active_claim_count:
            raise ValueError("Reducer active-claim count differs from node truth")
        waiting = any(item.status == "waiting_user" for item in self.nodes)
        if self.execution_status == "awaiting_user":
            if not waiting or self.pending_user_revision is None:
                raise ValueError("Awaiting-user execution has no pending user proof")
        elif self.pending_user_revision is not None:
            raise ValueError("Pending user revision appears outside awaiting-user state")
        failed = tuple(item for item in self.nodes if item.status == "failed")
        if self.repair_available and (
            len(failed) != 1
            or failed[0].attempt_count >= failed[0].max_attempts
            or self.repair_count >= 2
            or self.budget_exhausted
            or self.deadline_exceeded
        ):
            raise ValueError("Task-loop repair availability has no bounded failure proof")
        expected_progress = self.build_progress_digest(
            task_id=self.task_id,
            execution_id=self.execution_id,
            execution_status=self.execution_status,
            execution_revision=self.execution_revision,
            nodes=self.nodes,
            repair_count=self.repair_count,
            repair_available=self.repair_available,
            budget_exhausted=self.budget_exhausted,
            deadline_exceeded=self.deadline_exceeded,
            pending_user_revision=self.pending_user_revision,
        )
        if self.semantic_progress_digest != expected_progress:
            raise ValueError("Reducer semantic progress digest does not match")
        return self

    def _reject_cycles(self) -> None:
        dependencies = {item.node_id: set(item.depends_on) for item in self.nodes}
        remaining = set(dependencies)
        while remaining:
            ready = {
                node_id
                for node_id in remaining
                if not (dependencies[node_id] & remaining)
            }
            if not ready:
                raise ValueError("Reducer snapshot dependency graph contains a cycle")
            remaining -= ready

    @classmethod
    def build_progress_digest(
        cls,
        *,
        task_id: str,
        execution_id: str | None,
        execution_status: TaskLoopReducerExecutionStatus,
        execution_revision: int,
        nodes: tuple[TaskLoopReducerNode, ...],
        repair_count: int,
        repair_available: bool,
        budget_exhausted: bool,
        deadline_exceeded: bool,
        pending_user_revision: int | None,
    ) -> str:
        material: dict[str, Any] = {
            "task_id": task_id,
            "execution_id": execution_id,
            "execution_status": execution_status,
            "execution_revision": execution_revision,
            "nodes": [
                item.model_dump(mode="json")
                for item in sorted(nodes, key=lambda value: value.node_id)
            ],
            "repair_count": repair_count,
            "repair_available": repair_available,
            "budget_exhausted": budget_exhausted,
            "deadline_exceeded": deadline_exceeded,
            "pending_user_revision": pending_user_revision,
        }
        return sha256_digest(material)

    @classmethod
    def build(cls, **values: Any) -> Self:
        progress = cls.build_progress_digest(
            task_id=values["task_id"],
            execution_id=values.get("execution_id"),
            execution_status=values["execution_status"],
            execution_revision=values["execution_revision"],
            nodes=values.get("nodes", ()),
            repair_count=values.get("repair_count", 0),
            repair_available=values.get("repair_available", False),
            budget_exhausted=values.get("budget_exhausted", False),
            deadline_exceeded=values.get("deadline_exceeded", False),
            pending_user_revision=values.get("pending_user_revision"),
        )
        return cls(**values, semantic_progress_digest=progress)


class TaskLoopReducerCommand(BaseModel):
    """A bounded reducer decision; the runtime must revalidate before acting."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.task-loop-reducer-command.v1"] = (
        "deskpilot.task-loop-reducer-command.v1"
    )
    kind: TaskLoopReducerCommandKind
    node_id: str | None = Field(default=None, pattern=PLAN_NODE_ID_PATTERN)
    expected_execution_revision: int = Field(ge=0)
    reason_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,99}$")
    source_progress_digest: str = Field(pattern=DIGEST_PATTERN)
    command_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def target_and_digest_match(self) -> Self:
        targeted = self.kind in {
            "execute_capability",
            "execute_agent",
            "verify_candidate",
            "reduce_control_node",
            "wait_user",
            "start_repair",
        }
        if targeted != (self.node_id is not None):
            raise ValueError("Reducer command target shape is invalid")
        material = self.model_dump(mode="json", exclude={"command_digest"})
        if self.command_digest != sha256_digest(material):
            raise ValueError("Reducer command digest does not match")
        return self

    @classmethod
    def build(
        cls,
        *,
        snapshot: TaskLoopReducerSnapshot,
        kind: TaskLoopReducerCommandKind,
        reason_code: str,
        node_id: str | None = None,
    ) -> Self:
        values = {
            "schema_version": "deskpilot.task-loop-reducer-command.v1",
            "kind": kind,
            "node_id": node_id,
            "expected_execution_revision": snapshot.execution_revision,
            "reason_code": reason_code,
            "source_progress_digest": snapshot.semantic_progress_digest,
        }
        return cls.model_validate(
            {**values, "command_digest": sha256_digest(values)}
        )


class TaskLoopReducer:
    """Choose one stable transition from persisted, proof-checked state."""

    def decide(self, snapshot: TaskLoopReducerSnapshot) -> TaskLoopReducerCommand:
        if snapshot.execution_status in _TERMINAL_EXECUTION_STATUSES:
            return self._command(snapshot, "noop", "EXECUTION_ALREADY_TERMINAL")
        if snapshot.execution_status == "planned":
            return self._command(snapshot, "activate_plan", "SEALED_PLAN_READY")
        if snapshot.execution_status == "paused":
            return self._command(snapshot, "noop", "EXECUTION_PAUSED")
        if snapshot.budget_exhausted:
            return self._command(
                snapshot,
                "terminate_budget_exhausted",
                "TASK_LOOP_BUDGET_EXHAUSTED",
            )
        if snapshot.deadline_exceeded:
            return self._command(snapshot, "terminate_failure", "TASK_LOOP_DEADLINE_EXCEEDED")

        failed = self._first(snapshot, status="failed")
        if failed is not None:
            if snapshot.repair_available:
                return self._command(
                    snapshot,
                    "start_repair",
                    "TASK_LOOP_BOUNDED_REPAIR_AVAILABLE",
                    node_id=failed.node_id,
                )
            return self._command(
                snapshot,
                "terminate_failure",
                "TASK_LOOP_NODE_FAILED",
            )
        waiting = self._first(snapshot, status="waiting_user")
        if snapshot.execution_status == "awaiting_user" or waiting is not None:
            if waiting is None:
                raise TaskLoopReducerProofError(
                    "Awaiting-user execution has no exact waiting node"
                )
            return self._command(
                snapshot,
                "wait_user",
                "TASK_LOOP_WAITING_USER",
                node_id=waiting.node_id,
            )

        candidate = self._first(snapshot, status="awaiting_verification")
        if candidate is not None:
            if not candidate.dependencies_verified:
                raise TaskLoopReducerProofError(
                    "Capability candidate lost a verified dependency"
                )
            return self._command(
                snapshot,
                "verify_candidate",
                "CANDIDATE_READY_FOR_VERIFICATION",
                node_id=candidate.node_id,
            )
        if snapshot.active_claim_count:
            return self._command(snapshot, "noop", "NODE_EXECUTION_IN_FLIGHT")

        ready = self._first(snapshot, status="ready")
        if ready is not None:
            if not ready.dependencies_verified:
                raise TaskLoopReducerProofError(
                    "Ready node has no complete verified dependency set"
                )
            kind_by_channel: dict[
                TaskLoopReducerNodeChannel, TaskLoopReducerCommandKind
            ] = {
                "capability": "execute_capability",
                "agent": "execute_agent",
                "control": "reduce_control_node",
            }
            reason_by_channel = {
                "capability": "CAPABILITY_NODE_READY",
                "agent": "AGENT_NODE_READY",
                "control": "CONTROL_NODE_READY",
            }
            return self._command(
                snapshot,
                kind_by_channel[ready.channel],
                reason_by_channel[ready.channel],
                node_id=ready.node_id,
            )

        if snapshot.nodes and all(item.status == "verified" for item in snapshot.nodes):
            return self._command(snapshot, "terminate_success", "TASK_LOOP_VERIFIED")
        if snapshot.no_progress_count >= 3:
            return self._command(
                snapshot,
                "terminate_no_progress",
                "TASK_LOOP_NO_PROGRESS",
            )
        return self._command(snapshot, "record_no_progress", "NO_RUNNABLE_NODE")

    @staticmethod
    def _first(
        snapshot: TaskLoopReducerSnapshot,
        *,
        status: TaskLoopReducerNodeStatus,
    ) -> TaskLoopReducerNode | None:
        candidates = tuple(item for item in snapshot.nodes if item.status == status)
        return min(candidates, key=lambda item: (item.local_key, item.node_id), default=None)

    @staticmethod
    def _command(
        snapshot: TaskLoopReducerSnapshot,
        kind: TaskLoopReducerCommandKind,
        reason_code: str,
        *,
        node_id: str | None = None,
    ) -> TaskLoopReducerCommand:
        return TaskLoopReducerCommand.build(
            snapshot=snapshot,
            kind=kind,
            reason_code=reason_code,
            node_id=node_id,
        )


__all__ = [
    "TaskLoopReducer",
    "TaskLoopReducerCommand",
    "TaskLoopReducerCommandKind",
    "TaskLoopReducerExecutionStatus",
    "TaskLoopReducerNode",
    "TaskLoopReducerNodeChannel",
    "TaskLoopReducerNodeStatus",
    "TaskLoopReducerProofError",
    "TaskLoopReducerSnapshot",
]
