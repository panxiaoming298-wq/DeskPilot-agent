"""Pure production ModelRequest builders shared by runtime and calibration."""

from typing import Literal, cast

from pydantic import JsonValue

from deskpilot.domain.agent_loop import (
    DynamicCoordinatorLoopDecision,
    WorkspaceBoundedCodingCoordinatorDecision,
    WorkspacePatchLoopDecision,
)
from deskpilot.domain.model_contracts import (
    ModelCapabilityRequirements,
    ModelExecutionBudget,
    ModelMessage,
    ModelRequest,
    ModelRole,
    PrivacyMode,
    StructuredOutputDefinition,
)
from deskpilot.domain.task_plans import PlanNodeBudget


def bind_agent_model_request(
    request: ModelRequest,
    *,
    agent_id: str,
    agent_version: str,
    contract_digest: str,
    prompt_package_digest: str,
    prompt_instruction: str,
) -> ModelRequest:
    """Render one frozen Prompt Package and bind its exact Agent identity."""

    metadata = {
        **request.metadata,
        "agent_id": agent_id,
        "agent_version": agent_version,
        "agent_contract_digest": contract_digest,
        "agent_prompt_package_digest": prompt_package_digest,
    }
    return request.model_copy(
        update={
            "messages": (
                ModelMessage(role="system", content=prompt_instruction),
                *request.messages[1:],
            ),
            "metadata": metadata,
        }
    )


def build_patch_planner_model_request(
    *,
    request_id: str,
    task_id: str,
    privacy_mode: PrivacyMode,
    budget: PlanNodeBudget,
    phase: Literal["request_route", "propose_patch"],
    path: str,
    project_path: str,
    test_path: str,
    test_kind: Literal["python", "node"],
    objective: str,
    route_binding_id: str,
    patch_binding_id: str,
    route_id: Literal[
        "workspace_agent_patch_test",
        "workspace_dynamic_patch_test",
        "workspace_coding_loop",
    ],
    upstream_data: list[dict[str, object]],
    observation_digest: str | None = None,
    source_text: str | None = None,
    provider_hint: str | None = None,
) -> ModelRequest:
    """Build the exact bounded Patch Planner request used in production."""

    return ModelRequest(
        request_id=request_id,
        task_id=task_id,
        role=ModelRole.TOOL_AGENT,
        messages=(
            ModelMessage(
                role="system",
                content=(
                    "Return exactly one strict patch-planner decision. You may only read the "
                    "exact server-bound file and may propose one exact replacement for that "
                    "same path. A proposal grants no write authority. Only the user may "
                    "confirm the exact staged digest, and the server fixes the test runtime "
                    "and argv."
                ),
            ),
            ModelMessage(
                role="user",
                content=str(
                    {
                        "phase": phase,
                        "allowed_route_binding_id": route_binding_id,
                        "allowed_patch_binding_id": patch_binding_id,
                        "path": path,
                        "project_path": project_path,
                        "test_path": test_path,
                        "test_kind": test_kind,
                        "external_untrusted_objective": objective,
                        "external_untrusted_verified_upstream_results": upstream_data,
                        "route_observation_digest": observation_digest,
                        "external_untrusted_workspace_data": source_text,
                    }
                )[:200_000],
            ),
        ),
        privacy_mode=privacy_mode,
        requirements=ModelCapabilityRequirements(
            structured_output=True,
            strict_json_schema=True,
            min_context_tokens=8_192,
        ),
        output_schema=StructuredOutputDefinition.from_model(
            name="workspace_patch_planner_loop_decision",
            description="One bound file read request or one unprivileged exact patch proposal",
            model=WorkspacePatchLoopDecision,
            strict=True,
        ),
        provider_hint=provider_hint,
        max_output_tokens=max(1, budget.output_tokens),
        timeout_seconds=float(budget.wall_seconds),
        execution_budget=ModelExecutionBudget(
            max_attempts=1,
            max_retry_delay_seconds=0,
            max_task_cost_micros=budget.cost_micros,
        ),
        metadata={
            "agent_id": "builtin.workspace_patch_planner",
            "agent_version": "1.0.0",
            "agent_loop_phase": phase,
            "workspace_route_id": route_id,
            "route_binding_id": route_binding_id,
            "workspace_patch_binding_id": patch_binding_id,
            "workspace_path": path,
            "workspace_project_path": project_path,
            "workspace_test_path": test_path,
            "workspace_test_kind": test_kind,
            "workspace_patch_objective": objective,
            "observation_digest": observation_digest,
            "workspace_patch_source_text": source_text,
        },
    )


