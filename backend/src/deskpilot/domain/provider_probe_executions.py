"""One-shot execution contracts for bounded Provider compatibility probes."""

from datetime import datetime, timedelta
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_contracts import DIGEST_PATTERN
from deskpilot.domain.model_contracts import PROVIDER_ID_PATTERN, ModelUsage
from deskpilot.domain.phase107_calibrations import REVIEWER_PATTERN
from deskpilot.domain.provider_probe_authorizations import (
    ProviderProbeFamily,
)

ProviderProbeExecutionMode = Literal["offline_mock", "live_provider"]
ProviderProbeExecutionStatus = Literal["completed", "failed"]
ProviderProbeTransport = Literal["nonstream", "stream"]

MAX_PROVIDER_PROBE_EXECUTION_PERMIT_VALIDITY = timedelta(minutes=15)
_RUN_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{7,63}$"


class ProviderProbeExecutionCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: Literal[
        "public-strict-json-nonstream-v1",
        "public-strict-json-stream-v1",
    ]
    transport: ProviderProbeTransport
    repeat_count: Literal[2] = 2
    system_prompt: str = Field(min_length=1, max_length=2_048)
    user_prompt_template: str = Field(min_length=1, max_length=2_048)
    schema_name: Literal["provider_probe_result"] = "provider_probe_result"
    expected_status: Literal["ok"] = "ok"

    @model_validator(mode="after")
    def exact_template_placeholders(self) -> Self:
        if self.user_prompt_template.count("{provider_family}") != 1 or (
            self.user_prompt_template.count("{case_id}") != 1
        ):
            raise ValueError("Provider probe prompt placeholders changed")
        return self


class ProviderProbeExecutionSuite(BaseModel):
    """Immutable public-synthetic inputs used by both mock and future live probes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.provider-probe-execution-suite.v1"]
    suite_id: Literal["phase115-three-provider-public-probe-execution"]
    version: Literal[1] = 1
    provider_probe_policy_digest: str = Field(pattern=DIGEST_PATTERN)
    data_class: Literal["public_synthetic"] = "public_synthetic"
    cases: tuple[ProviderProbeExecutionCase, ...] = Field(min_length=2, max_length=2)
    exact_request_count: Literal[4] = 4
    maximum_permit_validity_minutes: Literal[15] = 15
    serial_execution: Literal[True] = True
    stop_on_first_error: Literal[True] = True
    usage_required: Literal[True] = True
    automatic_retries: Literal[0] = 0
    hidden_retries: Literal[False] = False
    request_and_response_bodies_logged: Literal[False] = False
    headers_logged: Literal[False] = False
    production_admission: Literal[False] = False
    cloud_activation: Literal[False] = False
    full_116c_b: Literal[False] = False

    @model_validator(mode="after")
    def exact_case_matrix(self) -> Self:
        if tuple((item.case_id, item.transport) for item in self.cases) != (
            ("public-strict-json-nonstream-v1", "nonstream"),
            ("public-strict-json-stream-v1", "stream"),
        ):
            raise ValueError("Provider probe execution cases changed")
        if sum(int(item.repeat_count) for item in self.cases) != self.exact_request_count:
            raise ValueError("Provider probe execution request count changed")
        return self


class ProviderProbeExecutionPermit(BaseModel):
    """Short-lived, one-shot authority for one exact four-request probe run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.provider-probe-execution-permit.v1"]
    policy_digest: str = Field(pattern=DIGEST_PATTERN)
    execution_suite_digest: str = Field(pattern=DIGEST_PATTERN)
    binding_digest: str = Field(pattern=DIGEST_PATTERN)
    readiness_report_digest: str = Field(pattern=DIGEST_PATTERN)
    provider_family: ProviderProbeFamily
    provider_id: str = Field(pattern=PROVIDER_ID_PATTERN)
    run_id: str = Field(pattern=_RUN_ID_PATTERN)
    execution_mode: ProviderProbeExecutionMode
    exact_request_count: Literal[4] = 4
    maximum_reserved_microunits: int = Field(ge=1)
    data_class: Literal["public_synthetic"] = "public_synthetic"
    network_access_authorized: bool
    credential_resolution_authorized: bool
    real_model_capture_authorized: bool
    automatic_retries: Literal[0] = 0
    operator_confirmation: str = Field(min_length=1, max_length=100)
    approved_by: str = Field(pattern=REVIEWER_PATTERN)
    approved_at: datetime
    valid_until: datetime
    production_admission: Literal[False] = False
    cloud_activation: Literal[False] = False
    full_116c_b: Literal[False] = False
    permit_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def exact_short_lived_authority(self) -> Self:
        if self.approved_at.tzinfo is None or self.valid_until.tzinfo is None:
            raise ValueError("Provider probe permit timestamps must be timezone-aware")
        if (
            self.valid_until <= self.approved_at
            or self.valid_until - self.approved_at > MAX_PROVIDER_PROBE_EXECUTION_PERMIT_VALIDITY
        ):
            raise ValueError("Provider probe permit validity must stay within 15 minutes")
        if self.execution_mode == "offline_mock":
            expected_flags = (False, False, False)
            expected_confirmation = "RUN FOUR OFFLINE MOCK PROVIDER PROBES"
        else:
            expected_flags = (True, True, True)
            expected_confirmation = "RUN FOUR LIVE PUBLIC SYNTHETIC PROVIDER PROBES"
        if (
            self.network_access_authorized,
            self.credential_resolution_authorized,
            self.real_model_capture_authorized,
        ) != expected_flags:
            raise ValueError("Provider probe permit authority does not match its mode")
        if self.operator_confirmation != expected_confirmation:
            raise ValueError("Provider probe permit confirmation phrase changed")
        if self.permit_digest != sha256_digest(
            self.model_dump(mode="json", exclude={"permit_digest"})
        ):
            raise ValueError("Provider probe execution permit digest changed")
        return self


class ProviderProbeSyntheticResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_family: ProviderProbeFamily
    case_id: Literal[
        "public-strict-json-nonstream-v1",
        "public-strict-json-stream-v1",
    ]
    status: Literal["ok"]


class ProviderProbeRequestReceipt(BaseModel):
    """Sanitized request evidence: no prompts, outputs, headers, URLs, or native IDs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.provider-probe-request-receipt.v1"]
    ordinal: int = Field(ge=1, le=4)
    case_id: Literal[
        "public-strict-json-nonstream-v1",
        "public-strict-json-stream-v1",
    ]
    transport: ProviderProbeTransport
    repeat_index: int = Field(ge=1, le=2)
    request_digest: str = Field(pattern=DIGEST_PATTERN)
    reserved_microunits: int = Field(ge=1)
    success: bool
    usage: ModelUsage | None = None
    latency_ms: int = Field(ge=0)
    native_response_id_digest: str | None = Field(
        default=None,
        pattern=DIGEST_PATTERN,
    )
    structured_output_digest: str | None = Field(
        default=None,
        pattern=DIGEST_PATTERN,
    )
    error_code: str | None = Field(default=None, min_length=1, max_length=100)
    receipt_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def evidence_matches_outcome(self) -> Self:
        evidence = (
            self.usage,
            self.native_response_id_digest,
            self.structured_output_digest,
        )
        if self.success and (any(item is None for item in evidence) or self.error_code):
            raise ValueError("Successful Provider probe receipt lacks exact evidence")
        if not self.success and (any(item is not None for item in evidence) or not self.error_code):
            raise ValueError("Failed Provider probe receipt leaked success evidence")
        if self.receipt_digest != sha256_digest(
            self.model_dump(mode="json", exclude={"receipt_digest"})
        ):
            raise ValueError("Provider probe request receipt digest changed")
        return self


class ProviderProbeRunReport(BaseModel):
    """Sanitized terminal evidence for one consumed execution permit."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.provider-probe-run-report.v1"]
    policy_digest: str = Field(pattern=DIGEST_PATTERN)
    execution_suite_digest: str = Field(pattern=DIGEST_PATTERN)
    binding_digest: str = Field(pattern=DIGEST_PATTERN)
    readiness_report_digest: str = Field(pattern=DIGEST_PATTERN)
    permit_digest: str = Field(pattern=DIGEST_PATTERN)
    run_id: str = Field(pattern=_RUN_ID_PATTERN)
    execution_mode: ProviderProbeExecutionMode
    provider_family: ProviderProbeFamily
    provider_id: str = Field(pattern=PROVIDER_ID_PATTERN)
    model_digest: str = Field(pattern=DIGEST_PATTERN)
    status: ProviderProbeExecutionStatus
    attempted_request_count: int = Field(ge=0, le=4)
    successful_request_count: int = Field(ge=0, le=4)
    reserved_microunits: int = Field(ge=0)
    receipts: tuple[ProviderProbeRequestReceipt, ...] = Field(max_length=4)
    terminal_error_code: str | None = Field(default=None, min_length=1, max_length=100)
    started_at: datetime
    completed_at: datetime
    credentials_resolved: bool
    network_request_count: int = Field(ge=0, le=4)
    real_model_capture: bool
    automatic_retries: Literal[0] = 0
    serial_execution: Literal[True] = True
    stopped_on_first_error: Literal[True] = True
    request_and_response_bodies_logged: Literal[False] = False
    headers_logged: Literal[False] = False
    credentials_logged: Literal[False] = False
    production_admission: Literal[False] = False
    cloud_activation: Literal[False] = False
    full_116c_b: Literal[False] = False
    report_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def exact_terminal_evidence(self) -> Self:
        if self.started_at.tzinfo is None or self.completed_at.tzinfo is None:
            raise ValueError("Provider probe report timestamps must be timezone-aware")
        if self.completed_at < self.started_at:
            raise ValueError("Provider probe report completed before it started")
        if self.attempted_request_count != len(self.receipts) or (
            self.successful_request_count != sum(1 for item in self.receipts if item.success)
        ):
            raise ValueError("Provider probe report counters do not match receipts")
        expected_network_count = (
            0 if self.execution_mode == "offline_mock" else self.attempted_request_count
        )
        if self.network_request_count != expected_network_count:
            raise ValueError("Provider probe network count does not match execution mode")
        if self.reserved_microunits != sum(item.reserved_microunits for item in self.receipts):
            raise ValueError("Provider probe report budget does not match receipts")
        completed = self.status == "completed"
        if completed != (
            self.attempted_request_count == 4
            and self.successful_request_count == 4
            and self.terminal_error_code is None
        ):
            raise ValueError("Provider probe report status does not match its outcome")
        if not completed and self.terminal_error_code is None:
            raise ValueError("Failed Provider probe report lacks a terminal error")
        if self.execution_mode == "offline_mock":
            if self.credentials_resolved or self.real_model_capture:
                raise ValueError("Offline mock probe claimed live execution effects")
        elif self.real_model_capture != bool(self.network_request_count):
            raise ValueError("Live Provider probe capture flag does not match requests")
        if self.report_digest != sha256_digest(
            self.model_dump(mode="json", exclude={"report_digest"})
        ):
            raise ValueError("Provider probe run report digest changed")
        return self
