from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from deskpilot.application.capability_executor_registry import (
    CapabilityExecutionBindingError,
    CapabilityExecutorDigestDriftError,
    CapabilityExecutorInputValidationError,
    CapabilityExecutorRegistry,
    CapabilityExecutorRuntimeDisabledError,
    CapabilityExecutorSchemaError,
    CapabilityExecutorVersionDriftError,
    CapabilityModelAuthorityRejectedError,
    DuplicateCapabilityExecutorError,
    UnknownCapabilityExecutorError,
)
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.capability_execution import (
    CapabilityApprovalRequirement,
    CapabilityEffectClass,
    CapabilityExecutionContext,
    CapabilityExecutorManifest,
    CapabilityRecoveryPolicy,
    CapabilityResultKind,
    VerifiedCapabilityResultRef,
)
from deskpilot.domain.task_plans import (
    CapabilityPack,
    CapabilityRef,
    DraftNodeKind,
    PlanNodeBudget,
)
from deskpilot.domain.tool_contracts import ToolRiskLevel


class QueryInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    query: str = Field(min_length=1)
    payload: dict[str, JsonValue] = Field(default_factory=dict)


class QueryOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    result_digest: str


class OtherInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str


class ForbiddenInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    cwd: str


class FakeExecutor:
    async def execute(
        self,
        context: CapabilityExecutionContext,
        arguments: BaseModel,
    ) -> BaseModel:
        del context
        return arguments

    async def verify(
        self,
        context: CapabilityExecutionContext,
        candidate: BaseModel,
    ) -> BaseModel:
        del context
        return candidate


def _pack(
    *,
    capability_id: str = "knowledge.local.v1",
    version: str = "1.0.0",
    runtime_enabled: bool = True,
    description: str = "Exact test capability.",
) -> CapabilityPack:
    values: dict[str, Any] = {
        "schema_version": "deskpilot.capability-pack.v1",
        "capability_id": capability_id,
        "version": version,
        "description": description,
        "allowed_operations": ("test.read",),
        "max_risk_level": ToolRiskLevel.R0,
        "external_ingress": True,
        "external_egress": False,
        "workspace_write": False,
        "runtime_enabled": runtime_enabled,
    }
    return CapabilityPack.model_validate({**values, "digest": sha256_digest(values)})


def _manifest(
    pack: CapabilityPack | None = None,
    *,
    input_model: type[BaseModel] = QueryInput,
    output_model: type[BaseModel] = QueryOutput,
    node_kinds: tuple[DraftNodeKind, ...] = (DraftNodeKind.CAPABILITY,),
    consumes: tuple[CapabilityResultKind, ...] = (CapabilityResultKind.WORKSPACE_FILE,),
) -> CapabilityExecutorManifest:
    return CapabilityExecutorManifest.from_pack(
        executor_id="builtin.knowledge.local.v1",
        pack=pack or _pack(),
        input_model=input_model,
        output_model=output_model,
        node_kinds=node_kinds,
        consumes=consumes,
        produces=CapabilityResultKind.KNOWLEDGE,
        effect_class=CapabilityEffectClass.READ_ONLY,
        approval_requirement=CapabilityApprovalRequirement.NONE,
        recovery_policy=CapabilityRecoveryPolicy.DETERMINISTIC_RETRY,
    )


def _budget() -> PlanNodeBudget:
    return PlanNodeBudget(
        model_calls=0,
        tool_calls=1,
        input_tokens=0,
        output_tokens=0,
        wall_seconds=60,
        retries=0,
        cost_micros=0,
        handoffs=0,
    )


def _result_ref(
    *,
    task_id: str = f"tsk_{'1' * 32}",
    result_kind: CapabilityResultKind = CapabilityResultKind.WORKSPACE_FILE,
) -> VerifiedCapabilityResultRef:
    return VerifiedCapabilityResultRef.build(
        task_id=task_id,
        run_id=f"run_{'2' * 64}",
        plan_generation=1,
        producer_node_id=f"pnd_{'3' * 64}",
        producer_attempt=1,
        capability=CapabilityRef(
            capability_id="workspace.file.read.v1",
            version="1.0.0",
            digest="4" * 64,
        ),
        result_kind=result_kind,
        result_schema_digest="5" * 64,
        result_digest="6" * 64,
        verification_digest="7" * 64,
    )


def _context(
    capability: CapabilityRef,
    *,
    node_kind: DraftNodeKind = DraftNodeKind.CAPABILITY,
    upstream: tuple[VerifiedCapabilityResultRef, ...] | None = None,
    consumed: tuple[VerifiedCapabilityResultRef, ...] | None = None,
) -> CapabilityExecutionContext:
    dependency_refs = upstream if upstream is not None else (_result_ref(),)
    return CapabilityExecutionContext.build(
        task_id=f"tsk_{'1' * 32}",
        run_id=f"run_{'2' * 64}",
        plan_id=f"epl_{'8' * 64}",
        plan_generation=1,
        plan_manifest_digest="9" * 64,
        node_id=f"pnd_{'a' * 64}",
        node_kind=node_kind,
        node_spec_digest="b" * 64,
        node_binding_id=f"mnb_{'c' * 64}",
        node_binding_digest="d" * 64,
        effective_authority_digest="e" * 64,
        runtime_eligibility_digest="f" * 64,
        node_attempt=1,
        claim_owner_id="workbench-capability-test",
        claim_fencing_token=3,
        capability=capability,
        step_input_digest="c" * 64,
        upstream_result_refs=dependency_refs,
        consumed_result_refs=consumed if consumed is not None else dependency_refs,
        budget=_budget(),
    )


