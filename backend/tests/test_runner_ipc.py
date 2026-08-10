from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from deskpilot.application.policy_engine import BuiltinPolicyEngine
from deskpilot.application.tool_registry import (
    ToolRegistry,
    ToolSchemaValidationError,
    UnknownToolError,
)
from deskpilot.domain.policy import (
    PolicyEffect,
    PolicyResource,
    ToolAuthorizationGrant,
    ToolAuthorizationRequest,
)
from deskpilot.domain.tool_contracts import (
    ToolContract,
    ToolExecutionContract,
    ToolIdempotency,
    ToolRiskLevel,
    ToolSecurityContract,
)
from deskpilot.runner.authorization import (
    MissingIdempotencyKeyError,
    MissingPolicyAuthorizationError,
    PolicyAuthorizationExpiredError,
    PolicyAuthorizationMismatchError,
    ToolCallAuthorizer,
    ToolContractMismatchError,
)
from deskpilot.runner.ipc_codec import (
    BootstrapCodec,
    DuplicateJsonKeyError,
    IpcFrameError,
    IpcFrameTooLargeError,
    NdjsonIpcCodec,
)
from deskpilot.runner.ipc_protocol import (
    InvalidSignatureError,
    IpcSigner,
    IpcVerifier,
    MessageExpiredError,
    MessageIssuedInFutureError,
    MessageTtlExceededError,
    ReplayDetectedError,
    RunnerBootstrap,
    RunnerHello,
    StartupNonceMismatchError,
    ToolCallRequest,
    ToolCallResult,
    UnknownKeyError,
)
from tests.authorization_helpers import (
    make_test_resource_projector,
    make_tool_authorization,
)

NOW = datetime(2026, 8, 9, 12, tzinfo=UTC)
SECRET = b"test-ipc-secret-that-is-at-least-32-bytes"
KEY_ID = "control-plane-key-1"
STARTUP_NONCE = "runner-session-0001"


class DiskUsageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str


class DiskUsageOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    free_bytes: int


def make_contract(
    *,
    idempotency: ToolIdempotency = ToolIdempotency.IDEMPOTENT,
    risk_level: ToolRiskLevel = ToolRiskLevel.R0,
) -> ToolContract:
    return ToolContract.from_models(
        name="computer.disk_usage",
        version="1.0.0",
        description="Read disk capacity.",
        input_model=DiskUsageInput,
        output_model=DiskUsageOutput,
        risk_level=risk_level,
        execution=ToolExecutionContract(timeout_seconds=5, idempotency=idempotency),
        security=ToolSecurityContract(capabilities=("filesystem.metadata.read",)),
    )


def make_request(
    contract: ToolContract,
    *,
    nonce: str = "command-nonce-0001",
    issued_at: datetime = NOW,
    expires_at: datetime | None = None,
    startup_nonce: str = STARTUP_NONCE,
    arguments: dict[str, object] | None = None,
    idempotency_key: str | None = None,
    call_id: str = "call-0001",
    authorization_call_id: str | None = None,
) -> ToolCallRequest:
    resolved_arguments = arguments if arguments is not None else {"path": "C:\\"}
    expected_resource_versions = {"disk:C": "observed-v1"}
    return ToolCallRequest(
        call_id=call_id,
        task_id="task-0001",
        step_id="step-0001",
        tool_name=contract.name,
        tool_version=contract.version,
        contract_digest=contract.digest,
        arguments=resolved_arguments,
        actor="local-user",
        idempotency_key=idempotency_key,
        expected_resource_versions=expected_resource_versions,
        authorization=make_tool_authorization(
            contract,
            task_id="task-0001",
            step_id="step-0001",
            call_id=authorization_call_id or call_id,
            actor_id="local-user",
            arguments=resolved_arguments,
            expected_resource_versions=expected_resource_versions,
            now=issued_at,
        ),
        issued_at=issued_at,
        expires_at=expires_at or issued_at + timedelta(seconds=30),
        nonce=nonce,
        startup_nonce=startup_nonce,
    )


def make_verifier(*, startup_nonce: str = STARTUP_NONCE) -> IpcVerifier:
    return IpcVerifier(
        key_id=KEY_ID,
        secret=SECRET,
        startup_nonce=startup_nonce,
    )


def reissue_authorization(
    grant: ToolAuthorizationGrant,
    **updates: object,
) -> ToolAuthorizationGrant:
    binding = grant.model_dump(mode="python", exclude={"authorization_id"})
    binding.update(updates)
    return ToolAuthorizationGrant.issue(**binding)


