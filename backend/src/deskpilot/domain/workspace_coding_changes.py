"""Immutable Reader-evidence to change-proposal and write-Plan proofs."""

from __future__ import annotations

from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_contracts import DIGEST_PATTERN, BoundAgentRef
from deskpilot.domain.task_plans import (
    MESSAGE_ID_PATTERN,
    PLAN_ID_PATTERN,
    PLAN_NODE_ID_PATTERN,
    TASK_ID_PATTERN,
    DraftPlan,
    ExecutablePlan,
    TaskContract,
)
from deskpilot.domain.workspace_coding_explorations import (
    WORKSPACE_CODING_FILE_SET_BINDING_ID_PATTERN,
)

WORKSPACE_CODING_CHANGE_RUN_BINDING_ID_PATTERN = r"^wcr_[0-9a-f]{64}$"
WORKSPACE_CODING_CHANGE_PROPOSAL_ID_PATTERN = r"^wcp_[0-9a-f]{64}$"
WORKSPACE_CODING_CHANGE_TURN_PROOF_ID_PATTERN = r"^wct_[0-9a-f]{64}$"
WORKSPACE_CODING_WRITE_PLAN_BINDING_ID_PATTERN = r"^wcw_[0-9a-f]{64}$"


def _content_id(prefix: str, material: Any) -> str:
    return f"{prefix}_{sha256_digest(material)}"


def _safe_relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        value != value.strip()
        or not value
        or "\\" in value
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(character in value for character in ("\x00", "\r", "\n", ":"))
    ):
        raise ValueError("Workspace coding change path must stay beneath its project")
    return path


class WorkspaceCodingChange(BaseModel):
    """One exact replacement proposed from one verified Reader ResultRef."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    relative_path: str = Field(min_length=1, max_length=500)
    old_text: str = Field(min_length=1, max_length=65_536)
    new_text: str = Field(max_length=65_536)
    source_result_ref_digest: str = Field(pattern=DIGEST_PATTERN)
    source_result_digest: str = Field(pattern=DIGEST_PATTERN)
    source_version_digest: str = Field(pattern=DIGEST_PATTERN)
    rationale: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def replacement_is_bounded(self) -> Self:
        _safe_relative_path(self.relative_path)
        if self.old_text == self.new_text:
            raise ValueError("Workspace coding change cannot be a no-op")
        return self


class WorkspaceCodingChangeDecision(BaseModel):
    """Unprivileged model output bound to the complete verified Reader result set."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.workspace-coding-change-decision.v1"] = (
        "deskpilot.workspace-coding-change-decision.v1"
    )
    # Reuse the existing durable AgentDecision vocabulary. The stage-specific
    # schema, binding id and reducer prove that this is not a generic final result.
    kind: Literal["submit_result"] = "submit_result"
    file_set_binding_id: str = Field(pattern=WORKSPACE_CODING_FILE_SET_BINDING_ID_PATTERN)
    reader_execution_id: str = Field(pattern=r"^tlx_[0-9a-f]{64}$")
    reader_execution_digest: str = Field(pattern=DIGEST_PATTERN)
    reader_result_set_digest: str = Field(pattern=DIGEST_PATTERN)
    changes: tuple[WorkspaceCodingChange, ...] = Field(min_length=2, max_length=8)
    decision_summary: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def paths_are_canonical(self) -> Self:
        paths = tuple(item.relative_path.casefold() for item in self.changes)
        if paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
            raise ValueError("Workspace coding change paths must be sorted and unique")
        return self


