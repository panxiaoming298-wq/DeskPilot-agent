"""Deterministic local provider for development, tests, and offline demos."""

import asyncio
import json
import math
import time
from collections.abc import AsyncIterator
from typing import Literal, cast

from pydantic import JsonValue

from deskpilot.domain.agent_loop import (
    AgentNeedsUserInputDecision,
    AgentProposeHandoffDecision,
    AgentProposeTaskGraphDecision,
    AgentTaskGraphConditionProposal,
    AgentTaskGraphNodeProposal,
    CoordinatorLoopDecision,
    CoordinatorSubmitResultDecision,
    DynamicCoordinatorLoopDecision,
    DynamicCoordinatorSubmitResultDecision,
    WorkspaceBoundedCodingCoordinatorDecision,
    WorkspaceBoundedCodingGraphDecision,
    WorkspaceBoundedCodingGraphNodeProposal,
    WorkspaceLoopDecision,
    WorkspacePatchChangeProposal,
    WorkspacePatchLoopDecision,
    WorkspacePatchSubmitProposalDecision,
    WorkspaceRouteRequestDecision,
    WorkspaceSubmitResultDecision,
)
from deskpilot.domain.artifact_runtime import (
    CitationJudgment,
    CitationVerificationDecision,
)
from deskpilot.domain.model_contracts import (
    ModelCapabilities,
    ModelFinishReason,
    ModelLocation,
    ModelProtocol,
    ModelProviderDescriptor,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    ModelStreamEventType,
    ModelUsage,
    ProviderHealth,
    ProviderHealthStatus,
    ToolCallingMode,
)
from deskpilot.domain.planning import (
    PlanStep,
    TaskClassification,
    TaskComplexity,
    TaskIntent,
    TaskPlan,
)
from deskpilot.domain.research import (
    ResearchAgentDecision,
    ResearchClaimProposal,
    ResearchLoopDecision,
    ResearchRouteRequestDecision,
    ResearchSubmitResultDecision,
)
from deskpilot.domain.task_plans import PlanNodeBudget
from deskpilot.domain.tool_contracts import ToolRiskLevel
from deskpilot.domain.turn_planning import TurnPlannerUnsupportedDecision
from deskpilot.domain.workspace_coding_changes import (
    WorkspaceCodingChange,
    WorkspaceCodingChangeDecision,
)
from deskpilot.domain.workspace_coding_explorations import (
    WorkspaceCodingExplorationCandidateFile,
    WorkspaceCodingExplorationDecision,
)

TASK_CLASSIFICATION_SCHEMA = "task_classification"
TASK_PLAN_SCHEMA = "task_plan"
RESEARCH_AGENT_DECISION_SCHEMA = "research_agent_decision"
RESEARCH_AGENT_LOOP_DECISION_SCHEMA = "research_agent_loop_decision"
WORKSPACE_AGENT_LOOP_DECISION_SCHEMA = "workspace_agent_loop_decision"
WORKSPACE_PATCH_PLANNER_LOOP_DECISION_SCHEMA = "workspace_patch_planner_loop_decision"
WORKSPACE_COORDINATOR_LOOP_DECISION_SCHEMA = "workspace_coordinator_loop_decision"
WORKSPACE_DYNAMIC_COORDINATOR_LOOP_DECISION_SCHEMA = "workspace_dynamic_coordinator_loop_decision"
WORKSPACE_BOUNDED_CODING_COORDINATOR_DECISION_SCHEMA = (
    "workspace_bounded_coding_coordinator_decision"
)
CITATION_VERIFICATION_DECISION_SCHEMA = "citation_verification_decision"
TURN_PLANNER_DECISION_SCHEMA = "turn_planner_decision"
WORKSPACE_CODING_EXPLORATION_DECISION_SCHEMA = "workspace_coding_exploration_decision"
WORKSPACE_CODING_CHANGE_DECISION_SCHEMA = "workspace_coding_change_decision"