def test_signed_envelope_round_trips_through_strict_ndjson_codec() -> None:
    request = make_request(make_contract())
    envelope = IpcSigner(key_id=KEY_ID, secret=SECRET).sign(request)
    codec = NdjsonIpcCodec()

    decoded = codec.decode(codec.encode(envelope))
    verified = make_verifier().verify(decoded, now=NOW)

    assert verified == request
    assert codec.encode(envelope).endswith(b"\n")


def test_signature_rejects_semantic_tampering_and_unknown_key() -> None:
    request = make_request(make_contract())
    envelope = IpcSigner(key_id=KEY_ID, secret=SECRET).sign(request)
    tampered = envelope.model_copy(
        update={"payload": request.model_copy(update={"arguments": {"path": "D:\\"}})}
    )

    with pytest.raises(InvalidSignatureError) as invalid:
        make_verifier().verify(tampered, now=NOW)
    assert invalid.value.code == "IPC_SIGNATURE_INVALID"

    unknown_key = envelope.model_copy(update={"key_id": "unknown-key"})
    with pytest.raises(UnknownKeyError) as unknown:
        make_verifier().verify(unknown_key, now=NOW)
    assert unknown.value.code == "IPC_KEY_UNKNOWN"


def test_verifier_binds_messages_to_runner_startup_nonce() -> None:
    request = make_request(make_contract(), startup_nonce="old-runner-session")
    envelope = IpcSigner(key_id=KEY_ID, secret=SECRET).sign(request)

    with pytest.raises(StartupNonceMismatchError) as mismatch:
        make_verifier().verify(envelope, now=NOW)
    assert mismatch.value.code == "IPC_STARTUP_NONCE_MISMATCH"


def test_verifier_enforces_freshness_ttl_and_replay_protection() -> None:
    signer = IpcSigner(key_id=KEY_ID, secret=SECRET)
    expired = signer.sign(
        make_request(
            make_contract(),
            issued_at=NOW - timedelta(seconds=40),
            expires_at=NOW - timedelta(seconds=10),
        )
    )
    future = signer.sign(make_request(make_contract(), issued_at=NOW + timedelta(seconds=6)))
    long_lived = signer.sign(make_request(make_contract(), expires_at=NOW + timedelta(seconds=61)))

    with pytest.raises(MessageExpiredError):
        make_verifier().verify(expired, now=NOW)
    with pytest.raises(MessageIssuedInFutureError):
        make_verifier().verify(future, now=NOW)
    with pytest.raises(MessageTtlExceededError):
        make_verifier().verify(long_lived, now=NOW)

    valid = signer.sign(make_request(make_contract()))
    verifier = make_verifier()
    verifier.verify(valid, now=NOW)
    with pytest.raises(ReplayDetectedError) as replay:
        verifier.verify(valid, now=NOW)
    assert replay.value.code == "IPC_REPLAY_DETECTED"


def test_authorizer_requires_exact_contract_schema_and_idempotency() -> None:
    contract = make_contract(idempotency=ToolIdempotency.KEY_REQUIRED)
    registry = ToolRegistry()
    registry.register(
        contract,
        DiskUsageInput,
        DiskUsageOutput,
        make_test_resource_projector(contract),
    )
    signer = IpcSigner(key_id=KEY_ID, secret=SECRET)

    valid = signer.sign(make_request(contract, idempotency_key="task-0001:step-0001"))
    authorized = ToolCallAuthorizer(verifier=make_verifier(), registry=registry).authorize(
        valid, now=NOW
    )
    assert authorized.arguments == DiskUsageInput(path="C:\\")

    missing_key = signer.sign(make_request(contract, nonce="command-nonce-0002"))
    with pytest.raises(MissingIdempotencyKeyError):
        ToolCallAuthorizer(verifier=make_verifier(), registry=registry).authorize(
            missing_key, now=NOW
        )

    wrong_digest_request = make_request(
        contract, nonce="command-nonce-0003", idempotency_key="idempotent-3"
    ).model_copy(update={"contract_digest": "0" * 64})
    with pytest.raises(ToolContractMismatchError):
        ToolCallAuthorizer(verifier=make_verifier(), registry=registry).authorize(
            signer.sign(wrong_digest_request), now=NOW
        )

    invalid_arguments = make_request(
        contract,
        nonce="command-nonce-0004",
        idempotency_key="idempotent-4",
    ).model_copy(update={"arguments": {"unknown": True}})
    with pytest.raises(ToolSchemaValidationError):
        ToolCallAuthorizer(verifier=make_verifier(), registry=registry).authorize(
            signer.sign(invalid_arguments), now=NOW
        )


