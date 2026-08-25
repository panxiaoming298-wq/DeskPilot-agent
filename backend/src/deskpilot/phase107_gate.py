"""Explicit live capture and read-only Judge-human calibration gates for phase 107."""

import argparse
import asyncio
import json
import os
from pathlib import Path

from deskpilot.application.model_gateway import ModelProvider
from deskpilot.application.phase107_calibration import (
    Phase107CalibrationError,
    Phase107CalibrationService,
)
from deskpilot.core.config import Settings
from deskpilot.domain.model_contracts import ModelProtocol
from deskpilot.infrastructure.credential_resolvers import create_default_credential_resolver
from deskpilot.model_providers.factory import create_configured_model_providers

DEFAULT_SUITE = Path("tests/fixtures/phase107-live-agent-calibration-suite.v1.json")
DEFAULT_SUITE_V3 = Path("tests/fixtures/phase115-live-agent-calibration-suite.v2.json")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    capture = subparsers.add_parser("capture")
    capture.add_argument("--suite", type=Path)
    capture.add_argument("--provider-id", required=True)
    capture.add_argument("--build-id", required=True)
    capture.add_argument("--artifact-schema-version", choices=("v1", "v2", "v3"), default="v2")
    capture.add_argument("--turn-planner-version", default="2.0.0")
    capture.add_argument("--coordinator-version")
    capture.add_argument("--patch-version")
    capture.add_argument("--output", type=Path, required=True)

    packet = subparsers.add_parser("packet")
    packet.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    packet.add_argument("--run", type=Path, required=True)
    packet.add_argument("--output", type=Path, required=True)

    judge = subparsers.add_parser("judge")
    judge.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    judge.add_argument("--run", type=Path, required=True)
    judge.add_argument("--packet", type=Path, required=True)
    judge.add_argument("--provider-id", required=True)
    judge.add_argument("--build-id", required=True)
    judge.add_argument("--output", type=Path, required=True)

    grade = subparsers.add_parser("grade")
    grade.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    grade.add_argument("--run", type=Path, required=True)
    grade.add_argument("--packet", type=Path, required=True)
    grade.add_argument("--judge", type=Path, required=True)
    grade.add_argument("--reviews", type=Path, required=True)
    grade.add_argument("--output", type=Path, required=True)

    compare = subparsers.add_parser("compare")
    compare.add_argument("--baseline", type=Path, required=True)
    compare.add_argument("--report", type=Path, required=True)
    return parser


async def _capture(arguments: argparse.Namespace) -> dict[str, object]:
    is_v3 = arguments.artifact_schema_version == "v3"
    provider = _live_provider(
        arguments.provider_id,
        live_allow_variable=(
            "DESKPILOT_PHASE115_LIVE_ALLOW"
            if is_v3
            else "DESKPILOT_PHASE107_LIVE_ALLOW"
        ),
        require_cloud=is_v3,
    )
    service = Phase107CalibrationService()
    suite = service.load_suite(
        arguments.suite or (DEFAULT_SUITE_V3 if is_v3 else DEFAULT_SUITE)
    )
    run = await service.capture(
        suite,
        provider,
        build_id=arguments.build_id,
        turn_planner_version=arguments.turn_planner_version,
        coordinator_version=arguments.coordinator_version
        or ("2.0.0" if is_v3 else "1.1.0"),
        patch_version=arguments.patch_version or ("2.0.0" if is_v3 else "1.0.0"),
        artifact_schema_version=arguments.artifact_schema_version,
    )
    service.dump(arguments.output, run)
    return {
        "run_id": run.run_id,
        "run_digest": run.run_digest,
        "cohort_digest": run.cohort_digest,
        "status": run.status,
        "sample_count": len(run.trials),
        "calibrated_agents": [
            {
                "agent_id": item.agent_id,
                "agent_version": item.agent_version,
                "agent_contract_digest": item.agent_contract_digest,
                "prompt_package_digest": item.prompt_package_digest,
            }
            for item in run.calibrated_agents
        ],
    }


