"""Immutable proofs for controlled workspace exploration and file-set confirmation."""

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

WORKSPACE_CODING_EXPLORATION_SNAPSHOT_ID_PATTERN = r"^wxs_[0-9a-f]{64}$"
WORKSPACE_CODING_EXPLORATION_PROPOSAL_ID_PATTERN = r"^wxp_[0-9a-f]{64}$"
WORKSPACE_CODING_FILE_SET_BINDING_ID_PATTERN = r"^wxb_[0-9a-f]{64}$"


def _safe_relative_path(value: str) -> PurePosixPath:
    pure = PurePosixPath(value)
    if (
        value != value.strip()
        or not value
        or "\\" in value
        or pure.is_absolute()
        or any(part in {"", ".", ".."} for part in pure.parts)
        or any(character in value for character in ("\x00", "\r", "\n", ":"))
    ):
        raise ValueError("Exploration path must stay beneath its project")
    return pure


def _content_id(prefix: str, material: dict[str, Any]) -> str:
    return f"{prefix}_{sha256_digest(material)}"


class WorkspaceCodingExplorationFileProof(BaseModel):
    """Content-addressed metadata for one file in the server snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    relative_path: str = Field(min_length=1, max_length=500)
    content_digest: str = Field(pattern=DIGEST_PATTERN)
    version_digest: str = Field(pattern=DIGEST_PATTERN)
    byte_count: int = Field(ge=0, le=2_097_152)
    proof_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def path_and_digest_match(self) -> Self:
        _safe_relative_path(self.relative_path)
        material = self.model_dump(mode="json", exclude={"proof_digest"})
        if self.proof_digest != sha256_digest(material):
            raise ValueError("Exploration file proof digest changed")
        return self

    @classmethod
    def build(cls, **values: Any) -> Self:
        return cls(**values, proof_digest=sha256_digest(values))


class WorkspaceCodingExplorationSnapshot(BaseModel):
    """Server-owned, bounded project catalog; it grants no read or write call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.workspace-coding-exploration-snapshot.v1"] = (
        "deskpilot.workspace-coding-exploration-snapshot.v1"
    )
    snapshot_id: str = Field(
        pattern=WORKSPACE_CODING_EXPLORATION_SNAPSHOT_ID_PATTERN
    )
    task_id: str = Field(pattern=TASK_ID_PATTERN)
    user_message_id: str = Field(pattern=MESSAGE_ID_PATTERN)
    user_message_digest: str = Field(pattern=DIGEST_PATTERN)
    project_path: str = Field(min_length=1, max_length=32_767)
    ecosystem: Literal["python", "node"]
    test_path: str = Field(min_length=1, max_length=500)
    objective_digest: str = Field(pattern=DIGEST_PATTERN)
    files: tuple[WorkspaceCodingExplorationFileProof, ...] = Field(
        min_length=2,
        max_length=256,
    )
    catalog_digest: str = Field(pattern=DIGEST_PATTERN)
    scanned_file_count: int = Field(ge=2, le=2_000)
    scanned_byte_count: int = Field(ge=0, le=33_554_432)
    truncated: bool
    created_at: datetime
    snapshot_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def scope_and_digests_match(self) -> Self:
        _safe_relative_path(self.test_path)
        if self.created_at.tzinfo is None:
            raise ValueError("Exploration snapshot timestamp must be timezone-aware")
        paths = tuple(item.relative_path.casefold() for item in self.files)
        if paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
            raise ValueError("Exploration file catalog must be sorted and unique")
        suffixes = (
            {".py", ".pyi"}
            if self.ecosystem == "python"
            else {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts"}
        )
        if any(
            PurePosixPath(item.relative_path).suffix.casefold() not in suffixes
            for item in self.files
        ):
            raise ValueError("Exploration file crossed its ecosystem")
        expected_catalog = sha256_digest(
            {"files": [item.model_dump(mode="json") for item in self.files]}
        )
        if self.catalog_digest != expected_catalog:
            raise ValueError("Exploration catalog digest changed")
        values = self.model_dump(mode="json")
        identity = {
            key: value
            for key, value in values.items()
            if key not in {"snapshot_id", "created_at", "snapshot_digest"}
        }
        if self.snapshot_id != _content_id("wxs", identity):
            raise ValueError("Exploration snapshot identity changed")
        material = {key: value for key, value in values.items() if key != "snapshot_digest"}
        if self.snapshot_digest != sha256_digest(material):
            raise ValueError("Exploration snapshot digest changed")
        return self

    @classmethod
    def build(cls, **values: Any) -> Self:
        base = {
            "schema_version": "deskpilot.workspace-coding-exploration-snapshot.v1",
            **values,
        }
        base["catalog_digest"] = sha256_digest(
            {
                "files": [
                    item.model_dump(mode="json")
                    if isinstance(item, BaseModel)
                    else item
                    for item in base["files"]
                ]
            }
        )
        identity = {
            key: value
            for key, value in base.items()
            if key not in {"created_at", "snapshot_id"}
        }
        base["snapshot_id"] = _content_id("wxs", identity)
        return cls(**base, snapshot_digest=sha256_digest(base))


class WorkspaceCodingExplorationCandidateFile(BaseModel):
    """One unprivileged exact path selected from a persisted snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    relative_path: str = Field(min_length=1, max_length=500)
    source_file_proof_digest: str = Field(pattern=DIGEST_PATTERN)
    rationale: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def path_is_relative(self) -> Self:
        _safe_relative_path(self.relative_path)
        return self


class WorkspaceCodingExplorationDecision(BaseModel):
    """Model output is a candidate only and never grants filesystem authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.workspace-coding-exploration-decision.v1"] = (
        "deskpilot.workspace-coding-exploration-decision.v1"
    )
    kind: Literal["propose_file_set"] = "propose_file_set"
    snapshot_id: str = Field(
        pattern=WORKSPACE_CODING_EXPLORATION_SNAPSHOT_ID_PATTERN
    )
    snapshot_digest: str = Field(pattern=DIGEST_PATTERN)
    files: tuple[WorkspaceCodingExplorationCandidateFile, ...] = Field(
        min_length=2,
        max_length=8,
    )
    decision_summary: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def candidates_are_canonical(self) -> Self:
        paths = tuple(item.relative_path.casefold() for item in self.files)
        if paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
            raise ValueError("Exploration candidates must be sorted and unique")
        return self


