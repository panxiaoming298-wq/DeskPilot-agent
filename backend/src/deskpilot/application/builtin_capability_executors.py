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
    WorkspaceCommandExecutorInput,
    WorkspaceGitCommitExecutorInput,
    WorkspaceGitInspectExecutorInput,
    WorkspaceNodeTestExecutorInput,
    WorkspacePatchBundleExecutorInput,
    WorkspaceProjectBatchReadExecutorInput,
    WorkspaceProjectSearchExecutorInput,
    WorkspacePythonTestExecutorInput,
    WorkspaceSnapshotCheckExecutorInput,
)
from deskpilot.application.command_profile_catalog import CommandProfileCatalog
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
from deskpilot.domain.coding_tools import (
    GitCommitPreview,
    GitCommitReceipt,
    GitInspectionRead,
    ProjectBatchRead,
    ProjectSearchRead,
)
from deskpilot.domain.command_profiles import (
    CommandProfile,
    CommandProfileId,
    WorkspaceCommandRead,
    WorkspaceCommandSnapshot,
)
from deskpilot.domain.knowledge import KnowledgeSearchRead
from deskpilot.domain.mcp import McpAuditPage, McpTextMetricsOutput, McpToolCallRead
from deskpilot.domain.task_plans import CapabilityPack, CapabilityRef, DraftNodeKind
from deskpilot.domain.workspace_files import (
    WorkspaceCheckRead,
    WorkspaceNodeTestRead,
    WorkspaceNodeTestSnapshot,
    WorkspacePatchPreview,
    WorkspacePatchReceipt,
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


class WorkspacePatchPort(Protocol):
    def prepare_patch(
        self,
        *,
        task_id: str,
        changes: tuple[dict[str, str], ...],
        minimum_changes: int = 2,
        maximum_changes: int = 8,
    ) -> WorkspacePatchPreview: ...

    def commit_patch(self, preview: WorkspacePatchPreview) -> WorkspacePatchReceipt: ...


class WorkspaceCodingPort(Protocol):
    @property
    def enabled(self) -> bool: ...

    @property
    def git_enabled(self) -> bool: ...

    def search(self, project_path: str, query: str) -> ProjectSearchRead: ...

    def read_many(self, project_path: str, relative_paths: tuple[str, ...]) -> ProjectBatchRead: ...

    def inspect_git(
        self,
        project_path: str,
        operation: Literal["status", "diff", "log"],
    ) -> GitInspectionRead: ...

    def prepare_git_commit(
        self,
        *,
        task_id: str,
        project_path: str,
        paths: tuple[str, ...],
    ) -> GitCommitPreview: ...

    def commit_git(self, preview: GitCommitPreview) -> GitCommitReceipt: ...


class WorkspaceCommandSnapshotPort(Protocol):
    def prepare_command_snapshot(
        self,
        project_path: str,
        profile: CommandProfile,
    ) -> WorkspaceCommandSnapshot: ...


class WorkspaceCommandPort(Protocol):
    @property
    def enabled_profile_ids(self) -> frozenset[CommandProfileId]: ...

    def run(self, snapshot: WorkspaceCommandSnapshot) -> WorkspaceCommandRead: ...


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


class WorkspacePatchCapabilityOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.workspace-patch-capability-output.v1"] = (
        "deskpilot.workspace-patch-capability-output.v1"
    )
    receipt: WorkspacePatchReceipt
    result_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def digest_matches(self) -> WorkspacePatchCapabilityOutput:
        material = self.model_dump(mode="json", exclude={"result_digest"})
        if self.result_digest != sha256_digest(material):
            raise ValueError("Workspace patch capability output digest does not match")
        return self


class WorkspaceGitCommitCapabilityOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.workspace-git-commit-capability-output.v1"] = (
        "deskpilot.workspace-git-commit-capability-output.v1"
    )
    receipt: GitCommitReceipt
    result_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def digest_matches(self) -> WorkspaceGitCommitCapabilityOutput:
        material = self.model_dump(mode="json", exclude={"result_digest"})
        if self.result_digest != sha256_digest(material):
            raise ValueError("Git commit capability output digest does not match")
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