def _registered() -> tuple[
    CapabilityExecutorRegistry,
    CapabilityExecutorManifest,
]:
    registry = CapabilityExecutorRegistry()
    manifest = _manifest()
    registry.register(manifest, QueryInput, QueryOutput, FakeExecutor())
    return registry, manifest


def test_manifest_and_registry_preserve_exact_declarative_binding() -> None:
    registry, manifest = _registered()

    registration = registry.resolve(manifest.capability)

    assert registration.manifest is manifest
    assert registration.input_model is QueryInput
    assert registration.output_model is QueryOutput
    assert manifest.input_schema == QueryInput.model_json_schema()
    assert manifest.output_schema == QueryOutput.model_json_schema()
    assert manifest.consumes == (CapabilityResultKind.WORKSPACE_FILE,)
    assert manifest.produces is CapabilityResultKind.KNOWLEDGE
    assert manifest.effect_class is CapabilityEffectClass.READ_ONLY
    assert manifest.approval_requirement is CapabilityApprovalRequirement.NONE
    assert manifest.recovery_policy is CapabilityRecoveryPolicy.DETERMINISTIC_RETRY
    assert registry.manifests() == (manifest,)
    assert "import_path" not in manifest.model_dump(mode="json")

    tampered = manifest.model_dump(mode="json")
    tampered["produces"] = CapabilityResultKind.MCP.value
    with pytest.raises(ValidationError, match="manifest digest"):
        CapabilityExecutorManifest.model_validate(tampered)


def test_registry_rejects_duplicate_unknown_version_and_digest_drift() -> None:
    registry, manifest = _registered()

    with pytest.raises(DuplicateCapabilityExecutorError) as duplicate:
        registry.register(manifest, QueryInput, QueryOutput, FakeExecutor())
    assert duplicate.value.code == "CAPABILITY_EXECUTOR_ALREADY_REGISTERED"

    with pytest.raises(UnknownCapabilityExecutorError) as unknown:
        registry.resolve(
            CapabilityRef(
                capability_id="unknown.capability.v1",
                version="1.0.0",
                digest="d" * 64,
            )
        )
    assert unknown.value.code == "CAPABILITY_EXECUTOR_NOT_REGISTERED"

    with pytest.raises(CapabilityExecutorVersionDriftError) as version:
        registry.resolve(manifest.capability.model_copy(update={"version": "1.0.1"}))
    assert version.value.code == "CAPABILITY_EXECUTOR_VERSION_DRIFT"

    with pytest.raises(CapabilityExecutorDigestDriftError) as digest:
        registry.resolve(manifest.capability.model_copy(update={"digest": "e" * 64}))
    assert digest.value.code == "CAPABILITY_EXECUTOR_DIGEST_DRIFT"

    drifted = _manifest(_pack(description="Same identity with changed declaration."))
    with pytest.raises(CapabilityExecutorDigestDriftError):
        registry.register(drifted, QueryInput, QueryOutput, FakeExecutor())


def test_registry_rejects_disabled_runtime_and_schema_drift() -> None:
    disabled = _manifest(_pack(runtime_enabled=False))
    registry = CapabilityExecutorRegistry()
    with pytest.raises(CapabilityExecutorRuntimeDisabledError) as runtime_disabled:
        registry.register(disabled, QueryInput, QueryOutput, FakeExecutor())
    assert runtime_disabled.value.code == "CAPABILITY_EXECUTOR_RUNTIME_DISABLED"

    manifest = _manifest()
    with pytest.raises(CapabilityExecutorSchemaError) as schema:
        registry.register(manifest, OtherInput, QueryOutput, FakeExecutor())
    assert schema.value.code == "CAPABILITY_EXECUTOR_SCHEMA_MISMATCH"

    forbidden = _manifest(input_model=ForbiddenInput)
    with pytest.raises(CapabilityModelAuthorityRejectedError) as authority:
        registry.register(forbidden, ForbiddenInput, QueryOutput, FakeExecutor())
    assert authority.value.code == "CAPABILITY_MODEL_AUTHORITY_REJECTED"


def test_execution_context_binds_node_capability_fence_refs_and_budget() -> None:
    registry, manifest = _registered()
    context = _context(manifest.capability)

    registration = registry.resolve_for_execution(
        context,
        bound_capability=manifest.capability,
        bound_node_kind=DraftNodeKind.CAPABILITY,
    )

    assert registration.manifest is manifest
    assert context.claim_owner_id == "workbench-capability-test"
    assert context.claim_fencing_token == 3
    assert context.step_input_digest == "c" * 64
    assert context.upstream_result_refs[0].result_kind is CapabilityResultKind.WORKSPACE_FILE
    assert context.budget == _budget()
    assert len(context.context_digest) == 64


