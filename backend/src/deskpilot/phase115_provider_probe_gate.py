"""Network-free manifest and operator-binding preflight for Provider probes."""

import argparse
import json
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from deskpilot.application.provider_probe_authorization import (
    ProviderProbeAuthorizationError,
    ProviderProbeOfflinePreflight,
    ProviderProbePolicyLoader,
    load_provider_probe_binding,
)
from deskpilot.application.provider_probe_execution import (
    ProviderProbeExecutionError,
    ProviderProbeExecutionSuiteLoader,
)


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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
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
                        "planned_requests_per_provider": (
                            policy.planned_requests_per_provider
                        ),
                        "planned_aggregate_requests": policy.planned_aggregate_requests,
                        "maximum_aggregate_requests": policy.maximum_aggregate_requests,
                        "profiles": [
                            {
                                "provider_family": item.provider_family,
                                "recommended_model": item.recommended_model,
                                "currency": item.budget.currency,
                                "maximum_total_microunits": (
                                    item.budget.maximum_total_microunits
                                ),
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
                                "cost_control": item.budget.cost_control.model_dump(
                                    mode="json"
                                ),
                            }
                            for item in policy.profiles
                        ],
                        "future_runner_guards": (
                            policy.future_runner_guards.model_dump(mode="json")
                        ),
                        "execution_contract": {
                            "suite_digest": execution_bundle.suite_digest,
                            "exact_request_count": (
                                execution_bundle.suite.exact_request_count
                            ),
                            "maximum_permit_validity_minutes": (
                                execution_bundle.suite.maximum_permit_validity_minutes
                            ),
                            "one_shot_permit_required": True,
                            "durable_permit_claim_required": True,
                            "offline_mock_supported": True,
                            "live_runner_library_implemented": True,
                            "live_run_cli_available": False,
                        },
                        "execution_boundary": policy.execution_boundary.model_dump(
                            mode="json"
                        ),
                    },
                    sort_keys=True,
                )
            )
            return 0
        binding = load_provider_probe_binding(arguments.binding)
        report = ProviderProbeOfflinePreflight(bundle).run(binding, now=arguments.now)
        print(json.dumps(report.model_dump(mode="json"), sort_keys=True))
        return 0 if report.ready else 2
    except (ProviderProbeAuthorizationError, ProviderProbeExecutionError) as error:
        print(json.dumps({"code": error.code, "detail": str(error)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