class WorkspaceCodingChangeRunBinding(BaseModel):
    """Exact Reader terminal proof to generation-2 proposer Plan/Run mapping."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.workspace-coding-change-run-binding.v1"] = (
        "deskpilot.workspace-coding-change-run-binding.v1"
    )
    binding_id: str = Field(pattern=WORKSPACE_CODING_CHANGE_RUN_BINDING_ID_PATTERN)
    file_set_binding_id: str = Field(pattern=WORKSPACE_CODING_FILE_SET_BINDING_ID_PATTERN)
    file_set_binding_digest: str = Field(pattern=DIGEST_PATTERN)
    reader_execution_id: str = Field(pattern=r"^tlx_[0-9a-f]{64}$")
    reader_execution_digest: str = Field(pattern=DIGEST_PATTERN)
    reader_terminal_event_digest: str = Field(pattern=DIGEST_PATTERN)
    reader_result_set_digest: str = Field(pattern=DIGEST_PATTERN)
    reader_result_ref_digests: tuple[str, ...] = Field(min_length=2, max_length=8)
    task_contract: TaskContract
    task_contract_digest: str = Field(pattern=DIGEST_PATTERN)
    draft_plan: DraftPlan
    draft_plan_digest: str = Field(pattern=DIGEST_PATTERN)
    expected_plan: ExecutablePlan
    expected_plan_manifest_digest: str = Field(pattern=DIGEST_PATTERN)
    run_id: str = Field(pattern=r"^run_[0-9a-f]{64}$")
    proposer_node_id: str = Field(pattern=PLAN_NODE_ID_PATTERN)
    proposer_node_spec_digest: str = Field(pattern=DIGEST_PATTERN)
    proposer_agent: BoundAgentRef
    activation_policy: Literal["verified_reader_zero_tool_proposer_v1"] = (
        "verified_reader_zero_tool_proposer_v1"
    )
    created_at: datetime
    binding_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def lineage_and_digest_match(self) -> Self:
        if self.created_at.tzinfo is None:
            raise ValueError("Change proposer binding timestamp must be timezone-aware")
        if (
            self.task_contract.version != 2
            or self.draft_plan.contract_version != 2
            or self.expected_plan.plan_generation != 2
            or self.task_contract.task_id != self.expected_plan.task_id
            or self.draft_plan.task_id != self.expected_plan.task_id
            or self.expected_plan.task_contract.digest != self.task_contract.digest
            or self.task_contract_digest != self.task_contract.digest
            or self.draft_plan_digest != sha256_digest(self.draft_plan)
            or self.expected_plan_manifest_digest != self.expected_plan.plan_manifest_digest
            or len(self.reader_result_ref_digests)
            != len(set(self.reader_result_ref_digests))
            or self.reader_result_set_digest
            != sha256_digest({"result_ref_digests": list(self.reader_result_ref_digests)})
        ):
            raise ValueError("Change proposer Contract, Plan or Reader lineage changed")
        nodes = {item.local_key: item for item in self.expected_plan.nodes}
        proposer = nodes.get("propose_change_set")
        if (
            set(nodes) != {"propose_change_set", "final_acceptance", "delivery"}
            or proposer is None
            or proposer.node_id != self.proposer_node_id
            or proposer.node_spec_digest != self.proposer_node_spec_digest
            or proposer.bound_agent != self.proposer_agent
            or proposer.capability is not None
            or proposer.depends_on
            or proposer.budget.tool_calls != 0
            or proposer.budget.model_calls != 1
            or self.proposer_agent.agent_id != "builtin.workspace_change_proposer"
            or self.proposer_agent.version != "1.0.0"
            or self.task_contract.capabilities
        ):
            raise ValueError("Change proposer binding crossed its zero-tool node")
        values = self.model_dump(mode="json")
        identity = {
            key: value
            for key, value in values.items()
            if key not in {"binding_id", "created_at", "binding_digest"}
        }
        if self.binding_id != _content_id("wcr", identity):
            raise ValueError("Change proposer binding identity changed")
        material = {key: value for key, value in values.items() if key != "binding_digest"}
        if self.binding_digest != sha256_digest(material):
            raise ValueError("Change proposer binding digest changed")
        return self

    @classmethod
    def build(cls, **values: Any) -> Self:
        base = {
            "schema_version": "deskpilot.workspace-coding-change-run-binding.v1",
            "activation_policy": "verified_reader_zero_tool_proposer_v1",
            **values,
        }
        base["task_contract_digest"] = base["task_contract"].digest
        base["draft_plan_digest"] = sha256_digest(base["draft_plan"])
        base["expected_plan_manifest_digest"] = base[
            "expected_plan"
        ].plan_manifest_digest
        base["reader_result_set_digest"] = sha256_digest(
            {"result_ref_digests": list(base["reader_result_ref_digests"])}
        )
        identity = {
            key: value for key, value in base.items() if key not in {"binding_id", "created_at"}
        }
        base["binding_id"] = _content_id("wcr", identity)
        return cls(**base, binding_digest=sha256_digest(base))


class WorkspaceCodingChangeProposal(BaseModel):
    """Verified no-write change proposal from one persistent Model Turn."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.workspace-coding-change-proposal.v1"] = (
        "deskpilot.workspace-coding-change-proposal.v1"
    )
    proposal_id: str = Field(pattern=WORKSPACE_CODING_CHANGE_PROPOSAL_ID_PATTERN)
    run_binding_id: str = Field(pattern=WORKSPACE_CODING_CHANGE_RUN_BINDING_ID_PATTERN)
    run_binding_digest: str = Field(pattern=DIGEST_PATTERN)
    proposer_agent: BoundAgentRef
    decision: WorkspaceCodingChangeDecision
    decision_digest: str = Field(pattern=DIGEST_PATTERN)
    created_at: datetime
    proposal_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def identity_and_digest_match(self) -> Self:
        if self.created_at.tzinfo is None or self.decision_digest != sha256_digest(self.decision):
            raise ValueError("Change proposal decision proof changed")
        values = self.model_dump(mode="json")
        identity = {
            key: value
            for key, value in values.items()
            if key not in {"proposal_id", "created_at", "proposal_digest"}
        }
        if self.proposal_id != _content_id("wcp", identity):
            raise ValueError("Change proposal identity changed")
        material = {key: value for key, value in values.items() if key != "proposal_digest"}
        if self.proposal_digest != sha256_digest(material):
            raise ValueError("Change proposal digest changed")
        return self

    @classmethod
    def build(cls, **values: Any) -> Self:
        base = {
            "schema_version": "deskpilot.workspace-coding-change-proposal.v1",
            **values,
        }
        base["decision_digest"] = sha256_digest(base["decision"])
        identity = {
            key: value for key, value in base.items() if key not in {"proposal_id", "created_at"}
        }
        base["proposal_id"] = _content_id("wcp", identity)
        return cls(**base, proposal_digest=sha256_digest(base))