class WorkspaceCodingExplorationProposal(BaseModel):
    """Verified model candidate bound to an immutable project snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.workspace-coding-exploration-proposal.v1"] = (
        "deskpilot.workspace-coding-exploration-proposal.v1"
    )
    proposal_id: str = Field(
        pattern=WORKSPACE_CODING_EXPLORATION_PROPOSAL_ID_PATTERN
    )
    snapshot_id: str = Field(
        pattern=WORKSPACE_CODING_EXPLORATION_SNAPSHOT_ID_PATTERN
    )
    snapshot_digest: str = Field(pattern=DIGEST_PATTERN)
    explorer_agent: BoundAgentRef
    decision: WorkspaceCodingExplorationDecision
    decision_digest: str = Field(pattern=DIGEST_PATTERN)
    created_at: datetime
    proposal_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def lineage_and_digests_match(self) -> Self:
        if self.created_at.tzinfo is None:
            raise ValueError("Exploration proposal timestamp must be timezone-aware")
        if (
            self.decision.snapshot_id != self.snapshot_id
            or self.decision.snapshot_digest != self.snapshot_digest
            or self.decision_digest != sha256_digest(self.decision)
        ):
            raise ValueError("Exploration proposal crossed its snapshot or decision")
        values = self.model_dump(mode="json")
        identity = {
            key: value
            for key, value in values.items()
            if key not in {"proposal_id", "created_at", "proposal_digest"}
        }
        if self.proposal_id != _content_id("wxp", identity):
            raise ValueError("Exploration proposal identity changed")
        material = {key: value for key, value in values.items() if key != "proposal_digest"}
        if self.proposal_digest != sha256_digest(material):
            raise ValueError("Exploration proposal digest changed")
        return self

    @classmethod
    def build(cls, **values: Any) -> Self:
        base = {
            "schema_version": "deskpilot.workspace-coding-exploration-proposal.v1",
            **values,
        }
        base["decision_digest"] = sha256_digest(base["decision"])
        identity = {
            key: value
            for key, value in base.items()
            if key not in {"created_at", "proposal_id"}
        }
        base["proposal_id"] = _content_id("wxp", identity)
        return cls(**base, proposal_digest=sha256_digest(base))


class WorkspaceCodingFileSetNodeMapping(BaseModel):
    """Bind one confirmed candidate to one exact Reader node."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ordinal: int = Field(ge=1, le=8)
    relative_path: str = Field(min_length=1, max_length=500)
    source_file_proof_digest: str = Field(pattern=DIGEST_PATTERN)
    plan_node_id: str = Field(pattern=PLAN_NODE_ID_PATTERN)
    plan_local_key: str = Field(pattern=r"^inspect_candidate_[0-9]{2}$")
    plan_node_spec_digest: str = Field(pattern=DIGEST_PATTERN)
    mapping_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def path_key_and_digest_match(self) -> Self:
        _safe_relative_path(self.relative_path)
        if self.plan_local_key != f"inspect_candidate_{self.ordinal:02d}":
            raise ValueError("File-set Reader mapping key changed")
        material = self.model_dump(mode="json", exclude={"mapping_digest"})
        if self.mapping_digest != sha256_digest(material):
            raise ValueError("File-set Reader mapping digest changed")
        return self

    @classmethod
    def build(cls, **values: Any) -> Self:
        return cls(**values, mapping_digest=sha256_digest(values))


