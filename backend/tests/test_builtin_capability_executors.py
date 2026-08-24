from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from deskpilot.application.builtin_capability_executors import (
    ArtifactHtmlCapabilityOutput,
    BrowserVerifyCapabilityOutput,
    BuiltinCapabilityRuntimeUnavailableError,
    create_builtin_capability_executor_registry,
)
from deskpilot.application.capability_catalog import create_builtin_capability_catalog
from deskpilot.application.capability_execution_engine import (
    CapabilityCandidateBindingRejectedError,
    CapabilityExecutionCandidate,
    CapabilityExecutionEngine,
    VerifiedCapabilityOutput,
)
from deskpilot.application.capability_executor_registry import (
    UnknownCapabilityExecutorError,
)
from deskpilot.application.capability_input_binding_catalog import (
    ArtifactHtmlExecutorInput,
    BoundCapabilityInput,
    BrowserVerifyExecutorInput,
    KnowledgeLocalExecutorInput,
    McpTextMetricsExecutorInput,
    WorkspaceCommandExecutorInput,
    WorkspaceNodeTestExecutorInput,
    WorkspaceProjectBatchReadExecutorInput,
    WorkspaceProjectSearchExecutorInput,
    WorkspacePythonTestExecutorInput,
    WorkspaceSnapshotCheckExecutorInput,
)
from deskpilot.application.command_profile_catalog import CommandProfileCatalog
from deskpilot.application.mcp_control_plane import SERVER_ID
from deskpilot.application.workspace_coding_runtime import WorkspaceCodingRuntime
from deskpilot.application.workspace_file_runtime import WorkspaceFileRuntime
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.artifact_runtime import (
    ArtifactRead,
    ArtifactRevisionRead,
    BrowserRenderRunRead,
    TaskWorkspaceRead,
)
from deskpilot.domain.capability_execution import (
    CapabilityApprovalRequirement,
    CapabilityEffectClass,
    CapabilityExecutionContext,
    CapabilityRecoveryPolicy,
    CapabilityResultKind,
    VerifiedCapabilityResultRef,
)
from deskpilot.domain.command_profiles import (
    CommandProfile,
    CommandProfileId,
    WorkspaceCommandRead,
    WorkspaceCommandSnapshot,
)
from deskpilot.domain.knowledge import KnowledgeSearchRead
from deskpilot.domain.mcp import (
    McpAuditEventRead,
    McpAuditPage,
    McpTextMetricsOutput,
    McpToolCallRead,
)
from deskpilot.domain.task_plans import CapabilityRef, DraftNodeKind, PlanNodeBudget
from deskpilot.domain.workspace_files import (
    WorkspaceCheckRead,
    WorkspaceNodeTestRead,
    WorkspaceNodeTestSnapshot,
    WorkspacePythonTestRead,
    WorkspacePythonTestSnapshot,
)
from deskpilot.mcp_servers.readonly_text_server import TOOL_NAME
from deskpilot.tools.workspace_checks import WorkspaceCheckInput

TASK_ID = f"tsk_{'1' * 32}"


def _ref(capability_id: str) -> CapabilityRef:
    pack = create_builtin_capability_catalog().resolve_preferred(capability_id)
    return CapabilityRef(
        capability_id=pack.capability_id,
        version=pack.version,
        digest=pack.digest,
    )


def _knowledge_result() -> KnowledgeSearchRead:
    material: dict[str, Any] = {
        "query_digest": sha256_digest({"query": "cats"}),
        "citations": (),
        "searched_sources": 0,
        "stale_source_ids": (),
    }
    return KnowledgeSearchRead(**material, result_digest=sha256_digest(material))


def _mcp_result() -> tuple[McpToolCallRead, McpAuditPage]:
    structured = McpTextMetricsOutput(
        character_count=8,
        line_count=1,
        word_count=2,
        text_digest="a" * 64,
    ).model_dump(mode="json")
    request_digest = "b" * 64
    result_digest = sha256_digest(
        {
            "server_id": SERVER_ID,
            "tool_name": TOOL_NAME,
            "protocol_version": "2025-11-25",
            "structured_content": structured,
        }
    )
    event = McpAuditEventRead(
        event_id="mcp_evt_1",
        sequence=1,
        server_id=SERVER_ID,
        action="tool_called",
        request_digest=request_digest,
        result_digest=result_digest,
        previous_event_digest=None,
        event_digest="c" * 64,
        details={"status": "succeeded"},
        occurred_at=datetime(2026, 8, 24, tzinfo=UTC),
    )
    return (
        McpToolCallRead(
            server_id=SERVER_ID,
            tool_name=TOOL_NAME,
            protocol_version="2025-11-25",
            structured_content=structured,
            request_digest=request_digest,
            result_digest=result_digest,
            audit_event_id=event.event_id,
        ),
        McpAuditPage(events=(event,), next_after_sequence=1),
    )


