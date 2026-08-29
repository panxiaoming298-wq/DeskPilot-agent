"""Provider probe manifest, offline preflight, and explicitly authorized live gate."""

import argparse
import asyncio
import json
import os
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Protocol

from deskpilot.application.provider_probe_authorization import (
    ProviderProbeAuthorizationError,
    ProviderProbeOfflinePreflight,
    ProviderProbePolicyBundle,
    ProviderProbePolicyLoader,
    load_provider_probe_binding,
)
from deskpilot.application.provider_probe_execution import (
    FilesystemProviderProbePermitLedger,
    LiveProviderProbeFactory,
    ProviderProbeExecutionError,
    ProviderProbeExecutionSuiteBundle,
    ProviderProbeExecutionSuiteLoader,
    ProviderProbeRunner,
    load_provider_probe_execution_permit,
    validate_provider_probe_report_output,
    write_provider_probe_report_exclusive,
)
from deskpilot.domain.provider_probe_authorizations import (
    ProviderProbeOperatorBinding,
)
from deskpilot.domain.provider_probe_executions import (
    ProviderProbeExecutionPermit,
    ProviderProbeRunReport,
)
from deskpilot.infrastructure.credential_resolvers import (
    create_default_credential_resolver,
)

PROVIDER_PROBE_LIVE_ALLOW_VARIABLE = "DESKPILOT_PHASE115_PROVIDER_PROBE_LIVE_ALLOW"


class _ProviderProbeRunnerPort(Protocol):
    async def run(
        self,
        binding: ProviderProbeOperatorBinding,
        permit: ProviderProbeExecutionPermit,
    ) -> ProviderProbeRunReport: ...


