from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select

from deskpilot.application.agent_execution_runtime import AgentExecutionRuntime
from deskpilot.application.builtin_capability_executors import (
    create_builtin_capability_executor_registry,
)
from deskpilot.application.capability_catalog import (
    create_builtin_capability_catalog,
)
from deskpilot.application.capability_executor_registry import (
    CapabilityExecutorRegistry,
)
from deskpilot.application.model_planner_node_binder import ModelPlannerNodeBinder
from deskpilot.application.task_loop_activation_runtime import TaskLoopActivationRuntime
from deskpilot.application.task_loop_agent_adapter_registry import (
    create_task_loop_agent_adapter_registry,
)
from deskpilot.application.task_loop_agent_runtime import (
    AgentSourcePlanProof,
    TaskLoopAgentProofRejectedError,
    TaskLoopAgentRuntime,
)
from deskpilot.application.workspace_file_runtime import WorkspaceFileRuntime
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.capability_execution import (
    CapabilityApprovalRequirement,
    CapabilityEffectClass,
    CapabilityExecutionContext,
    CapabilityExecutorManifest,
    CapabilityRecoveryPolicy,
    CapabilityResultKind,
)
from deskpilot.domain.task_plans import CapabilityPack, CapabilityRef, DraftNodeKind
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import (
    AgentInvocationRecord,
    ConversationMessageRecord,
    ModelPlannerNodeBindingRecord,
    TaskExecutionNodeRecord,
    TaskLoopNodeAttemptRecord,
    TaskLoopVerifiedResultRecord,
    TaskRecord,
    TurnRouteRecord,
    WorkspaceAgentResultRecord,
)

sys.path.insert(0, str(Path(__file__).parent))

from test_multi_step_plan_runtime import (  # type: ignore[import-not-found]  # noqa: E402
    NOW,
    ScriptedTurnPlannerProvider,
    _runtimes,
    _seed_turn,
    _unsupported_route,
)


class _NoopInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _NoopOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    result_digest: str


class _NoopExecutor:
    async def execute(
        self,
        context: CapabilityExecutionContext,
        arguments: BaseModel,
    ) -> BaseModel:
        del context, arguments
        raise AssertionError("eligibility-only executor must not run")

    async def verify(
        self,
        context: CapabilityExecutionContext,
        candidate: BaseModel,
    ) -> BaseModel:
        del context, candidate
        raise AssertionError("eligibility-only executor must not verify")


def _offer_key(request: object, parameter_name: str) -> str:
    messages = cast(Any, request).messages
    payload = json.loads(messages[1].content)
    matches = [
        item["offer"]["offer_key"]
        for item in payload["offers"]
        if any(
            spec["parameter_name"] == parameter_name
            for spec in item["parameter_specs"]
        )
    ]
    assert len(matches) == 1, (
        parameter_name,
        [
            (
                item["intent_description"],
                [spec["parameter_name"] for spec in item["parameter_specs"]],
            )
            for item in payload["offers"]
        ],
    )
    return str(matches[0])


def _select_workspace_then_knowledge(request: object) -> dict[str, object]:
    return {
        "schema_version": "deskpilot.turn-planner-decision.v1",
        "kind": "propose_steps",
        "steps": [
            {
                "offer_key": _offer_key(request, "path"),
                "parameters": [{"name": "path", "value": "cats.md"}],
            },
            {
                "offer_key": _offer_key(request, "query"),
                "parameters": [{"name": "query", "value": "stats"}],
            },
        ],
    }


def _select_research_then_knowledge(request: object) -> dict[str, object]:
    return {
        "schema_version": "deskpilot.turn-planner-decision.v1",
        "kind": "propose_steps",
        "steps": [
            {
                "offer_key": _offer_key(request, "goal"),
                "parameters": [{"name": "goal", "value": "cats"}],
            },
            {
                "offer_key": _offer_key(request, "query"),
                "parameters": [{"name": "query", "value": "stats"}],
            },
        ],
    }


def _ref(pack: CapabilityPack) -> CapabilityRef:
    return CapabilityRef(
        capability_id=pack.capability_id,
        version=pack.version,
        digest=pack.digest,
    )