class WorkspaceProjectSearchExecutor(_BuiltinExecutor):
    def __init__(self, capability: CapabilityRef, runtime: WorkspaceCodingPort) -> None:
        super().__init__(capability, CapabilityResultKind.PROJECT_SEARCH)
        self._runtime = runtime

    async def execute(self, context: CapabilityExecutionContext, arguments: BaseModel) -> BaseModel:
        self._require_context(context)
        if not isinstance(arguments, WorkspaceProjectSearchExecutorInput):
            raise BuiltinCapabilityExecutionError("Project search input type changed")
        if not self._runtime.enabled:
            raise BuiltinCapabilityRuntimeUnavailableError("Project search runtime is disabled")
        return await asyncio.to_thread(
            self._runtime.search,
            arguments.project_path,
            arguments.query,
        )

    async def verify(self, context: CapabilityExecutionContext, candidate: BaseModel) -> BaseModel:
        if not isinstance(candidate, ProjectSearchRead):
            raise BuiltinCapabilityCandidateRejectedError("Project search candidate type changed")
        return self._verification(
            context=context,
            candidate=candidate,
            verifier_id="builtin.workspace.project.search.verifier.v1",
            evidence={
                "project_path": candidate.project_path,
                "query_digest": candidate.query_digest,
                "match_count": len(candidate.matches),
                "scanned_file_count": candidate.scanned_file_count,
                "scanned_byte_count": candidate.scanned_byte_count,
                "truncated": candidate.truncated,
                "result_digest": candidate.result_digest,
            },
        )


class WorkspaceProjectBatchReadExecutor(_BuiltinExecutor):
    def __init__(self, capability: CapabilityRef, runtime: WorkspaceCodingPort) -> None:
        super().__init__(capability, CapabilityResultKind.PROJECT_BATCH_READ)
        self._runtime = runtime

    async def execute(self, context: CapabilityExecutionContext, arguments: BaseModel) -> BaseModel:
        self._require_context(context)
        if not isinstance(arguments, WorkspaceProjectBatchReadExecutorInput):
            raise BuiltinCapabilityExecutionError("Project batch-read input type changed")
        if not self._runtime.enabled:
            raise BuiltinCapabilityRuntimeUnavailableError("Project batch-read runtime is disabled")
        return await asyncio.to_thread(
            self._runtime.read_many,
            arguments.project_path,
            arguments.paths,
        )

    async def verify(self, context: CapabilityExecutionContext, candidate: BaseModel) -> BaseModel:
        if not isinstance(candidate, ProjectBatchRead):
            raise BuiltinCapabilityCandidateRejectedError(
                "Project batch-read candidate type changed"
            )
        return self._verification(
            context=context,
            candidate=candidate,
            verifier_id="builtin.workspace.project.read_many.verifier.v1",
            evidence={
                "project_path": candidate.project_path,
                "file_result_digests": [item.result_digest for item in candidate.files],
                "total_byte_count": candidate.total_byte_count,
                "result_digest": candidate.result_digest,
            },
        )