def _workspace_check_result() -> WorkspaceCheckRead:
    material: dict[str, Any] = {
        "schema_version": "deskpilot.workspace-check.v1",
        "profile": "python-syntax",
        "relative_path": "src/app.py",
        "snapshot_digest": "d" * 64,
        "status": "passed",
        "checked_file_count": 1,
        "issues": (),
        "isolation_mode": "windows_appcontainer",
        "network_access": False,
        "output_truncated": False,
    }
    return WorkspaceCheckRead.model_validate({**material, "result_digest": sha256_digest(material)})


def _python_result() -> WorkspacePythonTestRead:
    material: dict[str, Any] = {
        "schema_version": "deskpilot.workspace-python-test.v1",
        "profile": "pytest-file",
        "project_path": "backend",
        "test_path": "tests/test_one.py",
        "snapshot_digest": "e" * 64,
        "runtime_digest": "f" * 64,
        "status": "passed",
        "exit_code": 0,
        "passed_count": 1,
        "failed_count": 0,
        "skipped_count": 0,
        "error_count": 0,
        "duration_ms": 12,
        "output": "1 passed",
        "output_truncated": False,
        "isolation_mode": "windows_appcontainer",
        "network_access": False,
        "process_limit": 1,
    }
    return WorkspacePythonTestRead.model_validate(
        {**material, "result_digest": sha256_digest(material)}
    )


def _node_result() -> WorkspaceNodeTestRead:
    material: dict[str, Any] = {
        "schema_version": "deskpilot.workspace-node-test.v1",
        "profile": "node-test-file",
        "project_path": "frontend",
        "test_path": "src/app.test.ts",
        "snapshot_digest": "1" * 64,
        "runtime_digest": "2" * 64,
        "status": "passed",
        "exit_code": 0,
        "passed_count": 1,
        "failed_count": 0,
        "skipped_count": 0,
        "error_count": 0,
        "duration_ms": 10,
        "output": "1 passed",
        "output_truncated": False,
        "isolation_mode": "windows_appcontainer",
        "network_access": False,
        "process_limit": 1,
    }
    return WorkspaceNodeTestRead.model_validate(
        {**material, "result_digest": sha256_digest(material)}
    )


def _command_result(profile: CommandProfile) -> WorkspaceCommandRead:
    output = "1 lint error"
    material: dict[str, Any] = {
        "schema_version": "deskpilot.workspace-command-read.v1",
        "command_profile_id": profile.command_profile_id,
        "profile_digest": profile.profile_digest,
        "project_path": "backend",
        "snapshot_digest": "1" * 64,
        "toolchain_digest": "2" * 64,
        "status": "failed",
        "exit_code": 1,
        "duration_ms": 12,
        "output_summary": output,
        "output_digest": sha256_digest({"output": output}),
        "output_truncated": False,
        "termination_reason": "completed",
        "cancellation_receipt_digest": None,
        "isolation_mode": "windows_appcontainer",
        "network_access": False,
        "temporary_snapshot": True,
        "snapshot_mutations_discarded": True,
    }
    return WorkspaceCommandRead.model_validate(
        {**material, "result_digest": sha256_digest(material)}
    )


class FakeKnowledge:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    async def search(self, query: str, limit: int) -> KnowledgeSearchRead:
        self.calls.append((query, limit))
        return _knowledge_result()


class FakeMcp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.audit_calls: list[tuple[int, int]] = []

    async def invoke(self, tool_name: str, arguments: dict[str, object]) -> McpToolCallRead:
        self.calls.append((tool_name, arguments))
        return _mcp_result()[0]

    async def list_audit(self, after_sequence: int, limit: int) -> McpAuditPage:
        self.audit_calls.append((after_sequence, limit))
        return _mcp_result()[1]


