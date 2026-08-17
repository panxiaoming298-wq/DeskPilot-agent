import json
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, ValidationError

from deskpilot.agents import create_builtin_agent_registry
from deskpilot.application.agent_registry import (
    AgentAlreadyRegisteredError,
    AgentContractInvalidError,
    AgentDisabledError,
    AgentHandoffNotAllowedError,
    AgentIoSchemaMismatchError,
    AgentPromptDigestMismatchError,
    AgentRegistration,
    AgentRegistry,
    AgentRegistryFrozenError,
    AgentRevokedError,
    AgentToolContractMismatchError,
    load_agent_contract,
    load_prompt_package,
)
from deskpilot.application.plan_binder import (
    AgentBudgetExceededError,
    AgentPlanBinder,
    AgentToolNotAllowedError,
)
from deskpilot.domain.agent_contracts import (
    AgentBudgetPolicy,
    AgentContextPolicy,
    AgentContract,
    AgentHandoffPolicy,
    AgentHandoffRef,
    AgentKind,
    AgentModelPolicy,
    AgentPlanBudget,
    AgentPlanDraftStep,
    AgentRegistryStatus,
    AgentResultPolicy,
    AgentToolGrant,
    AgentToolPolicy,
    PromptPackageRef,
)
from deskpilot.domain.model_contracts import (
    ModelCapabilityRequirements,
    ModelLocation,
    ModelRole,
)
from deskpilot.domain.tool_contracts import ToolRiskLevel
from deskpilot.model_providers.fake import FakeModelProvider
from deskpilot.tools import create_builtin_registry


class SampleInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    objective_ref: str


class SampleOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    result_ref: str


def _write_prompt(root: Path, package_id: str = "test.agent_prompt") -> object:
    root.mkdir(parents=True, exist_ok=True)
    (root / "agent.txt").write_text(
        "Use only supplied references and return evidence references.", encoding="utf-8"
    )
    (root / "agent.json").write_text(
        json.dumps(
            {
                "schema_version": "deskpilot.prompt-package.v1",
                "package_id": package_id,
                "version": "1.0.0",
                "renderer_version": 1,
                "instruction_file": "agent.txt",
                "variables": [],
            }
        ),
        encoding="utf-8",
    )
    return load_prompt_package(root, "agent.json")


def _registration(
    prompt: object,
    *,
    agent_id: str = "test.alpha",
    delegates: tuple[AgentHandoffRef, ...] = (),
    receives: tuple[AgentHandoffRef, ...] = (),
    grant_disk: bool = False,
) -> AgentRegistration:
    from deskpilot.application.agent_registry import PromptPackage

    assert isinstance(prompt, PromptPackage)
    grants: tuple[AgentToolGrant, ...] = ()
    tool_calls = 0
    if grant_disk:
        disk = create_builtin_registry().resolve("computer.disk_usage", "1.0.0").contract
        grants = (
            AgentToolGrant(
                name=disk.name,
                version=disk.version,
                contract_digest=disk.digest,
                max_calls=1,
            ),
        )
        tool_calls = 1
    contract = AgentContract(
        schema_version="deskpilot.agent-contract.v1",
        agent_id=agent_id,
        version="1.0.0",
        kind=AgentKind.WORKER,
        display_name=agent_id,
        description="A fixed test Agent.",
        provides=("test.evidence.read",),
        prompt_package=PromptPackageRef(
            package_id=prompt.manifest.package_id,
            version=prompt.manifest.version,
            renderer_version=prompt.manifest.renderer_version,
            digest=prompt.digest,
        ),
        input_schema=SampleInput.model_json_schema(),
        output_schema=SampleOutput.model_json_schema(),
        tool_policy=AgentToolPolicy(
            max_risk_level=ToolRiskLevel.R0,
            grants=grants,
        ),
        handoff_policy=AgentHandoffPolicy(
            may_delegate_to=delegates,
            may_receive_from=receives,
            max_outgoing_handoffs=len(delegates),
        ),
        model_policy=AgentModelPolicy(
            role=ModelRole.TOOL_AGENT,
            allowed_locations=(ModelLocation.LOCAL,),
            allowed_privacy_modes=("local_only",),
            requirements=ModelCapabilityRequirements(
                structured_output=True,
                strict_json_schema=True,
                min_context_tokens=1_024,
            ),
        ),
        context_policy=AgentContextPolicy(allowed_sources=("task_contract",)),
        budget_policy=AgentBudgetPolicy(
            max_model_calls=1,
            max_tool_calls=tool_calls,
            max_input_tokens=2_000,
            max_output_tokens=500,
            max_wall_seconds=30,
            max_retries=0,
            max_cost_micros=10_000,
            max_handoffs=len(delegates),
        ),
        result_policy=AgentResultPolicy(required_evidence=("result_ref",)),
    )
    return AgentRegistration(
        contract=contract,
        input_model=SampleInput,
        output_model=SampleOutput,
        prompt_package=prompt,
    )