class WorkspaceCodingChangeTurnProof(BaseModel):
    """Exact succeeded Invocation/Turn/Decision proof for one change proposal."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.workspace-coding-change-turn-proof.v1"] = (
        "deskpilot.workspace-coding-change-turn-proof.v1"
    )
    proof_id: str = Field(pattern=WORKSPACE_CODING_CHANGE_TURN_PROOF_ID_PATTERN)
    proposal_id: str = Field(pattern=WORKSPACE_CODING_CHANGE_PROPOSAL_ID_PATTERN)
    proposal_digest: str = Field(pattern=DIGEST_PATTERN)
    run_binding_id: str = Field(pattern=WORKSPACE_CODING_CHANGE_RUN_BINDING_ID_PATTERN)
    run_binding_digest: str = Field(pattern=DIGEST_PATTERN)
    invocation_id: str = Field(pattern=r"^inv_[0-9a-f]{64}$")
    turn_id: str = Field(pattern=r"^amt_[0-9a-f]{64}$")
    agent_decision_id: str = Field(pattern=r"^agd_[0-9a-f]{64}$")
    agent_decision_digest: str = Field(pattern=DIGEST_PATTERN)
    model_request_digest: str = Field(pattern=DIGEST_PATTERN)
    model_response_digest: str = Field(pattern=DIGEST_PATTERN)
    created_at: datetime
    proof_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def identity_and_digest_match(self) -> Self:
        if self.created_at.tzinfo is None:
            raise ValueError("Change proposal Turn proof timestamp must be timezone-aware")
        values = self.model_dump(mode="json")
        identity = {
            key: value
            for key, value in values.items()
            if key not in {"proof_id", "created_at", "proof_digest"}
        }
        if self.proof_id != _content_id("wct", identity):
            raise ValueError("Change proposal Turn proof identity changed")
        material = {key: value for key, value in values.items() if key != "proof_digest"}
        if self.proof_digest != sha256_digest(material):
            raise ValueError("Change proposal Turn proof digest changed")
        return self

    @classmethod
    def build(cls, **values: Any) -> Self:
        base = {
            "schema_version": "deskpilot.workspace-coding-change-turn-proof.v1",
            **values,
        }
        identity = {
            key: value for key, value in base.items() if key not in {"proof_id", "created_at"}
        }
        base["proof_id"] = _content_id("wct", identity)
        return cls(**base, proof_digest=sha256_digest(base))


class WorkspaceCodingWritePlanBinding(BaseModel):
    """Fresh exact confirmation to one immutable successor coding-loop Plan."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.workspace-coding-write-plan-binding.v1"] = (
        "deskpilot.workspace-coding-write-plan-binding.v1"
    )
    binding_id: str = Field(pattern=WORKSPACE_CODING_WRITE_PLAN_BINDING_ID_PATTERN)
    proposal_id: str = Field(pattern=WORKSPACE_CODING_CHANGE_PROPOSAL_ID_PATTERN)
    proposal_digest: str = Field(pattern=DIGEST_PATTERN)
    successor_task_id: str = Field(pattern=TASK_ID_PATTERN)
    confirmation_message_id: str = Field(pattern=MESSAGE_ID_PATTERN)
    confirmation_message_digest: str = Field(pattern=DIGEST_PATTERN)
    route_id: Literal["workspace_coding_loop"] = "workspace_coding_loop"
    route_version: Literal["2"] = "2"
    recipe_manifest: dict[str, Any]
    recipe_digest: str = Field(pattern=DIGEST_PATTERN)
    parameter_binding_manifest: dict[str, Any]
    parameter_binding_digest: str = Field(pattern=DIGEST_PATTERN)
    parameters: dict[str, str]
    parameters_digest: str = Field(pattern=DIGEST_PATTERN)
    task_contract: TaskContract
    task_contract_digest: str = Field(pattern=DIGEST_PATTERN)
    draft_plan: DraftPlan
    draft_plan_digest: str = Field(pattern=DIGEST_PATTERN)
    expected_plan: ExecutablePlan
    expected_plan_manifest_digest: str = Field(pattern=DIGEST_PATTERN)
    activation_policy: Literal["fresh_confirmed_write_plan_not_activated_v1"] = (
        "fresh_confirmed_write_plan_not_activated_v1"
    )
    created_at: datetime
    binding_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def lineage_and_digest_match(self) -> Self:
        if self.created_at.tzinfo is None:
            raise ValueError("Write Plan binding timestamp must be timezone-aware")
        if (
            self.task_contract.task_id != self.successor_task_id
            or self.draft_plan.task_id != self.successor_task_id
            or self.expected_plan.task_id != self.successor_task_id
            or self.task_contract.version != 1
            or self.draft_plan.contract_version != 1
            or self.expected_plan.plan_generation != 1
            or self.expected_plan.task_contract.digest != self.task_contract.digest
            or self.task_contract_digest != self.task_contract.digest
            or self.draft_plan_digest != sha256_digest(self.draft_plan)
            or self.expected_plan_manifest_digest != self.expected_plan.plan_manifest_digest
            or self.recipe_digest != sha256_digest(self.recipe_manifest)
            or self.parameter_binding_digest
            != sha256_digest(self.parameter_binding_manifest)
            or self.parameters_digest
            != sha256_digest({"parameters": dict(sorted(self.parameters.items()))})
        ):
            raise ValueError("Write Plan recipe, parameters or Plan lineage changed")
        values = self.model_dump(mode="json")
        identity = {
            key: value
            for key, value in values.items()
            if key not in {"binding_id", "created_at", "binding_digest"}
        }
        if self.binding_id != _content_id("wcw", identity):
            raise ValueError("Write Plan binding identity changed")
        material = {key: value for key, value in values.items() if key != "binding_digest"}
        if self.binding_digest != sha256_digest(material):
            raise ValueError("Write Plan binding digest changed")
        return self

    @classmethod
    def build(cls, **values: Any) -> Self:
        base = {
            "schema_version": "deskpilot.workspace-coding-write-plan-binding.v1",
            "route_id": "workspace_coding_loop",
            "route_version": "2",
            "activation_policy": "fresh_confirmed_write_plan_not_activated_v1",
            **values,
        }
        base["recipe_digest"] = sha256_digest(base["recipe_manifest"])
        base["parameter_binding_digest"] = sha256_digest(
            base["parameter_binding_manifest"]
        )
        base["parameters_digest"] = sha256_digest(
            {"parameters": dict(sorted(base["parameters"].items()))}
        )
        base["task_contract_digest"] = base["task_contract"].digest
        base["draft_plan_digest"] = sha256_digest(base["draft_plan"])
        base["expected_plan_manifest_digest"] = base[
            "expected_plan"
        ].plan_manifest_digest
        identity = {
            key: value for key, value in base.items() if key not in {"binding_id", "created_at"}
        }
        base["binding_id"] = _content_id("wcw", identity)
        return cls(**base, binding_digest=sha256_digest(base))