class FakeWorkspace:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []

    def prepare_check(self, profile: str, relative_path: str) -> WorkspaceCheckInput:
        self.calls.append(("check", profile, relative_path))
        return cast(WorkspaceCheckInput, object())

    def prepare_python_test(self, project_path: str, test_path: str) -> WorkspacePythonTestSnapshot:
        self.calls.append(("python", project_path, test_path))
        return cast(WorkspacePythonTestSnapshot, object())

    def prepare_node_test(self, project_path: str, test_path: str) -> WorkspaceNodeTestSnapshot:
        self.calls.append(("node", project_path, test_path))
        return cast(WorkspaceNodeTestSnapshot, object())


class FakeCheckRuntime:
    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self.calls = 0

    def run(self, snapshot: WorkspaceCheckInput) -> WorkspaceCheckRead:
        del snapshot
        self.calls += 1
        return _workspace_check_result()


class FakePythonRuntime:
    enabled = True

    def __init__(self) -> None:
        self.calls = 0

    def run(self, snapshot: WorkspacePythonTestSnapshot) -> WorkspacePythonTestRead:
        del snapshot
        self.calls += 1
        return _python_result()


class FakeNodeRuntime:
    enabled = True

    def __init__(self) -> None:
        self.calls = 0

    def run(self, snapshot: WorkspaceNodeTestSnapshot) -> WorkspaceNodeTestRead:
        del snapshot
        self.calls += 1
        return _node_result()


class FakeCommandSnapshots:
    def __init__(self) -> None:
        self.calls: list[tuple[str, CommandProfileId]] = []

    def prepare_command_snapshot(
        self,
        project_path: str,
        profile: CommandProfile,
    ) -> WorkspaceCommandSnapshot:
        self.calls.append((project_path, profile.command_profile_id))
        return cast(WorkspaceCommandSnapshot, object())


class FakeCommandRuntime:
    enabled_profile_ids: frozenset[CommandProfileId] = frozenset({"python.ruff.v1"})

    def __init__(self, profile: CommandProfile) -> None:
        self._profile = profile
        self.calls = 0

    def run(self, snapshot: WorkspaceCommandSnapshot) -> WorkspaceCommandRead:
        del snapshot
        self.calls += 1
        return _command_result(self._profile)


class FakeArtifacts:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, bool]] = []

    async def build_html_node(
        self,
        run_id: str,
        *,
        node_id: str,
        local_key: str | None = None,
        defer_task_loop_edge: bool = False,
    ) -> TaskWorkspaceRead:
        self.calls.append(("artifact", run_id, node_id, defer_task_loop_edge))
        revision = ArtifactRevisionRead(
            revision_id=f"arv_{'1' * 64}",
            artifact_id=f"art_{'2' * 64}",
            revision_no=1,
            media_type="text/html",
            content_digest="3" * 64,
            byte_count=10,
            patch_receipt_id=f"prc_{'4' * 64}",
            created_at=datetime(2026, 8, 24, tzinfo=UTC),
        )
        return TaskWorkspaceRead(
            workspace_id=f"wsp_{'5' * 64}",
            task_id=TASK_ID,
            run_id=run_id,
            allowed_extensions=(".html",),
            max_total_bytes=1_000,
            max_files=2,
            status="active",
            artifacts=(
                ArtifactRead(
                    artifact_id=revision.artifact_id,
                    relative_path="index.html",
                    active_revision=revision,
                ),
            ),
            created_at=datetime(2026, 8, 24, tzinfo=UTC),
            updated_at=datetime(2026, 8, 24, tzinfo=UTC),
        )

    async def verify_browser_node(
        self,
        run_id: str,
        *,
        node_id: str,
        local_key: str | None = None,
        defer_task_loop_edge: bool = False,
    ) -> BrowserRenderRunRead:
        self.calls.append(("browser", run_id, node_id, defer_task_loop_edge))
        return BrowserRenderRunRead(
            browser_run_id=f"brr_{'6' * 64}",
            run_id=run_id,
            node_id=node_id,
            revision_id=f"arv_{'1' * 64}",
            status="passed",
            engine="fake-browser",
            profile_id="deskpilot.browser-static-html.v1",
            viewport_width=1280,
            viewport_height=720,
            title="Report",
            heading_count=1,
            link_count=0,
            external_request_count=0,
            console_error_count=0,
            page_error_count=0,
            issue_codes=(),
            dom_digest="7" * 64,
            screenshot_digest="8" * 64,
            evidence_digest="9" * 64,
            created_at=datetime(2026, 8, 24, tzinfo=UTC),
            completed_at=datetime(2026, 8, 24, tzinfo=UTC),
        )


