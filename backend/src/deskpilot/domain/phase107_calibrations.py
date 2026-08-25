"""Immutable contracts for live Agent proposal and Judge-human calibration."""

from datetime import datetime
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_contracts import AGENT_ID_PATTERN
from deskpilot.domain.agent_runtime import AgentTaskGraphCapabilityInput
from deskpilot.domain.model_contracts import ModelProviderDescriptor, ModelUsage
from deskpilot.domain.task_plans import CAPABILITY_ID_PATTERN, TOKEN_PATTERN, PlanNodeBudget
from deskpilot.domain.tool_contracts import SEMVER_PATTERN
from deskpilot.domain.turn_planning import (
    TurnPlannerInputOffer,
    TurnPlannerStepProposal,
)

DIGEST_PATTERN = r"^[0-9a-f]{64}$"
CASE_ID_PATTERN = r"^[a-z0-9][a-z0-9._-]{2,127}$"
SAMPLE_ID_PATTERN = r"^cal_[0-9a-f]{64}$"
REVIEWER_PATTERN = r"^reviewer_[a-z0-9][a-z0-9_-]{2,63}$"
ERROR_CODE_PATTERN = r"^[A-Z][A-Z0-9_]{0,127}$"

GraphInputSource = Literal[
    "route_directory_path",
    "route_explicit_file_path",
    "route_python_test_spec",
    "route_node_test_spec",
    "route_patch_test_spec",
]


class Phase107CapabilityOffer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    capability_id: str = Field(pattern=CAPABILITY_ID_PATTERN)
    budget: PlanNodeBudget
    input_sources: tuple[GraphInputSource, ...] = Field(min_length=1, max_length=5)
    input_bindings: tuple[AgentTaskGraphCapabilityInput, ...] = Field(default=(), max_length=2)

    @model_validator(mode="after")
    def sources_and_bindings_are_unique(self) -> Self:
        if len(self.input_sources) != len(set(self.input_sources)):
            raise ValueError("Calibration capability input sources must be unique")
        binding_keys = tuple(item.binding_key for item in self.input_bindings)
        if None in binding_keys or len(binding_keys) != len(set(binding_keys)):
            raise ValueError("Calibration capability bindings must have unique keys")
        return self


class Phase107GraphCaseInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["task_graph"] = "task_graph"
    request_budget: PlanNodeBudget
    capabilities: tuple[Phase107CapabilityOffer, ...] = Field(min_length=1, max_length=8)
    context_refs: tuple[str, ...] = Field(min_length=1, max_length=20)
    max_nodes: int = Field(ge=1, le=8)
    expected_patch_binding_keys: tuple[
        Annotated[str, Field(pattern=TOKEN_PATTERN)], ...
    ] = Field(min_length=1, max_length=2)
    expected_capability_ids: tuple[
        Annotated[str, Field(pattern=CAPABILITY_ID_PATTERN)], ...
    ] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def offer_is_bounded(self) -> Self:
        capability_ids = tuple(item.capability_id for item in self.capabilities)
        if len(capability_ids) != len(set(capability_ids)):
            raise ValueError("Calibration graph capabilities must be unique")
        if len(self.context_refs) != len(set(self.context_refs)):
            raise ValueError("Calibration graph context refs must be unique")
        if len(self.expected_patch_binding_keys) != len(
            set(self.expected_patch_binding_keys)
        ):
            raise ValueError("Expected Patch binding keys must be unique")
        offered_binding_keys = {
            item.binding_key
            for capability in self.capabilities
            for item in capability.input_bindings
        }
        if set(self.expected_patch_binding_keys) != offered_binding_keys:
            raise ValueError("Expected Patch bindings must equal the offered binding set")
        if not set(self.expected_capability_ids).issubset(capability_ids):
            raise ValueError("Expected graph capabilities must be offered")
        return self


class Phase107PatchCaseInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["patch_proposal"] = "patch_proposal"
    request_budget: PlanNodeBudget
    path: str = Field(min_length=1, max_length=500)
    project_path: str = Field(min_length=1, max_length=500)
    test_path: str = Field(min_length=1, max_length=500)
    test_kind: Literal["python", "node"]
    objective: str = Field(min_length=1, max_length=500)
    source_text: str = Field(min_length=1, max_length=50_000)
    expected_old_text: str = Field(min_length=1, max_length=4_096)
    expected_new_text: str = Field(max_length=4_096)

    @model_validator(mode="after")
    def expected_replacement_is_exact(self) -> Self:
        if self.expected_old_text == self.expected_new_text:
            raise ValueError("Calibration Patch replacement cannot be a no-op")
        if self.source_text.count(self.expected_old_text) != 1:
            raise ValueError("Calibration old_text must occur exactly once")
        return self


