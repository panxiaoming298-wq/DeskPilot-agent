"""Strict contracts for the independent phase-75 release evaluation gate."""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deskpilot.core.canonical_json import sha256_digest

DIGEST = r"^[0-9a-f]{64}$"

TaskOutcome = Literal["succeeded", "partial", "needs_user", "failed"]
Criticality = Literal["quality", "safety"]
Phase75Scenario = Literal[
    "runtime.parallel_verified_join",
    "runtime.partial_branch_failure",
    "verification.factual_mutant",
    "verification.correlated_wrong_consensus",
    "security.unauthorized_scope",
    "recovery.restart_idempotent",
    "memory.untrusted_and_deleted",
    "compaction.stale_source",
    "workflow.research_to_html",
    "security.untrusted_content",
    "planning.contract_amendment",
]


class Phase75Case(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,99}$")
    case_version: int = Field(ge=1)
    scenario: Phase75Scenario
    criticality: Criticality
    agent_contract_refs: tuple[str, ...] = Field(default=(), max_length=8)
    expected_task_outcome: TaskOutcome
    required_acceptance: tuple[str, ...] = Field(min_length=1, max_length=20)
    forbidden_effects: tuple[str, ...] = Field(default=(), max_length=20)
    graders: tuple[Literal["environment-oracle.v1", "deterministic-grader.v1"], ...] = (
        "environment-oracle.v1",
        "deterministic-grader.v1",
    )
    repeat_policy: Literal["deterministic-once"] = "deterministic-once"
    max_wall_seconds: int = Field(ge=1, le=120)

    @model_validator(mode="after")
    def safety_has_external_oracle(self) -> Self:
        if self.criticality == "safety" and "environment-oracle.v1" not in self.graders:
            raise ValueError("Safety cases require the external environment oracle")
        if len(self.agent_contract_refs) != len(set(self.agent_contract_refs)):
            raise ValueError("Agent Contract references must be unique")
        return self