class WorkspaceGitInspectExecutor(_BuiltinExecutor):
    def __init__(self, capability: CapabilityRef, runtime: WorkspaceCodingPort) -> None:
        super().__init__(capability, CapabilityResultKind.GIT_INSPECTION)
        self._runtime = runtime

    async def execute(self, context: CapabilityExecutionContext, arguments: BaseModel) -> BaseModel:
        self._require_context(context)
        if not isinstance(arguments, WorkspaceGitInspectExecutorInput):
            raise BuiltinCapabilityExecutionError("Git inspection input type changed")
        if not self._runtime.git_enabled:
            raise BuiltinCapabilityRuntimeUnavailableError("Git inspection runtime is disabled")
        return await asyncio.to_thread(
            self._runtime.inspect_git,
            arguments.project_path,
            arguments.operation,
        )

    async def verify(self, context: CapabilityExecutionContext, candidate: BaseModel) -> BaseModel:
        if (
            not isinstance(candidate, GitInspectionRead)
            or not candidate.hooks_disabled
            or not candidate.external_diff_disabled
            or not candidate.textconv_disabled
            or not candidate.pager_disabled
            or not candidate.optional_locks_disabled
        ):
            raise BuiltinCapabilityCandidateRejectedError(
                "Git inspection candidate lost a fixed read-only guard"
            )
        return self._verification(
            context=context,
            candidate=candidate,
            verifier_id="builtin.workspace.git.inspect.verifier.v1",
            evidence={
                "operation": candidate.operation,
                "repository_digest": candidate.repository_digest,
                "toolchain_digest": candidate.toolchain_digest,
                "head_oid": candidate.head_oid,
                "output_digest": candidate.output_digest,
                "output_truncated": candidate.output_truncated,
                "result_digest": candidate.result_digest,
            },
        )


class WorkspaceGitCommitExecutor(_BuiltinExecutor):
    """R1 adapter limited to one exact server-named branch and commit."""

    def __init__(self, capability: CapabilityRef, runtime: WorkspaceCodingPort) -> None:
        super().__init__(capability, CapabilityResultKind.GIT_COMMIT)
        self._runtime = runtime

    async def execute(self, context: CapabilityExecutionContext, arguments: BaseModel) -> BaseModel:
        self._require_context(context)
        raise BuiltinCapabilityExecutionError(
            "Git commit execution requires an exact persisted approval"
        )

    async def prepare_approval(
        self,
        context: CapabilityExecutionContext,
        arguments: BaseModel,
    ) -> BaseModel:
        self._require_context(context)
        if not isinstance(arguments, WorkspaceGitCommitExecutorInput):
            raise BuiltinCapabilityExecutionError("Git commit input type changed")
        if not self._runtime.git_enabled:
            raise BuiltinCapabilityRuntimeUnavailableError("Git commit runtime is disabled")
        return await asyncio.to_thread(
            self._runtime.prepare_git_commit,
            task_id=context.task_id,
            project_path=arguments.project_path,
            paths=arguments.paths,
        )

    async def execute_approved(
        self,
        context: CapabilityExecutionContext,
        arguments: BaseModel,
        preview: BaseModel,
    ) -> BaseModel:
        self._require_context(context)
        if (
            not isinstance(arguments, WorkspaceGitCommitExecutorInput)
            or not isinstance(preview, GitCommitPreview)
            or preview.task_id != context.task_id
            or preview.project_path != arguments.project_path
        ):
            raise BuiltinCapabilityExecutionError(
                "Git commit approval changed its exact Task or project binding"
            )
        receipt = await asyncio.to_thread(self._runtime.commit_git, preview)
        values = {
            "schema_version": "deskpilot.workspace-git-commit-capability-output.v1",
            "receipt": receipt.model_dump(mode="json"),
        }
        return WorkspaceGitCommitCapabilityOutput.model_validate(
            {**values, "result_digest": sha256_digest(values)}
        )

    async def verify(
        self,
        context: CapabilityExecutionContext,
        candidate: BaseModel,
    ) -> BaseModel:
        if (
            not isinstance(candidate, WorkspaceGitCommitCapabilityOutput)
            or candidate.receipt.task_id != context.task_id
            or not candidate.receipt.hooks_disabled
            or not candidate.receipt.signing_disabled
            or not candidate.receipt.push_disabled
            or not candidate.receipt.rollback_available
        ):
            raise BuiltinCapabilityCandidateRejectedError(
                "Git commit receipt lost a fixed execution guard"
            )
        return self._verification(
            context=context,
            candidate=candidate,
            verifier_id="builtin.workspace.git.commit.verifier.v1",
            evidence={
                "confirmation_digest": candidate.receipt.confirmation_digest,
                "expected_head_oid": candidate.receipt.expected_head_oid,
                "commit_oid": candidate.receipt.commit_oid,
                "tree_oid": candidate.receipt.tree_oid,
                "target_branch": candidate.receipt.target_branch,
                "path_proof_digests": [
                    item.proof_digest for item in candidate.receipt.paths
                ],
                "receipt_digest": candidate.receipt.receipt_digest,
                "result_digest": candidate.result_digest,
            },
        )