def test_authorizer_rejects_unregistered_tool_name_or_version() -> None:
    contract = make_contract()
    registry = ToolRegistry()
    unknown_request = make_request(contract).model_copy(update={"tool_version": "2.0.0"})

    with pytest.raises(UnknownToolError):
        ToolCallAuthorizer(verifier=make_verifier(), registry=registry).authorize(
            IpcSigner(key_id=KEY_ID, secret=SECRET).sign(unknown_request),
            now=NOW,
        )


def test_tool_call_schema_requires_a_policy_authorization() -> None:
    payload = make_request(make_contract()).model_dump(mode="json")
    payload.pop("authorization")

    with pytest.raises(ValidationError) as missing:
        ToolCallRequest.model_validate(payload)

    assert missing.value.errors()[0]["type"] == "missing"


def test_authorizer_rejects_grant_bound_to_another_call() -> None:
    contract = make_contract()
    registry = ToolRegistry()
    registry.register(
        contract,
        DiskUsageInput,
        DiskUsageOutput,
        make_test_resource_projector(contract),
    )
    request = make_request(
        contract,
        call_id="call-0002",
        authorization_call_id="call-0001",
    )

    with pytest.raises(PolicyAuthorizationMismatchError) as mismatch:
        ToolCallAuthorizer(verifier=make_verifier(), registry=registry).authorize(
            IpcSigner(key_id=KEY_ID, secret=SECRET).sign(request),
            now=NOW,
        )

    assert mismatch.value.code == "POLICY_AUTHORIZATION_MISMATCH"


def test_authorizer_rejects_arguments_changed_after_policy_decision() -> None:
    contract = make_contract()
    registry = ToolRegistry()
    registry.register(
        contract,
        DiskUsageInput,
        DiskUsageOutput,
        make_test_resource_projector(contract),
    )
    request = make_request(contract)
    changed = request.model_copy(update={"arguments": {"path": "D:\\"}})

    with pytest.raises(PolicyAuthorizationMismatchError):
        ToolCallAuthorizer(verifier=make_verifier(), registry=registry).authorize(
            IpcSigner(key_id=KEY_ID, secret=SECRET).sign(changed),
            now=NOW,
        )


def test_authorizer_rejects_expired_user_approval_grant() -> None:
    contract = make_contract(risk_level=ToolRiskLevel.R1)
    registry = ToolRegistry()
    registry.register(
        contract,
        DiskUsageInput,
        DiskUsageOutput,
        make_test_resource_projector(contract),
    )
    request = make_request(contract)
    expired_authorization = make_tool_authorization(
        contract,
        task_id=request.task_id,
        step_id=request.step_id,
        call_id=request.call_id,
        actor_id=request.actor,
        arguments=dict(request.arguments),
        expected_resource_versions=dict(request.expected_resource_versions),
        grant_expires_at=NOW - timedelta(seconds=1),
        now=NOW - timedelta(minutes=5),
    )
    expired = request.model_copy(update={"authorization": expired_authorization})

    with pytest.raises(PolicyAuthorizationExpiredError) as error:
        ToolCallAuthorizer(verifier=make_verifier(), registry=registry).authorize(
            IpcSigner(key_id=KEY_ID, secret=SECRET).sign(expired),
            now=NOW,
        )

    assert error.value.code == "POLICY_AUTHORIZATION_EXPIRED"