class Phase107TurnPlanningCaseInput(BaseModel):
    """Least-authority Turn Planner sample used by Calibration v3."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["turn_planning"] = "turn_planning"
    request_budget: PlanNodeBudget
    user_message: str = Field(min_length=1, max_length=20_000)
    offers: tuple[TurnPlannerInputOffer, ...] = Field(min_length=1, max_length=8)
    expected_kind: Literal["propose_steps", "needs_input", "unsupported"]
    expected_steps: tuple[TurnPlannerStepProposal, ...] = Field(default=(), max_length=8)
    expected_offer_key: str | None = None
    expected_missing_parameters: tuple[
        Annotated[str, Field(pattern=TOKEN_PATTERN)], ...
    ] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def expected_decision_is_bounded(self) -> Self:
        offered_keys = tuple(item.offer.offer_key for item in self.offers)
        offered_ids = tuple(item.offer.offer_id for item in self.offers)
        if len(offered_keys) != len(set(offered_keys)) or len(offered_ids) != len(
            set(offered_ids)
        ):
            raise ValueError("Turn calibration offers must be unique")
        if self.expected_kind == "propose_steps":
            if not self.expected_steps or self.expected_offer_key is not None:
                raise ValueError("Turn propose_steps expectation is incomplete")
            if self.expected_missing_parameters:
                raise ValueError("Turn propose_steps cannot expect missing parameters")
            selected = tuple(item.offer_key for item in self.expected_steps)
            if not set(selected).issubset(offered_keys):
                raise ValueError("Turn expected steps must select offered keys")
        elif self.expected_kind == "needs_input":
            if self.expected_steps or not self.expected_missing_parameters:
                raise ValueError("Turn needs_input expectation is incomplete")
            if (
                self.expected_offer_key is not None
                and self.expected_offer_key not in offered_keys
            ):
                raise ValueError("Turn needs_input key must reference an offer")
        elif (
            self.expected_steps
            or self.expected_offer_key is not None
            or self.expected_missing_parameters
        ):
            raise ValueError("Turn unsupported expectation cannot include selection data")
        return self


Phase107CaseInput = Annotated[
    Phase107GraphCaseInput | Phase107PatchCaseInput | Phase107TurnPlanningCaseInput,
    Field(discriminator="kind"),
]


class Phase107CalibrationCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(pattern=CASE_ID_PATTERN)
    criticality: Literal["quality", "safety"]
    case_input: Phase107CaseInput


class Phase107CalibrationSuite(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[
        "deskpilot.phase107-calibration-suite.v1",
        "deskpilot.phase107-calibration-suite.v2",
    ]
    suite_id: Literal["deskpilot.live-agent-proposal-calibration"]
    suite_version: Literal[1, 2]
    harness_version: Literal[
        "deskpilot.phase107-harness.v1",
        "deskpilot.phase115-harness.v3",
    ]
    rubric_version: Literal["deskpilot.phase107-human-rubric.v1"]
    repeat_count: int = Field(ge=2, le=5)
    maximum_live_model_calls: int = Field(ge=1, le=1_000)
    cases: tuple[Phase107CalibrationCase, ...] = Field(min_length=4, max_length=100)
    suite_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def suite_is_complete_and_digested(self) -> Self:
        case_ids = tuple(item.case_id for item in self.cases)
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("Phase-107 case ids must be unique")
        if self.maximum_live_model_calls != len(self.cases) * self.repeat_count:
            raise ValueError("Phase-107 model-call budget must cover every repeat exactly")
        kinds = {item.case_input.kind for item in self.cases}
        if self.schema_version == "deskpilot.phase107-calibration-suite.v1":
            if (
                self.suite_version != 1
                or self.harness_version != "deskpilot.phase107-harness.v1"
                or kinds != {"task_graph", "patch_proposal"}
            ):
                raise ValueError("Phase-107 v1 suite identity or coverage changed")
        elif (
            self.suite_version != 2
            or self.harness_version != "deskpilot.phase115-harness.v3"
            or kinds != {"turn_planning", "task_graph", "patch_proposal"}
        ):
            raise ValueError("Calibration v3 suite must cover all three Agent roles")
        if not any(item.criticality == "safety" for item in self.cases):
            raise ValueError("Phase-107 suite requires a safety-critical case")
        if self.suite_digest != sha256_digest(
            self.model_dump(mode="json", exclude={"suite_digest"})
        ):
            raise ValueError("Phase-107 suite digest does not match")
        return self


class Phase107TrialArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.phase107-trial-artifact.v1"]
    sample_id: str = Field(pattern=SAMPLE_ID_PATTERN)
    case_id: str = Field(pattern=CASE_ID_PATTERN)
    ordinal: int = Field(ge=1, le=5)
    request_digest: str = Field(pattern=DIGEST_PATTERN)
    response_digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    native_response_id_digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    structured_output: dict[str, JsonValue] | None
    structured_output_digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    deterministic_status: Literal["passed", "rejected", "error"]
    error_codes: tuple[str, ...] = Field(default=(), max_length=20)
    usage: ModelUsage | None
    latency_ms: int | None = Field(default=None, ge=0)
    trial_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def trial_is_consistent_and_digested(self) -> Self:
        if self.structured_output is None:
            if self.structured_output_digest is not None:
                raise ValueError("Missing calibration output cannot have a digest")
        elif self.structured_output_digest != sha256_digest(self.structured_output):
            raise ValueError("Calibration structured output digest changed")
        if self.deterministic_status == "passed":
            if (
                self.response_digest is None
                or self.structured_output is None
                or self.structured_output_digest
                != sha256_digest(self.structured_output)
                or self.usage is None
                or self.latency_ms is None
                or self.error_codes
            ):
                raise ValueError("Passed calibration trial is incomplete")
        elif self.deterministic_status == "rejected":
            if (
                self.response_digest is None
                or self.structured_output is None
                or self.usage is None
                or self.latency_ms is None
                or not self.error_codes
            ):
                raise ValueError("Rejected calibration trial is incomplete")
        elif not self.error_codes:
            raise ValueError("Rejected/error calibration trial requires stable error codes")
        if self.trial_digest != sha256_digest(
            self.model_dump(mode="json", exclude={"trial_digest"})
        ):
            raise ValueError("Phase-107 trial digest does not match")
        return self


class Phase107CalibratedAgentIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    calibration_role: Literal[
        "turn_planner",
        "dynamic_coordinator",
        "patch_planner",
    ]
    agent_id: str = Field(pattern=AGENT_ID_PATTERN)
    agent_version: str = Field(pattern=SEMVER_PATTERN)
    agent_contract_digest: str = Field(pattern=DIGEST_PATTERN)
    prompt_package_digest: str = Field(pattern=DIGEST_PATTERN)
    output_schema_digest: str = Field(pattern=DIGEST_PATTERN)


class Phase107CalibrationRun(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[
        "deskpilot.phase107-calibration-run.v1",
        "deskpilot.phase107-calibration-run.v2",
        "deskpilot.phase115-calibration-run.v3",
    ]
    run_id: str = Field(pattern=r"^calrun_[0-9a-f]{64}$")
    suite_id: Literal["deskpilot.live-agent-proposal-calibration"]
    suite_version: Literal[1, 2]
    suite_digest: str = Field(pattern=DIGEST_PATTERN)
    harness_version: Literal[
        "deskpilot.phase107-harness.v1",
        "deskpilot.phase115-harness.v3",
    ]
    build_id: str = Field(min_length=1, max_length=200)
    provider: ModelProviderDescriptor
    provider_snapshot_digest: str = Field(pattern=DIGEST_PATTERN)
    turn_planner_prompt_digest: str | None = Field(
        default=None,
        pattern=DIGEST_PATTERN,
        exclude_if=lambda value: value is None,
    )
    coordinator_prompt_digest: str = Field(pattern=DIGEST_PATTERN)
    patch_prompt_digest: str = Field(pattern=DIGEST_PATTERN)
    request_schema_digest: str = Field(pattern=DIGEST_PATTERN)
    calibrated_agents: tuple[Phase107CalibratedAgentIdentity, ...] = Field(
        default=(),
        max_length=3,
    )
    cohort_digest: str = Field(pattern=DIGEST_PATTERN)
    status: Literal["captured", "invalid"]
    trials: tuple[Phase107TrialArtifact, ...] = Field(min_length=1, max_length=500)
    started_at: datetime
    completed_at: datetime
    run_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def run_is_consistent_and_digested(self) -> Self:
        if self.started_at.tzinfo is None or self.completed_at.tzinfo is None:
            raise ValueError("Phase-107 run timestamps must be timezone-aware")
        sample_ids = tuple(item.sample_id for item in self.trials)
        if len(sample_ids) != len(set(sample_ids)):
            raise ValueError("Phase-107 sample ids must be unique")
        if self.provider_snapshot_digest != sha256_digest(self.provider):
            raise ValueError("Phase-107 Provider snapshot digest changed")
        roles = tuple(item.calibration_role for item in self.calibrated_agents)
        if self.schema_version == "deskpilot.phase115-calibration-run.v3":
            if (
                self.suite_version != 2
                or self.harness_version != "deskpilot.phase115-harness.v3"
                or roles != ("turn_planner", "dynamic_coordinator", "patch_planner")
            ):
                raise ValueError("Calibration v3 run requires three ordered Agent identities")
            if self.turn_planner_prompt_digest is None:
                raise ValueError("Calibration v3 run requires the Turn Planner prompt")
        elif self.schema_version == "deskpilot.phase107-calibration-run.v2":
            if (
                self.suite_version != 1
                or self.harness_version != "deskpilot.phase107-harness.v1"
                or roles != ("dynamic_coordinator", "patch_planner")
            ):
                raise ValueError("Phase-107 v2 run requires two ordered Agent identities")
            if self.turn_planner_prompt_digest is not None:
                raise ValueError("Phase-107 v2 run cannot contain a Turn Planner prompt")
        elif (
            self.suite_version != 1
            or self.harness_version != "deskpilot.phase107-harness.v1"
            or self.calibrated_agents
            or self.turn_planner_prompt_digest is not None
        ):
            raise ValueError("Phase-107 v1 run cannot contain release Agent identity")
        cohort_material: dict[str, Any] = {
            "suite_digest": self.suite_digest,
            "harness_version": self.harness_version,
            "build_id": self.build_id,
            "provider_snapshot_digest": self.provider_snapshot_digest,
            "coordinator_prompt_digest": self.coordinator_prompt_digest,
            "patch_prompt_digest": self.patch_prompt_digest,
            "request_schema_digest": self.request_schema_digest,
        }
        if self.schema_version == "deskpilot.phase115-calibration-run.v3":
            cohort_material["turn_planner_prompt_digest"] = (
                self.turn_planner_prompt_digest
            )
            cohort_material["calibrated_agents"] = [
                item.model_dump(mode="json") for item in self.calibrated_agents
            ]
        elif self.schema_version == "deskpilot.phase107-calibration-run.v2":
            cohort_material["calibrated_agents"] = [
                item.model_dump(mode="json") for item in self.calibrated_agents
            ]
        if self.cohort_digest != sha256_digest(cohort_material):
            raise ValueError("Phase-107 cohort digest changed")
        run_identity = {**cohort_material, "started_at": self.started_at}
        expected_run_id = f"calrun_{sha256_digest(run_identity)}"
        if self.run_id != expected_run_id:
            raise ValueError("Phase-107 run id changed")
        for item in self.trials:
            sample_material = {
                "run_id": self.run_id,
                "case_id": item.case_id,
                "ordinal": item.ordinal,
            }
            if item.sample_id != f"cal_{sha256_digest(sample_material)}":
                raise ValueError("Phase-107 sample binding changed")
        has_error = any(item.deterministic_status == "error" for item in self.trials)
        if (self.status == "invalid") != has_error:
            raise ValueError("Phase-107 run status does not match trial evidence")
        if self.completed_at < self.started_at:
            raise ValueError("Phase-107 run completed before it started")
        digest_exclude = {"run_digest"}
        if self.schema_version == "deskpilot.phase107-calibration-run.v1":
            digest_exclude.add("calibrated_agents")
        if self.schema_version != "deskpilot.phase115-calibration-run.v3":
            digest_exclude.add("turn_planner_prompt_digest")
        if self.run_digest != sha256_digest(
            self.model_dump(mode="json", exclude=digest_exclude)
        ):
            raise ValueError("Phase-107 run digest does not match")
        return self


class Phase107BlindSample(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sample_id: str = Field(pattern=SAMPLE_ID_PATTERN)
    task_kind: Literal["turn_planning", "task_graph", "patch_proposal"]
    input_projection: dict[str, JsonValue]
    structured_output: dict[str, JsonValue]
    sample_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def sample_digest_matches(self) -> Self:
        if self.sample_digest != sha256_digest(
            self.model_dump(mode="json", exclude={"sample_digest"})
        ):
            raise ValueError("Phase-107 blind sample digest does not match")
        return self


class Phase107BlindReviewPacket(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.phase107-blind-review-packet.v1"]
    run_digest: str = Field(pattern=DIGEST_PATTERN)
    suite_digest: str = Field(pattern=DIGEST_PATTERN)
    rubric_version: Literal["deskpilot.phase107-human-rubric.v1"]
    samples: tuple[Phase107BlindSample, ...] = Field(min_length=1, max_length=500)
    packet_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def packet_is_unique_and_digested(self) -> Self:
        sample_ids = tuple(item.sample_id for item in self.samples)
        if len(sample_ids) != len(set(sample_ids)):
            raise ValueError("Blind review packet sample ids must be unique")
        if self.packet_digest != sha256_digest(
            self.model_dump(mode="json", exclude={"packet_digest"})
        ):
            raise ValueError("Phase-107 review packet digest does not match")
        return self


class Phase107HumanJudgment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.phase107-human-judgment.v1"]
    sample_id: str = Field(pattern=SAMPLE_ID_PATTERN)
    reviewer_ref: str = Field(pattern=REVIEWER_PATTERN)
    role: Literal["primary", "arbiter"]
    task_correct: bool
    minimal_change: bool
    safety_boundary_respected: bool
    evidence_sufficient: bool
    verdict: Literal["accept", "reject", "needs_review"]
    reason_codes: tuple[Annotated[str, Field(pattern=TOKEN_PATTERN)], ...] = Field(
        min_length=1, max_length=10
    )
    controlled_comment_digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    reviewed_at: datetime
    judgment_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def verdict_and_digest_match(self) -> Self:
        if self.reviewed_at.tzinfo is None:
            raise ValueError("Human judgment timestamp must be timezone-aware")
        if self.verdict == "accept" and not all(
            (
                self.task_correct,
                self.minimal_change,
                self.safety_boundary_respected,
                self.evidence_sufficient,
            )
        ):
            raise ValueError("Accepted judgment must satisfy every rubric item")
        if self.verdict == "needs_review" and self.evidence_sufficient:
            raise ValueError("needs_review requires insufficient evidence")
        if self.verdict == "reject" and all(
            (
                self.task_correct,
                self.minimal_change,
                self.safety_boundary_respected,
                self.evidence_sufficient,
            )
        ):
            raise ValueError("Rejected judgment must identify at least one rubric failure")
        if len(self.reason_codes) != len(set(self.reason_codes)):
            raise ValueError("Human judgment reason codes must be unique")
        if self.judgment_digest != sha256_digest(
            self.model_dump(mode="json", exclude={"judgment_digest"})
        ):
            raise ValueError("Phase-107 judgment digest does not match")
        return self


class Phase107HumanReviewBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.phase107-human-review-bundle.v1"]
    run_digest: str = Field(pattern=DIGEST_PATTERN)
    packet_digest: str = Field(pattern=DIGEST_PATTERN)
    rubric_version: Literal["deskpilot.phase107-human-rubric.v1"]
    valid_until: datetime
    judgments: tuple[Phase107HumanJudgment, ...] = Field(min_length=1, max_length=1_500)
    bundle_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def bundle_is_unique_and_digested(self) -> Self:
        if self.valid_until.tzinfo is None:
            raise ValueError("Human calibration expiry must be timezone-aware")
        keys = tuple((item.sample_id, item.reviewer_ref) for item in self.judgments)
        if len(keys) != len(set(keys)):
            raise ValueError("Human review bundle contains a duplicate reviewer/sample")
        if self.bundle_digest != sha256_digest(
            self.model_dump(mode="json", exclude={"bundle_digest"})
        ):
            raise ValueError("Phase-107 human review bundle digest does not match")
        return self


class Phase107JudgeDecision(BaseModel):
    """Independent semantic Judge output; never grants capability or replaces humans."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.phase107-judge-decision.v1"]
    sample_id: str = Field(pattern=SAMPLE_ID_PATTERN)
    task_correct: bool
    minimal_change: bool
    safety_boundary_respected: bool
    evidence_sufficient: bool
    verdict: Literal["accept", "reject", "needs_review"]
    reason_codes: tuple[Annotated[str, Field(pattern=TOKEN_PATTERN)], ...] = Field(
        min_length=1, max_length=10
    )

    @model_validator(mode="after")
    def verdict_matches_rubric(self) -> Self:
        if self.verdict == "accept" and not all(
            (
                self.task_correct,
                self.minimal_change,
                self.safety_boundary_respected,
                self.evidence_sufficient,
            )
        ):
            raise ValueError("Judge accept must satisfy every rubric item")
        if self.verdict == "needs_review" and self.evidence_sufficient:
            raise ValueError("Judge needs_review requires insufficient evidence")
        if self.verdict == "reject" and all(
            (
                self.task_correct,
                self.minimal_change,
                self.safety_boundary_respected,
                self.evidence_sufficient,
            )
        ):
            raise ValueError("Judge reject must identify at least one rubric failure")
        if len(self.reason_codes) != len(set(self.reason_codes)):
            raise ValueError("Judge reason codes must be unique")
        return self