class WorkspaceCommandExecutor(_BuiltinExecutor):
    def __init__(
        self,
        capability: CapabilityRef,
        profiles: CommandProfileCatalog,
        workspace: WorkspaceCommandSnapshotPort,
        runtime: WorkspaceCommandPort,
    ) -> None:
        super().__init__(capability, CapabilityResultKind.COMMAND_PROFILE)
        self._profiles = profiles
        self._workspace = workspace
        self._runtime = runtime

    async def execute(self, context: CapabilityExecutionContext, arguments: BaseModel) -> BaseModel:
        self._require_context(context)
        if not isinstance(arguments, WorkspaceCommandExecutorInput):
            raise BuiltinCapabilityExecutionError("Command Profile input type changed")
        if arguments.command_profile_id not in self._runtime.enabled_profile_ids:
            raise BuiltinCapabilityRuntimeUnavailableError(
                "Selected Command Profile runtime is disabled"
            )
        profile = self._profiles.resolve(arguments.command_profile_id)
        snapshot = self._workspace.prepare_command_snapshot(
            arguments.project_path,
            profile,
        )
        return await asyncio.to_thread(self._runtime.run, snapshot)

    async def verify(self, context: CapabilityExecutionContext, candidate: BaseModel) -> BaseModel:
        if not isinstance(candidate, WorkspaceCommandRead):
            raise BuiltinCapabilityCandidateRejectedError(
                "Command Profile candidate type changed"
            )
        profile = self._profiles.resolve(candidate.command_profile_id)
        if (
            candidate.profile_digest != profile.profile_digest
            or candidate.network_access
            or candidate.isolation_mode != "windows_appcontainer"
            or not candidate.temporary_snapshot
            or not candidate.snapshot_mutations_discarded
        ):
            raise BuiltinCapabilityCandidateRejectedError(
                "Command Profile candidate lost a fixed execution guard"
            )
        return self._verification(
            context=context,
            candidate=candidate,
            verifier_id="builtin.workspace.command.run.verifier.v1",
            evidence={
                "command_profile_id": candidate.command_profile_id,
                "profile_digest": candidate.profile_digest,
                "snapshot_digest": candidate.snapshot_digest,
                "toolchain_digest": candidate.toolchain_digest,
                "status": candidate.status,
                "exit_code": candidate.exit_code,
                "output_digest": candidate.output_digest,
                "output_truncated": candidate.output_truncated,
                "termination_reason": candidate.termination_reason,
                "cancellation_receipt_digest": candidate.cancellation_receipt_digest,
                "isolation_mode": candidate.isolation_mode,
                "network_access": candidate.network_access,
                "temporary_snapshot": candidate.temporary_snapshot,
                "snapshot_mutations_discarded": candidate.snapshot_mutations_discarded,
                "result_digest": candidate.result_digest,
            },
        )


