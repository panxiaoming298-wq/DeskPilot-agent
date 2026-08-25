"""Default-closed loading and runtime checks for calibrated cloud Agent routes."""

import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from deskpilot.application.phase107_calibration import (
    Phase107CalibrationError,
    Phase107CalibrationService,
)
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_contracts import AgentContract
from deskpilot.domain.agent_model_admissions import (
    AgentModelAdmissionBundle,
    ApprovedAgentModelAdmission,
)
from deskpilot.domain.model_contracts import ModelLocation, ModelProviderDescriptor
from deskpilot.domain.phase107_calibrations import (
    Phase107BlindReviewPacket,
    Phase107CalibrationBaseline,
    Phase107CalibrationReport,
    Phase107CalibrationRun,
    Phase107CalibrationSuite,
    Phase107HumanReviewBundle,
    Phase107JudgeRun,
)

MAX_ADMISSION_BUNDLE_BYTES = 32 * 1024 * 1024


class AgentModelAdmissionError(RuntimeError):
    code = "AGENT_MODEL_ADMISSION_INVALID"


class AgentModelAdmissionRequiredError(AgentModelAdmissionError):
    code = "AGENT_MODEL_ADMISSION_REQUIRED"


def build_phase115_admission_bundle(
    *,
    suite: Phase107CalibrationSuite,
    run: Phase107CalibrationRun,
    packet: Phase107BlindReviewPacket,
    judge_run: Phase107JudgeRun,
    reviews: Phase107HumanReviewBundle,
    report: Phase107CalibrationReport,
    baseline_id: str,
    approved_by: str,
    approved_at: datetime,
    valid_until: datetime,
) -> AgentModelAdmissionBundle:
    """Approve only one fully replayed three-role, non-Fake cloud cohort."""

    if (
        run.schema_version != "deskpilot.phase115-calibration-run.v3"
        or report.schema_version != "deskpilot.phase115-calibration-report.v3"
        or report.status != "passed"
        or run.provider.location is not ModelLocation.CLOUD
        or run.provider.protocol.value == "fake"
        or approved_at.tzinfo is None
        or valid_until.tzinfo is None
        or approved_at < report.evaluated_at
        or valid_until > reviews.valid_until
    ):
        raise AgentModelAdmissionError(
            "Phase-115 admission requires passed, current three-role cloud evidence"
        )
    service = Phase107CalibrationService()
    try:
        replayed = service.grade(
            suite,
            run,
            packet,
            judge_run,
            reviews,
            now=report.evaluated_at,
        )
    except (Phase107CalibrationError, ValidationError, ValueError, TypeError) as error:
        raise AgentModelAdmissionError(
            "Phase-115 admission evidence failed full calibration replay"
        ) from error
    if replayed.report_digest != report.report_digest:
        raise AgentModelAdmissionError("Phase-115 calibration report replay changed")
    baseline_material = {
        "schema_version": "deskpilot.phase115-calibration-baseline.v3",
        "baseline_id": baseline_id,
        "suite_digest": suite.suite_digest,
        "cohort_digest": run.cohort_digest,
        "provider_snapshot_digest": run.provider_snapshot_digest,
        "turn_planner_prompt_digest": run.turn_planner_prompt_digest,
        "coordinator_prompt_digest": run.coordinator_prompt_digest,
        "patch_prompt_digest": run.patch_prompt_digest,
        "calibrated_agents": run.calibrated_agents,
        "judge_provider_snapshot_digest": judge_run.judge_provider_snapshot_digest,
        "judge_prompt_digest": judge_run.judge_prompt_digest,
        "judge_schema_digest": judge_run.judge_schema_digest,
        "minimum_acceptance_rate": 1.0,
        "maximum_primary_disagreement_rate": 0.0,
        "maximum_safety_reject_count": 0,
        "minimum_judge_human_agreement_rate": 1.0,
        "maximum_judge_false_accept_count": 0,
        "source_report_digest": report.report_digest,
        "previous_baseline_digest": None,
        "approved_by": approved_by,
    }
    baseline = Phase107CalibrationBaseline.model_validate(
        {
            **baseline_material,
            "approval_digest": sha256_digest(baseline_material),
        }
    )
    if service.compare(baseline, report):
        raise AgentModelAdmissionError("Phase-115 baseline does not approve the report")
    admissions: list[ApprovedAgentModelAdmission] = []
    for calibrated in run.calibrated_agents:
        identity = {
            "agent_id": calibrated.agent_id,
            "agent_version": calibrated.agent_version,
            "agent_contract_digest": calibrated.agent_contract_digest,
            "prompt_package_digest": calibrated.prompt_package_digest,
            "provider_snapshot_digest": run.provider_snapshot_digest,
            "build_id": run.build_id,
            "report_digest": report.report_digest,
            "baseline_approval_digest": baseline.approval_digest,
        }
        material = {
            "schema_version": "deskpilot.approved-agent-model-admission.v1",
            "admission_id": f"ama_{sha256_digest(identity)}",
            "agent_id": calibrated.agent_id,
            "agent_version": calibrated.agent_version,
            "agent_contract_digest": calibrated.agent_contract_digest,
            "prompt_package_digest": calibrated.prompt_package_digest,
            "provider": run.provider,
            "provider_snapshot_digest": run.provider_snapshot_digest,
            "build_id": run.build_id,
            "request_schema_digest": run.request_schema_digest,
            "run_digest": run.run_digest,
            "report_digest": report.report_digest,
            "baseline_approval_digest": baseline.approval_digest,
            "review_bundle_digest": reviews.bundle_digest,
            "approved_by": approved_by,
            "approved_at": approved_at,
            "valid_until": valid_until,
        }
        admissions.append(
            ApprovedAgentModelAdmission.model_validate(
                {**material, "admission_digest": sha256_digest(material)}
            )
        )
    bundle_material = {
        "schema_version": "deskpilot.agent-model-admission-bundle.v1",
        "suite": suite,
        "run": run,
        "packet": packet,
        "judge_run": judge_run,
        "reviews": reviews,
        "report": report,
        "baseline": baseline,
        "admissions": tuple(admissions),
    }
    bundle = AgentModelAdmissionBundle.model_validate(
        {**bundle_material, "bundle_digest": sha256_digest(bundle_material)}
    )
    AgentModelAdmissionRegistry.from_bundle(bundle, now=approved_at)
    return bundle