class FakeModelProvider:
    def __init__(
        self,
        *,
        provider_id: str = "fake-local",
        display_name: str = "DeskPilot Fake Model",
        model: str = "deskpilot-fake-v1",
        location: ModelLocation = ModelLocation.LOCAL,
        delay_seconds: float = 0,
        failure_message: str | None = None,
    ) -> None:
        if delay_seconds < 0:
            raise ValueError("Fake model delay cannot be negative")
        self._descriptor = ModelProviderDescriptor(
            provider_id=provider_id,
            display_name=display_name,
            model=model,
            protocol=ModelProtocol.FAKE,
            location=location,
            capabilities=ModelCapabilities(
                streaming=True,
                structured_output=True,
                strict_json_schema=True,
                tool_calling=ToolCallingMode.NONE,
                parallel_tool_calls=False,
                vision=False,
                embeddings=False,
                max_context_tokens=32_768,
            ),
        )
        self._delay_seconds = delay_seconds
        self._failure_message = failure_message

    @property
    def descriptor(self) -> ModelProviderDescriptor:
        return self._descriptor

    async def complete(self, request: ModelRequest) -> ModelResponse:
        started = time.monotonic()
        if self._delay_seconds:
            await asyncio.sleep(self._delay_seconds)
        if self._failure_message is not None:
            raise RuntimeError(self._failure_message)

        structured_output = self._structured_output(request)
        output_text = None
        if structured_output is None:
            output_text = f"Fake response for role {request.role.value}"
        serialized_output = (
            json.dumps(structured_output, ensure_ascii=False, sort_keys=True)
            if structured_output is not None
            else output_text or ""
        )
        input_characters = sum(len(message.content) for message in request.messages)
        input_tokens = max(1, math.ceil(input_characters / 4))
        output_tokens = max(1, math.ceil(len(serialized_output) / 4))
        return ModelResponse(
            request_id=request.request_id,
            provider_id=self._descriptor.provider_id,
            model=self._descriptor.model,
            native_response_id=f"fake-{request.request_id}",
            output_text=output_text,
            structured_output=structured_output,
            finish_reason=ModelFinishReason.STOP,
            usage=ModelUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
            ),
            latency_ms=max(0, round((time.monotonic() - started) * 1_000)),
        )

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        response = await self.complete(request)
        yield ModelStreamEvent(
            request_id=request.request_id,
            provider_id=self._descriptor.provider_id,
            sequence=0,
            type=ModelStreamEventType.RESPONSE_STARTED,
        )
        text = response.output_text or json.dumps(
            response.structured_output,
            ensure_ascii=False,
            sort_keys=True,
        )
        sequence = 1
        for offset in range(0, len(text), 32):
            yield ModelStreamEvent(
                request_id=request.request_id,
                provider_id=self._descriptor.provider_id,
                sequence=sequence,
                type=ModelStreamEventType.OUTPUT_TEXT_DELTA,
                text_delta=text[offset : offset + 32],
            )
            sequence += 1
        yield ModelStreamEvent(
            request_id=request.request_id,
            provider_id=self._descriptor.provider_id,
            sequence=sequence,
            type=ModelStreamEventType.USAGE,
            usage=response.usage,
        )
        sequence += 1
        yield ModelStreamEvent(
            request_id=request.request_id,
            provider_id=self._descriptor.provider_id,
            sequence=sequence,
            type=ModelStreamEventType.RESPONSE_COMPLETED,
            response=response,
        )

    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider_id=self._descriptor.provider_id,
            status=(
                ProviderHealthStatus.DEGRADED
                if self._failure_message is not None
                else ProviderHealthStatus.READY
            ),
            latency_ms=0,
            detail=(
                "Fake failure injection is enabled"
                if self._failure_message is not None
                else "Deterministic offline provider"
            ),
        )

    @staticmethod
    def _structured_output(request: ModelRequest) -> dict[str, JsonValue] | None:
        if request.output_schema is None:
            return None
        if request.output_schema.name == TASK_CLASSIFICATION_SCHEMA:
            return TaskClassification(
                intent=TaskIntent.COMPUTER_INFO,
                complexity=TaskComplexity.SIMPLE,
                risk_level=ToolRiskLevel.R0,
                requires_planning=True,
                confidence=1.0,
                recommended_agent="computer",
                rationale="Fake Provider 固定选择安全的磁盘容量只读演示路径。",
            ).model_dump(mode="json")
        if request.output_schema.name == TURN_PLANNER_DECISION_SCHEMA:
            # The default offline provider must never infer that selecting the first
            # capability offer is safe. Positive planner paths use explicit fixtures.
            return TurnPlannerUnsupportedDecision(kind="unsupported").model_dump(mode="json")
        if request.output_schema.name == WORKSPACE_CODING_EXPLORATION_DECISION_SCHEMA:
            raw_files = request.metadata.get("workspace_exploration_files")
            snapshot_id = request.metadata.get("workspace_exploration_snapshot_id")
            snapshot_digest = request.metadata.get("workspace_exploration_snapshot_digest")
            if (
                not isinstance(raw_files, list)
                or len(raw_files) < 2
                or not isinstance(snapshot_id, str)
                or not isinstance(snapshot_digest, str)
            ):
                raise ValueError("Fake Explorer fixture requires an exact snapshot")
            selected = raw_files[:2]
            return WorkspaceCodingExplorationDecision(
                snapshot_id=snapshot_id,
                snapshot_digest=snapshot_digest,
                files=tuple(
                    WorkspaceCodingExplorationCandidateFile(
                        relative_path=str(item["relative_path"]),
                        source_file_proof_digest=str(item["proof_digest"]),
                        rationale=f"检查 {item['relative_path']}",
                    )
                    for item in selected
                    if isinstance(item, dict)
                ),
                decision_summary="选择两个与目标相关的候选实现文件。",
            ).model_dump(mode="json")
        if request.output_schema.name == WORKSPACE_CODING_CHANGE_DECISION_SCHEMA:
            raw_readers = request.metadata.get("workspace_change_readers")
            binding_id = request.metadata.get("workspace_change_file_set_binding_id")
            execution_id = request.metadata.get("workspace_change_reader_execution_id")
            execution_digest = request.metadata.get("workspace_change_reader_execution_digest")
            result_set_digest = request.metadata.get("workspace_change_reader_result_set_digest")
            ecosystem = request.metadata.get("workspace_change_ecosystem")
            if (
                not isinstance(raw_readers, list)
                or not 2 <= len(raw_readers) <= 8
                or not all(isinstance(item, dict) for item in raw_readers)
            ):
                raise ValueError("Fake Change Proposer requires exact Reader evidence")
            reader_items = cast(list[dict[str, JsonValue]], raw_readers)
            if (
                not all(
                    isinstance(item.get("content"), str) and item["content"]
                    for item in reader_items
                )
                or not all(
                    isinstance(value, str)
                    for value in (
                        binding_id,
                        execution_id,
                        execution_digest,
                        result_set_digest,
                    )
                )
            ):
                raise ValueError("Fake Change Proposer requires exact Reader evidence")
            comment = " # DeskPilot proposed change" if ecosystem == "python" else " // proposed"
            changes = []
            for item in reader_items:
                content = str(item["content"])
                old_text = next((line for line in content.splitlines() if line), content)
                changes.append(
                    WorkspaceCodingChange(
                        relative_path=str(item["relative_path"]),
                        old_text=old_text,
                        new_text=f"{old_text}{comment}",
                        source_result_ref_digest=str(item["result_ref_digest"]),
                        source_result_digest=str(item["result_digest"]),
                        source_version_digest=str(item["version_digest"]),
                        rationale=(
                            f"基于 {item['relative_path']} 的已验证 Reader 内容"
                            "提出精确替换。"
                        ),
                    )
                )
            return WorkspaceCodingChangeDecision(
                file_set_binding_id=cast(str, binding_id),
                reader_execution_id=cast(str, execution_id),
                reader_execution_digest=cast(str, execution_digest),
                reader_result_set_digest=cast(str, result_set_digest),
                changes=tuple(changes),
                decision_summary="为确认文件集逐一形成无写权限精确替换提案。",
            ).model_dump(mode="json")
        if request.output_schema.name == TASK_PLAN_SCHEMA:
            return TaskPlan(
                summary="Fake Provider 生成的真实只读工具验证计划",
                steps=(
                    PlanStep(
                        step_id="s1",
                        agent="supervisor",
                        title="确认任务目标",
                    ),
                    PlanStep(
                        step_id="s2",
                        agent="computer",
                        title="读取磁盘容量元数据",
                        tool_name="computer.disk_usage",
                        tool_version="1.0.0",
                        depends_on=("s1",),
                    ),
                    PlanStep(
                        step_id="s3",
                        agent="verifier",
                        title="验证结果",
                        depends_on=("s2",),
                    ),
                ),
            ).model_dump(mode="json")
        if request.output_schema.name == RESEARCH_AGENT_DECISION_SCHEMA:
            raw_ids = request.metadata.get("page_snapshot_ids", [])
            snapshot_ids = tuple(str(item) for item in raw_ids) if isinstance(raw_ids, list) else ()
            if not snapshot_ids:
                raise ValueError("Fake research fixture requires Page Snapshot IDs")
            return ResearchAgentDecision(
                claims=(
                    ResearchClaimProposal(
                        statement="受控页面快照包含与研究目标直接相关的公开信息。",
                        page_snapshot_ids=snapshot_ids[:2],
                    ),
                ),
            ).model_dump(mode="json")
        if request.output_schema.name == RESEARCH_AGENT_LOOP_DECISION_SCHEMA:
            phase = request.metadata.get("agent_loop_phase")
            if phase == "request_route":
                binding_id = request.metadata.get("route_binding_id")
                query = request.metadata.get("research_query")
                if not isinstance(binding_id, str) or not isinstance(query, str):
                    raise ValueError("Fake research loop fixture requires a bound Route")
                return cast(
                    dict[str, JsonValue],
                    ResearchLoopDecision(
                        root=ResearchRouteRequestDecision(
                            route_binding_id=binding_id,
                            query=query,
                            decision_summary="请求受控公开来源取证。",
                        )
                    ).model_dump(mode="json"),
                )
            raw_ids = request.metadata.get("page_snapshot_ids", [])
            snapshot_ids = tuple(str(item) for item in raw_ids) if isinstance(raw_ids, list) else ()
            if phase != "submit_result" or not snapshot_ids:
                raise ValueError("Fake research loop fixture requires Route observations")
            return cast(
                dict[str, JsonValue],
                ResearchLoopDecision(
                    root=ResearchSubmitResultDecision(
                        claims=(
                            ResearchClaimProposal(
                                statement="受控页面快照包含与研究目标直接相关的公开信息。",
                                page_snapshot_ids=snapshot_ids[:2],
                            ),
                        ),
                        decision_summary="基于受控快照提交待验证候选事实。",
                    )
                ).model_dump(mode="json"),
            )
        if request.output_schema.name == WORKSPACE_AGENT_LOOP_DECISION_SCHEMA:
            phase = request.metadata.get("agent_loop_phase")
            binding_id = request.metadata.get("route_binding_id")
            path = request.metadata.get("workspace_path")
            test_path = request.metadata.get("workspace_test_path")
            read_kind = request.metadata.get("workspace_read_kind")
            if phase == "request_route":
                if (
                    not isinstance(binding_id, str)
                    or not isinstance(path, str)
                    or read_kind not in {"file", "directory", "python_test", "node_test"}
                    or (
                        read_kind in {"python_test", "node_test"} and not isinstance(test_path, str)
                    )
                ):
                    raise ValueError("Fake workspace loop fixture requires a bound Route")
                if not path:
                    if read_kind != "file":
                        raise ValueError("Fake directory loop fixture requires an exact path")
                    decision: AgentNeedsUserInputDecision | WorkspaceRouteRequestDecision = (
                        AgentNeedsUserInputDecision(
                            question_code="WORKSPACE_FILE_PATH_REQUIRED",
                            question="请告诉我要读取的工作区相对文件路径。",
                            blocking_fields=("path",),
                            answer_schema="workspace_relative_file_path.v1",
                            insufficient_context="当前任务没有提供文件路径。",
                            pending_actions=("读取并复核一个工作区文本文件",),
                            decision_summary="缺少只读 Route 的必需路径。",
                        )
                    )
                else:
                    decision = WorkspaceRouteRequestDecision(
                        route_binding_id=binding_id,
                        path=path,
                        test_path=(test_path if isinstance(test_path, str) else None),
                        decision_summary=(
                            "请求读取受控工作区文本文件。"
                            if read_kind == "file"
                            else (
                                "请求列出受控工作区直接子目录项。"
                                if read_kind == "directory"
                                else "请求运行服务器绑定的固定测试文件。"
                            )
                        ),
                    )
                return cast(
                    dict[str, JsonValue],
                    WorkspaceLoopDecision(root=decision).model_dump(mode="json"),
                )
            observation_digest = request.metadata.get("observation_digest")
            if phase != "submit_result" or not isinstance(observation_digest, str):
                raise ValueError("Fake workspace loop fixture requires a Route observation")
            return cast(
                dict[str, JsonValue],
                WorkspaceLoopDecision(
                    root=WorkspaceSubmitResultDecision(
                        observation_digest=observation_digest,
                        decision_summary=(
                            "提交绑定文件版本证明的只读结果。"
                            if read_kind == "file"
                            else (
                                "提交绑定目录观察证明的只读结果。"
                                if read_kind == "directory"
                                else "提交绑定快照、运行时与隔离证明的固定测试结果。"
                            )
                        ),
                    )
                ).model_dump(mode="json"),
            )
        if request.output_schema.name == WORKSPACE_PATCH_PLANNER_LOOP_DECISION_SCHEMA:
            phase = request.metadata.get("agent_loop_phase")
            route_binding_id = request.metadata.get("route_binding_id")
            patch_binding_id = request.metadata.get("workspace_patch_binding_id")
            path = request.metadata.get("workspace_path")
            if phase == "request_route":
                if not isinstance(route_binding_id, str) or not isinstance(path, str) or not path:
                    raise ValueError("Fake patch planner fixture requires a bound file Route")
                return cast(
                    dict[str, JsonValue],
                    WorkspacePatchLoopDecision(
                        root=WorkspaceRouteRequestDecision(
                            route_binding_id=route_binding_id,
                            path=path,
                            decision_summary="请求读取服务器绑定的单个补丁目标文件。",
                        )
                    ).model_dump(mode="json"),
                )
            observation_digest = request.metadata.get("observation_digest")
            source_text = request.metadata.get("workspace_patch_source_text")
            expected_change = request.metadata.get("workspace_patch_expected_change")
            if (
                phase != "propose_patch"
                or not isinstance(patch_binding_id, str)
                or not isinstance(observation_digest, str)
                or not isinstance(path, str)
                or not isinstance(source_text, str)
            ):
                raise ValueError("Fake patch planner fixture requires a bound observation")
            if expected_change is not None:
                if (
                    not isinstance(expected_change, dict)
                    or expected_change.get("path") != path
                    or not isinstance(expected_change.get("old_text"), str)
                    or not isinstance(expected_change.get("new_text"), str)
                    or source_text.count(cast(str, expected_change["old_text"])) != 1
                ):
                    raise ValueError(
                        "Fake patch planner fixture requires one exact confirmed change"
                    )
                old_text = cast(str, expected_change["old_text"])
                new_text = cast(str, expected_change["new_text"])
            else:
                old_text = next((line for line in source_text.splitlines() if line), "")
                if not old_text or len(old_text) > 4_096:
                    raise ValueError(
                        "Fake patch planner fixture requires one bounded non-empty line"
                    )
                suffix = (
                    "  # DeskPilot proposal"
                    if path.endswith(".py")
                    else "  // DeskPilot proposal"
                )
                new_text = f"{old_text}{suffix}"
            return cast(
                dict[str, JsonValue],
                WorkspacePatchLoopDecision(
                    root=WorkspacePatchSubmitProposalDecision(
                        patch_binding_id=patch_binding_id,
                        observation_digest=observation_digest,
                        changes=(
                            WorkspacePatchChangeProposal(
                                path=path,
                                old_text=old_text,
                                new_text=new_text,
                                rationale="生成一个等待用户确认的单点、精确替换候选补丁。",
                            ),
                        ),
                        decision_summary="提交无写权限的精确补丁建议。",
                    )
                ).model_dump(mode="json"),
            )
        if request.output_schema.name == WORKSPACE_COORDINATOR_LOOP_DECISION_SCHEMA:
            phase = request.metadata.get("agent_loop_phase")
            if phase == "propose_handoff":
                binding_id = request.metadata.get("handoff_binding_id")
                capability_id = request.metadata.get("target_capability_id")
                objective_ref = request.metadata.get("child_objective_ref")
                context_refs = request.metadata.get("handoff_context_refs")
                budget_slice = request.metadata.get("child_budget_slice")
                if (
                    not isinstance(binding_id, str)
                    or capability_id != "workspace.directory.read.v1"
                    or not isinstance(objective_ref, str)
                    or not isinstance(context_refs, list)
                    or not isinstance(budget_slice, dict)
                ):
                    raise ValueError("Fake coordinator fixture requires a bound child slot")
                return cast(
                    dict[str, JsonValue],
                    CoordinatorLoopDecision(
                        root=AgentProposeHandoffDecision(
                            handoff_binding_id=binding_id,
                            target_capability_id="workspace.directory.read.v1",
                            objective_ref=objective_ref,
                            context_refs=tuple(str(item) for item in context_refs),
                            budget_slice=PlanNodeBudget.model_validate(budget_slice),
                            decision_summary="提议激活服务器预编译的只读目录 Reader 子任务。",
                        )
                    ).model_dump(mode="json"),
                )
            observation_digest = request.metadata.get("child_observation_digest")
            if phase != "submit_result" or not isinstance(observation_digest, str):
                raise ValueError("Fake coordinator fixture requires a verified child result")
            return cast(
                dict[str, JsonValue],
                CoordinatorLoopDecision(
                    root=CoordinatorSubmitResultDecision(
                        child_observation_digest=observation_digest,
                        decision_summary="只基于已验证的子 Agent 结果提交父任务结果。",
                    )
                ).model_dump(mode="json"),
            )
        if request.output_schema.name == WORKSPACE_BOUNDED_CODING_COORDINATOR_DECISION_SCHEMA:
            capabilities = request.metadata.get("task_graph_allowed_capabilities")
            max_nodes = request.metadata.get("task_graph_max_nodes")
            if (
                not isinstance(capabilities, list)
                or not capabilities
                or not all(isinstance(item, dict) and "local_key" in item for item in capabilities)
                or not isinstance(max_nodes, int)
                or len(capabilities) != max_nodes
            ):
                raise ValueError("Fake bounded coding fixture requires an exact server graph")
            return cast(
                dict[str, JsonValue],
                WorkspaceBoundedCodingCoordinatorDecision(
                    root=WorkspaceBoundedCodingGraphDecision(
                        nodes=tuple(
                            WorkspaceBoundedCodingGraphNodeProposal.model_validate(item)
                            for item in capabilities
                        ),
                        output_node_key=str(
                            cast(dict[str, JsonValue], capabilities[-1])["local_key"]
                        ),
                        decision_summary=("确认服务器封存的多文件固定编码图，不扩展任何执行权限。"),
                    )
                ).model_dump(mode="json"),
            )
        if request.output_schema.name == WORKSPACE_DYNAMIC_COORDINATOR_LOOP_DECISION_SCHEMA:
            phase = request.metadata.get("agent_loop_phase")
            if phase == "propose_task_graph":
                capabilities = request.metadata.get("task_graph_allowed_capabilities")
                context_refs = request.metadata.get("task_graph_context_refs")
                max_nodes = request.metadata.get("task_graph_max_nodes")
                if (
                    isinstance(capabilities, list)
                    and capabilities
                    and all(isinstance(item, dict) and "local_key" in item for item in capabilities)
                    and isinstance(max_nodes, int)
                    and len(capabilities) == max_nodes
                ):
                    return cast(
                        dict[str, JsonValue],
                        DynamicCoordinatorLoopDecision(
                            root=AgentProposeTaskGraphDecision(
                                nodes=tuple(
                                    AgentTaskGraphNodeProposal.model_validate(item)
                                    for item in capabilities
                                ),
                                output_node_key=str(
                                    cast(dict[str, JsonValue], capabilities[-1])["local_key"]
                                ),
                                decision_summary=(
                                    "确认服务器封存的固定编码图，不扩展任何执行权限。"
                                ),
                            )
                        ).model_dump(mode="json"),
                    )
                if (
                    not isinstance(capabilities, list)
                    or not capabilities
                    or not isinstance(capabilities[0], dict)
                    or not isinstance(capabilities[0].get("capability_id"), str)
                    or not isinstance(capabilities[0].get("budget"), dict)
                    or not isinstance(capabilities[0].get("input_sources"), list)
                    or not capabilities[0]["input_sources"]
                    or not isinstance(context_refs, list)
                    or not context_refs
                    or not isinstance(max_nodes, int)
                    or max_nodes < 1
                ):
                    raise ValueError(
                        "Fake dynamic coordinator fixture requires a server graph offer"
                    )
                offered = {
                    str(item["capability_id"]): item
                    for item in capabilities
                    if isinstance(item, dict)
                    and isinstance(item.get("capability_id"), str)
                    and isinstance(item.get("budget"), dict)
                    and isinstance(item.get("input_sources"), list)
                    and item["input_sources"]
                }
                patch_capability = offered.get("workspace.patch.propose.v1")
                directory_capability = offered.get("workspace.directory.read.v1")
                if patch_capability is not None:
                    raw_input_bindings = patch_capability.get("input_bindings")
                    input_bindings = (
                        raw_input_bindings if isinstance(raw_input_bindings, list) else []
                    )
                    if (
                        directory_capability is None
                        or (
                            raw_input_bindings is not None
                            and not isinstance(raw_input_bindings, list)
                        )
                        or len(input_bindings) > 2
                        or any(
                            not isinstance(item, dict)
                            or not isinstance(item.get("binding_key"), str)
                            for item in input_bindings
                        )
                        or max_nodes < max(1, len(input_bindings)) + 2
                    ):
                        raise ValueError(
                            "Fake dynamic Patch fixture requires bounded approval bindings"
                        )
                    binding_keys: list[str | None] = (
                        [
                            str(item["binding_key"])
                            for item in input_bindings
                            if isinstance(item, dict)
                        ]
                        if input_bindings
                        else [None]
                    )
                    directory_source = cast(list[JsonValue], directory_capability["input_sources"])[
                        0
                    ]
                    patch_source = cast(list[JsonValue], patch_capability["input_sources"])[0]
                    shared_context = (str(context_refs[0]),)
                    graph_nodes: list[AgentTaskGraphNodeProposal] = [
                        AgentTaskGraphNodeProposal(
                            local_key="directory_context",
                            target_capability_id="workspace.directory.read.v1",
                            objective="读取修复前的受限目录上下文。",
                            context_refs=shared_context,
                            input_source=cast(
                                Literal[
                                    "route_directory_path",
                                    "route_explicit_file_path",
                                    "route_python_test_spec",
                                    "route_node_test_spec",
                                    "route_patch_test_spec",
                                ],
                                directory_source,
                            ),
                            budget_slice=PlanNodeBudget.model_validate(
                                directory_capability["budget"]
                            ),
                        )
                    ]
                    previous_key = "directory_context"
                    for index, binding_key in enumerate(binding_keys, start=1):
                        local_key = (
                            "patch_approval"
                            if len(binding_keys) == 1
                            else f"patch_approval_{index}"
                        )
                        depends_on = (previous_key,)
                        conditions = (
                            (AgentTaskGraphConditionProposal(source_local_key=previous_key),)
                            if previous_key.startswith("patch_approval")
                            else ()
                        )
                        graph_nodes.append(
                            AgentTaskGraphNodeProposal(
                                local_key=local_key,
                                target_capability_id="workspace.patch.propose.v1",
                                objective=(
                                    "消费一个服务器签发的节点绑定，提出补丁并独立等待确认。"
                                ),
                                context_refs=shared_context,
                                input_source=cast(
                                    Literal[
                                        "route_directory_path",
                                        "route_explicit_file_path",
                                        "route_python_test_spec",
                                        "route_node_test_spec",
                                        "route_patch_test_spec",
                                    ],
                                    patch_source,
                                ),
                                input_binding_key=binding_key,
                                depends_on=depends_on,
                                conditions=conditions,
                                budget_slice=PlanNodeBudget.model_validate(
                                    patch_capability["budget"]
                                ),
                            )
                        )
                        previous_key = local_key
                    graph_nodes.append(
                        AgentTaskGraphNodeProposal(
                            local_key="directory_output",
                            target_capability_id="workspace.directory.read.v1",
                            objective="在所有补丁均通过固定测试后重新读取目录。",
                            context_refs=shared_context,
                            input_source=cast(
                                Literal[
                                    "route_directory_path",
                                    "route_explicit_file_path",
                                    "route_python_test_spec",
                                    "route_node_test_spec",
                                    "route_patch_test_spec",
                                ],
                                directory_source,
                            ),
                            depends_on=(previous_key,),
                            conditions=(
                                AgentTaskGraphConditionProposal(source_local_key=previous_key),
                            ),
                            budget_slice=PlanNodeBudget.model_validate(
                                directory_capability["budget"]
                            ),
                        )
                    )
                    return cast(
                        dict[str, JsonValue],
                        DynamicCoordinatorLoopDecision(
                            root=AgentProposeTaskGraphDecision(
                                nodes=tuple(graph_nodes),
                                output_node_key="directory_output",
                                decision_summary=(
                                    "提出可组合节点级 Patch/Approval 与验证后目录输出的受控 DAG。"
                                ),
                            )
                        ).model_dump(mode="json"),
                    )
                capability = capabilities[0]
                return cast(
                    dict[str, JsonValue],
                    DynamicCoordinatorLoopDecision(
                        root=AgentProposeTaskGraphDecision(
                            nodes=(
                                AgentTaskGraphNodeProposal(
                                    local_key="directory_reader",
                                    target_capability_id=str(capability["capability_id"]),
                                    objective="列出并复核工作区目录快照。",
                                    context_refs=(str(context_refs[0]),),
                                    input_source=cast(
                                        Literal[
                                            "route_directory_path",
                                            "route_explicit_file_path",
                                            "route_python_test_spec",
                                            "route_node_test_spec",
                                            "route_patch_test_spec",
                                        ],
                                        cast(list[JsonValue], capability["input_sources"])[0],
                                    ),
                                    budget_slice=PlanNodeBudget.model_validate(
                                        capability["budget"]
                                    ),
                                ),
                            ),
                            output_node_key="directory_reader",
                            decision_summary=("提出一个服务器裁决、预算守恒的只读目录子任务图。"),
                        )
                    ).model_dump(mode="json"),
                )
            observation_digest = request.metadata.get("task_graph_observation_digest")
            if phase != "submit_result" or not isinstance(observation_digest, str):
                raise ValueError("Fake dynamic coordinator fixture requires a verified graph join")
            return cast(
                dict[str, JsonValue],
                DynamicCoordinatorLoopDecision(
                    root=DynamicCoordinatorSubmitResultDecision(
                        task_graph_observation_digest=observation_digest,
                        decision_summary=("只基于服务器验证后的完整子任务图 join 提交父任务结果。"),
                    )
                ).model_dump(mode="json"),
            )
        if request.output_schema.name == CITATION_VERIFICATION_DECISION_SCHEMA:
            raw_ids = request.metadata.get("claim_ids", [])
            claim_ids = tuple(str(item) for item in raw_ids) if isinstance(raw_ids, list) else ()
            if not claim_ids:
                raise ValueError("Fake citation verifier requires Claim IDs")
            return CitationVerificationDecision(
                judgments=tuple(
                    CitationJudgment(
                        claim_id=claim_id,
                        supported=True,
                        reason_code="SUPPORTED",
                    )
                    for claim_id in claim_ids
                )
            ).model_dump(mode="json")
        raise ValueError(f"Fake provider has no fixture for Schema {request.output_schema.name}")
