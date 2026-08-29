"""Fail-closed execution service for one-shot Provider compatibility probes."""

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import yaml
from pydantic import SecretStr, ValidationError
from yaml.tokens import AliasToken, AnchorToken

from deskpilot.application.credential_resolver import (
    CredentialResolutionError,
    CredentialResolver,
)
from deskpilot.application.model_gateway import ModelGatewayError, ModelProvider
from deskpilot.application.provider_probe_authorization import (
    ProviderProbeOfflinePreflight,
    ProviderProbePolicyBundle,
)
from deskpilot.core.canonical_json import canonical_json_bytes, sha256_digest
from deskpilot.domain.model_contracts import (
    ModelCapabilityRequirements,
    ModelExecutionBudget,
    ModelLocation,
    ModelMessage,
    ModelProtocol,
    ModelRequest,
    ModelResponse,
    ModelRole,
    ModelStreamEventType,
    StructuredOutputDefinition,
    ToolCallingMode,
)
from deskpilot.domain.provider_probe_authorizations import (
    ProviderProbeCasePolicy,
    ProviderProbeOperatorBinding,
    ProviderProbeProfilePolicy,
)
from deskpilot.domain.provider_probe_executions import (
    ProviderProbeExecutionCase,
    ProviderProbeExecutionMode,
    ProviderProbeExecutionPermit,
    ProviderProbeExecutionSuite,
    ProviderProbeRequestReceipt,
    ProviderProbeRunReport,
    ProviderProbeSyntheticResult,
)
from deskpilot.model_providers.openai_compatible_responses import (
    OpenAICompatibleResponsesProvider,
)

MAX_PROVIDER_PROBE_EXECUTION_SUITE_BYTES = 65_536
MAX_PROVIDER_PROBE_EXECUTION_PERMIT_BYTES = 65_536
_WINDOWS_REPARSE_POINT = 0x400


class ProviderProbeExecutionError(RuntimeError):
    code = "PROVIDER_PROBE_EXECUTION_REJECTED"


class _ProviderProbeResponseError(RuntimeError):
    code = "PROVIDER_PROBE_RESPONSE_MISMATCH"


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProviderProbeExecutionError(
                "Provider probe execution permit contains a duplicate JSON key"
            )
        result[key] = value
    return result


def load_provider_probe_execution_permit(
    path: Path,
) -> ProviderProbeExecutionPermit:
    try:
        if path.is_symlink() or not path.is_file():
            raise ProviderProbeExecutionError(
                "Provider probe execution permit must be one regular file"
            )
        payload = path.read_bytes()
        if not payload or len(payload) > MAX_PROVIDER_PROBE_EXECUTION_PERMIT_BYTES:
            raise ProviderProbeExecutionError(
                "Provider probe execution permit is empty or exceeds its size limit"
            )
        json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
        return ProviderProbeExecutionPermit.model_validate_json(payload, strict=True)
    except ProviderProbeExecutionError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError) as error:
        raise ProviderProbeExecutionError(
            "Provider probe execution permit failed strict loading"
        ) from error


def validate_provider_probe_report_output(path: Path) -> Path:
    """Validate a new local JSON destination without creating or replacing it."""
    try:
        if path.suffix.lower() != ".json" or path.exists() or path.is_symlink():
            raise ProviderProbeExecutionError(
                "Provider probe report output must be one new JSON file"
            )
        parent = path.parent.resolve(strict=True)
        attributes = getattr(parent.stat(), "st_file_attributes", 0)
        if path.parent.is_symlink() or not parent.is_dir() or attributes & _WINDOWS_REPARSE_POINT:
            raise ProviderProbeExecutionError(
                "Provider probe report parent must be one non-reparse directory"
            )
        return parent / path.name
    except ProviderProbeExecutionError:
        raise
    except OSError as error:
        raise ProviderProbeExecutionError("Provider probe report output is unavailable") from error


