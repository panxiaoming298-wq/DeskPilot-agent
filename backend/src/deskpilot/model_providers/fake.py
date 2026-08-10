"""Deterministic local provider for development, tests, and offline demos."""

import asyncio
import json
import math
import time
from collections.abc import AsyncIterator

from pydantic import JsonValue

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
from deskpilot.domain.tool_contracts import ToolRiskLevel

TASK_CLASSIFICATION_SCHEMA = "task_classification"
TASK_PLAN_SCHEMA = "task_plan"


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
        raise ValueError(
            f"Fake provider has no fixture for Schema {request.output_schema.name}"
        )
