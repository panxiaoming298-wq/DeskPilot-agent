import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import httpx
import pytest

from deskpilot.application.model_gateway import ModelProvider
from deskpilot.application.provider_probe_authorization import (
    ProviderProbeOfflinePreflight,
    ProviderProbePolicyLoader,
)
from deskpilot.application.provider_probe_execution import (
    FilesystemProviderProbePermitLedger,
    ProviderProbeExecutionError,
    ProviderProbeExecutionSuiteBundle,
    ProviderProbeExecutionSuiteLoader,
    ProviderProbeProviderFactory,
    ProviderProbeRunner,
    load_provider_probe_execution_permit,
    write_provider_probe_report_exclusive,
)
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.provider_probe_authorizations import (
    ProviderProbeOperatorBinding,
)
from deskpilot.domain.provider_probe_executions import (
    ProviderProbeExecutionMode,
    ProviderProbeExecutionPermit,
    ProviderProbeRunReport,
)
from deskpilot.model_providers.openai_compatible_responses import (
    OpenAICompatibleResponsesProvider,
)
from deskpilot.phase115_provider_probe_gate import PROVIDER_PROBE_LIVE_ALLOW_VARIABLE
from deskpilot.phase115_provider_probe_gate import main as provider_probe_gate_main

BACKEND_ROOT = Path(__file__).parents[1]
EXECUTION_SUITE_PATH = (
    BACKEND_ROOT / "src" / "deskpilot" / "evaluations" / "phase115_provider_probe_execution_v1.yaml"
)
FIXED_NOW = datetime(2026, 8, 29, 9, tzinfo=UTC)

_BINDINGS = {
    "openai": (
        "openai-gpt56-luna",
        "gpt-5.6-luna",
        "https://api.openai.com/v1",
        "OPENAI_RESPONSES",
        "openai_application_envelope",
    ),
    "deepseek": (
        "deepseek-v4-flash",
        "deepseek-v4-flash",
        "https://api.deepseek.com",
        "DEEPSEEK",
        "deepseek_prepaid_balance",
    ),
    "bailian": (
        "bailian-qwen38-max",
        "qwen3.8-max",
        "https://workspace-123.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        "BAILIAN",
        "bailian_billing_alert",
    ),
}


def _binding(family: str) -> ProviderProbeOperatorBinding:
    policy_bundle = ProviderProbePolicyLoader().load()
    profile = next(item for item in policy_bundle.policy.profiles if item.provider_family == family)
    provider_id, model, base_url, credential_id, cost_mode = _BINDINGS[family]
    material: dict[str, Any] = {
        "schema_version": "deskpilot.provider-probe-operator-binding.v2",
        "policy_digest": policy_bundle.policy_digest,
        "provider_family": family,
        "provider_id": provider_id,
        "exact_model": model,
        "base_url": base_url,
        "credential_ref": {
            "backend": "windows_credential_manager",
            "identifier": credential_id,
        },
        "currency": profile.budget.currency,
        "maximum_total_microunits": profile.budget.maximum_total_microunits,
        "maximum_per_request_microunits": (profile.budget.maximum_per_request_microunits),
        "maximum_requests": profile.budget.maximum_requests,
        "automatic_retries": 0,
        "exact_model_confirmed": True,
        "credential_presence_confirmed": True,
        "base_url_key_pair_confirmed": True,
        "cost_control_mode": cost_mode,
        "provider_hard_limit_enforcing": False,
        "dedicated_probe_credential_confirmed": True,
        "application_budget_envelope_confirmed": True,
        "prepaid_balance_available_confirmed": family == "deepseek",
        "prepaid_balance_checked_at": FIXED_NOW if family == "deepseek" else None,
        "billing_alert_confirmed": family == "bailian",
        "billing_delay_acknowledged": family == "bailian",
        "free_quota_stop_enabled": False,
        "pricing_source_checked_at": FIXED_NOW,
        "confirmed_by": "reviewer_operator_owner",
        "confirmed_at": FIXED_NOW,
        "valid_until": FIXED_NOW + timedelta(hours=12),
    }
    return ProviderProbeOperatorBinding.model_validate(
        {**material, "binding_digest": sha256_digest(material)}
    )