def write_provider_probe_report_exclusive(
    path: Path,
    report: ProviderProbeRunReport,
) -> None:
    """Durably create one sanitized report without an overwrite path."""
    output = validate_provider_probe_report_output(path)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    flags |= getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(output, flags, 0o600)
    except FileExistsError as error:
        raise ProviderProbeExecutionError("Provider probe report output already exists") from error
    except OSError as error:
        raise ProviderProbeExecutionError(
            "Provider probe report output could not be created"
        ) from error
    payload = canonical_json_bytes(report) + b"\n"
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as error:
        raise ProviderProbeExecutionError(
            "Provider probe report output could not be durably written"
        ) from error


@dataclass(frozen=True, slots=True)
class ProviderProbeExecutionSuiteBundle:
    suite: ProviderProbeExecutionSuite
    suite_digest: str


class ProviderProbeProviderFactory(Protocol):
    @property
    def execution_mode(self) -> ProviderProbeExecutionMode: ...

    def build(
        self,
        binding: ProviderProbeOperatorBinding,
        credential: SecretStr | None,
    ) -> ModelProvider: ...


class ProviderProbePermitLedger(Protocol):
    def claim(
        self,
        permit: ProviderProbeExecutionPermit,
        *,
        claimed_at: datetime,
    ) -> bool: ...


class LiveProviderProbeFactory:
    """Trusted live HTTP composition; no CLI or default application wiring uses it."""

    @property
    def execution_mode(self) -> ProviderProbeExecutionMode:
        return "live_provider"

    def build(
        self,
        binding: ProviderProbeOperatorBinding,
        credential: SecretStr | None,
    ) -> ModelProvider:
        if credential is None:
            raise ProviderProbeExecutionError("Live Provider probe requires a resolved credential")
        return OpenAICompatibleResponsesProvider(
            provider_id=binding.provider_id,
            display_name=f"{binding.provider_family.title()} public probe",
            model=binding.exact_model,
            base_url=binding.base_url,
            api_key=credential,
            location=ModelLocation.CLOUD,
            supports_streaming=True,
            supports_structured_output=True,
            supports_strict_json_schema=True,
            trust_env=False,
        )