class WorkspaceCodingChangeWorkbenchRead(BaseModel):
    """Unified no-write proposal and confirmed successor Plan projection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.workspace-coding-change-workbench.v1"] = (
        "deskpilot.workspace-coding-change-workbench.v1"
    )
    phase: Literal[
        "reader_succeeded",
        "proposal_turn_ready",
        "proposal_blocked",
        "proposal_ready",
        "confirmed_write_plan",
    ]
    reader_task_id: str = Field(pattern=TASK_ID_PATTERN)
    file_set_binding_id: str = Field(pattern=WORKSPACE_CODING_FILE_SET_BINDING_ID_PATTERN)
    reader_execution_id: str = Field(pattern=r"^tlx_[0-9a-f]{64}$")
    reader_result_set_digest: str = Field(pattern=DIGEST_PATTERN)
    run_binding_id: str | None = Field(
        default=None, pattern=WORKSPACE_CODING_CHANGE_RUN_BINDING_ID_PATTERN
    )
    run_id: str | None = Field(default=None, pattern=r"^run_[0-9a-f]{64}$")
    run_status: str | None = Field(default=None, max_length=32)
    invocation_id: str | None = Field(default=None, pattern=r"^inv_[0-9a-f]{64}$")
    turn_id: str | None = Field(default=None, pattern=r"^amt_[0-9a-f]{64}$")
    turn_status: str | None = Field(default=None, max_length=32)
    proposal_id: str | None = Field(
        default=None, pattern=WORKSPACE_CODING_CHANGE_PROPOSAL_ID_PATTERN
    )
    proposal_digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    changes: tuple[WorkspaceCodingChange, ...] = Field(default=(), max_length=8)
    confirmation_text: str | None = Field(default=None, max_length=200)
    write_plan_binding_id: str | None = Field(
        default=None, pattern=WORKSPACE_CODING_WRITE_PLAN_BINDING_ID_PATTERN
    )
    successor_task_id: str | None = Field(default=None, pattern=TASK_ID_PATTERN)
    plan_id: str | None = Field(default=None, pattern=PLAN_ID_PATTERN)
    plan_manifest_digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    requires_user_confirmation: bool
    projection_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def phase_and_digest_match(self) -> Self:
        run = (self.run_binding_id, self.run_id, self.run_status)
        turn = (self.invocation_id, self.turn_id, self.turn_status)
        proposal = (self.proposal_id, self.proposal_digest, self.confirmation_text)
        write = (
            self.write_plan_binding_id,
            self.successor_task_id,
            self.plan_id,
            self.plan_manifest_digest,
        )
        if self.phase == "reader_succeeded":
            valid = not any((*run, *turn, *proposal, *write)) and not self.changes
        elif self.phase == "proposal_turn_ready":
            valid = (
                all(run)
                and (
                    all(turn)
                    or not any(turn)
                    or (
                        self.invocation_id is not None
                        and self.turn_id is None
                        and self.turn_status is None
                    )
                )
                and not any((*proposal, *write))
                and not self.changes
            )
        elif self.phase == "proposal_blocked":
            valid = all((*run, *turn)) and self.turn_status in {"failed", "outcome_unknown"}
            valid = valid and not any((*proposal, *write)) and not self.changes
        elif self.phase == "proposal_ready":
            valid = all((*run, *turn, *proposal)) and not any(write) and bool(self.changes)
            valid = valid and self.requires_user_confirmation
        else:
            valid = all((*run, *turn, *proposal, *write)) and bool(self.changes)
            valid = valid and not self.requires_user_confirmation
        if not valid:
            raise ValueError("Workspace coding change Workbench phase is inconsistent")
        if self.phase not in {"proposal_ready"} and self.requires_user_confirmation:
            raise ValueError("Only an unconfirmed change proposal may require confirmation")
        material = self.model_dump(mode="json", exclude={"projection_digest"})
        if self.projection_digest != sha256_digest(material):
            raise ValueError("Workspace coding change Workbench digest changed")
        return self


__all__ = [
    "WorkspaceCodingChange",
    "WorkspaceCodingChangeDecision",
    "WorkspaceCodingChangeProposal",
    "WorkspaceCodingChangeRunBinding",
    "WorkspaceCodingChangeTurnProof",
    "WorkspaceCodingChangeWorkbenchRead",
    "WorkspaceCodingWritePlanBinding",
]
