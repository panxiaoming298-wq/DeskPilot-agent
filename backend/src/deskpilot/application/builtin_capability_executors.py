"""Trusted read-only adapters for the first generic Capability Registry."""

from __future__ import annotations

import asyncio
from typing import Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deskpilot.application.capability_catalog import CapabilityCatalog
from deskpilot.application.capability_executor_registry import (
    CapabilityExecutor,
    CapabilityExecutorRegistry,
)
from deskpilot.application.capability_input_binding_catalog import (
    ArtifactHtmlExecutorInput,
    BrowserVerifyExecutorInput,
    KnowledgeLocalExecutorInput,
    McpTextMetricsExecutorInput,
    WorkspaceNodeTestExecutorInput,
    WorkspacePythonTestExecutorInput,
    WorkspaceSnapshotCheckExecutorInput,
)
from deskpilot.application.mcp_control_plane import SERVER_ID
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_contracts import DIGEST_PATTERN
from deskpilot.domain.artifact_runtime import BrowserRenderRunRead, TaskWorkspaceRead
from deskpilot.domain.capability_execution import (
    CapabilityApprovalRequirement,
    CapabilityEffectClass,
    CapabilityExecutionContext,
    CapabilityExecutorManifest,
    CapabilityRecoveryPolicy,
    CapabilityResultKind,
    VerifiedCapabilityResultRef,
)
from deskpilot.domain.knowledge import KnowledgeSearchRead
from deskpilot.domain.mcp import McpAuditPage, McpTextMetricsOutput, McpToolCallRead
from deskpilot.domain.task_plans import CapabilityPack, CapabilityRef, DraftNodeKind
from deskpilot.domain.workspace_files import (
    WorkspaceCheckRead,
    WorkspaceNodeTestRead,
    WorkspaceNodeTestSnapshot,
    WorkspacePythonTestRead,
    WorkspacePythonTestSnapshot,
)
from deskpilot.mcp_servers.readonly_text_server import TOOL_NAME
from deskpilot.tools.workspace_checks import WorkspaceCheckInput


class BuiltinCapabilityExecutionError(RuntimeError):
    code = "BUILTIN_CAPABILITY_EXECUTION_REJECTED"


class BuiltinCapabilityRuntimeUnavailableError(BuiltinCapabilityExecutionError):
    code = "BUILTIN_CAPABILITY_RUNTIME_UNAVAILABLE"


class BuiltinCapabilityCandidateRejectedError(BuiltinCapabilityExecutionError):
    code = "BUILTIN_CAPABILITY_CANDIDATE_REJECTED"