def _permit(
    binding: ProviderProbeOperatorBinding,
    *,
    execution_mode: Literal["offline_mock", "live_provider"] = "offline_mock",
    overrides: dict[str, Any] | None = None,
) -> ProviderProbeExecutionPermit:
    policy_bundle = ProviderProbePolicyLoader().load()
    execution_bundle = ProviderProbeExecutionSuiteLoader(policy_bundle).load()
    readiness = ProviderProbeOfflinePreflight(policy_bundle).run(
        binding,
        now=FIXED_NOW,
    )
    profile = next(
        item
        for item in policy_bundle.policy.profiles
        if item.provider_family == binding.provider_family
    )
    material: dict[str, Any] = {
        "schema_version": "deskpilot.provider-probe-execution-permit.v1",
        "policy_digest": policy_bundle.policy_digest,
        "execution_suite_digest": execution_bundle.suite_digest,
        "binding_digest": binding.binding_digest,
        "readiness_report_digest": sha256_digest(readiness),
        "provider_family": binding.provider_family,
        "provider_id": binding.provider_id,
        "run_id": f"probe-run-{binding.provider_family}-0001",
        "execution_mode": execution_mode,
        "exact_request_count": 4,
        "maximum_reserved_microunits": (4 * profile.budget.maximum_per_request_microunits),
        "data_class": "public_synthetic",
        "network_access_authorized": execution_mode == "live_provider",
        "credential_resolution_authorized": execution_mode == "live_provider",
        "real_model_capture_authorized": execution_mode == "live_provider",
        "automatic_retries": 0,
        "operator_confirmation": (
            "RUN FOUR LIVE PUBLIC SYNTHETIC PROVIDER PROBES"
            if execution_mode == "live_provider"
            else "RUN FOUR OFFLINE MOCK PROVIDER PROBES"
        ),
        "approved_by": "reviewer_operator_owner",
        "approved_at": FIXED_NOW,
        "valid_until": FIXED_NOW + timedelta(minutes=10),
        "production_admission": False,
        "cloud_activation": False,
        "full_116c_b": False,
    }
    material.update(overrides or {})
    return ProviderProbeExecutionPermit.model_validate(
        {**material, "permit_digest": sha256_digest(material)}
    )


def _sse(event_type: str, sequence: int, **payload: object) -> str:
    body = {"type": event_type, "sequence_number": sequence, **payload}
    return f"event: {event_type}\ndata: {json.dumps(body)}\n\n"


def _claim_files(root: Path) -> list[Path]:
    return list(root.glob("provider-probe-*.claimed"))


def _directory_entries(root: Path) -> list[Path]:
    return list(root.iterdir())


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _failed_live_report(
    binding: ProviderProbeOperatorBinding,
    permit: ProviderProbeExecutionPermit,
) -> ProviderProbeRunReport:
    material = {
        "schema_version": "deskpilot.provider-probe-run-report.v1",
        "policy_digest": permit.policy_digest,
        "execution_suite_digest": permit.execution_suite_digest,
        "binding_digest": binding.binding_digest,
        "readiness_report_digest": permit.readiness_report_digest,
        "permit_digest": permit.permit_digest,
        "run_id": permit.run_id,
        "execution_mode": "live_provider",
        "provider_family": binding.provider_family,
        "provider_id": binding.provider_id,
        "model_digest": sha256_digest({"model": binding.exact_model}),
        "status": "failed",
        "attempted_request_count": 0,
        "successful_request_count": 0,
        "reserved_microunits": 0,
        "receipts": [],
        "terminal_error_code": "CREDENTIAL_NOT_FOUND",
        "started_at": FIXED_NOW,
        "completed_at": FIXED_NOW,
        "credentials_resolved": False,
        "network_request_count": 0,
        "real_model_capture": False,
        "automatic_retries": 0,
        "serial_execution": True,
        "stopped_on_first_error": True,
        "request_and_response_bodies_logged": False,
        "headers_logged": False,
        "credentials_logged": False,
        "production_admission": False,
        "cloud_activation": False,
        "full_116c_b": False,
    }
    return ProviderProbeRunReport.model_validate(
        {**material, "report_digest": sha256_digest(material)}
    )