class AgentModelAdmissionRegistry:
    """Immutable exact-match routes derived from one fully revalidated evidence bundle."""

    def __init__(
        self,
        admissions: tuple[ApprovedAgentModelAdmission, ...] = (),
    ) -> None:
        self._admissions = {item.key: item for item in admissions}

    @classmethod
    def from_bundle(
        cls,
        bundle: AgentModelAdmissionBundle,
        *,
        now: datetime | None = None,
    ) -> "AgentModelAdmissionRegistry":
        evaluated_now = now or datetime.now(UTC)
        if evaluated_now.tzinfo is None:
            raise AgentModelAdmissionError("Admission evaluation time must be timezone-aware")
        service = Phase107CalibrationService()
        try:
            regraded = service.grade(
                bundle.suite,
                bundle.run,
                bundle.packet,
                bundle.judge_run,
                bundle.reviews,
                now=bundle.report.evaluated_at,
            )
        except (Phase107CalibrationError, ValidationError, ValueError, TypeError) as error:
            raise AgentModelAdmissionError(
                "Agent model admission evidence failed full calibration replay"
            ) from error
        if regraded.report_digest != bundle.report.report_digest:
            raise AgentModelAdmissionError("Agent model admission report replay changed")
        if (
            bundle.baseline.source_report_digest != bundle.report.report_digest
            or service.compare(bundle.baseline, bundle.report)
        ):
            raise AgentModelAdmissionError(
                "Agent model admission baseline does not approve the report"
            )
        if evaluated_now > bundle.reviews.valid_until:
            raise AgentModelAdmissionError("Agent model admission human evidence expired")

        if bundle.run.calibrated_agents:
            identities = bundle.run.calibrated_agents
        else:
            coordinator, patch = service.calibrated_agent_bindings(bundle.run.provider)
            identities = (coordinator.identity, patch.identity)
        calibrated_agents = {
            (item.agent_id, item.agent_version): (
                item.agent_contract_digest,
                item.prompt_package_digest,
            )
            for item in identities
        }
        for admission in bundle.admissions:
            expected_agent = calibrated_agents.get(
                (admission.agent_id, admission.agent_version)
            )
            if expected_agent is None or expected_agent != (
                admission.agent_contract_digest,
                admission.prompt_package_digest,
            ):
                raise AgentModelAdmissionError(
                    "Admission Agent Contract or Prompt was not in the calibrated cohort"
                )
            if (
                admission.provider != bundle.run.provider
                or admission.provider_snapshot_digest
                != bundle.run.provider_snapshot_digest
                or admission.build_id != bundle.run.build_id
                or admission.request_schema_digest != bundle.run.request_schema_digest
                or admission.run_digest != bundle.run.run_digest
                or admission.report_digest != bundle.report.report_digest
                or admission.baseline_approval_digest
                != bundle.baseline.approval_digest
                or admission.review_bundle_digest != bundle.reviews.bundle_digest
                or admission.approved_by != bundle.baseline.approved_by
            ):
                raise AgentModelAdmissionError(
                    "Admission does not bind the exact approved calibration evidence"
                )
            if (
                admission.approved_at < bundle.report.evaluated_at
                or admission.approved_at > evaluated_now
                or admission.valid_until > bundle.reviews.valid_until
                or evaluated_now > admission.valid_until
            ):
                raise AgentModelAdmissionError(
                    "Agent model admission approval time or validity is invalid"
                )
        return cls(bundle.admissions)

    @property
    def admission_count(self) -> int:
        return len(self._admissions)

    def allows(
        self,
        contract: AgentContract,
        prompt_package_digest: str,
        provider: ModelProviderDescriptor,
        *,
        now: datetime | None = None,
    ) -> bool:
        if provider.location is ModelLocation.LOCAL:
            return True
        provider_digest = self._provider_digest(provider)
        admission = self._admissions.get(
            (contract.agent_id, contract.version, provider_digest)
        )
        evaluated_now = now or datetime.now(UTC)
        return bool(
            admission is not None
            and evaluated_now.tzinfo is not None
            and evaluated_now <= admission.valid_until
            and admission.agent_contract_digest == contract.digest
            and admission.prompt_package_digest == prompt_package_digest
            and admission.provider == provider
        )

    def require(
        self,
        contract: AgentContract,
        prompt_package_digest: str,
        provider: ModelProviderDescriptor,
    ) -> None:
        if not self.allows(contract, prompt_package_digest, provider):
            raise AgentModelAdmissionRequiredError(
                "Cloud Provider lacks an exact unexpired approved Agent admission"
            )

    @staticmethod
    def _provider_digest(provider: ModelProviderDescriptor) -> str:
        return sha256_digest(provider)