def test_execution_binding_rejects_node_capability_and_result_kind_mismatch() -> None:
    registry, manifest = _registered()
    context = _context(manifest.capability)
    other = CapabilityRef(
        capability_id="mcp.text.metrics.v1",
        version="1.0.0",
        digest="f" * 64,
    )

    with pytest.raises(CapabilityExecutionBindingError):
        registry.resolve_for_execution(
            context,
            bound_capability=other,
            bound_node_kind=DraftNodeKind.CAPABILITY,
        )
    with pytest.raises(CapabilityExecutionBindingError):
        registry.resolve_for_execution(
            context,
            bound_capability=manifest.capability,
            bound_node_kind=DraftNodeKind.AGENT,
        )

    wrong_kind = _context(
        manifest.capability,
        upstream=(_result_ref(result_kind=CapabilityResultKind.MCP),),
        consumed=(_result_ref(result_kind=CapabilityResultKind.MCP),),
    )
    with pytest.raises(CapabilityExecutionBindingError, match="ResultRef kinds"):
        registry.resolve_for_execution(
            wrong_kind,
            bound_capability=manifest.capability,
            bound_node_kind=DraftNodeKind.CAPABILITY,
        )

    agent_context = _context(manifest.capability, node_kind=DraftNodeKind.AGENT)
    with pytest.raises(CapabilityExecutionBindingError, match="cannot run"):
        registry.resolve_for_execution(
            agent_context,
            bound_capability=manifest.capability,
            bound_node_kind=DraftNodeKind.AGENT,
        )


@pytest.mark.parametrize(
    "reserved",
    (
        "executable",
        "cwd",
        "env",
        "permissions",
        "result_ref",
        "ResultRef",
        "upstream_result_refs",
        "capability_ref",
        "approval_digest",
    ),
)
def test_model_input_cannot_supply_execution_authority(reserved: str) -> None:
    registry, manifest = _registered()

    with pytest.raises(CapabilityModelAuthorityRejectedError) as rejected:
        registry.validate_model_input(
            manifest.capability,
            {"query": "safe", "payload": {"nested": {reserved: "model supplied"}}},
        )
    assert rejected.value.code == "CAPABILITY_MODEL_AUTHORITY_REJECTED"


def test_registry_validates_model_input_and_executor_output_schema() -> None:
    registry, manifest = _registered()

    parsed = registry.validate_model_input(
        manifest.capability,
        {"query": "safe", "payload": {"topic": "local"}},
    )
    output = registry.validate_output(
        manifest.capability,
        {"result_digest": "a" * 64},
    )

    assert parsed == QueryInput(query="safe", payload={"topic": "local"})
    assert output == QueryOutput(result_digest="a" * 64)
    with pytest.raises(CapabilityExecutorInputValidationError):
        registry.validate_model_input(manifest.capability, {"payload": {}})


def test_result_ref_and_context_are_digest_checked_and_task_scoped() -> None:
    ref = _result_ref()
    tampered = ref.model_dump(mode="json")
    tampered["result_digest"] = "0" * 64
    with pytest.raises(ValidationError, match="ResultRef digest"):
        VerifiedCapabilityResultRef.model_validate(tampered)

    manifest = _manifest()
    context = _context(manifest.capability)
    changed = context.model_dump(mode="json")
    changed["claim_fencing_token"] = 4
    with pytest.raises(ValidationError, match="context digest"):
        CapabilityExecutionContext.model_validate(changed)

    with pytest.raises(ValidationError, match="crosses Task scope"):
        _context(
            manifest.capability,
            upstream=(_result_ref(task_id=f"tsk_{'0' * 32}"),),
        )

    with pytest.raises(ValidationError, match="duplicate ResultRefs"):
        _context(manifest.capability, upstream=(ref, ref))


def test_dependency_gate_does_not_implicitly_become_semantic_input() -> None:
    pack = _pack()
    manifest = _manifest(pack, consumes=())
    registry = CapabilityExecutorRegistry()
    registry.register(manifest, QueryInput, QueryOutput, FakeExecutor())
    dependency = _result_ref()
    context = _context(pack_ref := manifest.capability, upstream=(dependency,), consumed=())

    registration = registry.resolve_for_execution(
        context,
        bound_capability=pack_ref,
        bound_node_kind=DraftNodeKind.CAPABILITY,
    )

    assert registration.manifest.consumes == ()
    assert context.upstream_result_refs == (dependency,)
    assert context.consumed_result_refs == ()


def test_semantic_input_must_be_a_verified_dependency() -> None:
    manifest = _manifest()
    with pytest.raises(ValidationError, match="verified dependency edge"):
        _context(
            manifest.capability,
            upstream=(),
            consumed=(_result_ref(),),
        )