def _register_eligibility_only(
    registry: CapabilityExecutorRegistry,
    pack: CapabilityPack,
    *,
    result_kind: CapabilityResultKind,
) -> None:
    manifest = CapabilityExecutorManifest.from_pack(
        executor_id=f"test.{pack.capability_id}",
        pack=pack,
        input_model=_NoopInput,
        output_model=_NoopOutput,
        node_kinds=(DraftNodeKind.CAPABILITY,),
        consumes=(),
        produces=result_kind,
        effect_class=CapabilityEffectClass.READ_ONLY,
        approval_requirement=CapabilityApprovalRequirement.NONE,
        recovery_policy=CapabilityRecoveryPolicy.DETERMINISTIC_RETRY,
    )
    registry.register(manifest, _NoopInput, _NoopOutput, _NoopExecutor())


def _executor_registry(
    planner: Any,
    *,
    research_artifacts: bool,
) -> CapabilityExecutorRegistry:
    capabilities = planner._capabilities  # noqa: SLF001 - exact shared fixture
    registry = create_builtin_capability_executor_registry(
        capabilities,
        knowledge=cast(Any, object()),
    )
    if research_artifacts:
        _register_eligibility_only(
            registry,
            capabilities.resolve_preferred("artifact.html.v1"),
            result_kind=CapabilityResultKind.ARTIFACT,
        )
        _register_eligibility_only(
            registry,
            capabilities.resolve_preferred("browser.verify.v1"),
            result_kind=CapabilityResultKind.BROWSER_VERIFICATION,
        )
    return registry


async def _activate(
    database: Database,
    provider: ScriptedTurnPlannerProvider,
    *,
    suffix: str,
    variants: frozenset[str],
    research_artifacts: bool = False,
    message: str | None = None,
) -> tuple[Any, AgentExecutionRuntime, Any, Any]:
    planner, planning, task_loops, _composer = _runtimes(database, provider)
    if research_artifacts:
        capabilities = create_builtin_capability_catalog(
            research_runtime_enabled=True
        )
        planner._capabilities = capabilities  # noqa: SLF001 - exact test fixture
        planning._compiler._capabilities = capabilities  # noqa: SLF001
        task_loops._composer._capabilities = capabilities  # noqa: SLF001
    task_id, fallback = await _seed_turn(database, suffix=suffix)
    if message is not None:
        async with database.session() as session, session.begin():
            task = await session.get(TaskRecord, task_id)
            message_record = await session.get(
                ConversationMessageRecord,
                fallback.user_message_id,
            )
            route = await session.get(TurnRouteRecord, task_id)
            assert task is not None and message_record is not None and route is not None
            material = {
                "message_id": message_record.message_id,
                "conversation_id": message_record.conversation_id,
                "task_id": task_id,
                "role": "user",
                "content": message,
                "content_ref": None,
                "classification": "internal",
                "created_at": NOW,
            }
            message_digest = sha256_digest(material)
            fallback = _unsupported_route(
                task_id=task_id,
                conversation_id=message_record.conversation_id,
                message_id=message_record.message_id,
                message_digest=message_digest,
            )
            task.goal = message
            message_record.content = message
            message_record.message_digest = message_digest
            route.candidate_digest = fallback.candidate_digest
            route.parameter_digest = fallback.parameter_digest
    await planner.prepare(
        task_id,
        fallback.user_message_id,
        fallback,
        variants,
    )
    interpreted = await planner.interpret(task_id)
    assert interpreted.binding is not None
    assert interpreted.binding.status == "multi_step_deferred", (
        interpreted.adjudication.outcome if interpreted.adjudication else None,
        interpreted.adjudication.reason_code if interpreted.adjudication else None,
        interpreted.binding.reason_code,
        provider.calls,
        len(provider.decisions),
    )
    await task_loops.plan(task_id)
    execution = AgentExecutionRuntime(
        database,
        planning._compiler,  # noqa: SLF001 - exact shared fixture
        planner._agents,  # noqa: SLF001 - exact shared fixture
    )
    activation = TaskLoopActivationRuntime(
        database,
        task_loops,
        planner,
        planning,
        execution,
        ModelPlannerNodeBinder(
            planner._agents,  # noqa: SLF001 - exact shared fixture
            _executor_registry(
                planner,
                research_artifacts=research_artifacts,
            ),
            create_task_loop_agent_adapter_registry(
                research_available=True,
                workspace_file_available=True,
            ),
        ),
        clock=lambda: NOW,
    )
    active = await activation.activate(task_id)
    revalidated = await planner.revalidate_deferred_plan(task_id)
    return active, execution, revalidated, fallback