def _success_response(request: httpx.Request, ordinal: int) -> httpx.Response:
    body = json.loads(request.content)
    schema = body["text"]["format"]["schema"]
    family = schema["properties"]["provider_family"]["enum"][0]
    case_id = schema["properties"]["case_id"]["enum"][0]
    output = json.dumps(
        {"provider_family": family, "case_id": case_id, "status": "ok"},
        separators=(",", ":"),
    )
    final = {
        "id": f"raw-native-response-{ordinal}",
        "model": body["model"],
        "status": "completed",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": output}],
            }
        ],
        "usage": {
            "input_tokens": 20 + ordinal,
            "output_tokens": 10,
            "total_tokens": 30 + ordinal,
            "input_tokens_details": {"cached_tokens": 0},
        },
    }
    if body["stream"] is False:
        return httpx.Response(200, json=final)
    created = {**final, "status": "in_progress", "output": [], "usage": None}
    content = (
        _sse("response.created", 0, response=created)
        + _sse("response.output_text.delta", 1, delta=output)
        + _sse("response.completed", 2, response=final)
    ).encode()
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=content,
    )


@dataclass
class _OfflineMockFactory(ProviderProbeProviderFactory):
    handler: Any
    calls: list[dict[str, Any]] = field(default_factory=list)

    @property
    def execution_mode(self) -> ProviderProbeExecutionMode:
        return "offline_mock"

    def build(
        self,
        binding: ProviderProbeOperatorBinding,
        credential: Any,
    ) -> ModelProvider:
        assert credential is None

        def capture(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            self.calls.append(body)
            return self.handler(request, len(self.calls))

        return OpenAICompatibleResponsesProvider(
            provider_id=binding.provider_id,
            display_name="Offline synthetic probe",
            model=binding.exact_model,
            base_url=binding.base_url,
            location="cloud",
            transport=httpx.MockTransport(capture),
            trust_env=False,
        )


@pytest.mark.parametrize("family", ("openai", "deepseek", "bailian"))
@pytest.mark.asyncio
async def test_offline_runner_executes_exact_four_serial_sanitized_requests(
    family: str,
    tmp_path: Path,
) -> None:
    binding = _binding(family)
    permit = _permit(binding)
    policy_bundle = ProviderProbePolicyLoader().load()
    execution_bundle = ProviderProbeExecutionSuiteLoader(policy_bundle).load()
    factory = _OfflineMockFactory(_success_response)
    runner = ProviderProbeRunner(
        policy_bundle=policy_bundle,
        execution_bundle=execution_bundle,
        provider_factory=factory,
        permit_ledger=FilesystemProviderProbePermitLedger(tmp_path),
        clock=lambda: FIXED_NOW,
    )

    report = await runner.run(binding, permit)

    assert report.status == "completed"
    assert report.attempted_request_count == 4
    assert report.successful_request_count == 4
    assert report.network_request_count == 0
    assert report.credentials_resolved is False
    assert report.real_model_capture is False
    assert report.production_admission is False
    assert report.cloud_activation is False
    assert report.full_116c_b is False
    assert [item["stream"] for item in factory.calls] == [False, False, True, True]
    assert [item["max_output_tokens"] for item in factory.calls] == [256] * 4
    assert all(item["store"] is False for item in factory.calls)
    assert [item.receipt_digest for item in report.receipts] == list(
        dict.fromkeys(item.receipt_digest for item in report.receipts)
    )
    serialized = report.model_dump_json()
    assert "raw-native-response" not in serialized
    assert binding.base_url not in serialized
    assert binding.credential_ref.identifier not in serialized
    assert "public synthetic compatibility probe" not in serialized.lower()
    report_path = tmp_path / "sanitized-report.json"
    write_provider_probe_report_exclusive(report_path, report)
    persisted = json.loads(_read_text(report_path))
    assert persisted["report_digest"] == report.report_digest
    assert binding.base_url not in _read_text(report_path)
    with pytest.raises(ProviderProbeExecutionError, match="new JSON"):
        write_provider_probe_report_exclusive(report_path, report)
    claim_files = _claim_files(tmp_path)
    assert len(claim_files) == 1
    assert binding.base_url not in claim_files[0].read_text(encoding="utf-8")

    with pytest.raises(ProviderProbeExecutionError, match="already consumed"):
        await runner.run(binding, permit)
    with pytest.raises(ProviderProbeExecutionError, match="already consumed"):
        await runner.run(
            binding,
            _permit(binding, overrides={"run_id": f"probe-run-{family}-0002"}),
        )
    assert len(factory.calls) == 4


@pytest.mark.asyncio
async def test_runner_stops_on_first_rate_limit_without_retry(tmp_path: Path) -> None:
    def rate_limited(_: httpx.Request, __: int) -> httpx.Response:
        return httpx.Response(
            429,
            json={"error": {"code": "rate_limit_exceeded"}},
        )

    binding = _binding("openai")
    policy_bundle = ProviderProbePolicyLoader().load()
    factory = _OfflineMockFactory(rate_limited)
    runner = ProviderProbeRunner(
        policy_bundle=policy_bundle,
        execution_bundle=ProviderProbeExecutionSuiteLoader(policy_bundle).load(),
        provider_factory=factory,
        permit_ledger=FilesystemProviderProbePermitLedger(tmp_path),
        clock=lambda: FIXED_NOW,
    )

    report = await runner.run(binding, _permit(binding))

    assert report.status == "failed"
    assert report.attempted_request_count == 1
    assert report.successful_request_count == 0
    assert report.terminal_error_code == "MODEL_RATE_LIMITED"
    assert report.receipts[0].error_code == "MODEL_RATE_LIMITED"
    assert len(factory.calls) == 1
    assert report.automatic_retries == 0
    assert report.stopped_on_first_error is True


@pytest.mark.asyncio
async def test_runner_requires_usage_and_stops_after_invalid_response(
    tmp_path: Path,
) -> None:
    def missing_usage(request: httpx.Request, ordinal: int) -> httpx.Response:
        response = _success_response(request, ordinal=ordinal)
        payload = response.json()
        payload.pop("usage")
        return httpx.Response(200, json=payload)

    binding = _binding("deepseek")
    policy_bundle = ProviderProbePolicyLoader().load()
    factory = _OfflineMockFactory(missing_usage)
    runner = ProviderProbeRunner(
        policy_bundle=policy_bundle,
        execution_bundle=ProviderProbeExecutionSuiteLoader(policy_bundle).load(),
        provider_factory=factory,
        permit_ledger=FilesystemProviderProbePermitLedger(tmp_path),
        clock=lambda: FIXED_NOW,
    )

    report = await runner.run(binding, _permit(binding))

    assert report.status == "failed"
    assert report.attempted_request_count == 1
    assert report.terminal_error_code == "MODEL_RESPONSE_INVALID"
    assert report.receipts[0].usage is None
    assert len(factory.calls) == 1


@pytest.mark.asyncio
async def test_runner_rejects_expired_or_drifted_permit_before_claim(
    tmp_path: Path,
) -> None:
    binding = _binding("bailian")
    policy_bundle = ProviderProbePolicyLoader().load()
    factory = _OfflineMockFactory(_success_response)
    runner = ProviderProbeRunner(
        policy_bundle=policy_bundle,
        execution_bundle=ProviderProbeExecutionSuiteLoader(policy_bundle).load(),
        provider_factory=factory,
        permit_ledger=FilesystemProviderProbePermitLedger(tmp_path),
        clock=lambda: FIXED_NOW + timedelta(minutes=11),
    )

    with pytest.raises(ProviderProbeExecutionError, match="permit"):
        await runner.run(binding, _permit(binding))

    assert not _directory_entries(tmp_path)
    assert factory.calls == []


def test_execution_suite_is_immutable_strict_and_policy_bound(tmp_path: Path) -> None:
    policy_bundle = ProviderProbePolicyLoader().load()
    bundle = ProviderProbeExecutionSuiteLoader(policy_bundle).load()

    assert bundle.suite.provider_probe_policy_digest == policy_bundle.policy_digest
    assert bundle.suite.exact_request_count == 4
    assert bundle.suite.maximum_permit_validity_minutes == 15
    assert bundle.suite.automatic_retries == 0
    assert bundle.suite.request_and_response_bodies_logged is False
    assert bundle.suite.headers_logged is False
    assert bundle.suite.production_admission is False
    assert bundle.suite.cloud_activation is False
    assert bundle.suite.full_116c_b is False
    assert bundle.suite_digest == (
        "5096f22c0d600a1282d6121437476dec2999be45bad93b09c8003d623fa1f326"
    )

    text = EXECUTION_SUITE_PATH.read_text(encoding="utf-8")
    aliased = tmp_path / "alias.yaml"
    aliased.write_text(
        text.replace(
            "data_class: public_synthetic",
            "data_class: &data_class public_synthetic",
            1,
        ),
        encoding="utf-8",
    )
    with pytest.raises(ProviderProbeExecutionError, match="aliases"):
        ProviderProbeExecutionSuiteLoader(policy_bundle, aliased).load()

    drifted = tmp_path / "drifted.yaml"
    drifted.write_text(
        text.replace(policy_bundle.policy_digest, f"'{('0' * 64)}'"),
        encoding="utf-8",
    )
    with pytest.raises(ProviderProbeExecutionError, match="another probe policy"):
        ProviderProbeExecutionSuiteLoader(policy_bundle, drifted).load()


def test_execution_permit_loader_rejects_duplicate_keys(tmp_path: Path) -> None:
    permit = _permit(_binding("openai"))
    valid_path = tmp_path / "valid-permit.json"
    valid_path.write_text(permit.model_dump_json(), encoding="utf-8")
    assert load_provider_probe_execution_permit(valid_path) == permit

    path = tmp_path / "duplicate-permit.json"
    path.write_text(
        permit.model_dump_json().replace(
            '"schema_version":',
            '"schema_version":"deskpilot.provider-probe-execution-permit.v1","schema_version":',
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ProviderProbeExecutionError, match="duplicate JSON key"):
        load_provider_probe_execution_permit(path)


def test_runner_recomputes_execution_bundle_digest_before_accepting_it(
    tmp_path: Path,
) -> None:
    policy_bundle = ProviderProbePolicyLoader().load()
    execution_bundle = ProviderProbeExecutionSuiteLoader(policy_bundle).load()

    with pytest.raises(ProviderProbeExecutionError, match="bundle digest"):
        ProviderProbeRunner(
            policy_bundle=policy_bundle,
            execution_bundle=ProviderProbeExecutionSuiteBundle(
                suite=execution_bundle.suite,
                suite_digest="0" * 64,
            ),
            provider_factory=_OfflineMockFactory(_success_response),
            permit_ledger=FilesystemProviderProbePermitLedger(tmp_path),
            clock=lambda: FIXED_NOW,
        )


@pytest.mark.parametrize(
    ("ci_value", "live_allow", "expected_detail"),
    (
        (None, None, PROVIDER_PROBE_LIVE_ALLOW_VARIABLE),
        ("true", "1", "CI is not allowed"),
    ),
)
def test_live_cli_fails_before_loading_inputs_or_building_runner(
    ci_value: str | None,
    live_allow: str | None,
    expected_detail: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    if ci_value is None:
        monkeypatch.delenv("CI", raising=False)
    else:
        monkeypatch.setenv("CI", ci_value)
    if live_allow is None:
        monkeypatch.delenv(PROVIDER_PROBE_LIVE_ALLOW_VARIABLE, raising=False)
    else:
        monkeypatch.setenv(PROVIDER_PROBE_LIVE_ALLOW_VARIABLE, live_allow)
    built = False

    def forbidden_builder(*_: object) -> Any:
        nonlocal built
        built = True
        raise AssertionError("live runner must not be built")

    output = tmp_path / "must-not-exist.json"
    result = provider_probe_gate_main(
        (
            "run",
            "--binding",
            str(tmp_path / "missing-binding.json"),
            "--permit",
            str(tmp_path / "missing-permit.json"),
            "--ledger",
            str(tmp_path),
            "--output",
            str(output),
            "--confirm-run-id",
            "probe-run-never-0001",
        ),
        live_runner_builder=forbidden_builder,
    )

    assert result == 2
    error = json.loads(capsys.readouterr().out)
    assert expected_detail in error["detail"]
    assert built is False
    assert output.exists() is False


def test_live_cli_requires_exact_run_id_and_new_output_before_runner_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv(PROVIDER_PROBE_LIVE_ALLOW_VARIABLE, "1")
    binding = _binding("openai")
    permit = _permit(binding, execution_mode="live_provider")
    binding_path = tmp_path / "binding.json"
    permit_path = tmp_path / "permit.json"
    binding_path.write_text(binding.model_dump_json(), encoding="utf-8")
    permit_path.write_text(permit.model_dump_json(), encoding="utf-8")
    built = False

    def forbidden_builder(*_: object) -> Any:
        nonlocal built
        built = True
        raise AssertionError("live runner must not be built")

    common = (
        "run",
        "--binding",
        str(binding_path),
        "--permit",
        str(permit_path),
        "--ledger",
        str(tmp_path),
        "--output",
        str(tmp_path / "report.json"),
    )
    assert (
        provider_probe_gate_main(
            (*common, "--confirm-run-id", "probe-run-wrong-0001"),
            live_runner_builder=forbidden_builder,
        )
        == 2
    )
    assert "run-id confirmation" in json.loads(capsys.readouterr().out)["detail"]

    output = tmp_path / "report.json"
    output.write_text("reserved", encoding="utf-8")
    assert (
        provider_probe_gate_main(
            (*common, "--confirm-run-id", permit.run_id),
            live_runner_builder=forbidden_builder,
        )
        == 2
    )
    assert "new JSON file" in json.loads(capsys.readouterr().out)["detail"]
    assert output.read_text(encoding="utf-8") == "reserved"
    assert built is False


def test_live_cli_writes_only_authority_bound_sanitized_terminal_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv(PROVIDER_PROBE_LIVE_ALLOW_VARIABLE, "1")
    binding = _binding("deepseek")
    permit = _permit(binding, execution_mode="live_provider")
    binding_path = tmp_path / "binding.json"
    permit_path = tmp_path / "permit.json"
    output = tmp_path / "terminal-report.json"
    binding_path.write_text(binding.model_dump_json(), encoding="utf-8")
    permit_path.write_text(permit.model_dump_json(), encoding="utf-8")
    report = _failed_live_report(binding, permit)
    calls = 0

    class StubRunner:
        async def run(
            self,
            actual_binding: ProviderProbeOperatorBinding,
            actual_permit: ProviderProbeExecutionPermit,
        ) -> ProviderProbeRunReport:
            assert actual_binding == binding
            assert actual_permit == permit
            return report

    def builder(*_: object) -> StubRunner:
        nonlocal calls
        calls += 1
        return StubRunner()

    result = provider_probe_gate_main(
        (
            "run",
            "--binding",
            str(binding_path),
            "--permit",
            str(permit_path),
            "--ledger",
            str(tmp_path),
            "--output",
            str(output),
            "--confirm-run-id",
            permit.run_id,
        ),
        live_runner_builder=builder,
    )

    assert result == 2
    assert calls == 1
    summary = json.loads(capsys.readouterr().out)
    assert summary["report_digest"] == report.report_digest
    assert summary["status"] == "failed"
    persisted = _read_text(output)
    assert json.loads(persisted)["terminal_error_code"] == "CREDENTIAL_NOT_FOUND"
    assert binding.base_url not in persisted
    assert binding.credential_ref.identifier not in persisted