def test_authorizer_rejects_risk_lowering_and_missing_high_risk_approval() -> None:
    contract = make_contract(risk_level=ToolRiskLevel.R1)
    registry = ToolRegistry()
    registry.register(
        contract,
        DiskUsageInput,
        DiskUsageOutput,
        make_test_resource_projector(contract),
    )
    request = make_request(contract)
    lowered = request.model_copy(
        update={
            "authorization": make_tool_authorization(
                contract,
                task_id=request.task_id,
                step_id=request.step_id,
                call_id=request.call_id,
                actor_id=request.actor,
                arguments=dict(request.arguments),
                expected_resource_versions=dict(request.expected_resource_versions),
                effective_risk=ToolRiskLevel.R0,
                now=NOW,
            )
        }
    )
    no_approval = request.model_copy(
        update={
            "authorization": make_tool_authorization(
                contract,
                task_id=request.task_id,
                step_id=request.step_id,
                call_id=request.call_id,
                actor_id=request.actor,
                arguments=dict(request.arguments),
                expected_resource_versions=dict(request.expected_resource_versions),
                require_approval=False,
                now=NOW,
            )
        }
    )
    signer = IpcSigner(key_id=KEY_ID, secret=SECRET)

    with pytest.raises(PolicyAuthorizationMismatchError):
        ToolCallAuthorizer(verifier=make_verifier(), registry=registry).authorize(
            signer.sign(lowered),
            now=NOW,
        )
    with pytest.raises(MissingPolicyAuthorizationError) as missing:
        ToolCallAuthorizer(verifier=make_verifier(), registry=registry).authorize(
            signer.sign(no_approval),
            now=NOW,
        )

    assert missing.value.code == "POLICY_AUTHORIZATION_REQUIRED"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("step_id", "step-other"),
        ("actor_id", "model:other"),
        ("origin", "mcp"),
        ("request_digest", "f" * 64),
        ("capabilities", ()),
        ("network_access", True),
        ("data_egress", True),
        ("side_effects", ("filesystem_write",)),
        ("reversible", True),
        ("interactive", False),
        ("batch_count", 2),
    ],
)
def test_authorizer_rejects_each_mutated_policy_binding_fact(
    field: str,
    value: object,
) -> None:
    contract = make_contract()
    registry = ToolRegistry()
    registry.register(
        contract,
        DiskUsageInput,
        DiskUsageOutput,
        make_test_resource_projector(contract),
    )
    request = make_request(contract)
    changed = request.model_copy(
        update={
            "authorization": reissue_authorization(
                request.authorization,
                **{field: value},
            )
        }
    )

    with pytest.raises(PolicyAuthorizationMismatchError):
        ToolCallAuthorizer(verifier=make_verifier(), registry=registry).authorize(
            IpcSigner(key_id=KEY_ID, secret=SECRET).sign(changed),
            now=NOW,
        )


def test_authorizer_requires_approval_for_elevated_effective_risk() -> None:
    contract = make_contract()
    registry = ToolRegistry()
    registry.register(
        contract,
        DiskUsageInput,
        DiskUsageOutput,
        make_test_resource_projector(contract),
    )
    request = make_request(contract)
    elevated = request.model_copy(
        update={
            "authorization": make_tool_authorization(
                contract,
                task_id=request.task_id,
                step_id=request.step_id,
                call_id=request.call_id,
                actor_id=request.actor,
                arguments=dict(request.arguments),
                expected_resource_versions=dict(request.expected_resource_versions),
                effective_risk=ToolRiskLevel.R2,
                require_approval=False,
                now=NOW,
            )
        }
    )

    with pytest.raises(MissingPolicyAuthorizationError):
        ToolCallAuthorizer(verifier=make_verifier(), registry=registry).authorize(
            IpcSigner(key_id=KEY_ID, secret=SECRET).sign(elevated),
            now=NOW,
        )


def test_authorizer_recomputes_authorization_id_after_signature_verification() -> None:
    contract = make_contract()
    registry = ToolRegistry()
    registry.register(
        contract,
        DiskUsageInput,
        DiskUsageOutput,
        make_test_resource_projector(contract),
    )
    request = make_request(contract)
    changed = request.model_copy(
        update={
            "authorization": request.authorization.model_copy(
                update={"authorization_id": f"auth_{'0' * 64}"}
            )
        }
    )

    with pytest.raises(PolicyAuthorizationMismatchError):
        ToolCallAuthorizer(verifier=make_verifier(), registry=registry).authorize(
            IpcSigner(key_id=KEY_ID, secret=SECRET).sign(changed),
            now=NOW,
        )