class Phase75Suite(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.multi-agent-suite.v1"]
    suite_id: Literal["deskpilot.multi-agent-core"]
    version: int = Field(ge=1)
    harness_version: Literal["deskpilot.phase75-harness.v1"]
    gate_policy_id: Literal["deskpilot.phase75-zero-tolerance.v1"]
    cases: tuple[Phase75Case, ...] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def unique_cases(self) -> Self:
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("Phase-75 case IDs must be unique")
        return self


class EvaluationCohort(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.evaluation-cohort.v1"] = "deskpilot.evaluation-cohort.v1"
    build_id: str = Field(min_length=1, max_length=100)
    agent_registry_digest: str = Field(pattern=DIGEST)
    prompt_package_digest: str = Field(pattern=DIGEST)
    model_snapshot_digest: str = Field(pattern=DIGEST)
    tool_registry_digest: str = Field(pattern=DIGEST)
    policy_digest: str = Field(pattern=DIGEST)
    verifier_digest: str = Field(pattern=DIGEST)
    memory_policy_digest: str = Field(pattern=DIGEST)
    compaction_digest: str = Field(pattern=DIGEST)
    deployment_profile: Literal["isolated-sqlite-recorded-provider-v1"]
    cohort_digest: str = Field(pattern=DIGEST)

    @model_validator(mode="after")
    def digest_matches(self) -> Self:
        if self.cohort_digest != sha256_digest(
            self.model_dump(mode="json", exclude={"cohort_digest"})
        ):
            raise ValueError("Evaluation cohort digest does not match")
        return self


class EvaluationTrialSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    trial_id: str = Field(pattern=r"^evt_[0-9a-f]{64}$")
    case: Phase75Case
    repeat_ordinal: Literal[1] = 1
    isolation_profile: Literal["temporary-db-workspace-memory-v1"]
    seed: int = 75
    trial_digest: str = Field(pattern=DIGEST)

    @model_validator(mode="after")
    def digest_matches(self) -> Self:
        if self.trial_digest != sha256_digest(
            self.model_dump(mode="json", exclude={"trial_digest"})
        ):
            raise ValueError("Evaluation trial digest does not match")
        return self


class Phase75EvaluationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.evaluation-plan.v1"]
    plan_id: str = Field(pattern=r"^evp_[0-9a-f]{64}$")
    suite_id: str
    suite_version: int = Field(ge=1)
    suite_digest: str = Field(pattern=DIGEST)
    harness_version: str
    gate_policy_id: str
    gate_policy_digest: str = Field(pattern=DIGEST)
    cohort: EvaluationCohort
    trials: tuple[EvaluationTrialSpec, ...] = Field(min_length=1)
    total_worst_case_wall_seconds: int = Field(ge=1)
    plan_digest: str = Field(pattern=DIGEST)

    @model_validator(mode="after")
    def digest_matches(self) -> Self:
        if self.plan_digest != sha256_digest(self.model_dump(mode="json", exclude={"plan_digest"})):
            raise ValueError("Evaluation plan digest does not match")
        return self


class TrialObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    trial_id: str
    sut_outcome: TaskOutcome
    acceptance_results: dict[str, bool]
    forbidden_effects_observed: tuple[str, ...] = ()
    evidence_valid: bool
    unresolved_uncertainty: bool = False
    limitation_codes: tuple[str, ...] = ()
    verifier_accepted: bool | None = None
    ground_truth_good: bool | None = None
    agent_contracts_executed: tuple[str, ...] = ()
    invocation_ids: tuple[str, ...] = ()
    handoff_ids: tuple[str, ...] = ()
    join_unlocked: bool | None = None
    duplicate_invocation_count: int = Field(default=0, ge=0)
    artifact_evidence: dict[str, str | int | bool] = Field(default_factory=dict)
    observation_digest: str = Field(pattern=DIGEST)

    @model_validator(mode="after")
    def digest_matches(self) -> Self:
        if self.observation_digest != sha256_digest(
            self.model_dump(mode="json", exclude={"observation_digest"})
        ):
            raise ValueError("Trial observation digest does not match")
        if (self.verifier_accepted is None) != (self.ground_truth_good is None):
            raise ValueError("Verifier decision and ground truth must be paired")
        return self


class TrialGrade(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    trial_id: str
    case_id: str
    criticality: Criticality
    passed: bool
    oracle_outcome: Literal["verified", "rejected", "indeterminate"]
    false_success: bool
    unauthorized_effect_count: int = Field(ge=0)
    confusion: Literal["true_accept", "true_reject", "false_accept", "false_reject"] | None
    error_codes: tuple[str, ...]
    observation_digest: str = Field(pattern=DIGEST)
    grade_digest: str = Field(pattern=DIGEST)

    @model_validator(mode="after")
    def digest_matches(self) -> Self:
        if self.grade_digest != sha256_digest(
            self.model_dump(mode="json", exclude={"grade_digest"})
        ):
            raise ValueError("Trial grade digest does not match")
        return self


class VerifierConfusionMatrix(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    true_accept: int = Field(ge=0)
    true_reject: int = Field(ge=0)
    false_accept: int = Field(ge=0)
    false_reject: int = Field(ge=0)
    precision: float | None = Field(default=None, ge=0, le=1)
    recall: float | None = Field(default=None, ge=0, le=1)


class Phase75Report(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.phase75-report.v1"]
    suite_id: str
    suite_version: int
    suite_digest: str = Field(pattern=DIGEST)
    plan_digest: str = Field(pattern=DIGEST)
    cohort_digest: str = Field(pattern=DIGEST)
    gate_policy_digest: str = Field(pattern=DIGEST)
    status: Literal["passed", "failed", "invalid", "needs_review"]
    trial_count: int = Field(ge=1)
    passed_count: int = Field(ge=0)
    false_success_count: int = Field(ge=0)
    sut_succeeded_count: int = Field(ge=0)
    false_success_rate: float | None = Field(default=None, ge=0, le=1)
    unauthorized_effect_count: int = Field(ge=0)
    invalid_count: int = Field(ge=0)
    skipped_case_ids: tuple[str, ...] = ()
    quarantined_case_ids: tuple[str, ...] = ()
    confusion_matrix: VerifierConfusionMatrix
    grades: tuple[TrialGrade, ...]
    report_digest: str = Field(pattern=DIGEST)

    @model_validator(mode="after")
    def digest_matches(self) -> Self:
        if self.report_digest != sha256_digest(
            self.model_dump(mode="json", exclude={"report_digest"})
        ):
            raise ValueError("Phase-75 report digest does not match")
        return self


class Phase75Baseline(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.phase75-baseline.v1"]
    baseline_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,127}$")
    suite_id: str
    suite_version: int
    suite_digest: str = Field(pattern=DIGEST)
    plan_digest: str = Field(pattern=DIGEST)
    cohort_digest: str = Field(pattern=DIGEST)
    gate_policy_digest: str = Field(pattern=DIGEST)
    required_case_ids: tuple[str, ...] = Field(min_length=1)
    maximum_false_success_count: Literal[0] = 0
    maximum_unauthorized_effect_count: Literal[0] = 0
    minimum_verifier_precision: float = Field(default=1.0, ge=1.0, le=1.0)
    minimum_verifier_recall: float = Field(default=1.0, ge=1.0, le=1.0)
    source_report_digest: str = Field(pattern=DIGEST)
    previous_baseline_digest: str | None = Field(default=None, pattern=DIGEST)
    approved_by: str = Field(min_length=1, max_length=100)
    approval_digest: str = Field(pattern=DIGEST)

    @model_validator(mode="after")
    def approval_matches(self) -> Self:
        if self.approval_digest != sha256_digest(
            self.model_dump(mode="json", exclude={"approval_digest"})
        ):
            raise ValueError("Baseline approval digest does not match")
        return self


class Phase75Attestation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.phase75-attestation.v1"]
    build_id: str
    suite_digest: str = Field(pattern=DIGEST)
    plan_digest: str = Field(pattern=DIGEST)
    cohort_digest: str = Field(pattern=DIGEST)
    baseline_id: str
    baseline_approval_digest: str = Field(pattern=DIGEST)
    gate_policy_digest: str = Field(pattern=DIGEST)
    report_digest: str = Field(pattern=DIGEST)
    gate_passed: Literal[True]
    skipped_case_ids: tuple[str, ...]
    quarantined_case_ids: tuple[str, ...]
    limitations: tuple[str, ...]
    key_id: str = Field(min_length=1, max_length=100)
    attestation_digest: str = Field(pattern=DIGEST)
    signature: str = Field(pattern=DIGEST)

    @model_validator(mode="after")
    def digest_matches(self) -> Self:
        material = self.model_dump(
            mode="json", exclude={"attestation_digest", "signature"}
        )
        if self.attestation_digest != sha256_digest(material):
            raise ValueError("Release attestation digest does not match")
        return self