async def _judge(arguments: argparse.Namespace) -> dict[str, object]:
    service = Phase107CalibrationService()
    suite = service.load_suite(arguments.suite)
    run = service.load_run(arguments.run)
    is_v3 = run.schema_version == "deskpilot.phase115-calibration-run.v3"
    provider = _live_provider(
        arguments.provider_id,
        live_allow_variable=(
            "DESKPILOT_PHASE115_LIVE_ALLOW"
            if is_v3
            else "DESKPILOT_PHASE107_LIVE_ALLOW"
        ),
        require_cloud=is_v3,
    )
    packet = service.load_packet(arguments.packet)
    judge_run = await service.judge(
        suite,
        run,
        packet,
        provider,
        build_id=arguments.build_id,
    )
    service.dump(arguments.output, judge_run)
    return {
        "judge_run_id": judge_run.judge_run_id,
        "judge_run_digest": judge_run.judge_run_digest,
        "judge_cohort_digest": judge_run.judge_cohort_digest,
        "status": judge_run.status,
        "sample_count": len(judge_run.trials),
    }


def _live_provider(
    provider_id: str,
    *,
    live_allow_variable: str = "DESKPILOT_PHASE107_LIVE_ALLOW",
    require_cloud: bool = False,
) -> ModelProvider:
    if os.getenv("CI", "").lower() in {"1", "true", "yes"}:
        raise Phase107CalibrationError("CI is not allowed to capture live model evidence")
    if os.getenv(live_allow_variable, "") != "1":
        raise Phase107CalibrationError(
            f"Live capture requires {live_allow_variable}=1"
        )
    try:
        settings = Settings()
        providers = create_configured_model_providers(
            settings,
            create_default_credential_resolver(),
        )
    except Exception as error:
        raise Phase107CalibrationError(
            "Live Provider configuration failed validation"
        ) from error
    provider = next(
        (item for item in providers if item.descriptor.provider_id == provider_id),
        None,
    )
    if provider is None:
        raise Phase107CalibrationError("Requested live Provider is not configured")
    if provider.descriptor.protocol is ModelProtocol.FAKE:
        raise Phase107CalibrationError("Fake Provider cannot produce live calibration evidence")
    if require_cloud and provider.descriptor.location.value != "cloud":
        raise Phase107CalibrationError(
            "Calibration v3 requires an explicitly selected cloud Provider"
        )
    capabilities = provider.descriptor.capabilities
    if (
        not capabilities.structured_output
        or not capabilities.strict_json_schema
        or capabilities.max_context_tokens < 8_192
    ):
        raise Phase107CalibrationError(
            "Live Provider does not satisfy the frozen Agent request contract"
        )
    return provider


def main() -> int:
    arguments = _parser().parse_args()
    service = Phase107CalibrationService()
    try:
        if arguments.command == "capture":
            result = asyncio.run(_capture(arguments))
        elif arguments.command == "packet":
            suite = service.load_suite(arguments.suite)
            run = service.load_run(arguments.run)
            packet = service.make_blind_packet(suite, run)
            service.dump(arguments.output, packet)
            result = {
                "packet_digest": packet.packet_digest,
                "sample_count": len(packet.samples),
            }
        elif arguments.command == "judge":
            result = asyncio.run(_judge(arguments))
        elif arguments.command == "grade":
            suite = service.load_suite(arguments.suite)
            run = service.load_run(arguments.run)
            packet = service.load_packet(arguments.packet)
            judge_run = service.load_judge_run(arguments.judge)
            reviews = service.load_review_bundle(arguments.reviews)
            report = service.grade(suite, run, packet, judge_run, reviews)
            service.dump(arguments.output, report)
            result = {
                "report_digest": report.report_digest,
                "status": report.status,
                "sample_count": report.sample_count,
                "acceptance_rate": report.acceptance_rate,
                "primary_disagreement_rate": report.primary_disagreement_rate,
                "safety_reject_count": report.safety_reject_count,
                "judge_human_agreement_rate": report.judge_human_agreement_rate,
                "judge_false_accept_count": report.judge_false_accept_count,
            }
        else:
            baseline = service.load_baseline(arguments.baseline)
            report = service.load_report(arguments.report)
            violations = service.compare(baseline, report)
            result = {
                "baseline_id": baseline.baseline_id,
                "report_digest": report.report_digest,
                "passed": not violations,
                "violations": violations,
            }
            print(json.dumps(result, sort_keys=True))
            return 0 if not violations else 1
        print(json.dumps(result, sort_keys=True))
        return 0
    except Phase107CalibrationError as error:
        print(json.dumps({"code": error.code, "detail": str(error)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