def build_dynamic_coordinator_model_request(
    *,
    request_id: str,
    task_id: str,
    privacy_mode: PrivacyMode,
    budget: PlanNodeBudget,
    phase: Literal["propose_task_graph", "submit_result"],
    offered_capabilities: list[dict[str, object]],
    allowed_context_refs: tuple[str, ...],
    max_nodes: int,
    repair_advice: dict[str, object] | None,
    import_sources: list[dict[str, object]],
    graph_id: str | None = None,
    graph_digest: str | None = None,
    observation_digest: str | None = None,
    external_untrusted_projection: dict[str, object] | None = None,
    provider_hint: str | None = None,
) -> ModelRequest:
    """Build the exact bounded dynamic Coordinator request used in production."""

    return ModelRequest(
        request_id=request_id,
        task_id=task_id,
        role=ModelRole.SUMMARIZER,
        messages=(
            ModelMessage(
                role="system",
                content=(
                    "Return exactly one strict dynamic coordinator decision. A proposed "
                    "task graph is untrusted until the server binds exact Agents, validates "
                    "the complete DAG, one output node and conserved budget, and seals it "
                    "atomically. The output node must transitively depend on every node. "
                    "Repair advice grants no capability. Cross-generation evidence may only "
                    "be selected by the exact server-offered import source key. Every Patch "
                    "node must select one unique server-offered input binding key; a selected "
                    "binding still grants no write authority and requires its own user "
                    "confirmation."
                ),
            ),
            ModelMessage(
                role="user",
                content=str(
                    {
                        "phase": phase,
                        "allowed_capabilities": offered_capabilities,
                        "allowed_context_refs": allowed_context_refs,
                        "max_nodes": max_nodes,
                        "repair_advice": repair_advice,
                        "verified_task_graph": (
                            {
                                "graph_id": graph_id,
                                "graph_digest": graph_digest,
                                "observation_digest": observation_digest,
                                "external_untrusted_projection": external_untrusted_projection,
                            }
                            if graph_id is not None
                            else None
                        ),
                    }
                )[:200_000],
            ),
        ),
        privacy_mode=privacy_mode,
        requirements=ModelCapabilityRequirements(
            structured_output=True,
            strict_json_schema=True,
            min_context_tokens=8_192,
        ),
        output_schema=StructuredOutputDefinition.from_model(
            name="workspace_dynamic_coordinator_loop_decision",
            description="One complete child DAG proposal or verified graph result",
            model=DynamicCoordinatorLoopDecision,
            strict=True,
        ),
        provider_hint=provider_hint,
        max_output_tokens=max(1, budget.output_tokens),
        timeout_seconds=float(budget.wall_seconds),
        execution_budget=ModelExecutionBudget(
            max_attempts=1,
            max_retry_delay_seconds=0,
            max_task_cost_micros=budget.cost_micros,
        ),
        metadata={
            "agent_id": "builtin.workspace_coordinator",
            "agent_version": "1.1.0",
            "agent_loop_phase": phase,
            "task_graph_allowed_capabilities": cast(JsonValue, offered_capabilities),
            "task_graph_context_refs": list(allowed_context_refs),
            "task_graph_max_nodes": max_nodes,
            "task_graph_repair_advice": cast(JsonValue, repair_advice),
            "task_graph_import_sources": cast(JsonValue, import_sources),
            "task_graph_id": graph_id,
            "task_graph_observation_digest": observation_digest,
        },
    )


def build_bounded_coding_coordinator_model_request(
    *,
    request_id: str,
    task_id: str,
    privacy_mode: PrivacyMode,
    budget: PlanNodeBudget,
    offered_capabilities: list[dict[str, object]],
    allowed_context_refs: tuple[str, ...],
) -> ModelRequest:
    """Build the distinct 3..8-file Coordinator request without changing v1.1."""

    base = build_dynamic_coordinator_model_request(
        request_id=request_id,
        task_id=task_id,
        privacy_mode=privacy_mode,
        budget=budget,
        phase="propose_task_graph",
        offered_capabilities=offered_capabilities,
        allowed_context_refs=allowed_context_refs,
        max_nodes=len(offered_capabilities),
        repair_advice=None,
        import_sources=[],
    )
    return base.model_copy(
        update={
            "output_schema": StructuredOutputDefinition.from_model(
                name="workspace_bounded_coding_coordinator_decision",
                description="One exact server-sealed 3..8-file coding DAG confirmation",
                model=WorkspaceBoundedCodingCoordinatorDecision,
                strict=True,
            ),
            "metadata": {
                **base.metadata,
                "agent_id": "builtin.workspace_bounded_coordinator",
                "agent_version": "1.0.0",
            },
        }
    )