def _source_proof(revalidated: Any, route_id: str) -> AgentSourcePlanProof:
    matches = [
        item
        for item in revalidated.steps
        if item.offer.trusted_recipe.route_id == route_id
    ]
    assert len(matches) == 1
    step = matches[0]
    return AgentSourcePlanProof(
        source_contract=step.route.contract,
        source_plan=step.offer.expected_plan,
    )


@pytest.mark.asyncio
async def test_workspace_agent_bridge_is_source_bound_verified_and_restart_safe(
    tmp_path: Path,
) -> None:
    database = Database(
        f"sqlite+aiosqlite:///{(tmp_path / 'workspace-agent.db').as_posix()}"
    )
    await database.migrate()
    provider = ScriptedTurnPlannerProvider([_select_workspace_then_knowledge])
    (tmp_path / "cats.md").write_text(
        "source-bound workspace content",
        encoding="utf-8",
    )
    try:
        active, execution, revalidated, fallback = await _activate(
            database,
            provider,
            suffix="e",
            variants=frozenset({"workspace_file_read", "knowledge_lookup"}),
            message="cats.md stats",
        )
        runtime = TaskLoopAgentRuntime(
            database,
            execution,
            create_task_loop_agent_adapter_registry(
                research_available=True,
                workspace_file_available=True,
            ),
            workspace=WorkspaceFileRuntime(str(tmp_path)),
        )
        claimed = await runtime.claim_next(active.execution_id, "workspace-worker")
        assert claimed is not None
        assert claimed.route_id == "workspace_file_read"
        assert claimed.parameter_name == "path"
        assert claimed.parameter_value == "cats.md"
        assert claimed.attempt.claim_fencing_token == claimed.claimed.claim_fencing_token

        source_proof = _source_proof(revalidated, "workspace_file_read")
        await runtime.run_workspace_file_candidate(claimed)
        restarted = TaskLoopAgentRuntime(
            database,
            execution,
            create_task_loop_agent_adapter_registry(
                research_available=True,
                workspace_file_available=True,
            ),
            workspace=WorkspaceFileRuntime(str(tmp_path)),
        )
        recovered_claim = await restarted.recover_pending(active.execution_id)
        assert recovered_claim is not None
        assert recovered_claim.attempt.attempt_id == claimed.attempt.attempt_id
        assert recovered_claim.claimed.invocation.result_id is not None
        first = await restarted.persist_verified_result(recovered_claim, source_proof)

        assert first.result_kind is CapabilityResultKind.WORKSPACE_FILE
        assert first.producer_node_id == claimed.binding.composite_node_id
        assert first.producer_attempt == 1
        async with database.session() as session:
            route = await session.get(TurnRouteRecord, fallback.task_id)
            attempt = await session.get(
                TaskLoopNodeAttemptRecord,
                claimed.attempt.attempt_id,
            )
            verified = await session.scalar(
                select(TaskLoopVerifiedResultRecord).where(
                    TaskLoopVerifiedResultRecord.attempt_id
                    == claimed.attempt.attempt_id
                )
            )
            invocation = await session.get(
                AgentInvocationRecord,
                claimed.claimed.invocation.invocation_id,
            )
            workspace = await session.get(
                WorkspaceAgentResultRecord,
                claimed.claimed.invocation.invocation_id,
            )
            node = await session.get(
                TaskExecutionNodeRecord,
                claimed.binding.composite_node_id,
            )
        assert route is not None and route.route_id is None and route.parameters == {}
        assert attempt is not None and attempt.status == "verified"
        assert attempt.verification_digest == first.verification_digest
        assert verified is not None and verified.producer_kind == "agent_bridge"
        assert verified.result_ref_manifest == first.model_dump(mode="json")
        assert verified.output_manifest["content"] == "source-bound workspace content"
        assert invocation is not None and invocation.verification_status == "verified"
        assert workspace is not None and workspace.result_digest == first.result_digest
        assert node is not None and node.status == "verified"

        restarted_again = TaskLoopAgentRuntime(
            database,
            execution,
            create_task_loop_agent_adapter_registry(
                research_available=True,
                workspace_file_available=True,
            ),
            workspace=WorkspaceFileRuntime(str(tmp_path)),
        )
        assert await restarted_again.recover_pending(active.execution_id) is None
        recovered = await restarted_again.persist_verified_result(
            recovered_claim,
            source_proof,
        )
        assert recovered == first
        async with database.session() as session:
            count = int(
                await session.scalar(
                    select(func.count()).select_from(TaskLoopVerifiedResultRecord)
                )
                or 0
            )
        assert count == 1
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_research_claim_uses_namespaced_source_goal_and_rejects_binding_drift(
    tmp_path: Path,
) -> None:
    database = Database(
        f"sqlite+aiosqlite:///{(tmp_path / 'research-agent.db').as_posix()}"
    )
    await database.migrate()
    provider = ScriptedTurnPlannerProvider([_select_research_then_knowledge])
    cast(Any, provider)._descriptor = provider.descriptor.model_copy(  # noqa: SLF001
        update={
            "capabilities": provider.descriptor.capabilities.model_copy(
                update={"max_context_tokens": 1_000_000}
            )
        }
    )
    try:
        active, execution, _revalidated, fallback = await _activate(
            database,
            provider,
            suffix="f",
            variants=frozenset({"research_to_html", "knowledge_lookup"}),
            research_artifacts=True,
        )
        async with database.session() as session, session.begin():
            route = await session.get(TurnRouteRecord, fallback.task_id)
            assert route is not None
            route.parameters = {"goal": "malicious TurnRoute fallback"}
            route.parameter_digest = sha256_digest(route.parameters)

        runtime = TaskLoopAgentRuntime(
            database,
            execution,
            create_task_loop_agent_adapter_registry(
                research_available=True,
                workspace_file_available=True,
            ),
        )
        async with database.session() as session, session.begin():
            bindings = tuple(
                (
                    await session.scalars(
                        select(ModelPlannerNodeBindingRecord).where(
                            ModelPlannerNodeBindingRecord.execution_id
                            == active.execution_id
                        )
                    )
                ).all()
            )
            binding = next(
                item
                for item in bindings
                if item.recipe_manifest["route_id"] == "research_to_html"
                and item.mapping_manifest["source_local_key"] == "research"
            )
            original_input = dict(binding.bound_input_manifest)
            binding.bound_input_manifest = {"goal": "drift"}
        async with database.session() as session:
            persisted_binding = await session.get(
                ModelPlannerNodeBindingRecord,
                binding.node_binding_id,
            )
            assert persisted_binding is not None
            assert persisted_binding.bound_input_manifest == {"goal": "drift"}
            assert persisted_binding.manifest["bound_input_manifest"] == original_input
            ready_nodes = tuple(
                (
                    await session.scalars(
                        select(TaskExecutionNodeRecord).where(
                            TaskExecutionNodeRecord.run_id == active.run_id,
                            TaskExecutionNodeRecord.status == "ready",
                        )
                    )
                ).all()
            )
            assert [(item.local_key, item.bound_agent) for item in ready_nodes] == [
                (
                    persisted_binding.mapping_manifest["composite_local_key"],
                    persisted_binding.effective_authority_manifest["bound_agent"],
                )
            ]
        with pytest.raises(TaskLoopAgentProofRejectedError):
            await runtime.claim_next(active.execution_id, "tampered-worker")
        async with database.session() as session, session.begin():
            binding = await session.get(
                ModelPlannerNodeBindingRecord,
                binding.node_binding_id,
            )
            assert binding is not None
            binding.bound_input_manifest = original_input

        claimed = await runtime.claim_next(active.execution_id, "research-worker")
        assert claimed is not None
        assert claimed.route_id == "research_to_html"
        assert claimed.parameter_name == "goal"
        assert claimed.parameter_value == "cats"
        assert claimed.binding.mapping.composite_local_key.startswith("s01_")
    finally:
        await database.dispose()
