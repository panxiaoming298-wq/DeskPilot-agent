"""Exact availability manifest for Agent adapters used by generic Task Loops."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_contracts import DIGEST_PATTERN, BoundAgentRef
from deskpilot.domain.task_plans import CapabilityRef


class TaskLoopAgentAdapterError(LookupError):
    code = "TASK_LOOP_AGENT_ADAPTER_NOT_ELIGIBLE"


class TaskLoopAgentAdapterManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.task-loop-agent-adapter.v1"] = (
        "deskpilot.task-loop-agent-adapter.v1"
    )
    adapter_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    route_id: Literal[
        "research_to_html",
        "workspace_file_read",
        "workspace_coding_loop",
    ]
    source_local_key: str = Field(min_length=1, max_length=64)
    agent_id: str = Field(min_length=1, max_length=128)
    agent_versions: tuple[str, ...] = Field(min_length=1, max_length=8)
    capability_id: str = Field(min_length=1, max_length=128)
    parameter_name: Literal["goal", "path", "primary_path", "secondary_path"]
    runtime_enabled: Literal[True] = True
    manifest_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def digest_matches(self) -> Self:
        if (
            len(self.agent_versions) != len(set(self.agent_versions))
            or tuple(sorted(self.agent_versions)) != self.agent_versions
        ):
            raise ValueError("Task Loop Agent adapter versions must be canonical")
        material = self.model_dump(mode="json", exclude={"manifest_digest"})
        if self.manifest_digest != sha256_digest(material):
            raise ValueError("Task Loop Agent adapter digest does not match")
        return self

    @classmethod
    def build(cls, **values: object) -> Self:
        material = {
            "schema_version": "deskpilot.task-loop-agent-adapter.v1",
            "runtime_enabled": True,
            **values,
        }
        return cls.model_validate(
            {**material, "manifest_digest": sha256_digest(material)}
        )


class TaskLoopAgentAdapterRegistry:
    def __init__(
        self,
        manifests: tuple[TaskLoopAgentAdapterManifest, ...] = (),
    ) -> None:
        self._by_route_node: dict[tuple[str, str], TaskLoopAgentAdapterManifest] = {
            (item.route_id, item.source_local_key): item for item in manifests
        }
        if len(self._by_route_node) != len(manifests):
            raise ValueError("Task Loop Agent adapter Route/node pairs must be unique")

    def resolve(
        self,
        *,
        route_id: str,
        source_local_key: str,
        bound_agent: BoundAgentRef,
        capability: CapabilityRef,
    ) -> TaskLoopAgentAdapterManifest:
        manifest = self._by_route_node.get((route_id, source_local_key))
        if (
            manifest is None
            or not manifest.runtime_enabled
            or manifest.source_local_key != source_local_key
            or manifest.agent_id != bound_agent.agent_id
            or bound_agent.version not in manifest.agent_versions
            or manifest.capability_id != capability.capability_id
        ):
            raise TaskLoopAgentAdapterError(
                "Exact Route, Agent, Capability, or local runtime adapter is unavailable"
            )
        return manifest

    def manifests(self) -> tuple[TaskLoopAgentAdapterManifest, ...]:
        return tuple(
            self._by_route_node[key]
            for key in sorted(self._by_route_node)
        )

    @property
    def snapshot_digest(self) -> str:
        return sha256_digest(
            {"manifests": [item.model_dump(mode="json") for item in self.manifests()]}
        )


def create_task_loop_agent_adapter_registry(
    *,
    research_available: bool,
    workspace_file_available: bool,
    workspace_coding_loop_available: bool = False,
) -> TaskLoopAgentAdapterRegistry:
    manifests: list[TaskLoopAgentAdapterManifest] = []
    if research_available:
        manifests.append(
            TaskLoopAgentAdapterManifest.build(
                adapter_id="builtin.task-loop.research.v1",
                route_id="research_to_html",
                source_local_key="research",
                agent_id="builtin.web_researcher",
                agent_versions=("1.1.0",),
                capability_id="research.read.v1",
                parameter_name="goal",
            )
        )
    if workspace_file_available:
        manifests.append(
            TaskLoopAgentAdapterManifest.build(
                adapter_id="builtin.task-loop.workspace-file.v1",
                route_id="workspace_file_read",
                source_local_key="workspace_file_read",
                agent_id="builtin.workspace_reader",
                agent_versions=("1.0.0", "1.1.0", "1.2.0"),
                capability_id="workspace.file.read.v1",
                parameter_name="path",
            )
        )
    if workspace_coding_loop_available:
        manifests.extend(
            (
                TaskLoopAgentAdapterManifest.build(
                    adapter_id="builtin.task-loop.workspace-coding-primary.v1",
                    route_id="workspace_coding_loop",
                    source_local_key="inspect_primary",
                    agent_id="builtin.workspace_reader",
                    agent_versions=("1.0.0", "1.1.0", "1.2.0"),
                    capability_id="workspace.file.read.v1",
                    parameter_name="primary_path",
                ),
                TaskLoopAgentAdapterManifest.build(
                    adapter_id="builtin.task-loop.workspace-coding-secondary.v1",
                    route_id="workspace_coding_loop",
                    source_local_key="inspect_secondary",
                    agent_id="builtin.workspace_reader",
                    agent_versions=("1.0.0", "1.1.0", "1.2.0"),
                    capability_id="workspace.file.read.v1",
                    parameter_name="secondary_path",
                ),
            )
        )
    return TaskLoopAgentAdapterRegistry(tuple(manifests))


__all__ = [
    "TaskLoopAgentAdapterError",
    "TaskLoopAgentAdapterManifest",
    "TaskLoopAgentAdapterRegistry",
    "create_task_loop_agent_adapter_registry",
]