def _registry_fixtures() -> tuple[
    object,
    FakeKnowledge,
    FakeMcp,
    FakeWorkspace,
    FakeCheckRuntime,
    FakePythonRuntime,
    FakeNodeRuntime,
]:
    knowledge = FakeKnowledge()
    mcp = FakeMcp()
    workspace = FakeWorkspace()
    checks = FakeCheckRuntime()
    python = FakePythonRuntime()
    node = FakeNodeRuntime()
    registry = create_builtin_capability_executor_registry(
        create_builtin_capability_catalog(),
        knowledge=knowledge,
        mcp=mcp,
        workspace=workspace,
        workspace_checks=checks,
        python_tests=python,
        node_tests=node,
    )
    return registry, knowledge, mcp, workspace, checks, python, node


def _bound(
    capability_id: str,
    arguments: BaseModelInput,
    *,
    digit: str,
    dependencies: tuple[VerifiedCapabilityResultRef, ...] = (),
    consumed: tuple[VerifiedCapabilityResultRef, ...] = (),
) -> BoundCapabilityInput:
    capability = _ref(capability_id)
    values: dict[str, Any] = {
        "schema_version": "deskpilot.bound-capability-input.v1",
        "task_id": TASK_ID,
        "node_id": f"pnd_{digit * 64}",
        "node_spec_digest": ("a" if digit != "a" else "b") * 64,
        "node_binding_id": f"mnb_{digit * 64}",
        "node_binding_digest": "c" * 64,
        "effective_authority_digest": "d" * 64,
        "runtime_eligibility_digest": "e" * 64,
        "capability": capability,
        "source_step_binding_id": f"mps_{digit * 64}",
        "source_step_binding_digest": "3" * 64,
        "source_offer_key": f"ofk_{digit * 64}",
        "route_id": "test_exact_readonly",
        "route_version": "2",
        "route_manifest_digest": "4" * 64,
        "parameter_bindings_digest": "5" * 64,
        "arguments": arguments,
        "arguments_digest": sha256_digest(arguments),
        "dependency_result_refs": dependencies,
        "consumed_result_refs": consumed,
    }
    return BoundCapabilityInput.model_validate({**values, "binding_digest": sha256_digest(values)})


BaseModelInput = (
    KnowledgeLocalExecutorInput
    | McpTextMetricsExecutorInput
    | WorkspaceSnapshotCheckExecutorInput
    | WorkspacePythonTestExecutorInput
    | WorkspaceNodeTestExecutorInput
    | WorkspaceCommandExecutorInput
    | WorkspaceProjectSearchExecutorInput
    | WorkspaceProjectBatchReadExecutorInput
    | ArtifactHtmlExecutorInput
    | BrowserVerifyExecutorInput
)


@pytest.mark.asyncio
async def test_project_coding_adapters_execute_and_verify_through_generic_engine(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "src" / "a.py").write_bytes(b"needle\n")
    (project / "README.md").write_bytes(b"DeskPilot\n")
    coding = WorkspaceCodingRuntime(
        WorkspaceFileRuntime(str(tmp_path)),
        shutil.which("git"),
    )
    registry = create_builtin_capability_executor_registry(
        create_builtin_capability_catalog(),
        workspace_coding=coding,
    )
    engine = CapabilityExecutionEngine(registry)
    cases = (
        (
            "workspace.project.search.v1",
            WorkspaceProjectSearchExecutorInput(project_path="project", query="needle"),
            "a",
            CapabilityResultKind.PROJECT_SEARCH,
        ),
        (
            "workspace.project.read_many.v1",
            WorkspaceProjectBatchReadExecutorInput(
                project_path="project",
                paths=("README.md", "src/a.py"),
            ),
            "b",
            CapabilityResultKind.PROJECT_BATCH_READ,
        ),
    )

    for capability_id, arguments, digit, result_kind in cases:
        bound = _bound(capability_id, arguments, digit=digit)
        context = _context(bound)
        candidate = await engine.execute_candidate(context, bound)
        verified = await engine.verify_candidate(context, bound, candidate)

        assert candidate.result_kind is result_kind
        assert verified.candidate == candidate
        assert verified.adapter_verification.result_digest == candidate.result_digest

    manifests = {item.capability.capability_id: item for item in registry.manifests()}
    assert {
        "workspace.project.search.v1",
        "workspace.project.read_many.v1",
    } <= set(manifests)
    assert all(
        manifests[capability_id].effect_class is CapabilityEffectClass.READ_ONLY
        for capability_id in (
            "workspace.project.search.v1",
            "workspace.project.read_many.v1",
        )
    )