def test_authorizer_rejects_policy_allowed_scope_forged_for_different_path(
    tmp_path: Path,
) -> None:
    contract = make_contract()
    allowed_path = tmp_path / "allowed"
    denied_path = tmp_path / "denied"
    allowed_path.mkdir()
    denied_path.mkdir()
    registry = ToolRegistry()
    registry.register(
        contract,
        DiskUsageInput,
        DiskUsageOutput,
        make_test_resource_projector(contract),
    )
    request = make_request(contract, arguments={"path": str(denied_path)})
    wrong_resource = PolicyResource(
        kind="filesystem_path",
        identifier=str(allowed_path.resolve(strict=True)),
        operations=request.authorization.capabilities,
        display_name=str(allowed_path.resolve(strict=True)),
    )
    mismatched_policy_request = ToolAuthorizationRequest(
        task_id=request.task_id,
        step_id=request.step_id,
        call_id=request.call_id,
        actor=request.actor,
        origin=request.authorization.origin,
        tool_name=request.tool_name,
        tool_version=request.tool_version,
        contract_digest=request.contract_digest,
        arguments_digest=request.authorization.arguments_digest,
        risk_level=contract.risk_level,
        side_effects=contract.side_effects,
        reversible=contract.reversible,
        capabilities=contract.security.capabilities,
        network_access=contract.security.network_access,
        data_egress=False,
        resources=(wrong_resource,),
        expected_resource_versions_digest=(request.authorization.expected_resource_versions_digest),
        interactive=True,
        batch_count=1,
    )
    policy = BuiltinPolicyEngine(
        allowed_resource_scopes=(wrong_resource.scope_key,),
    )
    assert policy.evaluate(mismatched_policy_request).effect is PolicyEffect.ALLOW

    changed = request.model_copy(
        update={
            "authorization": reissue_authorization(
                request.authorization,
                request_digest=mismatched_policy_request.request_digest,
                resource_scope_digest=(mismatched_policy_request.resource_scope_digest),
                resources=mismatched_policy_request.resources,
            )
        }
    )

    with pytest.raises(PolicyAuthorizationMismatchError):
        ToolCallAuthorizer(verifier=make_verifier(), registry=registry).authorize(
            IpcSigner(key_id=KEY_ID, secret=SECRET).sign(changed),
            now=NOW,
        )


def test_codec_rejects_ambiguous_invalid_and_oversized_frames() -> None:
    codec = NdjsonIpcCodec(max_frame_bytes=1_024)

    with pytest.raises(IpcFrameError):
        codec.decode(b"{}")
    with pytest.raises(IpcFrameError):
        codec.decode(b"{}\n{}\n")
    with pytest.raises(DuplicateJsonKeyError) as duplicate:
        codec.decode(b'{"key_id":"a","key_id":"b"}\n')
    assert duplicate.value.code == "IPC_DUPLICATE_JSON_KEY"
    with pytest.raises(IpcFrameError):
        codec.decode(b'{"value":NaN}\n')
    with pytest.raises(IpcFrameTooLargeError):
        codec.decode(b"x" * 1_024 + b"\n")


def test_bootstrap_codec_is_strict_and_never_uses_command_line_secrets() -> None:
    bootstrap = RunnerBootstrap(
        key_id=KEY_ID,
        secret="A" * 43,
        startup_nonce=STARTUP_NONCE,
        heartbeat_interval_seconds=0.5,
    )
    codec = BootstrapCodec()

    assert codec.decode(codec.encode(bootstrap)) == bootstrap
    with pytest.raises(DuplicateJsonKeyError):
        codec.decode(b'{"key_id":"a","key_id":"b"}\n')


def test_runner_responses_are_signed_and_session_bound() -> None:
    hello = RunnerHello(
        runner_id="runner-1",
        startup_nonce=STARTUP_NONCE,
        supported_protocols=("deskpilot.runner.v1",),
        occurred_at=NOW,
    )
    result = ToolCallResult(
        runner_id="runner-1",
        startup_nonce=STARTUP_NONCE,
        call_id="call-0001",
        status="succeeded",
        output={"free_bytes": 42},
        started_at=NOW,
        finished_at=NOW + timedelta(milliseconds=5),
    )
    signer = IpcSigner(key_id=KEY_ID, secret=SECRET)

    assert make_verifier().verify(signer.sign(hello), now=NOW) == hello
    assert make_verifier().verify(signer.sign(result), now=NOW) == result


@pytest.mark.parametrize("status", ["failed", "cancelled", "unknown"])
def test_non_succeeded_runner_results_require_a_structured_error(status: str) -> None:
    with pytest.raises(ValidationError):
        ToolCallResult(
            runner_id="runner-1",
            startup_nonce=STARTUP_NONCE,
            call_id="call-0001",
            status=status,  # type: ignore[arg-type]
            started_at=NOW,
            finished_at=NOW,
        )