class CapabilityAdapterVerification(BaseModel):
    """Adapter proof only; the execution engine separately seals verification."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.capability-adapter-verification.v1"] = (
        "deskpilot.capability-adapter-verification.v1"
    )
    verifier_id: str
    context_digest: str = Field(pattern=DIGEST_PATTERN)
    capability: CapabilityRef
    result_kind: CapabilityResultKind
    result_schema_digest: str = Field(pattern=DIGEST_PATTERN)
    result_digest: str = Field(pattern=DIGEST_PATTERN)
    evidence_digest: str = Field(pattern=DIGEST_PATTERN)
    verification_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def digest_matches(self) -> CapabilityAdapterVerification:
        material = self.model_dump(mode="json", exclude={"verification_digest"})
        if self.verification_digest != sha256_digest(material):
            raise ValueError("Capability adapter verification digest does not match")
        return self

    @classmethod
    def build(
        cls,
        *,
        verifier_id: str,
        context: CapabilityExecutionContext,
        result_kind: CapabilityResultKind,
        candidate: BaseModel,
        evidence_digest: str,
    ) -> CapabilityAdapterVerification:
        result_digest = candidate.model_dump(mode="json").get("result_digest")
        if not isinstance(result_digest, str):
            raise BuiltinCapabilityCandidateRejectedError(
                "Capability candidate omitted its result digest"
            )
        values = {
            "schema_version": "deskpilot.capability-adapter-verification.v1",
            "verifier_id": verifier_id,
            "context_digest": context.context_digest,
            "capability": context.capability,
            "result_kind": result_kind,
            "result_schema_digest": sha256_digest(type(candidate).model_json_schema()),
            "result_digest": result_digest,
            "evidence_digest": evidence_digest,
        }
        return cls.model_validate({**values, "verification_digest": sha256_digest(values)})


class KnowledgeSearchPort(Protocol):
    async def search(self, query: str, limit: int) -> KnowledgeSearchRead: ...


class McpTextMetricsPort(Protocol):
    async def invoke(self, tool_name: str, arguments: dict[str, object]) -> McpToolCallRead: ...

    async def list_audit(self, after_sequence: int, limit: int) -> McpAuditPage: ...


class WorkspaceSnapshotPort(Protocol):
    def prepare_check(self, profile: str, relative_path: str) -> WorkspaceCheckInput: ...

    def prepare_python_test(
        self, project_path: str, test_path: str
    ) -> WorkspacePythonTestSnapshot: ...

    def prepare_node_test(self, project_path: str, test_path: str) -> WorkspaceNodeTestSnapshot: ...


class WorkspaceCheckPort(Protocol):
    @property
    def enabled(self) -> bool: ...

    def run(self, snapshot: WorkspaceCheckInput) -> WorkspaceCheckRead: ...


class WorkspacePythonTestPort(Protocol):
    @property
    def enabled(self) -> bool: ...

    def run(self, snapshot: WorkspacePythonTestSnapshot) -> WorkspacePythonTestRead: ...


class WorkspaceNodeTestPort(Protocol):
    @property
    def enabled(self) -> bool: ...

    def run(self, snapshot: WorkspaceNodeTestSnapshot) -> WorkspaceNodeTestRead: ...


class ArtifactDeliveryPort(Protocol):
    async def build_html_node(
        self,
        run_id: str,
        *,
        node_id: str,
        local_key: str | None = None,
        defer_task_loop_edge: bool = False,
    ) -> TaskWorkspaceRead: ...

    async def verify_browser_node(
        self,
        run_id: str,
        *,
        node_id: str,
        local_key: str | None = None,
        defer_task_loop_edge: bool = False,
    ) -> BrowserRenderRunRead: ...


class ArtifactHtmlCapabilityOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.artifact-html-capability-output.v1"] = (
        "deskpilot.artifact-html-capability-output.v1"
    )
    source_result_digest: str = Field(pattern=DIGEST_PATTERN)
    workspace: TaskWorkspaceRead
    result_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def digest_matches(self) -> ArtifactHtmlCapabilityOutput:
        material = self.model_dump(mode="json", exclude={"result_digest"})
        if self.result_digest != sha256_digest(material):
            raise ValueError("Artifact capability output digest does not match")
        return self


class BrowserVerifyCapabilityOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.browser-verify-capability-output.v1"] = (
        "deskpilot.browser-verify-capability-output.v1"
    )
    source_result_digest: str = Field(pattern=DIGEST_PATTERN)
    browser: BrowserRenderRunRead
    result_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def digest_matches(self) -> BrowserVerifyCapabilityOutput:
        material = self.model_dump(mode="json", exclude={"result_digest"})
        if self.result_digest != sha256_digest(material):
            raise ValueError("Browser capability output digest does not match")
        return self


class _BuiltinExecutor:
    def __init__(self, capability: CapabilityRef, result_kind: CapabilityResultKind) -> None:
        self._capability = capability
        self._result_kind = result_kind

    def _require_context(self, context: CapabilityExecutionContext) -> None:
        if (
            context.capability != self._capability
            or context.node_kind is not DraftNodeKind.CAPABILITY
        ):
            raise BuiltinCapabilityExecutionError(
                "Builtin executor context changed after exact registry resolution"
            )

    def _verification(
        self,
        *,
        context: CapabilityExecutionContext,
        candidate: BaseModel,
        verifier_id: str,
        evidence: dict[str, object],
    ) -> CapabilityAdapterVerification:
        self._require_context(context)
        return CapabilityAdapterVerification.build(
            verifier_id=verifier_id,
            context=context,
            result_kind=self._result_kind,
            candidate=candidate,
            evidence_digest=sha256_digest(evidence),
        )


class ArtifactHtmlExecutor(_BuiltinExecutor):
    def __init__(
        self,
        capability: CapabilityRef,
        artifacts: ArtifactDeliveryPort,
    ) -> None:
        super().__init__(capability, CapabilityResultKind.ARTIFACT)
        self._artifacts = artifacts

    async def execute(
        self,
        context: CapabilityExecutionContext,
        arguments: BaseModel,
    ) -> BaseModel:
        self._require_context(context)
        if not isinstance(arguments, ArtifactHtmlExecutorInput):
            raise BuiltinCapabilityExecutionError("Artifact executor input type changed")
        source = self._source_ref(context, CapabilityResultKind.VERIFIED_CLAIMS)
        if arguments.verified_claims_digest != source.result_digest:
            raise BuiltinCapabilityExecutionError(
                "Artifact executor input crossed its verified Claims ResultRef"
            )
        workspace = await self._artifacts.build_html_node(
            context.run_id,
            node_id=context.node_id,
            defer_task_loop_edge=True,
        )
        values = {
            "schema_version": "deskpilot.artifact-html-capability-output.v1",
            "source_result_digest": source.result_digest,
            "workspace": workspace.model_dump(mode="json"),
        }
        return ArtifactHtmlCapabilityOutput.model_validate(
            {**values, "result_digest": sha256_digest(values)}
        )

    async def verify(
        self,
        context: CapabilityExecutionContext,
        candidate: BaseModel,
    ) -> BaseModel:
        if not isinstance(candidate, ArtifactHtmlCapabilityOutput):
            raise BuiltinCapabilityCandidateRejectedError(
                "Artifact candidate type changed"
            )
        source = self._source_ref(context, CapabilityResultKind.VERIFIED_CLAIMS)
        if (
            candidate.source_result_digest != source.result_digest
            or candidate.workspace.task_id != context.task_id
            or candidate.workspace.run_id != context.run_id
            or candidate.workspace.status != "active"
            or not candidate.workspace.artifacts
        ):
            raise BuiltinCapabilityCandidateRejectedError(
                "Artifact candidate lineage or workspace proof changed"
            )
        return self._verification(
            context=context,
            candidate=candidate,
            verifier_id="builtin.artifact.html.verifier.v1",
            evidence={
                "source_result_ref_digest": source.result_ref_digest,
                "workspace_id": candidate.workspace.workspace_id,
                "artifact_revision_digests": [
                    item.active_revision.content_digest
                    for item in candidate.workspace.artifacts
                ],
                "result_digest": candidate.result_digest,
            },
        )

    @staticmethod
    def _source_ref(
        context: CapabilityExecutionContext,
        result_kind: CapabilityResultKind,
    ) -> VerifiedCapabilityResultRef:
        matches = tuple(
            item
            for item in context.consumed_result_refs
            if item.result_kind is result_kind
        )
        if len(matches) != 1:
            raise BuiltinCapabilityExecutionError(
                "Artifact executor has no unique semantic ResultRef"
            )
        return matches[0]


class BrowserVerifyExecutor(_BuiltinExecutor):
    def __init__(
        self,
        capability: CapabilityRef,
        artifacts: ArtifactDeliveryPort,
    ) -> None:
        super().__init__(capability, CapabilityResultKind.BROWSER_VERIFICATION)
        self._artifacts = artifacts

    async def execute(
        self,
        context: CapabilityExecutionContext,
        arguments: BaseModel,
    ) -> BaseModel:
        self._require_context(context)
        if not isinstance(arguments, BrowserVerifyExecutorInput):
            raise BuiltinCapabilityExecutionError("Browser executor input type changed")
        source = self._source_ref(context)
        if arguments.artifact_digest != source.result_digest:
            raise BuiltinCapabilityExecutionError(
                "Browser executor input crossed its verified Artifact ResultRef"
            )
        browser = await self._artifacts.verify_browser_node(
            context.run_id,
            node_id=context.node_id,
            defer_task_loop_edge=True,
        )
        values = {
            "schema_version": "deskpilot.browser-verify-capability-output.v1",
            "source_result_digest": source.result_digest,
            "browser": browser.model_dump(mode="json"),
        }
        return BrowserVerifyCapabilityOutput.model_validate(
            {**values, "result_digest": sha256_digest(values)}
        )

    async def verify(
        self,
        context: CapabilityExecutionContext,
        candidate: BaseModel,
    ) -> BaseModel:
        if not isinstance(candidate, BrowserVerifyCapabilityOutput):
            raise BuiltinCapabilityCandidateRejectedError(
                "Browser candidate type changed"
            )
        source = self._source_ref(context)
        if (
            candidate.source_result_digest != source.result_digest
            or candidate.browser.run_id != context.run_id
            or candidate.browser.node_id != context.node_id
        ):
            raise BuiltinCapabilityCandidateRejectedError(
                "Browser candidate lineage changed"
            )
        return self._verification(
            context=context,
            candidate=candidate,
            verifier_id="builtin.browser.verify.verifier.v1",
            evidence={
                "source_result_ref_digest": source.result_ref_digest,
                "status": candidate.browser.status,
                "external_request_count": candidate.browser.external_request_count,
                "evidence_digest": candidate.browser.evidence_digest,
                "result_digest": candidate.result_digest,
            },
        )

    @staticmethod
    def _source_ref(
        context: CapabilityExecutionContext,
    ) -> VerifiedCapabilityResultRef:
        return ArtifactHtmlExecutor._source_ref(context, CapabilityResultKind.ARTIFACT)


class KnowledgeLocalExecutor(_BuiltinExecutor):
    def __init__(self, capability: CapabilityRef, knowledge: KnowledgeSearchPort) -> None:
        super().__init__(capability, CapabilityResultKind.KNOWLEDGE)
        self._knowledge = knowledge

    async def execute(self, context: CapabilityExecutionContext, arguments: BaseModel) -> BaseModel:
        self._require_context(context)
        if not isinstance(arguments, KnowledgeLocalExecutorInput):
            raise BuiltinCapabilityExecutionError("Knowledge executor input type changed")
        return await self._knowledge.search(arguments.query, arguments.limit)

    async def verify(self, context: CapabilityExecutionContext, candidate: BaseModel) -> BaseModel:
        if not isinstance(candidate, KnowledgeSearchRead):
            raise BuiltinCapabilityCandidateRejectedError("Knowledge candidate type changed")
        material = candidate.model_dump(mode="json", exclude={"result_digest"})
        if candidate.result_digest != sha256_digest(material):
            raise BuiltinCapabilityCandidateRejectedError(
                "Knowledge candidate result digest changed"
            )
        return self._verification(
            context=context,
            candidate=candidate,
            verifier_id="builtin.knowledge.local.verifier.v1",
            evidence={
                "query_digest": candidate.query_digest,
                "citation_proofs": [item.retrieval_proof_digest for item in candidate.citations],
                "result_digest": candidate.result_digest,
            },
        )


class McpTextMetricsExecutor(_BuiltinExecutor):
    def __init__(self, capability: CapabilityRef, mcp: McpTextMetricsPort) -> None:
        super().__init__(capability, CapabilityResultKind.MCP)
        self._mcp = mcp

    async def execute(self, context: CapabilityExecutionContext, arguments: BaseModel) -> BaseModel:
        self._require_context(context)
        if not isinstance(arguments, McpTextMetricsExecutorInput):
            raise BuiltinCapabilityExecutionError("MCP executor input type changed")
        return await self._mcp.invoke(TOOL_NAME, {"text": arguments.text})

    async def verify(self, context: CapabilityExecutionContext, candidate: BaseModel) -> BaseModel:
        if not isinstance(candidate, McpToolCallRead):
            raise BuiltinCapabilityCandidateRejectedError("MCP candidate type changed")
        if candidate.server_id != SERVER_ID or candidate.tool_name != TOOL_NAME:
            raise BuiltinCapabilityCandidateRejectedError("MCP candidate identity changed")
        McpTextMetricsOutput.model_validate(candidate.structured_content)
        expected_result = sha256_digest(
            {
                "server_id": candidate.server_id,
                "tool_name": candidate.tool_name,
                "protocol_version": candidate.protocol_version,
                "structured_content": candidate.structured_content,
            }
        )
        if candidate.result_digest != expected_result:
            raise BuiltinCapabilityCandidateRejectedError("MCP candidate result digest changed")
        audit_event_digest = await self._audit_event_digest(candidate)
        return self._verification(
            context=context,
            candidate=candidate,
            verifier_id="builtin.mcp.text.metrics.verifier.v1",
            evidence={
                "request_digest": candidate.request_digest,
                "result_digest": candidate.result_digest,
                "audit_event_id": candidate.audit_event_id,
                "audit_event_digest": audit_event_digest,
            },
        )

    async def _audit_event_digest(self, candidate: McpToolCallRead) -> str:
        after = 0
        # ``list_audit`` verifies the complete hash chain before returning a
        # page.  The cap prevents corrupt or hostile state from causing an
        # unbounded verification loop.
        for _ in range(50):
            page = await self._mcp.list_audit(after, 200)
            matching = tuple(
                item
                for item in page.events
                if item.event_id == candidate.audit_event_id
                and item.action == "tool_called"
                and item.request_digest == candidate.request_digest
                and item.result_digest == candidate.result_digest
            )
            if len(matching) == 1:
                return matching[0].event_digest
            if page.next_after_sequence <= after:
                break
            after = page.next_after_sequence
        raise BuiltinCapabilityCandidateRejectedError(
            "MCP candidate has no matching verified audit event"
        )


class WorkspaceSnapshotCheckExecutor(_BuiltinExecutor):
    def __init__(
        self,
        capability: CapabilityRef,
        workspace: WorkspaceSnapshotPort,
        runtime: WorkspaceCheckPort,
    ) -> None:
        super().__init__(capability, CapabilityResultKind.WORKSPACE_CHECK)
        self._workspace = workspace
        self._runtime = runtime

    async def execute(self, context: CapabilityExecutionContext, arguments: BaseModel) -> BaseModel:
        self._require_context(context)
        if not isinstance(arguments, WorkspaceSnapshotCheckExecutorInput):
            raise BuiltinCapabilityExecutionError("Workspace check input type changed")
        if not self._runtime.enabled:
            raise BuiltinCapabilityRuntimeUnavailableError("Workspace check runtime is disabled")
        snapshot = self._workspace.prepare_check(arguments.profile, arguments.path)
        return await asyncio.to_thread(self._runtime.run, snapshot)

    async def verify(self, context: CapabilityExecutionContext, candidate: BaseModel) -> BaseModel:
        if not isinstance(candidate, WorkspaceCheckRead):
            raise BuiltinCapabilityCandidateRejectedError("Workspace check candidate type changed")
        return self._workspace_verification(
            context,
            candidate,
            "builtin.workspace.snapshot.check.verifier.v1",
        )

    def _workspace_verification(
        self,
        context: CapabilityExecutionContext,
        candidate: WorkspaceCheckRead,
        verifier_id: str,
    ) -> CapabilityAdapterVerification:
        if candidate.network_access or candidate.isolation_mode != "windows_appcontainer":
            raise BuiltinCapabilityCandidateRejectedError("Workspace check isolation proof changed")
        return self._verification(
            context=context,
            candidate=candidate,
            verifier_id=verifier_id,
            evidence={
                "snapshot_digest": candidate.snapshot_digest,
                "status": candidate.status,
                "result_digest": candidate.result_digest,
                "network_access": candidate.network_access,
                "isolation_mode": candidate.isolation_mode,
            },
        )


class WorkspacePythonTestExecutor(_BuiltinExecutor):
    def __init__(
        self,
        capability: CapabilityRef,
        workspace: WorkspaceSnapshotPort,
        runtime: WorkspacePythonTestPort,
    ) -> None:
        super().__init__(capability, CapabilityResultKind.PYTHON_TEST)
        self._workspace = workspace
        self._runtime = runtime

    async def execute(self, context: CapabilityExecutionContext, arguments: BaseModel) -> BaseModel:
        self._require_context(context)
        if not isinstance(arguments, WorkspacePythonTestExecutorInput):
            raise BuiltinCapabilityExecutionError("Python test executor input type changed")
        if not self._runtime.enabled:
            raise BuiltinCapabilityRuntimeUnavailableError("Python test runtime is disabled")
        snapshot = self._workspace.prepare_python_test(arguments.project_path, arguments.test_path)
        return await asyncio.to_thread(self._runtime.run, snapshot)

    async def verify(self, context: CapabilityExecutionContext, candidate: BaseModel) -> BaseModel:
        if not isinstance(candidate, WorkspacePythonTestRead):
            raise BuiltinCapabilityCandidateRejectedError("Python test candidate type changed")
        return self._verification(
            context=context,
            candidate=candidate,
            verifier_id="builtin.workspace.python.test.verifier.v1",
            evidence=_test_evidence(candidate),
        )


class WorkspaceNodeTestExecutor(_BuiltinExecutor):
    def __init__(
        self,
        capability: CapabilityRef,
        workspace: WorkspaceSnapshotPort,
        runtime: WorkspaceNodeTestPort,
    ) -> None:
        super().__init__(capability, CapabilityResultKind.NODE_TEST)
        self._workspace = workspace
        self._runtime = runtime

    async def execute(self, context: CapabilityExecutionContext, arguments: BaseModel) -> BaseModel:
        self._require_context(context)
        if not isinstance(arguments, WorkspaceNodeTestExecutorInput):
            raise BuiltinCapabilityExecutionError("Node test executor input type changed")
        if not self._runtime.enabled:
            raise BuiltinCapabilityRuntimeUnavailableError("Node test runtime is disabled")
        snapshot = self._workspace.prepare_node_test(arguments.project_path, arguments.test_path)
        return await asyncio.to_thread(self._runtime.run, snapshot)

    async def verify(self, context: CapabilityExecutionContext, candidate: BaseModel) -> BaseModel:
        if not isinstance(candidate, WorkspaceNodeTestRead):
            raise BuiltinCapabilityCandidateRejectedError("Node test candidate type changed")
        return self._verification(
            context=context,
            candidate=candidate,
            verifier_id="builtin.workspace.node.test.verifier.v1",
            evidence=_test_evidence(candidate),
        )


def _test_evidence(
    candidate: WorkspacePythonTestRead | WorkspaceNodeTestRead,
) -> dict[str, object]:
    if (
        candidate.network_access
        or candidate.isolation_mode != "windows_appcontainer"
        or candidate.process_limit != 1
    ):
        raise BuiltinCapabilityCandidateRejectedError("Workspace test isolation proof changed")
    return {
        "snapshot_digest": candidate.snapshot_digest,
        "runtime_digest": candidate.runtime_digest,
        "status": candidate.status,
        "exit_code": candidate.exit_code,
        "result_digest": candidate.result_digest,
        "network_access": candidate.network_access,
        "isolation_mode": candidate.isolation_mode,
        "process_limit": candidate.process_limit,
    }


def create_builtin_capability_executor_registry(
    capabilities: CapabilityCatalog,
    *,
    knowledge: KnowledgeSearchPort | None = None,
    mcp: McpTextMetricsPort | None = None,
    workspace: WorkspaceSnapshotPort | None = None,
    workspace_checks: WorkspaceCheckPort | None = None,
    python_tests: WorkspacePythonTestPort | None = None,
    node_tests: WorkspaceNodeTestPort | None = None,
    artifacts: ArtifactDeliveryPort | None = None,
) -> CapabilityExecutorRegistry:
    """Register only exact packs whose concrete trusted runtime is available."""

    registry = CapabilityExecutorRegistry()
    if knowledge is not None:
        pack = capabilities.resolve_preferred("knowledge.local.v1")
        _register(
            registry,
            pack,
            executor_id="builtin.knowledge.local.v1",
            input_model=KnowledgeLocalExecutorInput,
            output_model=KnowledgeSearchRead,
            result_kind=CapabilityResultKind.KNOWLEDGE,
            executor=KnowledgeLocalExecutor(_ref(pack), knowledge),
        )
    if mcp is not None:
        pack = capabilities.resolve_preferred("mcp.text.metrics.v1")
        _register(
            registry,
            pack,
            executor_id="builtin.mcp.text.metrics.v1",
            input_model=McpTextMetricsExecutorInput,
            output_model=McpToolCallRead,
            result_kind=CapabilityResultKind.MCP,
            executor=McpTextMetricsExecutor(_ref(pack), mcp),
            recovery_policy=CapabilityRecoveryPolicy.NO_AUTOMATIC_REPLAY,
        )
    if workspace is not None and workspace_checks is not None and workspace_checks.enabled:
        pack = capabilities.resolve_preferred("workspace.snapshot.check.v1")
        _register(
            registry,
            pack,
            executor_id="builtin.workspace.snapshot.check.v1",
            input_model=WorkspaceSnapshotCheckExecutorInput,
            output_model=WorkspaceCheckRead,
            result_kind=CapabilityResultKind.WORKSPACE_CHECK,
            executor=WorkspaceSnapshotCheckExecutor(_ref(pack), workspace, workspace_checks),
        )
    if workspace is not None and python_tests is not None and python_tests.enabled:
        pack = capabilities.resolve_preferred("workspace.python.test.v1")
        _register(
            registry,
            pack,
            executor_id="builtin.workspace.python.test.v1",
            input_model=WorkspacePythonTestExecutorInput,
            output_model=WorkspacePythonTestRead,
            result_kind=CapabilityResultKind.PYTHON_TEST,
            executor=WorkspacePythonTestExecutor(_ref(pack), workspace, python_tests),
        )
    if workspace is not None and node_tests is not None and node_tests.enabled:
        pack = capabilities.resolve_preferred("workspace.node.test.v1")
        _register(
            registry,
            pack,
            executor_id="builtin.workspace.node.test.v1",
            input_model=WorkspaceNodeTestExecutorInput,
            output_model=WorkspaceNodeTestRead,
            result_kind=CapabilityResultKind.NODE_TEST,
            executor=WorkspaceNodeTestExecutor(_ref(pack), workspace, node_tests),
        )
    if artifacts is not None:
        artifact_pack = capabilities.resolve_preferred("artifact.html.v1")
        _register(
            registry,
            artifact_pack,
            executor_id="builtin.artifact.html.v1",
            input_model=ArtifactHtmlExecutorInput,
            output_model=ArtifactHtmlCapabilityOutput,
            result_kind=CapabilityResultKind.ARTIFACT,
            consumes=(CapabilityResultKind.VERIFIED_CLAIMS,),
            effect_class=CapabilityEffectClass.WORKSPACE_WRITE,
            executor=ArtifactHtmlExecutor(_ref(artifact_pack), artifacts),
        )
        browser_pack = capabilities.resolve_preferred("browser.verify.v1")
        _register(
            registry,
            browser_pack,
            executor_id="builtin.browser.verify.v1",
            input_model=BrowserVerifyExecutorInput,
            output_model=BrowserVerifyCapabilityOutput,
            result_kind=CapabilityResultKind.BROWSER_VERIFICATION,
            consumes=(CapabilityResultKind.ARTIFACT,),
            executor=BrowserVerifyExecutor(_ref(browser_pack), artifacts),
        )
    return registry


def _register(
    registry: CapabilityExecutorRegistry,
    pack: CapabilityPack,
    *,
    executor_id: str,
    input_model: type[BaseModel],
    output_model: type[BaseModel],
    result_kind: CapabilityResultKind,
    executor: _BuiltinExecutor,
    consumes: tuple[CapabilityResultKind, ...] = (),
    effect_class: CapabilityEffectClass = CapabilityEffectClass.READ_ONLY,
    recovery_policy: CapabilityRecoveryPolicy = CapabilityRecoveryPolicy.DETERMINISTIC_RETRY,
) -> None:
    if not pack.runtime_enabled:
        return
    manifest = CapabilityExecutorManifest.from_pack(
        executor_id=executor_id,
        pack=pack,
        input_model=input_model,
        output_model=output_model,
        node_kinds=(DraftNodeKind.CAPABILITY,),
        consumes=consumes,
        produces=result_kind,
        effect_class=effect_class,
        approval_requirement=CapabilityApprovalRequirement.NONE,
        recovery_policy=recovery_policy,
    )
    registry.register(
        manifest,
        input_model,
        output_model,
        cast(CapabilityExecutor, executor),
    )


def _ref(pack: CapabilityPack) -> CapabilityRef:
    return CapabilityRef(
        capability_id=pack.capability_id,
        version=pack.version,
        digest=pack.digest,
    )


__all__ = [
    "ArtifactDeliveryPort",
    "ArtifactHtmlCapabilityOutput",
    "ArtifactHtmlExecutor",
    "BrowserVerifyCapabilityOutput",
    "BrowserVerifyExecutor",
    "BuiltinCapabilityCandidateRejectedError",
    "BuiltinCapabilityExecutionError",
    "BuiltinCapabilityRuntimeUnavailableError",
    "CapabilityAdapterVerification",
    "KnowledgeLocalExecutor",
    "McpTextMetricsExecutor",
    "WorkspaceNodeTestExecutor",
    "WorkspacePythonTestExecutor",
    "WorkspaceSnapshotCheckExecutor",
    "create_builtin_capability_executor_registry",
]