@pytest.mark.asyncio
async def test_command_profile_adapter_verifies_failure_receipt_for_generic_repair() -> None:
    profiles = CommandProfileCatalog()
    profile = profiles.resolve("python.ruff.v1")
    snapshots = FakeCommandSnapshots()
    runtime = FakeCommandRuntime(profile)
    registry = create_builtin_capability_executor_registry(
        create_builtin_capability_catalog(),
        command_profiles=profiles,
        command_snapshots=snapshots,
        command_runtime=runtime,
    )
    engine = CapabilityExecutionEngine(registry)
    arguments = WorkspaceCommandExecutorInput(
        project_path="backend",
        command_profile_id="python.ruff.v1",
    )
    bound = _bound("workspace.command.run.v1", arguments, digit="d")
    context = _context(bound)

    candidate = await engine.execute_candidate(context, bound)
    verified = await engine.verify_candidate(context, bound, candidate)

    assert snapshots.calls == [("backend", "python.ruff.v1")]
    assert runtime.calls == 1
    assert candidate.result_kind is CapabilityResultKind.COMMAND_PROFILE
    assert candidate.output_manifest["status"] == "failed"
    assert verified.adapter_verification.result_digest == candidate.result_digest
    manifest = registry.resolve(bound.capability).manifest
    assert manifest.effect_class is CapabilityEffectClass.READ_ONLY
    assert manifest.recovery_policy is CapabilityRecoveryPolicy.NO_AUTOMATIC_REPLAY


def _context(bound: BoundCapabilityInput) -> CapabilityExecutionContext:
    return CapabilityExecutionContext.build(
        task_id=bound.task_id,
        run_id=f"run_{'6' * 64}",
        plan_id=f"epl_{'7' * 64}",
        plan_generation=1,
        plan_manifest_digest="8" * 64,
        node_id=bound.node_id,
        node_kind=DraftNodeKind.CAPABILITY,
        node_spec_digest=bound.node_spec_digest,
        node_binding_id=bound.node_binding_id,
        node_binding_digest=bound.node_binding_digest,
        effective_authority_digest=bound.effective_authority_digest,
        runtime_eligibility_digest=bound.runtime_eligibility_digest,
        node_attempt=1,
        claim_owner_id="task-loop-capability-test",
        claim_fencing_token=1,
        capability=bound.capability,
        step_input_digest=bound.binding_digest,
        upstream_result_refs=bound.dependency_result_refs,
        consumed_result_refs=bound.consumed_result_refs,
        budget=PlanNodeBudget(
            model_calls=0,
            tool_calls=1,
            input_tokens=0,
            output_tokens=0,
            wall_seconds=60,
            retries=0,
            cost_micros=0,
            handoffs=0,
        ),
    )