class FilesystemProviderProbePermitLedger:
    """Atomically consume permits in an operator-staged local directory."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def claim(
        self,
        permit: ProviderProbeExecutionPermit,
        *,
        claimed_at: datetime,
    ) -> bool:
        try:
            root = self._root.resolve(strict=True)
            attributes = getattr(root.stat(), "st_file_attributes", 0)
            if self._root.is_symlink() or not root.is_dir() or attributes & _WINDOWS_REPARSE_POINT:
                raise ProviderProbeExecutionError(
                    "Provider probe permit ledger must be one non-reparse directory"
                )
            claim_key = sha256_digest(
                {
                    "policy_digest": permit.policy_digest,
                    "execution_suite_digest": permit.execution_suite_digest,
                    "binding_digest": permit.binding_digest,
                    "provider_family": permit.provider_family,
                }
            )
            marker = root / f"provider-probe-{claim_key}.claimed"
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            flags |= getattr(os, "O_BINARY", 0)
            try:
                descriptor = os.open(marker, flags, 0o600)
            except FileExistsError:
                return False
            payload = canonical_json_bytes(
                {
                    "schema_version": "deskpilot.provider-probe-permit-claim.v1",
                    "claim_key": claim_key,
                    "permit_digest": permit.permit_digest,
                    "binding_digest": permit.binding_digest,
                    "provider_family": permit.provider_family,
                    "run_id": permit.run_id,
                    "claimed_at": claimed_at,
                }
            )
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
            except OSError as error:
                raise ProviderProbeExecutionError(
                    "Provider probe permit claim could not be durably recorded"
                ) from error
            return True
        except ProviderProbeExecutionError:
            raise
        except OSError as error:
            raise ProviderProbeExecutionError(
                "Provider probe permit ledger is unavailable"
            ) from error


class ProviderProbeExecutionSuiteLoader:
    def __init__(
        self,
        policy_bundle: ProviderProbePolicyBundle,
        suite_path: Path | None = None,
    ) -> None:
        if policy_bundle.policy_digest != sha256_digest(policy_bundle.policy):
            raise ProviderProbeExecutionError("Provider probe policy bundle digest is invalid")
        self._policy_bundle = policy_bundle
        self._suite_path = suite_path or (
            Path(__file__).parents[1] / "evaluations" / "phase115_provider_probe_execution_v1.yaml"
        )

    def load(self) -> ProviderProbeExecutionSuiteBundle:
        try:
            suite = ProviderProbeExecutionSuite.model_validate(self._strict_yaml(self._suite_path))
        except ProviderProbeExecutionError:
            raise
        except ValidationError as error:
            raise ProviderProbeExecutionError(
                "Provider probe execution suite failed strict validation"
            ) from error
        policy = self._policy_bundle.policy
        if suite.provider_probe_policy_digest != self._policy_bundle.policy_digest:
            raise ProviderProbeExecutionError(
                "Provider probe execution suite targets another probe policy"
            )
        policy_cases = {item.case_id: item for item in policy.cases}
        for execution_case in suite.cases:
            policy_case = policy_cases.get(execution_case.case_id)
            if policy_case is None or (
                execution_case.transport != policy_case.transport
                or execution_case.repeat_count != policy_case.repeat_count
                or len(execution_case.system_prompt)
                + len(
                    self._render_user_prompt(
                        execution_case,
                        provider_family="deepseek",
                    )
                )
                > policy_case.maximum_input_characters
            ):
                raise ProviderProbeExecutionError(
                    "Provider probe execution suite drifted from its policy"
                )
        guards = policy.future_runner_guards
        if (
            suite.exact_request_count != policy.planned_requests_per_provider
            or suite.serial_execution != guards.serial_execution
            or suite.stop_on_first_error != guards.stop_on_first_error
            or suite.usage_required != guards.usage_required
            or suite.request_and_response_bodies_logged != guards.request_and_response_bodies_logged
            or suite.headers_logged != guards.headers_logged
        ):
            raise ProviderProbeExecutionError(
                "Provider probe execution guards drifted from their policy"
            )
        return ProviderProbeExecutionSuiteBundle(
            suite=suite,
            suite_digest=sha256_digest(suite),
        )

    @staticmethod
    def _strict_yaml(path: Path) -> object:
        try:
            if path.is_symlink() or not path.is_file():
                raise ProviderProbeExecutionError(
                    "Provider probe execution suite must be one regular file"
                )
            payload = path.read_bytes()
            if not payload or len(payload) > MAX_PROVIDER_PROBE_EXECUTION_SUITE_BYTES:
                raise ProviderProbeExecutionError(
                    "Provider probe execution suite is empty or exceeds its size limit"
                )
            text = payload.decode("utf-8")
            if any(isinstance(token, AnchorToken | AliasToken) for token in yaml.scan(text)):
                raise ProviderProbeExecutionError(
                    "Provider probe execution suite YAML aliases are not allowed"
                )
            return yaml.safe_load(text)
        except ProviderProbeExecutionError:
            raise
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
            raise ProviderProbeExecutionError(
                "Provider probe execution suite failed strict loading"
            ) from error

    @staticmethod
    def _render_user_prompt(
        execution_case: ProviderProbeExecutionCase,
        *,
        provider_family: str,
    ) -> str:
        return execution_case.user_prompt_template.replace(
            "{provider_family}", provider_family
        ).replace("{case_id}", execution_case.case_id)


class ProviderProbeRunner:
    """Consume one permit and execute four requests serially with no retry path."""

    def __init__(
        self,
        *,
        policy_bundle: ProviderProbePolicyBundle,
        execution_bundle: ProviderProbeExecutionSuiteBundle,
        provider_factory: ProviderProbeProviderFactory,
        permit_ledger: ProviderProbePermitLedger,
        credential_resolver: CredentialResolver | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if policy_bundle.policy_digest != sha256_digest(policy_bundle.policy):
            raise ProviderProbeExecutionError("Provider probe policy bundle digest is invalid")
        if (
            execution_bundle.suite_digest != sha256_digest(execution_bundle.suite)
            or execution_bundle.suite.provider_probe_policy_digest != policy_bundle.policy_digest
        ):
            raise ProviderProbeExecutionError("Provider probe execution bundle digest is invalid")
        self._policy_bundle = policy_bundle
        self._execution_bundle = execution_bundle
        self._provider_factory = provider_factory
        self._permit_ledger = permit_ledger
        self._credential_resolver = credential_resolver
        self._clock = clock or (lambda: datetime.now(UTC))

    async def run(
        self,
        binding: ProviderProbeOperatorBinding,
        permit: ProviderProbeExecutionPermit,
    ) -> ProviderProbeRunReport:
        started_at = self._now()
        readiness = ProviderProbeOfflinePreflight(self._policy_bundle).run(
            binding,
            now=started_at,
        )
        if not readiness.ready:
            raise ProviderProbeExecutionError(
                "Provider probe binding did not pass current offline readiness"
            )
        readiness_digest = sha256_digest(readiness)
        profile = self._profile(binding.provider_family)
        self._validate_permit(
            binding,
            permit,
            readiness_digest=readiness_digest,
            profile=profile,
            now=started_at,
        )
        if self._provider_factory.execution_mode != permit.execution_mode:
            raise ProviderProbeExecutionError(
                "Provider probe factory does not match the permitted execution mode"
            )
        if permit.execution_mode == "offline_mock":
            if self._credential_resolver is not None:
                raise ProviderProbeExecutionError(
                    "Offline mock Provider probe cannot receive a credential resolver"
                )
        elif self._credential_resolver is None:
            raise ProviderProbeExecutionError(
                "Live Provider probe requires an explicit credential resolver"
            )
        if not self._permit_ledger.claim(permit, claimed_at=started_at):
            raise ProviderProbeExecutionError(
                "Provider probe execution permit was already consumed"
            )

        credential: SecretStr | None = None
        credentials_resolved = False
        if permit.execution_mode == "live_provider":
            assert self._credential_resolver is not None
            try:
                credential = self._credential_resolver.resolve(binding.credential_ref)
                credentials_resolved = True
            except CredentialResolutionError as error:
                return self._report(
                    binding=binding,
                    permit=permit,
                    readiness_digest=readiness_digest,
                    receipts=(),
                    started_at=started_at,
                    credentials_resolved=False,
                    terminal_error_code=error.code,
                )
            except Exception:
                return self._report(
                    binding=binding,
                    permit=permit,
                    readiness_digest=readiness_digest,
                    receipts=(),
                    started_at=started_at,
                    credentials_resolved=False,
                    terminal_error_code="CREDENTIAL_RESOLUTION_FAILED",
                )

        try:
            provider = self._provider_factory.build(binding, credential)
            self._validate_provider(provider, binding)
        except Exception:
            return self._report(
                binding=binding,
                permit=permit,
                readiness_digest=readiness_digest,
                receipts=(),
                started_at=started_at,
                credentials_resolved=credentials_resolved,
                terminal_error_code="PROVIDER_PROBE_PROVIDER_INVALID",
            )

        receipts: list[ProviderProbeRequestReceipt] = []
        terminal_error_code: str | None = None
        ordinal = 0
        for execution_case in self._execution_bundle.suite.cases:
            for repeat_index in range(1, execution_case.repeat_count + 1):
                if self._now() > permit.valid_until:
                    terminal_error_code = "PROVIDER_PROBE_PERMIT_EXPIRED"
                    break
                ordinal += 1
                reserved = profile.budget.maximum_per_request_microunits
                if sum(item.reserved_microunits for item in receipts) + reserved > (
                    permit.maximum_reserved_microunits
                ):
                    terminal_error_code = "PROVIDER_PROBE_BUDGET_EXHAUSTED"
                    break
                request = self._request(
                    binding,
                    permit,
                    execution_case,
                    repeat_index=repeat_index,
                    ordinal=ordinal,
                    maximum_output_tokens=self._case_policy(
                        execution_case.case_id
                    ).maximum_output_tokens,
                    reserved_microunits=reserved,
                )
                try:
                    response = await self._execute(provider, request, execution_case)
                    self._validate_response(response, request, binding, execution_case)
                    receipt = self._success_receipt(
                        response,
                        request=request,
                        execution_case=execution_case,
                        repeat_index=repeat_index,
                        ordinal=ordinal,
                        reserved_microunits=reserved,
                    )
                except Exception as error:
                    terminal_error_code = self._error_code(error)
                    receipt = self._failed_receipt(
                        request=request,
                        execution_case=execution_case,
                        repeat_index=repeat_index,
                        ordinal=ordinal,
                        reserved_microunits=reserved,
                        error_code=terminal_error_code,
                    )
                receipts.append(receipt)
                if not receipt.success:
                    break
            if terminal_error_code is not None:
                break

        if terminal_error_code is None and len(receipts) != 4:
            terminal_error_code = "PROVIDER_PROBE_REQUEST_COUNT_MISMATCH"
        return self._report(
            binding=binding,
            permit=permit,
            readiness_digest=readiness_digest,
            receipts=tuple(receipts),
            started_at=started_at,
            credentials_resolved=credentials_resolved,
            terminal_error_code=terminal_error_code,
        )

    def _validate_permit(
        self,
        binding: ProviderProbeOperatorBinding,
        permit: ProviderProbeExecutionPermit,
        *,
        readiness_digest: str,
        profile: ProviderProbeProfilePolicy,
        now: datetime,
    ) -> None:
        expected_reserved = (
            self._execution_bundle.suite.exact_request_count
            * profile.budget.maximum_per_request_microunits
        )
        if (
            now < permit.approved_at
            or now > permit.valid_until
            or permit.policy_digest != self._policy_bundle.policy_digest
            or permit.execution_suite_digest != self._execution_bundle.suite_digest
            or permit.binding_digest != binding.binding_digest
            or permit.readiness_report_digest != readiness_digest
            or permit.provider_family != binding.provider_family
            or permit.provider_id != binding.provider_id
            or permit.exact_request_count != self._execution_bundle.suite.exact_request_count
            or permit.maximum_reserved_microunits != expected_reserved
            or permit.maximum_reserved_microunits > profile.budget.maximum_total_microunits
        ):
            raise ProviderProbeExecutionError(
                "Provider probe execution permit does not match current evidence"
            )

    @staticmethod
    def _validate_provider(
        provider: ModelProvider,
        binding: ProviderProbeOperatorBinding,
    ) -> None:
        descriptor = provider.descriptor
        capabilities = descriptor.capabilities
        if (
            descriptor.provider_id != binding.provider_id
            or descriptor.model != binding.exact_model
            or descriptor.protocol is not ModelProtocol.OPENAI_RESPONSES
            or descriptor.location is not ModelLocation.CLOUD
            or not capabilities.streaming
            or not capabilities.structured_output
            or not capabilities.strict_json_schema
            or capabilities.tool_calling is not ToolCallingMode.NONE
        ):
            raise ProviderProbeExecutionError(
                "Provider probe adapter does not match the frozen contract"
            )

    def _profile(self, family: str) -> ProviderProbeProfilePolicy:
        for profile in self._policy_bundle.policy.profiles:
            if profile.provider_family == family:
                return profile
        raise ProviderProbeExecutionError("Provider probe family is not frozen")

    def _case_policy(self, case_id: str) -> ProviderProbeCasePolicy:
        for policy_case in self._policy_bundle.policy.cases:
            if policy_case.case_id == case_id:
                return policy_case
        raise ProviderProbeExecutionError("Provider probe case is not frozen")

    def _request(
        self,
        binding: ProviderProbeOperatorBinding,
        permit: ProviderProbeExecutionPermit,
        execution_case: ProviderProbeExecutionCase,
        *,
        repeat_index: int,
        ordinal: int,
        maximum_output_tokens: int,
        reserved_microunits: int,
    ) -> ModelRequest:
        user_prompt = ProviderProbeExecutionSuiteLoader._render_user_prompt(
            execution_case,
            provider_family=binding.provider_family,
        )
        request_id = (
            "probe-"
            + sha256_digest(
                {
                    "run_id": permit.run_id,
                    "case_id": execution_case.case_id,
                    "repeat_index": repeat_index,
                    "ordinal": ordinal,
                }
            )[:32]
        )
        return ModelRequest(
            request_id=request_id,
            task_id=f"provider-probe-{permit.run_id}",
            role=ModelRole.VERIFIER,
            messages=(
                ModelMessage(role="system", content=execution_case.system_prompt),
                ModelMessage(role="user", content=user_prompt),
            ),
            privacy_mode="quality_first",
            requirements=ModelCapabilityRequirements(
                streaming=execution_case.transport == "stream",
                structured_output=True,
                strict_json_schema=True,
            ),
            output_schema=StructuredOutputDefinition(
                name=execution_case.schema_name,
                description="Public synthetic Provider Responses compatibility result",
                json_schema={
                    "type": "object",
                    "properties": {
                        "provider_family": {
                            "type": "string",
                            "enum": [binding.provider_family],
                        },
                        "case_id": {
                            "type": "string",
                            "enum": [execution_case.case_id],
                        },
                        "status": {"type": "string", "enum": ["ok"]},
                    },
                    "required": ["provider_family", "case_id", "status"],
                    "additionalProperties": False,
                },
                strict=True,
            ),
            provider_hint=binding.provider_id,
            temperature=0,
            max_output_tokens=maximum_output_tokens,
            timeout_seconds=30,
            execution_budget=ModelExecutionBudget(
                max_attempts=1,
                max_retry_delay_seconds=0,
                max_task_cost_micros=reserved_microunits,
            ),
            metadata={},
        )

    @staticmethod
    async def _execute(
        provider: ModelProvider,
        request: ModelRequest,
        execution_case: ProviderProbeExecutionCase,
    ) -> ModelResponse:
        if execution_case.transport == "nonstream":
            return await provider.complete(request)
        terminal: ModelResponse | None = None
        async for event in provider.stream(request):
            if event.type is ModelStreamEventType.RESPONSE_COMPLETED:
                terminal = event.response
        if terminal is None:
            raise _ProviderProbeResponseError(
                "Provider probe stream did not produce terminal evidence"
            )
        return terminal

    @staticmethod
    def _validate_response(
        response: ModelResponse,
        request: ModelRequest,
        binding: ProviderProbeOperatorBinding,
        execution_case: ProviderProbeExecutionCase,
    ) -> None:
        if (
            response.request_id != request.request_id
            or response.provider_id != binding.provider_id
            or response.model != binding.exact_model
            or response.native_response_id is None
            or response.structured_output is None
        ):
            raise _ProviderProbeResponseError(
                "Provider probe response identity or evidence changed"
            )
        try:
            result = ProviderProbeSyntheticResult.model_validate(response.structured_output)
        except ValidationError as error:
            raise _ProviderProbeResponseError(
                "Provider probe structured output failed validation"
            ) from error
        if (
            result.provider_family != binding.provider_family
            or result.case_id != execution_case.case_id
            or result.status != execution_case.expected_status
        ):
            raise _ProviderProbeResponseError(
                "Provider probe structured output did not match the request"
            )

    @staticmethod
    def _success_receipt(
        response: ModelResponse,
        *,
        request: ModelRequest,
        execution_case: ProviderProbeExecutionCase,
        repeat_index: int,
        ordinal: int,
        reserved_microunits: int,
    ) -> ProviderProbeRequestReceipt:
        assert response.native_response_id is not None
        assert response.structured_output is not None
        material = {
            "schema_version": "deskpilot.provider-probe-request-receipt.v1",
            "ordinal": ordinal,
            "case_id": execution_case.case_id,
            "transport": execution_case.transport,
            "repeat_index": repeat_index,
            "request_digest": sha256_digest(request),
            "reserved_microunits": reserved_microunits,
            "success": True,
            "usage": response.usage.model_dump(mode="json"),
            "latency_ms": response.latency_ms,
            "native_response_id_digest": sha256_digest(
                {"native_response_id": response.native_response_id}
            ),
            "structured_output_digest": sha256_digest(response.structured_output),
            "error_code": None,
        }
        return ProviderProbeRequestReceipt.model_validate(
            {**material, "receipt_digest": sha256_digest(material)}
        )

    @staticmethod
    def _failed_receipt(
        *,
        request: ModelRequest,
        execution_case: ProviderProbeExecutionCase,
        repeat_index: int,
        ordinal: int,
        reserved_microunits: int,
        error_code: str,
    ) -> ProviderProbeRequestReceipt:
        material = {
            "schema_version": "deskpilot.provider-probe-request-receipt.v1",
            "ordinal": ordinal,
            "case_id": execution_case.case_id,
            "transport": execution_case.transport,
            "repeat_index": repeat_index,
            "request_digest": sha256_digest(request),
            "reserved_microunits": reserved_microunits,
            "success": False,
            "usage": None,
            "latency_ms": 0,
            "native_response_id_digest": None,
            "structured_output_digest": None,
            "error_code": error_code,
        }
        return ProviderProbeRequestReceipt.model_validate(
            {**material, "receipt_digest": sha256_digest(material)}
        )

    def _report(
        self,
        *,
        binding: ProviderProbeOperatorBinding,
        permit: ProviderProbeExecutionPermit,
        readiness_digest: str,
        receipts: tuple[ProviderProbeRequestReceipt, ...],
        started_at: datetime,
        credentials_resolved: bool,
        terminal_error_code: str | None,
    ) -> ProviderProbeRunReport:
        completed = terminal_error_code is None and len(receipts) == 4
        material = {
            "schema_version": "deskpilot.provider-probe-run-report.v1",
            "policy_digest": self._policy_bundle.policy_digest,
            "execution_suite_digest": self._execution_bundle.suite_digest,
            "binding_digest": binding.binding_digest,
            "readiness_report_digest": readiness_digest,
            "permit_digest": permit.permit_digest,
            "run_id": permit.run_id,
            "execution_mode": permit.execution_mode,
            "provider_family": binding.provider_family,
            "provider_id": binding.provider_id,
            "model_digest": sha256_digest({"model": binding.exact_model}),
            "status": "completed" if completed else "failed",
            "attempted_request_count": len(receipts),
            "successful_request_count": sum(1 for item in receipts if item.success),
            "reserved_microunits": sum(item.reserved_microunits for item in receipts),
            "receipts": [item.model_dump(mode="json") for item in receipts],
            "terminal_error_code": terminal_error_code,
            "started_at": started_at,
            "completed_at": self._now(),
            "credentials_resolved": credentials_resolved,
            "network_request_count": (
                len(receipts) if permit.execution_mode == "live_provider" else 0
            ),
            "real_model_capture": (permit.execution_mode == "live_provider" and bool(receipts)),
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

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ProviderProbeExecutionError(
                "Provider probe execution clock must be timezone-aware"
            )
        return value

    @staticmethod
    def _error_code(error: Exception) -> str:
        if isinstance(error, ModelGatewayError | _ProviderProbeResponseError):
            return error.code
        return "PROVIDER_PROBE_UNEXPECTED_ERROR"