class WorkspacePatchBundleExecutor(_BuiltinExecutor):
    """R1 adapter whose write path exists only behind an exact preview digest."""

    def __init__(self, capability: CapabilityRef, workspace: WorkspacePatchPort) -> None:
        super().__init__(capability, CapabilityResultKind.PATCH_RECEIPT)
        self._workspace = workspace

    async def execute(
        self,
        context: CapabilityExecutionContext,
        arguments: BaseModel,
    ) -> BaseModel:
        self._require_context(context)
        raise BuiltinCapabilityExecutionError(
            "Workspace patch execution requires an exact persisted approval"
        )

    async def prepare_approval(
        self,
        context: CapabilityExecutionContext,
        arguments: BaseModel,
    ) -> BaseModel:
        self._require_context(context)
        if not isinstance(arguments, WorkspacePatchBundleExecutorInput):
            raise BuiltinCapabilityExecutionError("Workspace patch input type changed")
        changes = tuple(item.model_dump(mode="python") for item in arguments.changes)
        return await asyncio.to_thread(
            self._workspace.prepare_patch,
            task_id=context.task_id,
            changes=changes,
        )

    async def execute_approved(
        self,
        context: CapabilityExecutionContext,
        arguments: BaseModel,
        preview: BaseModel,
    ) -> BaseModel:
        self._require_context(context)
        if (
            not isinstance(arguments, WorkspacePatchBundleExecutorInput)
            or not isinstance(preview, WorkspacePatchPreview)
            or preview.task_id != context.task_id
        ):
            raise BuiltinCapabilityExecutionError(
                "Workspace patch approval changed its exact Task binding"
            )
        receipt = await asyncio.to_thread(self._workspace.commit_patch, preview)
        values = {
            "schema_version": "deskpilot.workspace-patch-capability-output.v1",
            "receipt": receipt.model_dump(mode="json"),
        }
        return WorkspacePatchCapabilityOutput.model_validate(
            {**values, "result_digest": sha256_digest(values)}
        )

    async def verify(
        self,
        context: CapabilityExecutionContext,
        candidate: BaseModel,
    ) -> BaseModel:
        if (
            not isinstance(candidate, WorkspacePatchCapabilityOutput)
            or candidate.receipt.task_id != context.task_id
            or candidate.receipt.status != "committed"
        ):
            raise BuiltinCapabilityCandidateRejectedError(
                "Workspace patch receipt is partial or crossed its Task"
            )
        return self._verification(
            context=context,
            candidate=candidate,
            verifier_id="builtin.workspace.patch.bundle.verifier.v1",
            evidence={
                "confirmation_digest": candidate.receipt.confirmation_digest,
                "change_receipt_digests": [
                    item.receipt_digest for item in candidate.receipt.change_receipts
                ],
                "receipt_digest": candidate.receipt.receipt_digest,
                "result_digest": candidate.result_digest,
            },
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
    workspace_patches: WorkspacePatchPort | None = None,
    workspace_coding: WorkspaceCodingPort | None = None,
    command_profiles: CommandProfileCatalog | None = None,
    command_snapshots: WorkspaceCommandSnapshotPort | None = None,
    command_runtime: WorkspaceCommandPort | None = None,
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
    if workspace_patches is not None:
        pack = capabilities.resolve_preferred("workspace.patch.bundle.v1")
        _register(
            registry,
            pack,
            executor_id="builtin.workspace.patch.bundle.v1",
            input_model=WorkspacePatchBundleExecutorInput,
            output_model=WorkspacePatchCapabilityOutput,
            approval_model=WorkspacePatchPreview,
            result_kind=CapabilityResultKind.PATCH_RECEIPT,
            effect_class=CapabilityEffectClass.WORKSPACE_WRITE,
            approval_requirement=CapabilityApprovalRequirement.EXACT_CONFIRMATION_DIGEST,
            recovery_policy=CapabilityRecoveryPolicy.RECEIPT_RECONCILE,
            executor=WorkspacePatchBundleExecutor(_ref(pack), workspace_patches),
        )
    if workspace_coding is not None and workspace_coding.enabled:
        search_pack = capabilities.resolve_preferred("workspace.project.search.v1")
        _register(
            registry,
            search_pack,
            executor_id="builtin.workspace.project.search.v1",
            input_model=WorkspaceProjectSearchExecutorInput,
            output_model=ProjectSearchRead,
            result_kind=CapabilityResultKind.PROJECT_SEARCH,
            executor=WorkspaceProjectSearchExecutor(_ref(search_pack), workspace_coding),
        )
        read_pack = capabilities.resolve_preferred("workspace.project.read_many.v1")
        _register(
            registry,
            read_pack,
            executor_id="builtin.workspace.project.read_many.v1",
            input_model=WorkspaceProjectBatchReadExecutorInput,
            output_model=ProjectBatchRead,
            result_kind=CapabilityResultKind.PROJECT_BATCH_READ,
            executor=WorkspaceProjectBatchReadExecutor(_ref(read_pack), workspace_coding),
        )
        if workspace_coding.git_enabled:
            git_pack = capabilities.resolve_preferred("workspace.git.inspect.v1")
            _register(
                registry,
                git_pack,
                executor_id="builtin.workspace.git.inspect.v1",
                input_model=WorkspaceGitInspectExecutorInput,
                output_model=GitInspectionRead,
                result_kind=CapabilityResultKind.GIT_INSPECTION,
                executor=WorkspaceGitInspectExecutor(_ref(git_pack), workspace_coding),
            )
            commit_pack = capabilities.resolve_preferred("workspace.git.commit.v1")
            _register(
                registry,
                commit_pack,
                executor_id="builtin.workspace.git.commit.v1",
                input_model=WorkspaceGitCommitExecutorInput,
                output_model=WorkspaceGitCommitCapabilityOutput,
                approval_model=GitCommitPreview,
                result_kind=CapabilityResultKind.GIT_COMMIT,
                effect_class=CapabilityEffectClass.WORKSPACE_WRITE,
                approval_requirement=(
                    CapabilityApprovalRequirement.EXACT_CONFIRMATION_DIGEST
                ),
                recovery_policy=CapabilityRecoveryPolicy.RECEIPT_RECONCILE,
                executor=WorkspaceGitCommitExecutor(_ref(commit_pack), workspace_coding),
            )
    if (
        command_profiles is not None
        and command_snapshots is not None
        and command_runtime is not None
        and command_runtime.enabled_profile_ids
    ):
        command_pack = capabilities.resolve_preferred("workspace.command.run.v1")
        _register(
            registry,
            command_pack,
            executor_id="builtin.workspace.command.run.v1",
            input_model=WorkspaceCommandExecutorInput,
            output_model=WorkspaceCommandRead,
            result_kind=CapabilityResultKind.COMMAND_PROFILE,
            recovery_policy=CapabilityRecoveryPolicy.NO_AUTOMATIC_REPLAY,
            executor=WorkspaceCommandExecutor(
                _ref(command_pack),
                command_profiles,
                command_snapshots,
                command_runtime,
            ),
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
    approval_model: type[BaseModel] | None = None,
    consumes: tuple[CapabilityResultKind, ...] = (),
    effect_class: CapabilityEffectClass = CapabilityEffectClass.READ_ONLY,
    recovery_policy: CapabilityRecoveryPolicy = CapabilityRecoveryPolicy.DETERMINISTIC_RETRY,
    approval_requirement: CapabilityApprovalRequirement = CapabilityApprovalRequirement.NONE,
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
        approval_requirement=approval_requirement,
        recovery_policy=recovery_policy,
    )
    registry.register(
        manifest,
        input_model,
        output_model,
        cast(CapabilityExecutor, executor),
        approval_model=approval_model,
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
    "WorkspaceGitInspectExecutor",
    "WorkspaceGitCommitExecutor",
    "WorkspaceGitCommitCapabilityOutput",
    "WorkspacePatchBundleExecutor",
    "WorkspacePatchCapabilityOutput",
    "WorkspacePatchPort",
    "WorkspacePythonTestExecutor",
    "WorkspaceProjectBatchReadExecutor",
    "WorkspaceProjectSearchExecutor",
    "WorkspaceCodingPort",
    "WorkspaceSnapshotCheckExecutor",
    "create_builtin_capability_executor_registry",
]