def _freeze(registry: AgentRegistry) -> None:
    registry.freeze(
        create_builtin_registry(),
        (FakeModelProvider().descriptor,),
    )


def _budget(**updates: int) -> AgentPlanBudget:
    values = {
        "model_calls": 1,
        "tool_calls": 0,
        "input_tokens": 1_000,
        "output_tokens": 200,
        "wall_seconds": 10,
        "retries": 0,
        "cost_micros": 1_000,
        "handoffs": 0,
    }
    values.update(updates)
    return AgentPlanBudget(**values)


def test_builtin_registry_is_frozen_redacted_and_supervisor_is_not_an_agent() -> None:
    registry = create_builtin_agent_registry(
        create_builtin_registry(), (FakeModelProvider().descriptor,)
    )
    snapshot = registry.snapshot()

    assert {item.agent_id for item in snapshot.agents} == {
        "builtin.computer_observer",
        "builtin.knowledge_researcher",
        "builtin.task_synthesizer",
        "builtin.web_researcher",
    }
    assert all(item.status is AgentRegistryStatus.ENABLED for item in snapshot.agents)
    assert "supervisor" not in snapshot.model_dump_json().lower()
    assert "instruction" not in snapshot.model_dump_json().lower()
    assert all(item.input_schema_digest and item.output_schema_digest for item in snapshot.agents)
    with pytest.raises(AgentRegistryFrozenError):
        computer = registry.resolve_exact("builtin.computer_observer", "1.0.0")
        registry.register(_registration(computer.prompt_package))


def test_agent_api_is_read_only_filterable_exact_and_redacted(client: TestClient) -> None:
    response = client.get("/api/v1/agents?capability=knowledge.local.search")
    exact = client.get("/api/v1/agents/builtin.knowledge_researcher/versions/1.0.0")
    snapshot = client.get("/api/v1/agents/registry-snapshot")

    assert response.status_code == exact.status_code == snapshot.status_code == 200
    assert [item["agent_id"] for item in response.json()["agents"]] == [
        "builtin.knowledge_researcher"
    ]
    assert exact.headers["cache-control"] == "no-store"
    assert "instruction" not in exact.text.lower()
    assert client.get("/api/v1/agents/builtin.missing/versions/1.0.0").status_code == 404
    assert client.post("/api/v1/agents", json={}).status_code == 405


def test_strict_contract_and_prompt_loaders_reject_unknown_duplicate_and_secret(
    tmp_path: Path,
) -> None:
    registration = _registration(_write_prompt(tmp_path / "valid"))
    payload = registration.contract.model_dump_json().encode()
    assert load_agent_contract(payload).digest == registration.contract.digest

    unknown = json.loads(payload)
    unknown["dynamic_python"] = "print('no')"
    with pytest.raises(AgentContractInvalidError):
        load_agent_contract(json.dumps(unknown).encode())

    duplicate = tmp_path / "duplicate"
    duplicate.mkdir()
    (duplicate / "agent.txt").write_text("Safe instruction.", encoding="utf-8")
    (duplicate / "agent.json").write_text(
        '{"schema_version":"deskpilot.prompt-package.v1","package_id":"test.prompt","package_id":"test.forged","version":"1.0.0","renderer_version":1,"instruction_file":"agent.txt","variables":[]}',
        encoding="utf-8",
    )
    with pytest.raises(AgentContractInvalidError):
        load_prompt_package(duplicate, "agent.json")

    secret = tmp_path / "secret"
    _write_prompt(secret)
    (secret / "agent.txt").write_text("Authorization: Bearer hidden", encoding="utf-8")
    with pytest.raises(AgentContractInvalidError):
        load_prompt_package(secret, "agent.json")
    with pytest.raises(AgentContractInvalidError):
        load_prompt_package(tmp_path / "missing", "agent.json")


def test_registry_rejects_duplicate_io_prompt_and_tool_drift(tmp_path: Path) -> None:
    prompt = _write_prompt(tmp_path / "prompt")
    registration = _registration(prompt, grant_disk=True)
    duplicate = AgentRegistry()
    duplicate.register(registration)
    with pytest.raises(AgentAlreadyRegisteredError):
        duplicate.register(registration)

    bad_io = AgentRegistry()
    bad_io.register(
        AgentRegistration(
            contract=registration.contract.model_copy(update={"input_schema": {}}),
            input_model=SampleInput,
            output_model=SampleOutput,
            prompt_package=registration.prompt_package,
        )
    )
    with pytest.raises(AgentIoSchemaMismatchError):
        _freeze(bad_io)

    changed_prompt = _write_prompt(tmp_path / "prompt")
    (tmp_path / "prompt" / "agent.txt").write_text("A changed instruction.", encoding="utf-8")
    changed_prompt = load_prompt_package(tmp_path / "prompt", "agent.json")
    bad_prompt = AgentRegistry()
    bad_prompt.register(
        AgentRegistration(
            contract=registration.contract,
            input_model=SampleInput,
            output_model=SampleOutput,
            prompt_package=changed_prompt,
        )
    )
    with pytest.raises(AgentPromptDigestMismatchError):
        _freeze(bad_prompt)

    bad_tool = AgentRegistry()
    grant = registration.contract.tool_policy.grants[0].model_copy(
        update={"contract_digest": "0" * 64}
    )
    bad_tool.register(
        AgentRegistration(
            contract=registration.contract.model_copy(
                update={
                    "tool_policy": registration.contract.tool_policy.model_copy(
                        update={"grants": (grant,)}
                    )
                }
            ),
            input_model=SampleInput,
            output_model=SampleOutput,
            prompt_package=registration.prompt_package,
        )
    )
    with pytest.raises(AgentToolContractMismatchError):
        _freeze(bad_tool)