@pytest.mark.asyncio
async def test_readonly_adapters_execute_then_verify_distinct_candidate() -> None:
    registry, knowledge, mcp, workspace, checks, python, node = _registry_fixtures()
    engine = CapabilityExecutionEngine(cast(Any, registry))
    cases = (
        (
            "knowledge.local.v1",
            KnowledgeLocalExecutorInput(query="cats"),
            "1",
            CapabilityResultKind.KNOWLEDGE,
        ),
        (
            "mcp.text.metrics.v1",
            McpTextMetricsExecutorInput(text="one two"),
            "2",
            CapabilityResultKind.MCP,
        ),
        (
            "workspace.snapshot.check.v1",
            WorkspaceSnapshotCheckExecutorInput(profile="python-syntax", path="src/app.py"),
            "3",
            CapabilityResultKind.WORKSPACE_CHECK,
        ),
        (
            "workspace.python.test.v1",
            WorkspacePythonTestExecutorInput(project_path="backend", test_path="tests/test_one.py"),
            "4",
            CapabilityResultKind.PYTHON_TEST,
        ),
        (
            "workspace.node.test.v1",
            WorkspaceNodeTestExecutorInput(project_path="frontend", test_path="src/app.test.ts"),
            "5",
            CapabilityResultKind.NODE_TEST,
        ),
    )
    for capability_id, arguments, digit, result_kind in cases:
        bound = _bound(capability_id, arguments, digit=digit)
        context = _context(bound)

        candidate = await engine.execute_candidate(context, bound)

        assert isinstance(candidate, CapabilityExecutionCandidate)
        assert not isinstance(candidate, VerifiedCapabilityOutput)
        assert candidate.result_kind is result_kind
        assert "result_ref" not in candidate.model_dump(mode="json")

        verified = await engine.verify_candidate(context, bound, candidate)

        assert isinstance(verified, VerifiedCapabilityOutput)
        assert verified.candidate == candidate
        assert verified.adapter_verification.result_digest == candidate.result_digest
        assert len(verified.verification_digest) == 64

    assert knowledge.calls == [("cats", 10)]
    assert mcp.calls == [(TOOL_NAME, {"text": "one two"})]
    assert mcp.audit_calls == [(0, 200)]
    assert workspace.calls == [
        ("check", "python-syntax", "src/app.py"),
        ("python", "backend", "tests/test_one.py"),
        ("node", "frontend", "src/app.test.ts"),
    ]
    assert (checks.calls, python.calls, node.calls) == (1, 1, 1)


@pytest.mark.asyncio
async def test_artifact_and_browser_adapters_consume_exact_verified_results() -> None:
    artifacts = FakeArtifacts()
    capabilities = create_builtin_capability_catalog(research_runtime_enabled=True)
    registry = create_builtin_capability_executor_registry(
        capabilities,
        artifacts=artifacts,
    )
    engine = CapabilityExecutionEngine(registry)
    claims_ref = VerifiedCapabilityResultRef.build(
        task_id=TASK_ID,
        run_id=f"run_{'6' * 64}",
        plan_generation=1,
        producer_node_id=f"pnd_{'a' * 64}",
        producer_attempt=1,
        capability=CapabilityRef(
            capability_id="research.read.v1",
            version="1.1.0",
            digest=capabilities.resolve_preferred("research.read.v1").digest,
        ),
        result_kind=CapabilityResultKind.VERIFIED_CLAIMS,
        result_schema_digest="b" * 64,
        result_digest="c" * 64,
        verification_digest="d" * 64,
    )
    artifact_bound = _bound(
        "artifact.html.v1",
        ArtifactHtmlExecutorInput(
            verified_claims_digest=claims_ref.result_digest,
        ),
        digit="b",
        dependencies=(claims_ref,),
        consumed=(claims_ref,),
    )
    artifact_context = _context(artifact_bound)

    artifact_candidate = await engine.execute_candidate(
        artifact_context,
        artifact_bound,
    )
    artifact_verified = await engine.verify_candidate(
        artifact_context,
        artifact_bound,
        artifact_candidate,
    )

    artifact_output = ArtifactHtmlCapabilityOutput.model_validate(
        artifact_candidate.output_manifest
    )
    assert artifact_output.workspace.run_id == artifact_context.run_id
    assert artifact_verified.candidate == artifact_candidate
    artifact_ref = VerifiedCapabilityResultRef.build(
        task_id=TASK_ID,
        run_id=artifact_context.run_id,
        plan_generation=1,
        producer_node_id=artifact_context.node_id,
        producer_attempt=1,
        capability=artifact_context.capability,
        result_kind=CapabilityResultKind.ARTIFACT,
        result_schema_digest=artifact_candidate.output_schema_digest,
        result_digest=artifact_candidate.result_digest,
        verification_digest=artifact_verified.verification_digest,
    )
    browser_bound = _bound(
        "browser.verify.v1",
        BrowserVerifyExecutorInput(artifact_digest=artifact_ref.result_digest),
        digit="c",
        dependencies=(artifact_ref,),
        consumed=(artifact_ref,),
    )
    browser_context = _context(browser_bound)

    browser_candidate = await engine.execute_candidate(browser_context, browser_bound)
    browser_verified = await engine.verify_candidate(
        browser_context,
        browser_bound,
        browser_candidate,
    )

    browser_output = BrowserVerifyCapabilityOutput.model_validate(
        browser_candidate.output_manifest
    )
    assert browser_output.browser.status == "passed"
    assert browser_verified.candidate == browser_candidate
    assert artifacts.calls == [
        ("artifact", artifact_context.run_id, artifact_context.node_id, True),
        ("browser", browser_context.run_id, browser_context.node_id, True),
    ]
    manifests = {item.capability.capability_id: item for item in registry.manifests()}
    assert manifests["artifact.html.v1"].consumes == (
        CapabilityResultKind.VERIFIED_CLAIMS,
    )
    assert manifests["artifact.html.v1"].effect_class is CapabilityEffectClass.WORKSPACE_WRITE
    assert manifests["browser.verify.v1"].consumes == (
        CapabilityResultKind.ARTIFACT,
    )