class WorkspaceCodingFileSetPlanBinding(BaseModel):
    """Exact user confirmation and successor generation-1 read-only Plan proof."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.workspace-coding-file-set-plan-binding.v1"] = (
        "deskpilot.workspace-coding-file-set-plan-binding.v1"
    )
    binding_id: str = Field(pattern=WORKSPACE_CODING_FILE_SET_BINDING_ID_PATTERN)
    proposal_id: str = Field(
        pattern=WORKSPACE_CODING_EXPLORATION_PROPOSAL_ID_PATTERN
    )
    proposal_digest: str = Field(pattern=DIGEST_PATTERN)
    successor_task_id: str = Field(pattern=TASK_ID_PATTERN)
    confirmation_message_id: str = Field(pattern=MESSAGE_ID_PATTERN)
    confirmation_message_digest: str = Field(pattern=DIGEST_PATTERN)
    task_contract: TaskContract
    task_contract_digest: str = Field(pattern=DIGEST_PATTERN)
    draft_plan: DraftPlan
    draft_plan_digest: str = Field(pattern=DIGEST_PATTERN)
    expected_plan: ExecutablePlan
    expected_plan_manifest_digest: str = Field(pattern=DIGEST_PATTERN)
    mappings: tuple[WorkspaceCodingFileSetNodeMapping, ...] = Field(
        min_length=2,
        max_length=8,
    )
    mappings_digest: str = Field(pattern=DIGEST_PATTERN)
    activation_policy: Literal["confirmed_read_only_generation_v1"] = (
        "confirmed_read_only_generation_v1"
    )
    created_at: datetime
    binding_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def generation_and_digests_match(self) -> Self:
        if self.created_at.tzinfo is None:
            raise ValueError("File-set binding timestamp must be timezone-aware")
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
            or self.expected_plan_manifest_digest
            != self.expected_plan.plan_manifest_digest
        ):
            raise ValueError("File-set binding Contract or Plan lineage changed")
        ordinals = tuple(item.ordinal for item in self.mappings)
        paths = tuple(item.relative_path.casefold() for item in self.mappings)
        node_ids = tuple(item.plan_node_id for item in self.mappings)
        if (
            ordinals != tuple(range(1, len(self.mappings) + 1))
            or paths != tuple(sorted(paths))
            or len(paths) != len(set(paths))
            or len(node_ids) != len(set(node_ids))
        ):
            raise ValueError("File-set node mappings are not canonical")
        by_id = {item.node_id: item for item in self.expected_plan.nodes}
        if any(
            (node := by_id.get(item.plan_node_id)) is None
            or node.local_key != item.plan_local_key
            or node.node_spec_digest != item.plan_node_spec_digest
            for item in self.mappings
        ):
            raise ValueError("File-set mapping crossed its expected Reader Plan")
        expected_mappings = sha256_digest(
            {"mappings": [item.model_dump(mode="json") for item in self.mappings]}
        )
        if self.mappings_digest != expected_mappings:
            raise ValueError("File-set mapping set digest changed")
        values = self.model_dump(mode="json")
        identity = {
            key: value
            for key, value in values.items()
            if key not in {"binding_id", "created_at", "binding_digest"}
        }
        if self.binding_id != _content_id("wxb", identity):
            raise ValueError("File-set Plan binding identity changed")
        material = {key: value for key, value in values.items() if key != "binding_digest"}
        if self.binding_digest != sha256_digest(material):
            raise ValueError("File-set Plan binding digest changed")
        return self

    @classmethod
    def build(cls, **values: Any) -> Self:
        base = {
            "schema_version": "deskpilot.workspace-coding-file-set-plan-binding.v1",
            "activation_policy": "confirmed_read_only_generation_v1",
            **values,
        }
        base["task_contract_digest"] = base["task_contract"].digest
        base["draft_plan_digest"] = sha256_digest(base["draft_plan"])
        base["expected_plan_manifest_digest"] = base[
            "expected_plan"
        ].plan_manifest_digest
        base["mappings_digest"] = sha256_digest(
            {
                "mappings": [
                    item.model_dump(mode="json")
                    if isinstance(item, BaseModel)
                    else item
                    for item in base["mappings"]
                ]
            }
        )
        identity = {
            key: value
            for key, value in base.items()
            if key not in {"created_at", "binding_id"}
        }
        base["binding_id"] = _content_id("wxb", identity)
        return cls(**base, binding_digest=sha256_digest(base))


class WorkspaceCodingExplorationWorkbenchRead(BaseModel):
    """Redacted Workbench projection for the source and confirmed successor Task."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.workspace-coding-exploration-workbench.v1"] = (
        "deskpilot.workspace-coding-exploration-workbench.v1"
    )
    phase: Literal[
        "snapshot_ready",
        "proposal_ready",
        "confirmed_read_only_plan",
    ]
    source_task_id: str = Field(pattern=TASK_ID_PATTERN)
    snapshot_id: str = Field(
        pattern=WORKSPACE_CODING_EXPLORATION_SNAPSHOT_ID_PATTERN
    )
    snapshot_digest: str = Field(pattern=DIGEST_PATTERN)
    project_path: str = Field(min_length=1, max_length=32_767)
    ecosystem: Literal["python", "node"]
    test_path: str = Field(min_length=1, max_length=500)
    catalog_file_count: int = Field(ge=2, le=256)
    catalog_truncated: bool
    proposal_id: str | None = Field(
        default=None,
        pattern=WORKSPACE_CODING_EXPLORATION_PROPOSAL_ID_PATTERN,
    )
    proposal_digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    candidates: tuple[WorkspaceCodingExplorationCandidateFile, ...] = Field(
        default=(),
        max_length=8,
    )
    confirmation_text: str | None = Field(default=None, max_length=200)
    binding_id: str | None = Field(
        default=None,
        pattern=WORKSPACE_CODING_FILE_SET_BINDING_ID_PATTERN,
    )
    binding_digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    successor_task_id: str | None = Field(default=None, pattern=TASK_ID_PATTERN)
    plan_generation: int | None = Field(default=None, ge=1)
    plan_id: str | None = Field(default=None, pattern=PLAN_ID_PATTERN)
    plan_manifest_digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    requires_user_confirmation: bool
    projection_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def phase_and_digest_match(self) -> Self:
        proposal_values = (
            self.proposal_id,
            self.proposal_digest,
            self.confirmation_text,
        )
        binding_values = (
            self.binding_id,
            self.binding_digest,
            self.successor_task_id,
            self.plan_generation,
            self.plan_id,
            self.plan_manifest_digest,
        )
        if self.phase == "snapshot_ready":
            if any(value is not None for value in (*proposal_values, *binding_values)):
                raise ValueError("Snapshot-only exploration cannot expose later proofs")
            if self.candidates or self.requires_user_confirmation:
                raise ValueError("Snapshot-only exploration has no candidate confirmation")
        elif self.phase == "proposal_ready":
            if any(value is None for value in proposal_values):
                raise ValueError("Proposal-ready exploration lost its proposal")
            if any(value is not None for value in binding_values):
                raise ValueError("Unconfirmed exploration cannot expose a Plan binding")
            if not self.candidates or not self.requires_user_confirmation:
                raise ValueError("Proposal-ready exploration must await exact confirmation")
        else:
            if any(value is None for value in (*proposal_values, *binding_values)):
                raise ValueError("Confirmed exploration lost its proof chain")
            if not self.candidates or self.requires_user_confirmation:
                raise ValueError("Confirmed exploration cannot still await confirmation")
        material = self.model_dump(mode="json", exclude={"projection_digest"})
        if self.projection_digest != sha256_digest(material):
            raise ValueError("Workspace exploration Workbench digest changed")
        return self


__all__ = [
    "WorkspaceCodingExplorationCandidateFile",
    "WorkspaceCodingExplorationDecision",
    "WorkspaceCodingExplorationFileProof",
    "WorkspaceCodingExplorationProposal",
    "WorkspaceCodingExplorationSnapshot",
    "WorkspaceCodingExplorationWorkbenchRead",
    "WorkspaceCodingFileSetNodeMapping",
    "WorkspaceCodingFileSetPlanBinding",
]