def test_registry_rejects_handoff_cycle(tmp_path: Path) -> None:
    prompt = _write_prompt(tmp_path)
    alpha = AgentHandoffRef(agent_id="test.alpha", version="1.0.0")
    beta = AgentHandoffRef(agent_id="test.beta", version="1.0.0")
    registry = AgentRegistry()
    registry.register(_registration(prompt, delegates=(beta,), receives=(beta,)))
    registry.register(
        _registration(
            prompt,
            agent_id="test.beta",
            delegates=(alpha,),
            receives=(alpha,),
        )
    )
    with pytest.raises(AgentHandoffNotAllowedError):
        _freeze(registry)


def test_unsatisfied_model_disables_descriptor_and_blocks_resolution(tmp_path: Path) -> None:
    registry = AgentRegistry()
    registry.register(_registration(_write_prompt(tmp_path)))
    registry.freeze(create_builtin_registry(), ())

    descriptor = registry.descriptor_exact("test.alpha", "1.0.0")
    assert descriptor.status is AgentRegistryStatus.DISABLED
    assert descriptor.status_reason == "model_requirements_unsatisfied"
    with pytest.raises(AgentDisabledError):
        registry.resolve_exact("test.alpha", "1.0.0")

    revoked = AgentRegistry()
    revoked.register(
        replace(
            _registration(_write_prompt(tmp_path / "revoked")),
            status=AgentRegistryStatus.REVOKED,
        )
    )
    revoked.freeze(create_builtin_registry(), ())
    assert revoked.descriptor_exact("test.alpha", "1.0.0").status is AgentRegistryStatus.REVOKED
    with pytest.raises(AgentRevokedError):
        revoked.resolve_exact("test.alpha", "1.0.0")


def test_plan_binder_seals_exact_digests_and_enforces_tool_and_budget(tmp_path: Path) -> None:
    registry = AgentRegistry()
    registry.register(_registration(_write_prompt(tmp_path), grant_disk=True))
    _freeze(registry)
    binder = AgentPlanBinder(registry)
    draft = AgentPlanDraftStep(
        step_id="observe",
        agent_selector="test.alpha",
        tool_name="computer.disk_usage",
        tool_version="1.0.0",
        budget=_budget(tool_calls=1),
    )

    bound = binder.bind(draft)
    binder.validate_bound(bound)
    registered = registry.resolve_exact("test.alpha", "1.0.0")
    assert bound.agent.contract_digest == registered.contract.digest
    assert bound.tool is not None and bound.tool.contract_digest

    with pytest.raises(AgentToolNotAllowedError):
        binder.bind(
            draft.model_copy(
                update={"tool_name": "files.move", "tool_version": "1.0.0"}
            )
        )
    with pytest.raises(AgentBudgetExceededError):
        binder.bind(draft.model_copy(update={"budget": _budget(model_calls=2)}))
    with pytest.raises(ValidationError):
        AgentPlanDraftStep.model_validate(
            {**draft.model_dump(), "contract_digest": "0" * 64}
        )


def test_unrelated_registry_addition_keeps_bound_reference_valid_but_drift_does_not(
    tmp_path: Path,
) -> None:
    prompt = _write_prompt(tmp_path)
    original = _registration(prompt)
    first = AgentRegistry()
    first.register(original)
    _freeze(first)
    bound = AgentPlanBinder(first).bind(
        AgentPlanDraftStep(
            step_id="read",
            agent_selector="test.alpha",
            budget=_budget(),
        )
    )

    expanded = AgentRegistry()
    expanded.register(original)
    expanded.register(_registration(prompt, agent_id="test.unrelated"))
    _freeze(expanded)
    AgentPlanBinder(expanded).validate_bound(bound)

    drifted = AgentRegistry()
    drifted.register(
        AgentRegistration(
            contract=original.contract.model_copy(update={"description": "Changed."}),
            input_model=SampleInput,
            output_model=SampleOutput,
            prompt_package=original.prompt_package,
        )
    )
    _freeze(drifted)
    with pytest.raises(AgentContractInvalidError):
        AgentPlanBinder(drifted).validate_bound(bound)