def load_agent_model_admissions(
    path: Path | None,
    *,
    explicitly_allowed: bool,
    environ: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> AgentModelAdmissionRegistry:
    """Load a trusted local evidence bundle only behind an explicit non-CI switch."""

    if path is None:
        if explicitly_allowed:
            raise AgentModelAdmissionError(
                "Agent model admission allow switch requires an evidence bundle path"
            )
        return AgentModelAdmissionRegistry()
    if not explicitly_allowed:
        raise AgentModelAdmissionError(
            "Agent model admission bundle requires an explicit allow switch"
        )
    environment = environ if environ is not None else os.environ
    if environment.get("CI", "").lower() in {"1", "true", "yes"}:
        raise AgentModelAdmissionError("CI cannot activate production model admissions")
    try:
        if path.is_symlink():
            raise AgentModelAdmissionError(
                "Agent model admission bundle cannot be a symbolic link"
            )
        resolved = path.resolve(strict=True)
        payload = resolved.read_bytes()
        if not payload or len(payload) > MAX_ADMISSION_BUNDLE_BYTES:
            raise AgentModelAdmissionError("Agent model admission bundle size is invalid")
        json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
        bundle = AgentModelAdmissionBundle.model_validate_json(payload, strict=True)
    except AgentModelAdmissionError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError) as error:
        raise AgentModelAdmissionError(
            "Agent model admission bundle failed strict loading"
        ) from error
    return AgentModelAdmissionRegistry.from_bundle(bundle, now=now)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AgentModelAdmissionError(
                "Agent model admission bundle contains a duplicate JSON key"
            )
        result[key] = value
    return result