class _ProviderProbeLiveRunnerBuilder(Protocol):
    def __call__(
        self,
        policy_bundle: ProviderProbePolicyBundle,
        execution_bundle: ProviderProbeExecutionSuiteBundle,
        ledger_path: Path,
    ) -> _ProviderProbeRunnerPort: ...


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise argparse.ArgumentTypeError("timestamp must be ISO-8601") from error
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("manifest")
    preflight = commands.add_parser("preflight")
    preflight.add_argument("--binding", type=Path, required=True)
    preflight.add_argument("--now", type=_timestamp)
    run = commands.add_parser("run")
    run.add_argument("--binding", type=Path, required=True)
    run.add_argument("--permit", type=Path, required=True)
    run.add_argument("--ledger", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--confirm-run-id", required=True)
    return parser


def _default_live_runner(
    policy_bundle: ProviderProbePolicyBundle,
    execution_bundle: ProviderProbeExecutionSuiteBundle,
    ledger_path: Path,
) -> _ProviderProbeRunnerPort:
    return ProviderProbeRunner(
        policy_bundle=policy_bundle,
        execution_bundle=execution_bundle,
        provider_factory=LiveProviderProbeFactory(),
        permit_ledger=FilesystemProviderProbePermitLedger(ledger_path),
        credential_resolver=create_default_credential_resolver(),
    )


def _assert_live_cli_authorized() -> None:
    if os.getenv("CI", "").lower() in {"1", "true", "yes"}:
        raise ProviderProbeExecutionError("CI is not allowed to execute live Provider probes")
    if os.getenv(PROVIDER_PROBE_LIVE_ALLOW_VARIABLE, "") != "1":
        raise ProviderProbeExecutionError(
            f"Live Provider probe requires {PROVIDER_PROBE_LIVE_ALLOW_VARIABLE}=1"
        )


async def _run_live(
    arguments: argparse.Namespace,
    *,
    policy_bundle: ProviderProbePolicyBundle,
    live_runner_builder: _ProviderProbeLiveRunnerBuilder,
) -> ProviderProbeRunReport:
    _assert_live_cli_authorized()
    binding = load_provider_probe_binding(arguments.binding)
    permit = load_provider_probe_execution_permit(arguments.permit)
    if permit.execution_mode != "live_provider":
        raise ProviderProbeExecutionError("Provider probe live CLI requires a live_provider permit")
    if arguments.confirm_run_id != permit.run_id:
        raise ProviderProbeExecutionError(
            "Provider probe live CLI run-id confirmation does not match"
        )
    validate_provider_probe_report_output(arguments.output)
    execution_bundle = ProviderProbeExecutionSuiteLoader(policy_bundle).load()
    runner = live_runner_builder(policy_bundle, execution_bundle, arguments.ledger)
    report = await runner.run(binding, permit)
    if (
        report.policy_digest != policy_bundle.policy_digest
        or report.execution_suite_digest != execution_bundle.suite_digest
        or report.binding_digest != binding.binding_digest
        or report.readiness_report_digest != permit.readiness_report_digest
        or report.permit_digest != permit.permit_digest
        or report.run_id != permit.run_id
        or report.execution_mode != "live_provider"
        or report.provider_family != binding.provider_family
        or report.provider_id != binding.provider_id
    ):
        raise ProviderProbeExecutionError(
            "Provider probe live report does not match its execution authority"
        )
    write_provider_probe_report_exclusive(arguments.output, report)
    return report


def main(
    argv: Sequence[str] | None = None,
    *,
    live_runner_builder: _ProviderProbeLiveRunnerBuilder = _default_live_runner,
) -> int:
    arguments = _parser().parse_args(argv)
    try:
        bundle = ProviderProbePolicyLoader().load()
        if arguments.command == "manifest":
            policy = bundle.policy
            execution_bundle = ProviderProbeExecutionSuiteLoader(bundle).load()
            print(
                json.dumps(
                    {
                        "policy_id": policy.policy_id,
                        "policy_digest": bundle.policy_digest,
                        "data_class": policy.data_class,
                        "planned_requests_per_provider": (policy.planned_requests_per_provider),
                        "planned_aggregate_requests": policy.planned_aggregate_requests,
                        "maximum_aggregate_requests": policy.maximum_aggregate_requests,
                        "profiles": [
                            {
                                "provider_family": item.provider_family,
                                "recommended_model": item.recommended_model,
                                "currency": item.budget.currency,
                                "maximum_total_microunits": (item.budget.maximum_total_microunits),
                                "maximum_per_request_microunits": (
                                    item.budget.maximum_per_request_microunits
                                ),
                                "maximum_requests": item.budget.maximum_requests,
                                "automatic_retries": item.budget.automatic_retries,
                                "credential_backend": item.credential_backend,
                                "planned_budget_envelope_microunits": (
                                    policy.planned_requests_per_provider
                                    * item.budget.maximum_per_request_microunits
                                ),
                                "cost_control": item.budget.cost_control.model_dump(mode="json"),
                            }
                            for item in policy.profiles
                        ],
                        "future_runner_guards": (
                            policy.future_runner_guards.model_dump(mode="json")
                        ),
                        "execution_contract": {
                            "suite_digest": execution_bundle.suite_digest,
                            "exact_request_count": (execution_bundle.suite.exact_request_count),
                            "maximum_permit_validity_minutes": (
                                execution_bundle.suite.maximum_permit_validity_minutes
                            ),
                            "one_shot_permit_required": True,
                            "durable_permit_claim_required": True,
                            "offline_mock_supported": True,
                            "live_runner_library_implemented": True,
                            "live_run_cli_available": True,
                            "live_run_default_allowed": False,
                            "live_allow_variable": PROVIDER_PROBE_LIVE_ALLOW_VARIABLE,
                            "ci_live_run_allowed": False,
                            "exact_run_id_confirmation_required": True,
                            "exclusive_report_output_required": True,
                        },
                        "execution_boundary_scope": "manifest_and_preflight_only",
                        "execution_boundary": policy.execution_boundary.model_dump(mode="json"),
                    },
                    sort_keys=True,
                )
            )
            return 0
        if arguments.command == "preflight":
            binding = load_provider_probe_binding(arguments.binding)
            readiness_report = ProviderProbeOfflinePreflight(bundle).run(
                binding,
                now=arguments.now,
            )
            print(json.dumps(readiness_report.model_dump(mode="json"), sort_keys=True))
            return 0 if readiness_report.ready else 2
        run_report = asyncio.run(
            _run_live(
                arguments,
                policy_bundle=bundle,
                live_runner_builder=live_runner_builder,
            )
        )
        print(
            json.dumps(
                {
                    "run_id": run_report.run_id,
                    "report_digest": run_report.report_digest,
                    "status": run_report.status,
                    "attempted_request_count": run_report.attempted_request_count,
                    "successful_request_count": run_report.successful_request_count,
                    "reserved_microunits": run_report.reserved_microunits,
                    "production_admission": run_report.production_admission,
                    "cloud_activation": run_report.cloud_activation,
                    "full_116c_b": run_report.full_116c_b,
                },
                sort_keys=True,
            )
        )
        return 0 if run_report.status == "completed" else 2
    except (ProviderProbeAuthorizationError, ProviderProbeExecutionError) as error:
        print(json.dumps({"code": error.code, "detail": str(error)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