def test_builtin_registration_is_exact_readonly_and_schema_sealed() -> None:
    registry, *_ = _registry_fixtures()
    manifests = cast(Any, registry).manifests()

    assert {(item.capability.capability_id, item.capability.version) for item in manifests} == {
        ("knowledge.local.v1", "1.0.0"),
        ("mcp.text.metrics.v1", "1.0.0"),
        ("workspace.snapshot.check.v1", "1.0.0"),
        ("workspace.python.test.v1", "1.0.0"),
        ("workspace.node.test.v1", "1.0.0"),
    }
    assert all(item.effect_class is CapabilityEffectClass.READ_ONLY for item in manifests)
    assert all(
        item.approval_requirement is CapabilityApprovalRequirement.NONE for item in manifests
    )
    assert all(item.node_kinds == (DraftNodeKind.CAPABILITY,) for item in manifests)
    assert all(item.consumes == () for item in manifests)
    mcp = next(item for item in manifests if item.capability.capability_id == "mcp.text.metrics.v1")
    assert mcp.recovery_policy is CapabilityRecoveryPolicy.NO_AUTOMATIC_REPLAY
    for manifest in manifests:
        properties = manifest.input_schema.get("properties", {})
        assert not set(properties).intersection(
            {"argv", "cwd", "env", "approval", "result_ref", "capability_ref"}
        )


def test_disabled_concrete_runtime_is_not_eligible_for_exact_resolution() -> None:
    capabilities = create_builtin_capability_catalog()
    registry = create_builtin_capability_executor_registry(
        capabilities,
        workspace=FakeWorkspace(),
        workspace_checks=FakeCheckRuntime(enabled=False),
    )

    with pytest.raises(UnknownCapabilityExecutorError):
        registry.resolve(_ref("workspace.snapshot.check.v1"))


@pytest.mark.asyncio
async def test_engine_rejects_candidate_or_context_binding_drift() -> None:
    registry, *_ = _registry_fixtures()
    engine = CapabilityExecutionEngine(cast(Any, registry))
    bound = _bound(
        "knowledge.local.v1",
        KnowledgeLocalExecutorInput(query="cats"),
        digit="9",
    )
    context = _context(bound)
    candidate = await engine.execute_candidate(context, bound)

    with pytest.raises(CapabilityCandidateBindingRejectedError):
        await engine.verify_candidate(
            context,
            bound,
            candidate.model_copy(update={"arguments_digest": "0" * 64}),
        )

    with pytest.raises(Exception, match="server-bound capability input"):
        await engine.execute_candidate(
            context.model_copy(update={"step_input_digest": "0" * 64}),
            bound,
        )


@pytest.mark.asyncio
async def test_disabled_adapter_fails_closed_even_after_registration() -> None:
    runtime = FakeCheckRuntime(enabled=True)
    registry = create_builtin_capability_executor_registry(
        create_builtin_capability_catalog(),
        workspace=FakeWorkspace(),
        workspace_checks=runtime,
    )
    bound = _bound(
        "workspace.snapshot.check.v1",
        WorkspaceSnapshotCheckExecutorInput(profile="python-syntax", path="src/app.py"),
        digit="8",
    )
    runtime.enabled = False

    with pytest.raises(BuiltinCapabilityRuntimeUnavailableError):
        await CapabilityExecutionEngine(registry).execute_candidate(_context(bound), bound)