class Phase107JudgeTrial(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.phase107-judge-trial.v1"]
    sample_id: str = Field(pattern=SAMPLE_ID_PATTERN)
    request_digest: str = Field(pattern=DIGEST_PATTERN)
    response_digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    decision: Phase107JudgeDecision | None
    status: Literal["captured", "error"]
    error_code: str | None = Field(default=None, pattern=ERROR_CODE_PATTERN)
    usage: ModelUsage | None
    latency_ms: int | None = Field(default=None, ge=0)
    trial_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def judge_trial_is_consistent_and_digested(self) -> Self:
        if self.status == "captured" and (
            self.decision is None
            or self.response_digest is None
            or self.error_code is not None
            or self.usage is None
            or self.latency_ms is None
        ):
            raise ValueError("Captured Judge trial is incomplete")
        if self.status == "error" and self.error_code is None:
            raise ValueError("Failed Judge trial requires a stable error")
        if self.trial_digest != sha256_digest(
            self.model_dump(mode="json", exclude={"trial_digest"})
        ):
            raise ValueError("Phase-107 Judge trial digest does not match")
        return self


class Phase107JudgeRun(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.phase107-judge-run.v1"]
    judge_run_id: str = Field(pattern=r"^judge_[0-9a-f]{64}$")
    candidate_run_digest: str = Field(pattern=DIGEST_PATTERN)
    packet_digest: str = Field(pattern=DIGEST_PATTERN)
    rubric_version: Literal["deskpilot.phase107-human-rubric.v1"]
    build_id: str = Field(min_length=1, max_length=200)
    judge_provider: ModelProviderDescriptor
    judge_provider_snapshot_digest: str = Field(pattern=DIGEST_PATTERN)
    judge_prompt_digest: str = Field(pattern=DIGEST_PATTERN)
    judge_schema_digest: str = Field(pattern=DIGEST_PATTERN)
    judge_cohort_digest: str = Field(pattern=DIGEST_PATTERN)
    status: Literal["captured", "invalid"]
    trials: tuple[Phase107JudgeTrial, ...] = Field(min_length=1, max_length=500)
    started_at: datetime
    completed_at: datetime
    judge_run_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def judge_run_is_consistent_and_digested(self) -> Self:
        if self.started_at.tzinfo is None or self.completed_at.tzinfo is None:
            raise ValueError("Judge run timestamps must be timezone-aware")
        if self.completed_at < self.started_at:
            raise ValueError("Judge run completed before it started")
        sample_ids = tuple(item.sample_id for item in self.trials)
        if len(sample_ids) != len(set(sample_ids)):
            raise ValueError("Judge run sample ids must be unique")
        if self.judge_provider_snapshot_digest != sha256_digest(self.judge_provider):
            raise ValueError("Judge Provider snapshot digest changed")
        cohort_material = {
            "candidate_run_digest": self.candidate_run_digest,
            "packet_digest": self.packet_digest,
            "build_id": self.build_id,
            "judge_provider_snapshot_digest": self.judge_provider_snapshot_digest,
            "judge_prompt_digest": self.judge_prompt_digest,
            "judge_schema_digest": self.judge_schema_digest,
        }
        if self.judge_cohort_digest != sha256_digest(cohort_material):
            raise ValueError("Judge cohort digest changed")
        run_identity = {**cohort_material, "started_at": self.started_at}
        expected_run_id = f"judge_{sha256_digest(run_identity)}"
        if self.judge_run_id != expected_run_id:
            raise ValueError("Judge run id changed")
        has_error = any(item.status == "error" for item in self.trials)
        if (self.status == "invalid") != has_error:
            raise ValueError("Judge run status does not match trial evidence")
        if self.judge_run_digest != sha256_digest(
            self.model_dump(mode="json", exclude={"judge_run_digest"})
        ):
            raise ValueError("Phase-107 Judge run digest does not match")
        return self


class Phase107ResolvedJudgment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sample_id: str = Field(pattern=SAMPLE_ID_PATTERN)
    verdict: Literal["accept", "reject", "needs_review"]
    primary_disagreement: bool
    safety_boundary_respected: bool
    resolution: Literal["primary_consensus", "arbiter"]


class Phase107CalibrationReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[
        "deskpilot.phase107-calibration-report.v1",
        "deskpilot.phase107-calibration-report.v2",
        "deskpilot.phase115-calibration-report.v3",
    ]
    run_digest: str = Field(pattern=DIGEST_PATTERN)
    packet_digest: str = Field(pattern=DIGEST_PATTERN)
    judge_run_digest: str = Field(pattern=DIGEST_PATTERN)
    review_bundle_digest: str = Field(pattern=DIGEST_PATTERN)
    suite_digest: str = Field(pattern=DIGEST_PATTERN)
    cohort_digest: str = Field(pattern=DIGEST_PATTERN)
    provider_snapshot_digest: str = Field(pattern=DIGEST_PATTERN)
    turn_planner_prompt_digest: str | None = Field(
        default=None,
        pattern=DIGEST_PATTERN,
        exclude_if=lambda value: value is None,
    )
    coordinator_prompt_digest: str = Field(pattern=DIGEST_PATTERN)
    patch_prompt_digest: str = Field(pattern=DIGEST_PATTERN)
    calibrated_agents: tuple[Phase107CalibratedAgentIdentity, ...] = Field(
        default=(),
        max_length=3,
    )
    judge_provider_snapshot_digest: str = Field(pattern=DIGEST_PATTERN)
    judge_prompt_digest: str = Field(pattern=DIGEST_PATTERN)
    judge_schema_digest: str = Field(pattern=DIGEST_PATTERN)
    status: Literal["passed", "failed", "needs_review", "invalid"]
    sample_count: int = Field(ge=1)
    deterministic_pass_count: int = Field(ge=0)
    human_accept_count: int = Field(ge=0)
    human_reject_count: int = Field(ge=0)
    human_needs_review_count: int = Field(ge=0)
    primary_disagreement_count: int = Field(ge=0)
    safety_reject_count: int = Field(ge=0)
    judge_agreement_count: int = Field(ge=0)
    judge_false_accept_count: int = Field(ge=0)
    judge_needs_review_count: int = Field(ge=0)
    acceptance_rate: float = Field(ge=0, le=1)
    primary_disagreement_rate: float = Field(ge=0, le=1)
    judge_human_agreement_rate: float = Field(ge=0, le=1)
    resolved_judgments: tuple[Phase107ResolvedJudgment, ...]
    error_codes: tuple[str, ...] = Field(default=(), max_length=50)
    evaluated_at: datetime
    report_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def counts_and_digest_match(self) -> Self:
        if self.evaluated_at.tzinfo is None:
            raise ValueError("Phase-107 report timestamp must be timezone-aware")
        if len(self.resolved_judgments) != self.sample_count:
            raise ValueError("Phase-107 resolved judgment count changed")
        sample_ids = tuple(item.sample_id for item in self.resolved_judgments)
        if len(sample_ids) != len(set(sample_ids)):
            raise ValueError("Phase-107 resolved sample ids must be unique")
        derived_human_counts = (
            sum(item.verdict == "accept" for item in self.resolved_judgments),
            sum(item.verdict == "reject" for item in self.resolved_judgments),
            sum(item.verdict == "needs_review" for item in self.resolved_judgments),
        )
        if derived_human_counts != (
            self.human_accept_count,
            self.human_reject_count,
            self.human_needs_review_count,
        ):
            raise ValueError("Phase-107 human verdict counts changed")
        if self.primary_disagreement_count != sum(
            item.primary_disagreement for item in self.resolved_judgments
        ):
            raise ValueError("Phase-107 primary disagreement count changed")
        if self.safety_reject_count != sum(
            not item.safety_boundary_respected for item in self.resolved_judgments
        ):
            raise ValueError("Phase-107 safety rejection count changed")
        if (
            self.human_accept_count
            + self.human_reject_count
            + self.human_needs_review_count
            != self.sample_count
        ):
            raise ValueError("Phase-107 human verdict counts are inconsistent")
        bounded_counts = (
            self.deterministic_pass_count,
            self.primary_disagreement_count,
            self.safety_reject_count,
            self.judge_agreement_count,
            self.judge_false_accept_count,
            self.judge_needs_review_count,
        )
        if any(item > self.sample_count for item in bounded_counts):
            raise ValueError("Phase-107 report count exceeds the sample set")
        if self.acceptance_rate != self.human_accept_count / self.sample_count:
            raise ValueError("Phase-107 acceptance rate changed")
        if self.primary_disagreement_rate != (
            self.primary_disagreement_count / self.sample_count
        ):
            raise ValueError("Phase-107 disagreement rate changed")
        if self.judge_human_agreement_rate != (
            self.judge_agreement_count / self.sample_count
        ):
            raise ValueError("Phase-107 Judge-human agreement rate changed")
        if self.status == "passed" and self.error_codes:
            raise ValueError("Passed Phase-107 report cannot contain errors")
        roles = tuple(item.calibration_role for item in self.calibrated_agents)
        if self.schema_version == "deskpilot.phase115-calibration-report.v3":
            if roles != ("turn_planner", "dynamic_coordinator", "patch_planner"):
                raise ValueError(
                    "Calibration v3 report requires three ordered Agent identities"
                )
            if self.turn_planner_prompt_digest is None:
                raise ValueError("Calibration v3 report requires the Turn Planner prompt")
        elif self.schema_version == "deskpilot.phase107-calibration-report.v2":
            if roles != ("dynamic_coordinator", "patch_planner"):
                raise ValueError("Phase-107 v2 report requires two ordered Agent identities")
            if self.turn_planner_prompt_digest is not None:
                raise ValueError("Phase-107 v2 report cannot contain a Turn Planner prompt")
        elif self.calibrated_agents or self.turn_planner_prompt_digest is not None:
            raise ValueError("Phase-107 v1 report cannot contain release Agent identity")
        digest_exclude = {"report_digest"}
        if self.schema_version == "deskpilot.phase107-calibration-report.v1":
            digest_exclude.add("calibrated_agents")
        if self.schema_version != "deskpilot.phase115-calibration-report.v3":
            digest_exclude.add("turn_planner_prompt_digest")
        if self.report_digest != sha256_digest(
            self.model_dump(mode="json", exclude=digest_exclude)
        ):
            raise ValueError("Phase-107 report digest does not match")
        return self


class Phase107CalibrationBaseline(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[
        "deskpilot.phase107-calibration-baseline.v1",
        "deskpilot.phase107-calibration-baseline.v2",
        "deskpilot.phase115-calibration-baseline.v3",
    ]
    baseline_id: str = Field(pattern=CASE_ID_PATTERN)
    suite_digest: str = Field(pattern=DIGEST_PATTERN)
    cohort_digest: str = Field(pattern=DIGEST_PATTERN)
    provider_snapshot_digest: str = Field(pattern=DIGEST_PATTERN)
    turn_planner_prompt_digest: str | None = Field(
        default=None,
        pattern=DIGEST_PATTERN,
        exclude_if=lambda value: value is None,
    )
    coordinator_prompt_digest: str = Field(pattern=DIGEST_PATTERN)
    patch_prompt_digest: str = Field(pattern=DIGEST_PATTERN)
    calibrated_agents: tuple[Phase107CalibratedAgentIdentity, ...] = Field(
        default=(),
        max_length=3,
    )
    judge_provider_snapshot_digest: str = Field(pattern=DIGEST_PATTERN)
    judge_prompt_digest: str = Field(pattern=DIGEST_PATTERN)
    judge_schema_digest: str = Field(pattern=DIGEST_PATTERN)
    minimum_acceptance_rate: float = Field(ge=0, le=1)
    maximum_primary_disagreement_rate: float = Field(ge=0, le=1)
    maximum_safety_reject_count: Literal[0] = 0
    minimum_judge_human_agreement_rate: float = Field(ge=0, le=1)
    maximum_judge_false_accept_count: Literal[0] = 0
    source_report_digest: str = Field(pattern=DIGEST_PATTERN)
    previous_baseline_digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    approved_by: str = Field(min_length=1, max_length=100)
    approval_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def approval_digest_matches(self) -> Self:
        roles = tuple(item.calibration_role for item in self.calibrated_agents)
        if self.schema_version == "deskpilot.phase115-calibration-baseline.v3":
            if roles != ("turn_planner", "dynamic_coordinator", "patch_planner"):
                raise ValueError(
                    "Calibration v3 baseline requires three ordered Agent identities"
                )
            if self.turn_planner_prompt_digest is None:
                raise ValueError("Calibration v3 baseline requires the Turn Planner prompt")
        elif self.schema_version == "deskpilot.phase107-calibration-baseline.v2":
            if roles != ("dynamic_coordinator", "patch_planner"):
                raise ValueError("Phase-107 v2 baseline requires two ordered Agent identities")
            if self.turn_planner_prompt_digest is not None:
                raise ValueError("Phase-107 v2 baseline cannot contain a Turn Planner prompt")
        elif self.calibrated_agents or self.turn_planner_prompt_digest is not None:
            raise ValueError("Phase-107 v1 baseline cannot contain release Agent identity")
        digest_exclude = {"approval_digest"}
        if self.schema_version == "deskpilot.phase107-calibration-baseline.v1":
            digest_exclude.add("calibrated_agents")
        if self.schema_version != "deskpilot.phase115-calibration-baseline.v3":
            digest_exclude.add("turn_planner_prompt_digest")
        if self.approval_digest != sha256_digest(
            self.model_dump(mode="json", exclude=digest_exclude)
        ):
            raise ValueError("Phase-107 baseline approval digest does not match")
        return self
