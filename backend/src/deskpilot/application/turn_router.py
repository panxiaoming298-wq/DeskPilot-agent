"""Trusted, deliberately small Conversation Turn router for phase 78."""

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import UTC, timedelta
from typing import Any, Literal, Protocol, cast

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from deskpilot.application.knowledge_base import (
    KnowledgeProofRejectedError,
    KnowledgeSourceError,
    LocalKnowledgeBase,
)
from deskpilot.application.mcp_control_plane import (
    McpControlError,
    McpControlPlane,
    McpServerDisabledError,
)
from deskpilot.application.verified_edges import mark_verified_and_unlock
from deskpilot.application.workspace_check_runtime import WorkspaceCheckError
from deskpilot.application.workspace_file_runtime import (
    WorkspaceFileError,
    WorkspaceFileRuntime,
    WorkspacePatchPartialError,
)
from deskpilot.application.workspace_node_test_runtime import WorkspaceNodeTestError
from deskpilot.application.workspace_python_test_runtime import WorkspacePythonTestError
from deskpilot.core.canonical_json import canonical_json_bytes, sha256_digest
from deskpilot.domain.agent_runtime import ExecutionNodeStatus, ExecutionRunStatus
from deskpilot.domain.knowledge import KnowledgeSearchRead
from deskpilot.domain.mcp import McpToolCallRead
from deskpilot.domain.task_workbench import (
    TurnRouteDecision,
    TurnRouteRead,
    TurnRouteStatus,
)
from deskpilot.domain.workspace_files import (
    WorkspaceCheckRead,
    WorkspaceDirectoryRead,
    WorkspaceEditPreview,
    WorkspaceEditReceipt,
    WorkspaceFileRead,
    WorkspaceNodeTestRead,
    WorkspaceNodeTestSnapshot,
    WorkspacePatchPreview,
    WorkspacePatchReceipt,
    WorkspacePatchTestRead,
    WorkspacePathOperationPreview,
    WorkspacePathOperationReceipt,
    WorkspacePythonTestRead,
    WorkspacePythonTestSnapshot,
)
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import (
    ConversationMessageRecord,
    TaskExecutionNodeRecord,
    TaskExecutionRunRecord,
    TurnRouteRecord,
    utc_now,
)
from deskpilot.tools.workspace_checks import WorkspaceCheckInput

RouteId = Literal[
    "research_to_html",
    "knowledge_lookup",
    "mcp_text_metrics",
    "workspace_file_read",
    "workspace_file_replace",
    "workspace_patch_bundle",
    "workspace_agent_patch_test",
    "workspace_dynamic_patch_test",
    "workspace_file_create",
    "workspace_file_rename",
    "workspace_directory_list",
    "workspace_directory_analyze",
    "workspace_snapshot_check",
    "workspace_python_test",
    "workspace_node_test",
]
CLASSIFIER_VERSION = "deskpilot.turn-router.rules.v5"
_LEGACY_CLASSIFIER_VERSIONS = (
    "deskpilot.turn-router.rules.v1",
    "deskpilot.turn-router.rules.v2",
    "deskpilot.turn-router.rules.v3",
    "deskpilot.turn-router.rules.v4",
)
_ROUTE_NODES = {
    "knowledge_lookup": "knowledge_lookup",
    "mcp_text_metrics": "mcp_text_metrics",
    "workspace_file_read": "workspace_file_read",
    "workspace_file_replace": "workspace_file_replace",
    "workspace_patch_bundle": "workspace_patch_bundle",
    "workspace_agent_patch_test": "workspace_agent_patch_test",
    "workspace_dynamic_patch_test": "workspace_dynamic_patch_test",
    "workspace_file_create": "workspace_file_create",
    "workspace_file_rename": "workspace_file_rename",
    "workspace_directory_list": "workspace_directory_list",
    "workspace_directory_analyze": "workspace_directory_analyze",
    "workspace_snapshot_check": "workspace_snapshot_check",
    "workspace_python_test": "workspace_python_test",
    "workspace_node_test": "workspace_node_test",
}
_ROUTE_SPECS: dict[RouteId, dict[str, object]] = {
    "research_to_html": {
        "route_id": "research_to_html",
        "version": "1",
        "producer_ref": "research_to_html.v1",
        "capabilities": ("research.read.v1", "artifact.html.v1", "browser.verify.v1"),
        "max_risk": "R1",
    },
    "knowledge_lookup": {
        "route_id": "knowledge_lookup",
        "version": "1",
        "producer_ref": "knowledge_lookup.v1",
        "capabilities": ("knowledge.local.v1",),
        "max_risk": "R0",
    },
    "mcp_text_metrics": {
        "route_id": "mcp_text_metrics",
        "version": "1",
        "producer_ref": "mcp_text_metrics.v1",
        "capabilities": ("mcp.text.metrics.v1",),
        "max_risk": "R0",
    },
    "workspace_file_read": {
        "route_id": "workspace_file_read",
        "version": "1",
        "producer_ref": "workspace_file_read.v1",
        "capabilities": ("workspace.file.read.v1",),
        "max_risk": "R0",
    },
    "workspace_file_replace": {
        "route_id": "workspace_file_replace",
        "version": "1",
        "producer_ref": "workspace_file_replace.v1",
        "capabilities": ("workspace.file.replace.v1",),
        "max_risk": "R1",
    },
    "workspace_patch_bundle": {
        "route_id": "workspace_patch_bundle",
        "version": "1",
        "producer_ref": "workspace_patch_bundle.v1",
        "capabilities": ("workspace.patch.bundle.v1",),
        "max_risk": "R1",
    },
    "workspace_agent_patch_test": {
        "route_id": "workspace_agent_patch_test",
        "version": "1",
        "producer_ref": "workspace_agent_patch_test.v1",
        "capabilities": (
            "workspace.file.read.v1",
            "workspace.patch.propose.v1",
            "workspace.patch.bundle.v1",
            "workspace.python.test.v1",
            "workspace.node.test.v1",
        ),
        "max_risk": "R1",
    },
    "workspace_dynamic_patch_test": {
        "route_id": "workspace_dynamic_patch_test",
        "version": "1",
        "producer_ref": "workspace_dynamic_patch_test.v1",
        "capabilities": (
            "workspace.directory.read.v1",
            "workspace.file.read.v1",
            "workspace.patch.propose.v1",
            "workspace.patch.bundle.v1",
            "workspace.python.test.v1",
            "workspace.node.test.v1",
        ),
        "max_risk": "R1",
    },
    "workspace_file_create": {
        "route_id": "workspace_file_create",
        "version": "1",
        "producer_ref": "workspace_file_create.v1",
        "capabilities": ("workspace.file.create.v1",),
        "max_risk": "R1",
    },
    "workspace_file_rename": {
        "route_id": "workspace_file_rename",
        "version": "1",
        "producer_ref": "workspace_file_rename.v1",
        "capabilities": ("workspace.file.rename.v1",),
        "max_risk": "R1",
    },
    "workspace_directory_list": {
        "route_id": "workspace_directory_list",
        "version": "1",
        "producer_ref": "workspace_directory_list.v1",
        "capabilities": ("workspace.directory.read.v1",),
        "max_risk": "R0",
    },
    "workspace_directory_analyze": {
        "route_id": "workspace_directory_analyze",
        "version": "1",
        "producer_ref": "workspace_directory_analyze.v1",
        "capabilities": (
            "workspace.directory.read.v1",
            "workspace.file.read.v1",
        ),
        "max_risk": "R0",
    },
    "workspace_snapshot_check": {
        "route_id": "workspace_snapshot_check",
        "version": "1",
        "producer_ref": "workspace_snapshot_check.v1",
        "capabilities": ("workspace.snapshot.check.v1",),
        "max_risk": "R0",
    },
    "workspace_python_test": {
        "route_id": "workspace_python_test",
        "version": "1",
        "producer_ref": "workspace_python_test.v1",
        "capabilities": ("workspace.python.test.v1",),
        "max_risk": "R0",
    },
    "workspace_node_test": {
        "route_id": "workspace_node_test",
        "version": "1",
        "producer_ref": "workspace_node_test.v1",
        "capabilities": ("workspace.node.test.v1",),
        "max_risk": "R0",
    },
}

_WORKSPACE_EDIT_PATTERN = re.compile(
    r"\s*(?:请)?在工作区文件\s+"
    r'(?:"(?P<path_ascii>[^\"]+)"|“(?P<path_cn>[^”]+)”|(?P<path_plain>\S+))'
    r'\s+中把\s+(?:"(?P<old_ascii>[\s\S]+?)"|“(?P<old_cn>[\s\S]+?)”)'
    r'\s+替换为\s+(?:"(?P<new_ascii>[\s\S]*?)"|“(?P<new_cn>[\s\S]*?)”)\s*'
)


class TurnRouterError(RuntimeError):
    code = "TURN_ROUTE_REJECTED"


class TurnRouteProofRejectedError(TurnRouterError):
    code = "TURN_ROUTE_PROOF_REJECTED"


class TurnRouteConflictError(TurnRouterError):
    code = "TURN_ROUTE_CONFLICT"


@dataclass(frozen=True)
class RouteCandidate:
    decision: TurnRouteDecision
    route_id: RouteId | None
    parameters: dict[str, str]
    reason_code: str


@dataclass(frozen=True)
class FollowupResolution:
    candidate: RouteCandidate
    source: TurnRouteRead
    rule: str


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


class TurnRouter:
    def __init__(
        self,
        database: Database,
        knowledge: LocalKnowledgeBase,
        mcp: McpControlPlane,
        workspace_files: WorkspaceFileRuntime,
        workspace_checks: WorkspaceCheckPort | None = None,
        workspace_python_tests: WorkspacePythonTestPort | None = None,
        workspace_node_tests: WorkspaceNodeTestPort | None = None,
    ) -> None:
        self._database = database
        self._knowledge = knowledge
        self._mcp = mcp
        self._workspace_files = workspace_files
        self._workspace_checks = workspace_checks
        self._workspace_python_tests = workspace_python_tests
        self._workspace_node_tests = workspace_node_tests

    @property
    def workspace_enabled(self) -> bool:
        return self._workspace_files.enabled

    @property
    def workspace_patch_enabled(self) -> bool:
        return self._workspace_files.patch_enabled

    @property
    def workspace_path_operation_enabled(self) -> bool:
        return self._workspace_files.path_operation_enabled

    @property
    def workspace_check_enabled(self) -> bool:
        return bool(self._workspace_checks is not None and self._workspace_checks.enabled)

    @property
    def workspace_python_test_enabled(self) -> bool:
        return bool(
            self._workspace_python_tests is not None and self._workspace_python_tests.enabled
        )

    @property
    def workspace_node_test_enabled(self) -> bool:
        return bool(self._workspace_node_tests is not None and self._workspace_node_tests.enabled)

    @staticmethod
    def classify(message: str) -> RouteCandidate:
        workspace_candidate = TurnRouter._workspace_candidate(message)
        if workspace_candidate is not None:
            return workspace_candidate
        text = " ".join(message.strip().split())
        folded = text.casefold()
        knowledge = any(
            token in folded for token in ("知识库", "本地知识", "资料库", "已导入文档", "文档里")
        )
        metrics = any(
            token in folded
            for token in (
                "字符数",
                "字数",
                "行数",
                "词数",
                "文本统计",
                "text metrics",
                "character count",
                "word count",
            )
        ) and any(token in folded for token in ("统计", "计算", "count", "metrics"))
        natural_research_goal = (
            None if knowledge or metrics else TurnRouter._natural_research_goal(text)
        )
        research = natural_research_goal is not None or any(
            token in folded
            for token in (
                "研究",
                "调研",
                "公开来源",
                "联网查询",
                "搜索网页",
                "html 报告",
                "html页面",
                "html 页面",
                "markdown 报告",
                "markdown报告",
                "pdf 报告",
                "pdf报告",
            )
        )
        matched = sum((knowledge, metrics, research))
        if matched > 1:
            return RouteCandidate(
                decision=TurnRouteDecision.NEEDS_CLARIFICATION,
                route_id=None,
                parameters={},
                reason_code="MULTIPLE_ROUTES_MATCHED",
            )
        if metrics:
            payload = TurnRouter._metrics_payload(message)
            if payload is None:
                return RouteCandidate(
                    decision=TurnRouteDecision.NEEDS_CLARIFICATION,
                    route_id=None,
                    parameters={},
                    reason_code="MCP_TEXT_MISSING",
                )
            if len(payload) > 4_096:
                return RouteCandidate(
                    decision=TurnRouteDecision.UNSUPPORTED,
                    route_id=None,
                    parameters={},
                    reason_code="MCP_TEXT_TOO_LONG",
                )
            return RouteCandidate(
                decision=TurnRouteDecision.ROUTED,
                route_id="mcp_text_metrics",
                parameters={"text": payload},
                reason_code="MCP_TEXT_METRICS_MATCHED",
            )
        if knowledge:
            query = re.sub(
                r"^(?:请|帮我|请帮我)?(?:从|在|查询|搜索|查找|查一下)*"
                r"(?:本地)?(?:知识库|知识|资料库|已导入文档|文档里)(?:中|里)?[，,:：\s]*",
                "",
                text,
                count=1,
            ).strip()
            if not query:
                return RouteCandidate(
                    decision=TurnRouteDecision.NEEDS_CLARIFICATION,
                    route_id=None,
                    parameters={},
                    reason_code="KNOWLEDGE_QUERY_MISSING",
                )
            return RouteCandidate(
                decision=TurnRouteDecision.ROUTED,
                route_id="knowledge_lookup",
                parameters={"query": query},
                reason_code="KNOWLEDGE_LOOKUP_MATCHED",
            )
        if research:
            if natural_research_goal is None and TurnRouter._research_goal_missing(text):
                return RouteCandidate(
                    decision=TurnRouteDecision.NEEDS_CLARIFICATION,
                    route_id=None,
                    parameters={},
                    reason_code="RESEARCH_GOAL_MISSING",
                )
            return RouteCandidate(
                decision=TurnRouteDecision.ROUTED,
                route_id="research_to_html",
                parameters={"goal": natural_research_goal or text},
                reason_code="RESEARCH_TO_HTML_MATCHED",
            )
        if len(text) < 4 or folded in {"继续", "处理一下", "帮我做", "开始"}:
            return RouteCandidate(
                decision=TurnRouteDecision.NEEDS_CLARIFICATION,
                route_id=None,
                parameters={},
                reason_code="OBJECTIVE_MISSING",
            )
        return RouteCandidate(
            decision=TurnRouteDecision.UNSUPPORTED,
            route_id=None,
            parameters={},
            reason_code="NO_TRUSTED_ROUTE",
        )

    @staticmethod
    def _workspace_candidate(message: str) -> RouteCandidate | None:
        dynamic_patch = re.fullmatch(
            r"\s*(?:请)?多\s*Agent\s*修复并测试工作区\s*[：:]\s*"
            r"目录\s*[：:]\s*(?P<directory>\"[^\"]+\"|“[^”]+”|\S+)\s+"
            r"文件\s*[：:]\s*(?P<file>\[[^\]\r\n]+\]|\"[^\"]+\"|“[^”]+”|\S+)\s+"
            r"(?P<kind>Python|Node)\s*项目\s*[：:]\s*"
            r"(?P<project>\"[^\"]+\"|“[^”]+”|\S+)\s+"
            r"(?P=kind)\s*测试\s*[：:]\s*"
            r"(?P<test>\"[^\"]+\"|“[^”]+”|\S+)\s+"
            r"目标\s*[：:]\s*(?P<objective>[\s\S]+?)\s*",
            message,
            flags=re.IGNORECASE,
        )
        if dynamic_patch is not None:
            kind = dynamic_patch.group("kind").casefold()
            dynamic_patch_paths = TurnRouter._patch_paths(dynamic_patch.group("file"))
            if dynamic_patch_paths is None:
                return RouteCandidate(
                    decision=TurnRouteDecision.NEEDS_CLARIFICATION,
                    route_id=None,
                    parameters={},
                    reason_code="WORKSPACE_COMMAND_INVALID",
                )
            return RouteCandidate(
                decision=TurnRouteDecision.ROUTED,
                route_id="workspace_dynamic_patch_test",
                parameters={
                    "path": TurnRouter._unquote_path(dynamic_patch.group("directory")),
                    "patch_path": dynamic_patch_paths[0],
                    "patch_paths_json": canonical_json_bytes(
                        {"paths": list(dynamic_patch_paths)}
                    ).decode("utf-8"),
                    "project_path": TurnRouter._unquote_path(dynamic_patch.group("project")),
                    "test_path": TurnRouter._unquote_path(dynamic_patch.group("test")),
                    "test_kind": kind,
                    "objective": TurnRouter._unquote_path(dynamic_patch.group("objective")),
                },
                reason_code="WORKSPACE_DYNAMIC_PATCH_TEST_MATCHED",
            )
        agent_patch = re.fullmatch(
            r"\s*(?:请)?修复并测试工作区\s*[：:]\s*"
            r"文件\s*[：:]\s*(?P<file>\"[^\"]+\"|“[^”]+”|\S+)\s+"
            r"(?P<kind>Python|Node)\s*项目\s*[：:]\s*"
            r"(?P<project>\"[^\"]+\"|“[^”]+”|\S+)\s+"
            r"(?P=kind)\s*测试\s*[：:]\s*"
            r"(?P<test>\"[^\"]+\"|“[^”]+”|\S+)\s+"
            r"目标\s*[：:]\s*(?P<objective>[\s\S]+?)\s*",
            message,
            flags=re.IGNORECASE,
        )
        if agent_patch is not None:
            kind = agent_patch.group("kind").casefold()
            return RouteCandidate(
                decision=TurnRouteDecision.ROUTED,
                route_id="workspace_agent_patch_test",
                parameters={
                    "path": TurnRouter._unquote_path(agent_patch.group("file")),
                    "project_path": TurnRouter._unquote_path(agent_patch.group("project")),
                    "test_path": TurnRouter._unquote_path(agent_patch.group("test")),
                    "test_kind": kind,
                    "objective": TurnRouter._unquote_path(agent_patch.group("objective")),
                },
                reason_code="WORKSPACE_AGENT_PATCH_TEST_MATCHED",
            )
        directory_tests = re.fullmatch(
            r"\s*(?:请)?分析并测试工作区\s*[：:]\s*"
            r'(?P<directory>"[^"]+"|“[^”]+”|\S+)\s+'
            r"Python\s*项目\s*[：:]\s*"
            r'(?P<python_project>"[^"]+"|“[^”]+”|\S+)\s+'
            r"Python\s*测试\s*[：:]\s*"
            r'(?P<python_test>"[^"]+"|“[^”]+”|\S+)\s+'
            r"Node\s*项目\s*[：:]\s*"
            r'(?P<node_project>"[^"]+"|“[^”]+”|\S+)\s+'
            r"Node\s*测试\s*[：:]\s*"
            r'(?P<node_test>"[^"]+"|“[^”]+”|\S+)\s*',
            message,
            flags=re.IGNORECASE,
        )
        if directory_tests is not None:
            return RouteCandidate(
                decision=TurnRouteDecision.ROUTED,
                route_id="workspace_directory_analyze",
                parameters={
                    "path": TurnRouter._unquote_path(directory_tests.group("directory")),
                    "python_project_path": TurnRouter._unquote_path(
                        directory_tests.group("python_project")
                    ),
                    "python_test_path": TurnRouter._unquote_path(
                        directory_tests.group("python_test")
                    ),
                    "node_project_path": TurnRouter._unquote_path(
                        directory_tests.group("node_project")
                    ),
                    "node_test_path": TurnRouter._unquote_path(directory_tests.group("node_test")),
                },
                reason_code="WORKSPACE_DIRECTORY_TEST_GRAPH_MATCHED",
            )
        directory_analysis = re.fullmatch(
            r"\s*(?:请)?分析工作区目录\s*[：:]\s*"
            r'(?P<directory>"[^"]+"|“[^”]+”|\S+)\s+'
            r"文件\s*[：:]\s*"
            r'(?P<file>"[^"]+"|“[^”]+”|\S+)\s*',
            message,
        )
        if directory_analysis is not None:
            return RouteCandidate(
                decision=TurnRouteDecision.ROUTED,
                route_id="workspace_directory_analyze",
                parameters={
                    "path": TurnRouter._unquote_path(directory_analysis.group("directory")),
                    "file_path": TurnRouter._unquote_path(directory_analysis.group("file")),
                },
                reason_code="WORKSPACE_DIRECTORY_ANALYZE_MATCHED",
            )
        node_test = re.fullmatch(
            r"\s*(?:请)?运行\s*Node\s*测试\s*[：:]\s*"
            r'(?:"(?P<project_ascii>[^\"]+)"|“(?P<project_cn>[^”]+)”|(?P<project_plain>\S+))\s+'
            r'(?:"(?P<test_ascii>[^\"]+)"|“(?P<test_cn>[^”]+)”|(?P<test_plain>\S+))\s*',
            message,
            flags=re.IGNORECASE,
        )
        if node_test is not None:
            projects = node_test.group("project_ascii", "project_cn", "project_plain")
            tests = node_test.group("test_ascii", "test_cn", "test_plain")
            return RouteCandidate(
                decision=TurnRouteDecision.ROUTED,
                route_id="workspace_node_test",
                parameters={
                    "project_path": next(item for item in projects if item is not None),
                    "test_path": next(item for item in tests if item is not None),
                },
                reason_code="WORKSPACE_NODE_TEST_MATCHED",
            )
        python_test = re.fullmatch(
            r"\s*(?:请)?运行项目测试\s*[：:]\s*"
            r'(?:"(?P<project_ascii>[^\"]+)"|“(?P<project_cn>[^”]+)”|(?P<project_plain>\S+))\s+'
            r'(?:"(?P<test_ascii>[^\"]+)"|“(?P<test_cn>[^”]+)”|(?P<test_plain>\S+))\s*',
            message,
        )
        if python_test is not None:
            projects = python_test.group("project_ascii", "project_cn", "project_plain")
            tests = python_test.group("test_ascii", "test_cn", "test_plain")
            return RouteCandidate(
                decision=TurnRouteDecision.ROUTED,
                route_id="workspace_python_test",
                parameters={
                    "project_path": next(item for item in projects if item is not None),
                    "test_path": next(item for item in tests if item is not None),
                },
                reason_code="WORKSPACE_PYTHON_TEST_MATCHED",
            )
        check = re.fullmatch(
            r"\s*(?:请)?运行工作区(?:检查|测试)\s*[：:]\s*"
            r"(?P<profile>python-syntax|json-parse)\s+(?P<path>.+?)\s*",
            message,
            flags=re.IGNORECASE,
        )
        if check is not None:
            return RouteCandidate(
                decision=TurnRouteDecision.ROUTED,
                route_id="workspace_snapshot_check",
                parameters={
                    "profile": check.group("profile").casefold(),
                    "path": TurnRouter._unquote_path(check.group("path")),
                },
                reason_code="WORKSPACE_SNAPSHOT_CHECK_MATCHED",
            )
        directory = re.fullmatch(
            r"\s*(?:请)?(?:列出|查看)工作区目录(?:\s*[：:]\s*|\s+)"
            r"(?P<path>.+?)\s*",
            message,
        )
        if directory is not None:
            return RouteCandidate(
                decision=TurnRouteDecision.ROUTED,
                route_id="workspace_directory_list",
                parameters={"path": TurnRouter._unquote_path(directory.group("path"))},
                reason_code="WORKSPACE_DIRECTORY_LIST_MATCHED",
            )
        create = re.fullmatch(
            r"\s*(?:请)?(?:新建|创建)工作区文件(?:\s*[：:]\s*|\s+)"
            r'(?P<path>"[^"]+"|“[^”]+”|\S+)\s+内容\s*[：:]\s*'
            r'(?P<content>"[\s\S]*"|“[\s\S]*”)\s*',
            message,
        )
        if create is not None:
            return RouteCandidate(
                decision=TurnRouteDecision.ROUTED,
                route_id="workspace_file_create",
                parameters={
                    "target_path": TurnRouter._unquote_path(create.group("path")),
                    "content": TurnRouter._unquote_path(create.group("content")),
                },
                reason_code="WORKSPACE_FILE_CREATE_MATCHED",
            )
        rename = re.fullmatch(
            r"\s*(?:请)?(?:将)?工作区文件(?:\s*[：:]\s*|\s+)"
            r'(?P<source>"[^"]+"|“[^”]+”|\S+)\s+'
            r"(?:重命名为|改名为)\s+"
            r'(?P<target>"[^"]+"|“[^”]+”|\S+)\s*',
            message,
        )
        if rename is not None:
            return RouteCandidate(
                decision=TurnRouteDecision.ROUTED,
                route_id="workspace_file_rename",
                parameters={
                    "source_path": TurnRouter._unquote_path(rename.group("source")),
                    "target_path": TurnRouter._unquote_path(rename.group("target")),
                },
                reason_code="WORKSPACE_FILE_RENAME_MATCHED",
            )
        patch_header = re.match(r"\s*(?:请)?批量修改工作区文件\s*[：:]\s*", message)
        if patch_header is not None:
            body = message[patch_header.end() :]
            clauses = re.split(r"\s*[；;]\s*(?=(?:请)?在工作区文件\s+)", body)
            changes: list[dict[str, str]] = []
            for clause in clauses:
                match = _WORKSPACE_EDIT_PATTERN.fullmatch(clause)
                if match is None:
                    return RouteCandidate(
                        decision=TurnRouteDecision.NEEDS_CLARIFICATION,
                        route_id=None,
                        parameters={},
                        reason_code="WORKSPACE_COMMAND_INVALID",
                    )
                paths = match.group("path_ascii", "path_cn", "path_plain")
                old_text = match.group("old_ascii", "old_cn")
                new_text = match.group("new_ascii", "new_cn")
                changes.append(
                    {
                        "path": next(item for item in paths if item is not None),
                        "old_text": next(item for item in old_text if item is not None),
                        "new_text": next(item for item in new_text if item is not None),
                    }
                )
            patch_paths = [item["path"] for item in changes]
            if not 2 <= len(changes) <= 8 or len(patch_paths) != len(set(patch_paths)):
                return RouteCandidate(
                    decision=TurnRouteDecision.NEEDS_CLARIFICATION,
                    route_id=None,
                    parameters={},
                    reason_code="WORKSPACE_PATCH_INVALID",
                )
            changes_json = canonical_json_bytes({"changes": changes}).decode("utf-8")
            return RouteCandidate(
                decision=TurnRouteDecision.ROUTED,
                route_id="workspace_patch_bundle",
                parameters={"changes_json": changes_json},
                reason_code="WORKSPACE_PATCH_BUNDLE_MATCHED",
            )

        edit = _WORKSPACE_EDIT_PATTERN.fullmatch(message)
        if edit is not None:
            edit_paths = edit.group("path_ascii", "path_cn", "path_plain")
            old_text = edit.group("old_ascii", "old_cn")
            new_text = edit.group("new_ascii", "new_cn")
            return RouteCandidate(
                decision=TurnRouteDecision.ROUTED,
                route_id="workspace_file_replace",
                parameters={
                    "path": next(item for item in edit_paths if item is not None),
                    "old_text": next(item for item in old_text if item is not None),
                    "new_text": next(item for item in new_text if item is not None),
                },
                reason_code="WORKSPACE_FILE_REPLACE_MATCHED",
            )
        read = re.fullmatch(
            r"\s*(?:请)?(?:读取|查看|打开)工作区文件(?:\s*[：:]\s*|\s+)"
            r"(?P<path>.+?)\s*",
            message,
        )
        if read is not None:
            read_path = read.group("path").strip()
            if len(read_path) >= 2 and read_path[0] + read_path[-1] in {'""', "“”"}:
                read_path = read_path[1:-1]
            return RouteCandidate(
                decision=TurnRouteDecision.ROUTED,
                route_id="workspace_file_read",
                parameters={"path": read_path},
                reason_code="WORKSPACE_FILE_READ_MATCHED",
            )
        natural = TurnRouter._natural_workspace_candidate(message)
        if natural is not None:
            return natural
        if any(
            token in message
            for token in (
                "工作区文件",
                "工作区目录",
                "工作区检查",
                "工作区测试",
                "运行项目测试",
                "运行 Node 测试",
            )
        ):
            return RouteCandidate(
                decision=TurnRouteDecision.NEEDS_CLARIFICATION,
                route_id=None,
                parameters={},
                reason_code="WORKSPACE_COMMAND_INVALID",
            )
        return None

    @staticmethod
    def _natural_workspace_candidate(message: str) -> RouteCandidate | None:
        prefix = r"\s*(?:(?:请|麻烦)(?:帮我)?|帮我)?\s*"
        path = r'(?P<path>"[^"]+"|“[^”]+”|[^\s，,。]+)'

        for pattern in (
            prefix + r'(?:在\s+)?(?P<project>"[^"]+"|“[^”]+”|[^\s，,。]+)\s*'
            r"(?:项目|目录)?(?:里|中|下)?(?:的)?\s*"
            r'(?:运行|执行|跑(?:一下)?|测试)\s+(?P<test>"[^"]+"|“[^”]+”|[^\s，,。]+)'
            r"\s*[。.]?\s*",
            prefix + r"(?:运行|执行|跑(?:一下)?|测试)\s*(?:一下\s+)?"
            r'(?P<project>"[^"]+"|“[^”]+”|[^\s，,。]+)\s*'
            r"(?:项目|目录)?(?:里|中|下)?(?:的)?\s+"
            r'(?P<test>"[^"]+"|“[^”]+”|[^\s，,。]+)\s*[。.]?\s*',
        ):
            matched_test = re.fullmatch(pattern, message, flags=re.IGNORECASE)
            if matched_test is not None:
                return TurnRouter._natural_test_candidate(
                    TurnRouter._unquote_path(matched_test.group("project")),
                    TurnRouter._unquote_path(matched_test.group("test")),
                )

        python_check = re.fullmatch(
            prefix
            + r"(?:检查|验证|扫描)\s+"
            + path
            + r"\s*(?:下|里的)?\s*(?:所有)?\s*(?:Python|py)\s*"
            r"(?:文件的)?\s*(?:语法|语法错误)\s*[。.]?\s*",
            message,
            flags=re.IGNORECASE,
        )
        if python_check is not None:
            return RouteCandidate(
                decision=TurnRouteDecision.ROUTED,
                route_id="workspace_snapshot_check",
                parameters={
                    "profile": "python-syntax",
                    "path": TurnRouter._unquote_path(python_check.group("path")),
                },
                reason_code="WORKSPACE_SNAPSHOT_CHECK_MATCHED",
            )
        json_check = re.fullmatch(
            prefix
            + r"(?:检查|验证)\s+"
            + path
            + r"\s*(?:是不是|是否)?\s*(?:合法|有效)?\s*(?:的)?\s*JSON(?:文件)?\s*[。.]?\s*",
            message,
            flags=re.IGNORECASE,
        )
        if json_check is not None:
            return RouteCandidate(
                decision=TurnRouteDecision.ROUTED,
                route_id="workspace_snapshot_check",
                parameters={
                    "profile": "json-parse",
                    "path": TurnRouter._unquote_path(json_check.group("path")),
                },
                reason_code="WORKSPACE_SNAPSHOT_CHECK_MATCHED",
            )

        directory = re.fullmatch(
            prefix
            + r"(?:列(?:出|一下)|看(?:看|一下)|查看)\s+"
            + path
            + r"\s*(?:这个)?(?:目录|文件夹)(?:里|下)?(?:有些什么|有什么|的内容)?\s*[。.]?\s*",
            message,
        )
        if directory is not None:
            return RouteCandidate(
                decision=TurnRouteDecision.ROUTED,
                route_id="workspace_directory_list",
                parameters={"path": TurnRouter._unquote_path(directory.group("path"))},
                reason_code="WORKSPACE_DIRECTORY_LIST_MATCHED",
            )

        create = re.fullmatch(
            prefix
            + r"(?:创建|新建)\s+"
            + path
            + r"\s*(?:(?:这个)?文件)?\s*[，,]\s*内容(?:是|为)\s*"
            r'(?P<content>"[\s\S]*"|“[\s\S]*”)\s*[。.]?\s*',
            message,
        )
        if create is not None:
            return RouteCandidate(
                decision=TurnRouteDecision.ROUTED,
                route_id="workspace_file_create",
                parameters={
                    "target_path": TurnRouter._unquote_path(create.group("path")),
                    "content": TurnRouter._unquote_path(create.group("content")),
                },
                reason_code="WORKSPACE_FILE_CREATE_MATCHED",
            )

        rename = re.fullmatch(
            prefix + r"(?:把|将)\s+" + path + r"\s*(?:(?:这个)?文件)?\s*(?:重命名|改名)(?:为|成)\s*"
            r'(?P<target>"[^"]+"|“[^”]+”|[^\s，,。]+)\s*[。.]?\s*',
            message,
        )
        if rename is not None:
            return RouteCandidate(
                decision=TurnRouteDecision.ROUTED,
                route_id="workspace_file_rename",
                parameters={
                    "source_path": TurnRouter._unquote_path(rename.group("path")),
                    "target_path": TurnRouter._unquote_path(rename.group("target")),
                },
                reason_code="WORKSPACE_FILE_RENAME_MATCHED",
            )

        edit = re.fullmatch(
            prefix + r"(?:把|将)\s+" + path + r"\s*(?:(?:这个)?文件)?\s*(?:里|中)(?:的)?\s*"
            r'(?P<old>"[\s\S]+?"|“[\s\S]+?”)\s*'
            r"(?:改为|改成|替换为|替换成)\s*"
            r'(?P<new>"[\s\S]*?"|“[\s\S]*?”)\s*[。.]?\s*',
            message,
        )
        if edit is not None:
            return RouteCandidate(
                decision=TurnRouteDecision.ROUTED,
                route_id="workspace_file_replace",
                parameters={
                    "path": TurnRouter._unquote_path(edit.group("path")),
                    "old_text": TurnRouter._unquote_path(edit.group("old")),
                    "new_text": TurnRouter._unquote_path(edit.group("new")),
                },
                reason_code="WORKSPACE_FILE_REPLACE_MATCHED",
            )

        read = re.fullmatch(
            prefix
            + r"(?:读(?:取|一下)?|打开|看(?:看|一下)|查看)\s*(?:一下\s+)?"
            + path
            + r"\s*(?:(?:这个)?文件)?\s*(?:(?:的)?内容|看看)?\s*[。.]?\s*",
            message,
        )
        if read is not None and TurnRouter._looks_like_file_path(read.group("path")):
            return RouteCandidate(
                decision=TurnRouteDecision.ROUTED,
                route_id="workspace_file_read",
                parameters={"path": TurnRouter._unquote_path(read.group("path"))},
                reason_code="WORKSPACE_FILE_READ_MATCHED",
            )
        if re.fullmatch(
            prefix + r"(?:读(?:取|一下)?|打开|看(?:看|一下)|查看)\s*(?:一下\s*)?"
            r"(?:这个)?文件\s*[。.]?\s*",
            message,
        ):
            return RouteCandidate(
                decision=TurnRouteDecision.ROUTED,
                route_id="workspace_file_read",
                parameters={"path": ""},
                reason_code="WORKSPACE_FILE_PATH_MISSING",
            )
        if TurnRouter._missing_test_project(message) is not None:
            return RouteCandidate(
                decision=TurnRouteDecision.NEEDS_CLARIFICATION,
                route_id=None,
                parameters={},
                reason_code="WORKSPACE_TEST_PATH_MISSING",
            )
        return None

    @staticmethod
    def _natural_test_candidate(project_path: str, test_path: str) -> RouteCandidate:
        normalized_test = test_path.replace("\\", "/")
        test_name = normalized_test.rsplit("/", 1)[-1].casefold()
        if (
            normalized_test.casefold().startswith("tests/")
            and test_name.endswith(".py")
            and (test_name.startswith("test_") or test_name.endswith("_test.py"))
        ):
            route_id: RouteId = "workspace_python_test"
            reason_code = "WORKSPACE_PYTHON_TEST_MATCHED"
        elif test_name.endswith((".spec.js", ".test.js")):
            route_id = "workspace_node_test"
            reason_code = "WORKSPACE_NODE_TEST_MATCHED"
        else:
            return RouteCandidate(
                decision=TurnRouteDecision.NEEDS_CLARIFICATION,
                route_id=None,
                parameters={},
                reason_code="WORKSPACE_COMMAND_INVALID",
            )
        return RouteCandidate(
            decision=TurnRouteDecision.ROUTED,
            route_id=route_id,
            parameters={"project_path": project_path, "test_path": normalized_test},
            reason_code=reason_code,
        )

    @staticmethod
    def _looks_like_file_path(value: str) -> bool:
        path = TurnRouter._unquote_path(value)
        return bool(re.search(r"\.[A-Za-z0-9]{1,10}$", path))

    @staticmethod
    def _missing_test_project(message: str) -> str | None:
        match = re.fullmatch(
            r"\s*(?:(?:请|麻烦)(?:帮我)?|帮我)?\s*(?:在\s+)?"
            r'(?P<project>"[^"]+"|“[^”]+”|[^\s，,。]+)\s*'
            r"(?:项目|目录)?(?:里|中|下)?(?:的)?\s*"
            r"(?:运行|执行|跑)(?:一下)?\s*(?:项目)?测试\s*[。.]?\s*",
            message,
        )
        return None if match is None else TurnRouter._unquote_path(match.group("project"))

    @staticmethod
    def _research_goal_missing(message: str) -> bool:
        return bool(
            re.fullmatch(
                r"\s*(?:(?:请|麻烦)(?:帮我)?|帮我)?\s*(?:给我)?\s*"
                r"(?:(?:做|生成|整理|写)(?:一份)?\s*(?:HTML|Markdown|PDF)\s*"
                r"(?:研究)?报告|(?:研究|调研|查一下|搜索一下)(?:一下)?)\s*[。.]?\s*",
                message,
                flags=re.IGNORECASE,
            )
        )

    @staticmethod
    def resolve_followup(
        source: TurnRouteRead,
        source_message: str,
        response: str,
    ) -> FollowupResolution | None:
        current = TurnRouter.classify(response)
        agent_workspace_input = (
            source.decision is TurnRouteDecision.ROUTED
            and source.route_id == "workspace_file_read"
            and source.status is TurnRouteStatus.WAITING_USER_INPUT
            and source.reason_code == "WORKSPACE_FILE_PATH_MISSING"
        )
        if current.decision is TurnRouteDecision.ROUTED or (
            source.decision is not TurnRouteDecision.NEEDS_CLARIFICATION
            and not agent_workspace_input
        ):
            return None
        text = response.strip()
        synthetic: str | None = None
        rule: str | None = None
        if source.reason_code == "MCP_TEXT_MISSING":
            synthetic, rule = f"统计字符数：{text}", "mcp_text_payload"
        elif source.reason_code == "KNOWLEDGE_QUERY_MISSING":
            synthetic, rule = f"查询知识库：{text}", "knowledge_query"
        elif source.reason_code == "RESEARCH_GOAL_MISSING":
            synthetic, rule = f"研究 {text} 并生成 PDF 报告", "research_goal"
        elif (
            source.reason_code == "WORKSPACE_FILE_PATH_MISSING"
            and TurnRouter._looks_like_file_path(text)
        ):
            synthetic = f"帮我看看 {text}"
            rule = "agent_workspace_file_path" if agent_workspace_input else "workspace_file_path"
        elif source.reason_code == "WORKSPACE_TEST_PATH_MISSING":
            project = TurnRouter._missing_test_project(source_message)
            if project is not None:
                synthetic, rule = f"在 {project} 里运行 {text}", "workspace_test_path"
        if synthetic is None or rule is None:
            return None
        resolved = TurnRouter.classify(synthetic)
        if resolved.decision is not TurnRouteDecision.ROUTED:
            return None
        return FollowupResolution(candidate=resolved, source=source, rule=rule)

    @staticmethod
    def _natural_research_goal(message: str) -> str | None:
        prefix = r"\s*(?:(?:请|麻烦)(?:帮我)?|帮我)?\s*"
        patterns = (
            prefix + r"(?:查(?:一下)?|查查|搜索(?:一下)?|研究(?:一下)?|调研(?:一下)?)\s*"
            r"(?:关于\s*)?(?P<goal>.+?)\s*[，,]?\s*(?:并|然后)?\s*"
            r"(?:整理|生成|写|做)(?:成|一份)?\s*(?:HTML|Markdown|PDF)?\s*(?:研究)?报告\s*[。.]?\s*",
            prefix + r"(?:给我)?\s*(?:做|生成|整理|写)(?:一份)?\s*(?:关于\s*)?"
            r"(?P<goal>.+?)\s*(?:的)?\s*(?:HTML|Markdown|PDF)\s*(?:研究)?报告\s*[。.]?\s*",
            prefix + r"(?:查(?:一下)?|查查|搜索(?:一下)?|研究(?:一下)?|调研(?:一下)?)\s*"
            r"(?:关于\s*)?(?P<goal>.+?)\s*[。.]?\s*",
        )
        for pattern in patterns:
            match = re.fullmatch(pattern, message, flags=re.IGNORECASE)
            if match is not None:
                goal = match.group("goal").strip(" ，,")
                if goal:
                    return goal
        return None

    @staticmethod
    def _unquote_path(value: str) -> str:
        result = value.strip()
        if len(result) >= 2 and result[0] + result[-1] in {'""', "“”"}:
            return result[1:-1]
        return result

    @staticmethod
    def _patch_paths(value: str) -> tuple[str, ...] | None:
        raw = value.strip()
        if not raw.startswith("["):
            path = TurnRouter._unquote_path(raw).strip()
            return (path,) if path else None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if (
            not isinstance(parsed, list)
            or not 1 <= len(parsed) <= 2
            or any(not isinstance(item, str) or not item.strip() for item in parsed)
        ):
            return None
        paths = tuple(item.strip() for item in parsed)
        if len(paths) != len(set(paths)):
            return None
        return paths

    @staticmethod
    def _metrics_payload(message: str) -> str | None:
        for pattern in (
            r"[：:]\s*(.+)$",
            r"[“\"]([^”\"]+)[”\"]\s*$",
            r"(?:以下|这段)(?:文字|文本)\s*[，,]?\s*(.+)$",
        ):
            match = re.search(pattern, message.strip(), flags=re.DOTALL)
            if match and match.group(1).strip():
                return match.group(1).strip()
        return None

    @staticmethod
    def route_manifest_digest(route_id: RouteId) -> str:
        return sha256_digest(_ROUTE_SPECS[route_id])

    async def create(
        self,
        *,
        task_id: str,
        conversation_id: str,
        user_message_id: str,
        message_digest: str,
        candidate: RouteCandidate,
        status: TurnRouteStatus,
        resolution: FollowupResolution | None = None,
    ) -> TurnRouteRead:
        if resolution is not None and (
            resolution.candidate != candidate
            or not self._is_resolution_source(resolution.source, resolution.rule)
        ):
            raise TurnRouteConflictError("Turn Route clarification resolution is invalid")
        parameters = candidate.parameters
        parameter_digest = sha256_digest(parameters)
        route_digest = (
            self.route_manifest_digest(candidate.route_id)
            if candidate.route_id is not None
            else None
        )
        candidate_digest = sha256_digest(
            {
                "classifier_version": CLASSIFIER_VERSION,
                "message_digest": message_digest,
                "decision": candidate.decision.value,
                "route_id": candidate.route_id,
                "parameters": parameters,
                "reason_code": candidate.reason_code,
            }
        )
        resolution_digest = (
            self._resolution_digest(
                source=resolution.source,
                rule=resolution.rule,
                target_user_message_id=user_message_id,
                target_message_digest=message_digest,
                target_candidate_digest=candidate_digest,
                target_parameter_digest=parameter_digest,
            )
            if resolution is not None
            else None
        )
        now = utc_now()
        record = TurnRouteRecord(
            task_id=task_id,
            conversation_id=conversation_id,
            user_message_id=user_message_id,
            decision=candidate.decision.value,
            route_id=candidate.route_id,
            route_version="1" if candidate.route_id is not None else None,
            route_manifest_digest=route_digest,
            candidate_digest=candidate_digest,
            parameters=parameters,
            parameter_digest=parameter_digest,
            resolved_from_task_id=(resolution.source.task_id if resolution is not None else None),
            resolution_rule=resolution.rule if resolution is not None else None,
            resolution_digest=resolution_digest,
            reason_code=candidate.reason_code,
            status=status.value,
            result_manifest=None,
            result_digest=None,
            error_code=None,
            revision=1,
            created_at=now,
            updated_at=now,
        )
        async with self._database.session() as session, session.begin():
            session.add(record)
        return await self.get(task_id)

    async def get(self, task_id: str) -> TurnRouteRead:
        async with self._database.session() as session:
            record = await session.get(TurnRouteRecord, task_id)
            if record is None:
                raise TurnRouteConflictError("Task has no Turn Route decision")
            message = await session.get(ConversationMessageRecord, record.user_message_id)
            if message is None:
                raise TurnRouteProofRejectedError("Turn Route user message is missing")
            self._validate_record(record, message.message_digest)
            await self._validate_resolution(session, record, message.message_digest)
            return self._read(record)

    async def get_result(
        self, task_id: str
    ) -> tuple[
        KnowledgeSearchRead | None,
        McpToolCallRead | None,
        WorkspaceFileRead | None,
        WorkspaceEditPreview | WorkspaceEditReceipt | None,
        WorkspacePatchPreview | WorkspacePatchReceipt | None,
        WorkspaceDirectoryRead | None,
        WorkspaceCheckRead | None,
        WorkspacePythonTestRead | None,
        WorkspaceNodeTestRead | None,
        WorkspacePathOperationPreview | WorkspacePathOperationReceipt | None,
    ]:
        async with self._database.session() as session:
            record = await session.get(TurnRouteRecord, task_id)
            if record is None:
                return None, None, None, None, None, None, None, None, None, None
            message = await session.get(ConversationMessageRecord, record.user_message_id)
            if message is None:
                raise TurnRouteProofRejectedError("Turn Route user message is missing")
            self._validate_record(record, message.message_digest)
            await self._validate_resolution(session, record, message.message_digest)
            return self._validated_result(record)

    async def prepare_workspace_edit(self, task_id: str) -> WorkspaceEditPreview:
        route = await self.get(task_id)
        if route.route_id != "workspace_file_replace":
            raise TurnRouteConflictError("Turn Route is not a workspace replacement")
        async with self._database.session() as session:
            record = await session.get(TurnRouteRecord, task_id)
            if record is None:
                raise TurnRouteConflictError("Workspace replacement route is missing")
            parameters = cast(dict[str, str], record.parameters)
        try:
            preview = self._workspace_files.prepare_replace(
                task_id=task_id,
                relative_path=parameters["path"],
                old_text=parameters["old_text"],
                new_text=parameters["new_text"],
            )
        except WorkspaceFileError as error:
            await self._fail_preparation(task_id, error.code)
            raise TurnRouteConflictError(str(error)) from error
        async with self._database.session() as session, session.begin():
            record = await session.scalar(
                select(TurnRouteRecord).where(TurnRouteRecord.task_id == task_id).with_for_update()
            )
            if (
                record is None
                or record.route_id != "workspace_file_replace"
                or record.status != TurnRouteStatus.NEEDS_USER_ACTION.value
                or record.parameter_digest != sha256_digest(record.parameters)
            ):
                raise TurnRouteConflictError("Workspace replacement preview was fenced")
            record.result_manifest = preview.model_dump(mode="json")
            record.result_digest = preview.confirmation_digest
            record.revision += 1
            record.updated_at = utc_now()
        return preview

    async def commit_workspace_edit(
        self, task_id: str, confirmation_digest: str
    ) -> WorkspaceEditReceipt:
        route = await self.get(task_id)
        if route.route_id != "workspace_file_replace":
            raise TurnRouteConflictError("Turn Route is not a workspace replacement")
        _, _, _, edit, _, _, _, _, _, _ = await self.get_result(task_id)
        if not isinstance(edit, WorkspaceEditPreview):
            if isinstance(edit, WorkspaceEditReceipt):
                if edit.confirmation_digest == confirmation_digest:
                    return edit
            raise TurnRouteConflictError("Workspace replacement preview is missing")
        if edit.confirmation_digest != confirmation_digest:
            raise TurnRouteProofRejectedError("Workspace replacement confirmation changed")
        if route.status is TurnRouteStatus.RUNNING:
            node_id, run_id, fencing_token = await self._reclaim_running_workspace_write(
                task_id, "workspace_file_replace"
            )
        else:
            node_id, run_id, fencing_token, _ = await self._claim(task_id, "workspace_file_replace")
        try:
            receipt = self._workspace_files.commit_replace(edit)
        except (WorkspaceFileError, OSError) as error:
            code = getattr(error, "code", "WORKSPACE_FILE_OS_ERROR")
            await self._fail(task_id, node_id, run_id, fencing_token, code)
            raise TurnRouteConflictError(str(error)) from error
        await self._complete(
            task_id,
            node_id,
            run_id,
            fencing_token,
            receipt.model_dump(mode="json"),
            receipt.receipt_digest,
        )
        return receipt

    async def prepare_workspace_patch(self, task_id: str) -> WorkspacePatchPreview:
        route = await self.get(task_id)
        if route.route_id != "workspace_patch_bundle":
            raise TurnRouteConflictError("Turn Route is not a workspace patch")
        async with self._database.session() as session:
            record = await session.get(TurnRouteRecord, task_id)
            if record is None:
                raise TurnRouteConflictError("Workspace patch route is missing")
            parameters = cast(dict[str, str], record.parameters)
        changes = self._decode_patch_changes(parameters["changes_json"])
        try:
            preview = self._workspace_files.prepare_patch(task_id=task_id, changes=changes)
        except WorkspaceFileError as error:
            await self._fail_preparation(task_id, error.code)
            raise TurnRouteConflictError(str(error)) from error
        async with self._database.session() as session, session.begin():
            record = await session.scalar(
                select(TurnRouteRecord).where(TurnRouteRecord.task_id == task_id).with_for_update()
            )
            if (
                record is None
                or record.route_id != "workspace_patch_bundle"
                or record.status != TurnRouteStatus.NEEDS_USER_ACTION.value
                or record.parameter_digest != sha256_digest(record.parameters)
            ):
                raise TurnRouteConflictError("Workspace patch preview was fenced")
            record.result_manifest = preview.model_dump(mode="json")
            record.result_digest = preview.confirmation_digest
            record.revision += 1
            record.updated_at = utc_now()
        return preview

    async def commit_workspace_patch(
        self, task_id: str, confirmation_digest: str
    ) -> WorkspacePatchReceipt:
        route = await self.get(task_id)
        if route.route_id != "workspace_patch_bundle":
            raise TurnRouteConflictError("Turn Route is not a workspace patch")
        _, _, _, _, patch, _, _, _, _, _ = await self.get_result(task_id)
        if not isinstance(patch, WorkspacePatchPreview):
            if isinstance(patch, WorkspacePatchReceipt):
                if patch.status == "committed" and patch.confirmation_digest == confirmation_digest:
                    return patch
            raise TurnRouteConflictError("Workspace patch preview is missing")
        if patch.confirmation_digest != confirmation_digest:
            raise TurnRouteProofRejectedError("Workspace patch confirmation changed")
        if route.status is TurnRouteStatus.RUNNING:
            node_id, run_id, fencing_token = await self._reclaim_running_workspace_write(
                task_id, "workspace_patch_bundle"
            )
        else:
            node_id, run_id, fencing_token, _ = await self._claim(task_id, "workspace_patch_bundle")
        try:
            receipt = self._workspace_files.commit_patch(patch)
        except WorkspacePatchPartialError as error:
            receipt = error.receipt
            await self._fail_with_result(
                task_id,
                node_id,
                run_id,
                fencing_token,
                error.code,
                receipt.model_dump(mode="json"),
                receipt.receipt_digest,
            )
            raise TurnRouteConflictError(str(error)) from error
        except (WorkspaceFileError, OSError) as error:
            code = getattr(error, "code", "WORKSPACE_FILE_OS_ERROR")
            await self._fail(task_id, node_id, run_id, fencing_token, code)
            raise TurnRouteConflictError(str(error)) from error
        await self._complete(
            task_id,
            node_id,
            run_id,
            fencing_token,
            receipt.model_dump(mode="json"),
            receipt.receipt_digest,
        )
        return receipt

    async def prepare_workspace_path_operation(self, task_id: str) -> WorkspacePathOperationPreview:
        route = await self.get(task_id)
        if route.route_id not in {"workspace_file_create", "workspace_file_rename"}:
            raise TurnRouteConflictError("Turn Route is not a workspace path operation")
        async with self._database.session() as session:
            record = await session.get(TurnRouteRecord, task_id)
            if record is None:
                raise TurnRouteConflictError("Workspace path operation route is missing")
            parameters = cast(dict[str, str], record.parameters)
        try:
            if route.route_id == "workspace_file_create":
                preview = self._workspace_files.prepare_create(
                    task_id=task_id,
                    target_path=parameters["target_path"],
                    content=parameters["content"],
                )
            else:
                preview = self._workspace_files.prepare_rename(
                    task_id=task_id,
                    source_path=parameters["source_path"],
                    target_path=parameters["target_path"],
                )
        except WorkspaceFileError as error:
            await self._fail_preparation(task_id, error.code)
            raise TurnRouteConflictError(str(error)) from error
        async with self._database.session() as session, session.begin():
            record = await session.scalar(
                select(TurnRouteRecord).where(TurnRouteRecord.task_id == task_id).with_for_update()
            )
            if (
                record is None
                or record.route_id != route.route_id
                or record.status != TurnRouteStatus.NEEDS_USER_ACTION.value
                or record.parameter_digest != sha256_digest(record.parameters)
            ):
                raise TurnRouteConflictError("Workspace path operation preview was fenced")
            record.result_manifest = preview.model_dump(mode="json")
            record.result_digest = preview.confirmation_digest
            record.revision += 1
            record.updated_at = utc_now()
        return preview

    async def commit_workspace_path_operation(
        self, task_id: str, confirmation_digest: str
    ) -> WorkspacePathOperationReceipt:
        route = await self.get(task_id)
        if route.route_id not in {"workspace_file_create", "workspace_file_rename"}:
            raise TurnRouteConflictError("Turn Route is not a workspace path operation")
        *_, path_operation = await self.get_result(task_id)
        if not isinstance(path_operation, WorkspacePathOperationPreview):
            if isinstance(path_operation, WorkspacePathOperationReceipt):
                if path_operation.confirmation_digest == confirmation_digest:
                    return path_operation
            raise TurnRouteConflictError("Workspace path operation preview is missing")
        if path_operation.confirmation_digest != confirmation_digest:
            raise TurnRouteProofRejectedError("Workspace path operation confirmation changed")
        route_id = cast(Literal["workspace_file_create", "workspace_file_rename"], route.route_id)
        if route.status is TurnRouteStatus.RUNNING:
            node_id, run_id, fencing_token = await self._reclaim_running_workspace_write(
                task_id, route_id
            )
        else:
            node_id, run_id, fencing_token, _ = await self._claim(task_id, route_id)
        try:
            receipt = self._workspace_files.commit_path_operation(path_operation)
        except (WorkspaceFileError, OSError) as error:
            code = getattr(error, "code", "WORKSPACE_FILE_OS_ERROR")
            await self._fail(task_id, node_id, run_id, fencing_token, code)
            raise TurnRouteConflictError(str(error)) from error
        await self._complete(
            task_id,
            node_id,
            run_id,
            fencing_token,
            receipt.model_dump(mode="json"),
            receipt.receipt_digest,
        )
        return receipt

    async def _reclaim_running_workspace_write(
        self,
        task_id: str,
        route_id: Literal[
            "workspace_file_replace",
            "workspace_patch_bundle",
            "workspace_file_create",
            "workspace_file_rename",
        ],
    ) -> tuple[str, str, int]:
        async with self._database.session() as session, session.begin():
            route = await session.scalar(
                select(TurnRouteRecord).where(TurnRouteRecord.task_id == task_id).with_for_update()
            )
            run = await session.scalar(
                select(TaskExecutionRunRecord)
                .where(
                    TaskExecutionRunRecord.task_id == task_id,
                    TaskExecutionRunRecord.status == ExecutionRunStatus.ACTIVE.value,
                )
                .with_for_update()
            )
            if (
                route is None
                or run is None
                or route.route_id != route_id
                or route.status != TurnRouteStatus.RUNNING.value
            ):
                raise TurnRouteConflictError("Workspace replacement recovery state is missing")
            node = await session.scalar(
                select(TaskExecutionNodeRecord)
                .where(
                    TaskExecutionNodeRecord.run_id == run.run_id,
                    TaskExecutionNodeRecord.local_key == route_id,
                )
                .with_for_update()
            )
            if node is None or node.status != ExecutionNodeStatus.RUNNING.value:
                raise TurnRouteConflictError("Workspace replacement recovery node is missing")
            now = utc_now()
            node.claim_fencing_token += 1
            node.claim_owner_id = CLASSIFIER_VERSION
            node.claim_acquired_at = now
            node.claim_heartbeat_at = now
            node.claim_expires_at = now + timedelta(seconds=90)
            node.revision += 1
            node.updated_at = now
            route.revision += 1
            route.updated_at = now
            return node.node_id, run.run_id, node.claim_fencing_token

    @staticmethod
    def _decode_patch_changes(value: str) -> tuple[dict[str, str], ...]:
        try:
            decoded = json.loads(value)
            changes = decoded["changes"]
            if not isinstance(changes, list):
                raise TypeError
            result_items: list[dict[str, str]] = []
            for item in changes:
                if (
                    not isinstance(item, dict)
                    or set(item) != {"path", "old_text", "new_text"}
                    or not all(isinstance(item[key], str) for key in item)
                ):
                    raise TypeError
                result_items.append(
                    {
                        "path": item["path"],
                        "old_text": item["old_text"],
                        "new_text": item["new_text"],
                    }
                )
            result = tuple(result_items)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise TurnRouteProofRejectedError("Workspace patch parameters are invalid") from error
        if len(result) != len(changes) or not 2 <= len(result) <= 8:
            raise TurnRouteProofRejectedError("Workspace patch parameters are incomplete")
        if canonical_json_bytes({"changes": list(result)}).decode("utf-8") != value:
            raise TurnRouteProofRejectedError("Workspace patch parameters are not canonical")
        return result

    async def mcp_enabled(self) -> bool:
        return (await self._mcp.list_servers())[0].enabled

    async def mark_needs_user_action(self, task_id: str) -> TurnRouteRead:
        async with self._database.session() as session, session.begin():
            record = await session.scalar(
                select(TurnRouteRecord).where(TurnRouteRecord.task_id == task_id).with_for_update()
            )
            if record is None or record.route_id != "mcp_text_metrics":
                raise TurnRouteConflictError("Task is not an MCP text metrics route")
            if record.status != TurnRouteStatus.NEEDS_USER_ACTION.value:
                record.status = TurnRouteStatus.NEEDS_USER_ACTION.value
                record.revision += 1
                record.updated_at = utc_now()
        return await self.get(task_id)

    async def execute_direct(
        self, task_id: str
    ) -> tuple[
        KnowledgeSearchRead | None,
        McpToolCallRead | None,
        WorkspaceFileRead | None,
        WorkspaceEditPreview | WorkspaceEditReceipt | None,
        WorkspacePatchPreview | WorkspacePatchReceipt | None,
        WorkspaceDirectoryRead | None,
        WorkspaceCheckRead | None,
        WorkspacePythonTestRead | None,
        WorkspaceNodeTestRead | None,
        WorkspacePathOperationPreview | WorkspacePathOperationReceipt | None,
    ]:
        route = await self.get(task_id)
        if route.route_id not in _ROUTE_NODES:
            raise TurnRouteConflictError("Turn Route is not a direct capability")
        if route.route_id == "mcp_text_metrics" and not await self.mcp_enabled():
            await self.mark_needs_user_action(task_id)
            raise McpServerDisabledError("MCP server must be enabled by the user")
        node_id, run_id, fencing_token, parameters = await self._claim(task_id, route.route_id)
        workspace_path_operation: (
            WorkspacePathOperationPreview | WorkspacePathOperationReceipt | None
        ) = None
        try:
            if route.route_id == "knowledge_lookup":
                knowledge = await self._knowledge.search(parameters["query"], 10)
                mcp = None
                workspace_file = None
                workspace_edit = None
                workspace_patch = None
                workspace_directory = None
                workspace_check = None
                workspace_python_test = None
                workspace_node_test = None
                result_manifest = knowledge.model_dump(mode="json")
                result_digest = knowledge.result_digest
            elif route.route_id == "mcp_text_metrics":
                knowledge = None
                mcp = await self._mcp.invoke("deskpilot.text.metrics", {"text": parameters["text"]})
                workspace_file = None
                workspace_edit = None
                workspace_patch = None
                workspace_directory = None
                workspace_check = None
                workspace_python_test = None
                workspace_node_test = None
                result_manifest = mcp.model_dump(mode="json")
                result_digest = mcp.result_digest
            elif route.route_id == "workspace_file_read":
                knowledge = None
                mcp = None
                workspace_file = self._workspace_files.read(parameters["path"])
                workspace_edit = None
                workspace_patch = None
                workspace_directory = None
                workspace_check = None
                workspace_python_test = None
                workspace_node_test = None
                result_manifest = workspace_file.model_dump(mode="json")
                result_digest = workspace_file.result_digest
            elif route.route_id in {
                "workspace_directory_list",
                "workspace_directory_analyze",
            }:
                knowledge = None
                mcp = None
                workspace_file = None
                workspace_edit = None
                workspace_patch = None
                workspace_directory = self._workspace_files.list_directory(parameters["path"])
                workspace_check = None
                workspace_python_test = None
                workspace_node_test = None
                result_manifest = workspace_directory.model_dump(mode="json")
                result_digest = workspace_directory.result_digest
            elif route.route_id == "workspace_snapshot_check":
                if self._workspace_checks is None:
                    raise WorkspaceCheckError("Workspace check runtime is unavailable")
                knowledge = None
                mcp = None
                workspace_file = None
                workspace_edit = None
                workspace_patch = None
                workspace_directory = None
                snapshot = self._workspace_files.prepare_check(
                    parameters["profile"], parameters["path"]
                )
                workspace_check = await asyncio.to_thread(self._workspace_checks.run, snapshot)
                workspace_python_test = None
                workspace_node_test = None
                result_manifest = workspace_check.model_dump(mode="json")
                result_digest = workspace_check.result_digest
            elif route.route_id == "workspace_python_test":
                if self._workspace_python_tests is None:
                    raise WorkspacePythonTestError("Python test runtime is unavailable")
                knowledge = None
                mcp = None
                workspace_file = None
                workspace_edit = None
                workspace_patch = None
                workspace_directory = None
                workspace_check = None
                test_snapshot = self._workspace_files.prepare_python_test(
                    parameters["project_path"], parameters["test_path"]
                )
                workspace_python_test = await asyncio.to_thread(
                    self._workspace_python_tests.run, test_snapshot
                )
                workspace_node_test = None
                result_manifest = workspace_python_test.model_dump(mode="json")
                result_digest = workspace_python_test.result_digest
            elif route.route_id == "workspace_node_test":
                if self._workspace_node_tests is None:
                    raise WorkspaceNodeTestError("Node test runtime is unavailable")
                knowledge = None
                mcp = None
                workspace_file = None
                workspace_edit = None
                workspace_patch = None
                workspace_directory = None
                workspace_check = None
                workspace_python_test = None
                node_test_snapshot = self._workspace_files.prepare_node_test(
                    parameters["project_path"], parameters["test_path"]
                )
                workspace_node_test = await asyncio.to_thread(
                    self._workspace_node_tests.run, node_test_snapshot
                )
                result_manifest = workspace_node_test.model_dump(mode="json")
                result_digest = workspace_node_test.result_digest
            else:
                raise TurnRouteConflictError("Workspace replacement requires explicit confirmation")
        except (
            KnowledgeSourceError,
            KnowledgeProofRejectedError,
            McpControlError,
            WorkspaceFileError,
            WorkspaceCheckError,
            WorkspacePythonTestError,
            WorkspaceNodeTestError,
        ) as error:
            await self._fail(task_id, node_id, run_id, fencing_token, error.code)
            raise TurnRouteConflictError(str(error)) from error
        await self._complete(
            task_id,
            node_id,
            run_id,
            fencing_token,
            result_manifest,
            result_digest,
        )
        return (
            knowledge,
            mcp,
            workspace_file,
            workspace_edit,
            workspace_patch,
            workspace_directory,
            workspace_check,
            workspace_python_test,
            workspace_node_test,
            workspace_path_operation,
        )

    async def _claim(self, task_id: str, route_id: RouteId) -> tuple[str, str, int, dict[str, str]]:
        node_key = _ROUTE_NODES[route_id]
        async with self._database.session() as session, session.begin():
            route = await session.scalar(
                select(TurnRouteRecord).where(TurnRouteRecord.task_id == task_id).with_for_update()
            )
            run = await session.scalar(
                select(TaskExecutionRunRecord)
                .where(
                    TaskExecutionRunRecord.task_id == task_id,
                    TaskExecutionRunRecord.status == ExecutionRunStatus.ACTIVE.value,
                )
                .with_for_update()
            )
            if route is None or run is None:
                raise TurnRouteConflictError("Direct Route execution state is missing")
            message = await session.scalar(
                select(ConversationMessageRecord).where(
                    ConversationMessageRecord.message_id == route.user_message_id
                )
            )
            if message is None:
                raise TurnRouteProofRejectedError("Route source message is missing")
            self._validate_record(route, message.message_digest)
            await self._validate_resolution(session, route, message.message_digest)
            if route.route_id != route_id:
                raise TurnRouteProofRejectedError("Route identity changed before execution")
            node = await session.scalar(
                select(TaskExecutionNodeRecord)
                .where(
                    TaskExecutionNodeRecord.run_id == run.run_id,
                    TaskExecutionNodeRecord.local_key == node_key,
                )
                .with_for_update()
            )
            if node is None or node.status != ExecutionNodeStatus.READY.value:
                raise TurnRouteConflictError("Direct Route node is not ready")
            if route.status not in {
                TurnRouteStatus.READY.value,
                TurnRouteStatus.NEEDS_USER_ACTION.value,
            }:
                raise TurnRouteConflictError("Direct Route is not ready")
            now = utc_now()
            node.status = ExecutionNodeStatus.RUNNING.value
            node.attempt_count += 1
            node.claim_fencing_token += 1
            node.claim_owner_id = CLASSIFIER_VERSION
            node.claim_acquired_at = now
            node.claim_heartbeat_at = now
            node.claim_expires_at = now + timedelta(seconds=90)
            node.revision += 1
            node.updated_at = now
            route.status = TurnRouteStatus.RUNNING.value
            route.revision += 1
            route.updated_at = now
            return (
                node.node_id,
                run.run_id,
                node.claim_fencing_token,
                cast(dict[str, str], route.parameters),
            )

    async def _complete(
        self,
        task_id: str,
        node_id: str,
        run_id: str,
        fencing_token: int,
        result_manifest: dict[str, Any],
        result_digest: str,
    ) -> None:
        async with self._database.session() as session, session.begin():
            route = await session.scalar(
                select(TurnRouteRecord).where(TurnRouteRecord.task_id == task_id).with_for_update()
            )
            run = await session.scalar(
                select(TaskExecutionRunRecord)
                .where(TaskExecutionRunRecord.run_id == run_id)
                .with_for_update()
            )
            node = await session.scalar(
                select(TaskExecutionNodeRecord)
                .where(TaskExecutionNodeRecord.node_id == node_id)
                .with_for_update()
            )
            if route is None or run is None or node is None:
                raise TurnRouteConflictError("Direct Route completion state is missing")
            if (
                route.status != TurnRouteStatus.RUNNING.value
                or run.status != ExecutionRunStatus.ACTIVE.value
                or node.status != ExecutionNodeStatus.RUNNING.value
                or node.claim_fencing_token != fencing_token
                or node.claim_owner_id != CLASSIFIER_VERSION
            ):
                raise TurnRouteConflictError("Direct Route completion was fenced")
            route.result_manifest = result_manifest
            route.result_digest = result_digest
            route.error_code = None
            route.status = TurnRouteStatus.SUCCEEDED.value
            route.revision += 1
            route.updated_at = utc_now()
            self._clear_claim(node)
            await mark_verified_and_unlock(session, run, node)
            for key in ("final_acceptance", "delivery"):
                control = await session.scalar(
                    select(TaskExecutionNodeRecord)
                    .where(
                        TaskExecutionNodeRecord.run_id == run_id,
                        TaskExecutionNodeRecord.local_key == key,
                    )
                    .with_for_update()
                )
                if control is None or control.status != ExecutionNodeStatus.READY.value:
                    raise TurnRouteProofRejectedError("Direct Route verified edge is incomplete")
                await mark_verified_and_unlock(session, run, control)
            run.status = ExecutionRunStatus.SUCCEEDED.value
            run.revision += 1
            run.updated_at = utc_now()

    async def _fail(
        self,
        task_id: str,
        node_id: str,
        run_id: str,
        fencing_token: int,
        error_code: str,
    ) -> None:
        async with self._database.session() as session, session.begin():
            route = await session.get(TurnRouteRecord, task_id)
            run = await session.get(TaskExecutionRunRecord, run_id)
            node = await session.get(TaskExecutionNodeRecord, node_id)
            if route is None or run is None or node is None:
                return
            if (
                node.claim_fencing_token != fencing_token
                or node.claim_owner_id != CLASSIFIER_VERSION
            ):
                return
            now = utc_now()
            route.status = TurnRouteStatus.FAILED.value
            route.error_code = error_code
            route.revision += 1
            route.updated_at = now
            node.status = ExecutionNodeStatus.FAILED.value
            self._clear_claim(node)
            node.revision += 1
            node.updated_at = now
            run.status = ExecutionRunStatus.FAILED.value
            run.revision += 1
            run.updated_at = now

    async def _fail_with_result(
        self,
        task_id: str,
        node_id: str,
        run_id: str,
        fencing_token: int,
        error_code: str,
        result_manifest: dict[str, Any],
        result_digest: str,
    ) -> None:
        async with self._database.session() as session, session.begin():
            route = await session.get(TurnRouteRecord, task_id)
            run = await session.get(TaskExecutionRunRecord, run_id)
            node = await session.get(TaskExecutionNodeRecord, node_id)
            if route is None or run is None or node is None:
                return
            if (
                node.claim_fencing_token != fencing_token
                or node.claim_owner_id != CLASSIFIER_VERSION
            ):
                return
            now = utc_now()
            route.result_manifest = result_manifest
            route.result_digest = result_digest
            route.status = TurnRouteStatus.FAILED.value
            route.error_code = error_code
            route.revision += 1
            route.updated_at = now
            node.status = ExecutionNodeStatus.FAILED.value
            self._clear_claim(node)
            node.revision += 1
            node.updated_at = now
            run.status = ExecutionRunStatus.FAILED.value
            run.revision += 1
            run.updated_at = now

    async def _fail_preparation(self, task_id: str, error_code: str) -> None:
        async with self._database.session() as session, session.begin():
            route = await session.get(TurnRouteRecord, task_id)
            if route is None:
                return
            route.status = TurnRouteStatus.FAILED.value
            route.error_code = error_code
            route.revision += 1
            route.updated_at = utc_now()

    @staticmethod
    def _clear_claim(node: TaskExecutionNodeRecord) -> None:
        node.claim_owner_id = None
        node.claim_acquired_at = None
        node.claim_heartbeat_at = None
        node.claim_expires_at = None

    def _validate_record(self, record: TurnRouteRecord, message_digest: str) -> None:
        try:
            decision = TurnRouteDecision(record.decision)
            status = TurnRouteStatus(record.status)
        except ValueError as error:
            raise TurnRouteProofRejectedError("Turn Route state is invalid") from error
        route_id = cast(RouteId | None, record.route_id)
        if decision is TurnRouteDecision.ROUTED:
            if route_id not in _ROUTE_SPECS or record.route_version != "1":
                raise TurnRouteProofRejectedError("Turn Route binding is unknown")
            expected_manifest = self.route_manifest_digest(route_id)
            if record.route_manifest_digest != expected_manifest:
                raise TurnRouteProofRejectedError("Turn Route manifest changed")
            if status is TurnRouteStatus.NOT_APPLICABLE:
                raise TurnRouteProofRejectedError("Routed Turn cannot be non-executable")
        elif (
            any(
                value is not None
                for value in (record.route_id, record.route_version, record.route_manifest_digest)
            )
            or status is not TurnRouteStatus.NOT_APPLICABLE
        ):
            raise TurnRouteProofRejectedError("Non-routed Turn carries an execution binding")
        if record.parameter_digest != sha256_digest(record.parameters):
            raise TurnRouteProofRejectedError("Turn Route parameters changed")
        candidate_material = {
            "message_digest": message_digest,
            "decision": record.decision,
            "route_id": record.route_id,
            "parameters": record.parameters,
            "reason_code": record.reason_code,
        }
        valid_candidate_digests = {
            sha256_digest({"classifier_version": version, **candidate_material})
            for version in (CLASSIFIER_VERSION, *_LEGACY_CLASSIFIER_VERSIONS)
        }
        if record.candidate_digest not in valid_candidate_digests:
            raise TurnRouteProofRejectedError("Turn Route candidate changed")
        resolution_values = (
            record.resolved_from_task_id,
            record.resolution_rule,
            record.resolution_digest,
        )
        if any(value is not None for value in resolution_values) != all(
            value is not None for value in resolution_values
        ):
            raise TurnRouteProofRejectedError("Turn Route resolution binding is incomplete")
        if record.resolved_from_task_id is not None and decision is not TurnRouteDecision.ROUTED:
            raise TurnRouteProofRejectedError("Non-routed Turn carries a resolution binding")
        self._validated_result(record)

    async def _validate_resolution(
        self,
        session: AsyncSession,
        record: TurnRouteRecord,
        message_digest: str,
    ) -> None:
        current = record
        current_message_digest = message_digest
        seen = {record.task_id}
        while current.resolved_from_task_id is not None:
            if current.resolved_from_task_id in seen:
                raise TurnRouteProofRejectedError("Turn Route resolution contains a cycle")
            seen.add(current.resolved_from_task_id)
            source = await session.get(TurnRouteRecord, current.resolved_from_task_id)
            if source is None:
                raise TurnRouteProofRejectedError("Clarification source Turn Route is missing")
            source_message = await session.get(ConversationMessageRecord, source.user_message_id)
            if source_message is None:
                raise TurnRouteProofRejectedError("Clarification source message is missing")
            self._validate_record(source, source_message.message_digest)
            if (
                source.conversation_id != current.conversation_id
                or not self._is_resolution_source(
                    self._read(source), cast(str, current.resolution_rule)
                )
                or source.created_at > current.created_at
            ):
                raise TurnRouteProofRejectedError("Clarification source binding is invalid")
            expected = self._resolution_digest(
                source=self._read(source),
                rule=cast(str, current.resolution_rule),
                target_user_message_id=current.user_message_id,
                target_message_digest=current_message_digest,
                target_candidate_digest=current.candidate_digest,
                target_parameter_digest=current.parameter_digest,
            )
            if current.resolution_digest != expected:
                raise TurnRouteProofRejectedError("Turn Route resolution proof changed")
            current = source
            current_message_digest = source_message.message_digest

    @staticmethod
    def _is_resolution_source(source: TurnRouteRead, rule: str) -> bool:
        if source.decision is TurnRouteDecision.NEEDS_CLARIFICATION:
            return True
        return bool(
            rule == "agent_workspace_file_path"
            and source.decision is TurnRouteDecision.ROUTED
            and source.route_id == "workspace_file_read"
            and source.status is TurnRouteStatus.WAITING_USER_INPUT
        )

    @staticmethod
    def _resolution_digest(
        *,
        source: TurnRouteRead,
        rule: str,
        target_user_message_id: str,
        target_message_digest: str,
        target_candidate_digest: str,
        target_parameter_digest: str,
    ) -> str:
        return sha256_digest(
            {
                "schema_version": "deskpilot.turn-route-resolution.v1",
                "source_task_id": source.task_id,
                "source_user_message_id": source.user_message_id,
                "source_candidate_digest": source.candidate_digest,
                "source_parameter_digest": source.parameter_digest,
                "source_reason_code": source.reason_code,
                "resolution_rule": rule,
                "target_user_message_id": target_user_message_id,
                "target_message_digest": target_message_digest,
                "target_candidate_digest": target_candidate_digest,
                "target_parameter_digest": target_parameter_digest,
            }
        )

    @staticmethod
    def _validated_result(
        record: TurnRouteRecord,
    ) -> tuple[
        KnowledgeSearchRead | None,
        McpToolCallRead | None,
        WorkspaceFileRead | None,
        WorkspaceEditPreview | WorkspaceEditReceipt | None,
        WorkspacePatchPreview | WorkspacePatchReceipt | None,
        WorkspaceDirectoryRead | None,
        WorkspaceCheckRead | None,
        WorkspacePythonTestRead | None,
        WorkspaceNodeTestRead | None,
        WorkspacePathOperationPreview | WorkspacePathOperationReceipt | None,
    ]:
        if record.result_manifest is None:
            if record.result_digest is not None:
                raise TurnRouteProofRejectedError("Turn Route result digest is orphaned")
            return None, None, None, None, None, None, None, None, None, None
        try:
            if record.route_id == "knowledge_lookup":
                knowledge_result = KnowledgeSearchRead.model_validate(record.result_manifest)
                expected = sha256_digest(
                    {
                        "query_digest": knowledge_result.query_digest,
                        "citations": [
                            item.model_dump(mode="json") for item in knowledge_result.citations
                        ],
                        "searched_sources": knowledge_result.searched_sources,
                        "stale_source_ids": list(knowledge_result.stale_source_ids),
                    }
                )
                if knowledge_result.result_digest != expected or record.result_digest != expected:
                    raise TurnRouteProofRejectedError("Knowledge Route result changed")
                return knowledge_result, None, None, None, None, None, None, None, None, None
            if record.route_id == "mcp_text_metrics":
                mcp_result = McpToolCallRead.model_validate(record.result_manifest)
                expected = sha256_digest(
                    {
                        "server_id": mcp_result.server_id,
                        "tool_name": mcp_result.tool_name,
                        "protocol_version": mcp_result.protocol_version,
                        "structured_content": mcp_result.structured_content,
                    }
                )
                if mcp_result.result_digest != expected or record.result_digest != expected:
                    raise TurnRouteProofRejectedError("MCP Route result changed")
                return None, mcp_result, None, None, None, None, None, None, None, None
            if record.route_id == "workspace_file_read":
                workspace_file = WorkspaceFileRead.model_validate(record.result_manifest)
                if record.result_digest != workspace_file.result_digest:
                    raise TurnRouteProofRejectedError("Workspace file read result changed")
                return None, None, workspace_file, None, None, None, None, None, None, None
            if record.route_id in {
                "workspace_directory_list",
                "workspace_directory_analyze",
            }:
                directory = WorkspaceDirectoryRead.model_validate(record.result_manifest)
                if record.result_digest != directory.result_digest:
                    raise TurnRouteProofRejectedError("Workspace directory result changed")
                return None, None, None, None, None, directory, None, None, None, None
            if record.route_id == "workspace_snapshot_check":
                check = WorkspaceCheckRead.model_validate(record.result_manifest)
                if record.result_digest != check.result_digest:
                    raise TurnRouteProofRejectedError("Workspace check result changed")
                return None, None, None, None, None, None, check, None, None, None
            if record.route_id == "workspace_python_test":
                python_test = WorkspacePythonTestRead.model_validate(record.result_manifest)
                if record.result_digest != python_test.result_digest:
                    raise TurnRouteProofRejectedError("Workspace Python test result changed")
                return None, None, None, None, None, None, None, python_test, None, None
            if record.route_id == "workspace_node_test":
                node_test = WorkspaceNodeTestRead.model_validate(record.result_manifest)
                if record.result_digest != node_test.result_digest:
                    raise TurnRouteProofRejectedError("Workspace Node test result changed")
                return None, None, None, None, None, None, None, None, node_test, None
            if record.route_id == "workspace_file_replace":
                schema_version = record.result_manifest.get("schema_version")
                if schema_version == "deskpilot.workspace-edit-preview.v1":
                    preview = WorkspaceEditPreview.model_validate(record.result_manifest)
                    if record.result_digest != preview.confirmation_digest:
                        raise TurnRouteProofRejectedError("Workspace replacement preview changed")
                    return None, None, None, preview, None, None, None, None, None, None
                if schema_version == "deskpilot.workspace-edit-receipt.v1":
                    receipt = WorkspaceEditReceipt.model_validate(record.result_manifest)
                    if record.result_digest != receipt.receipt_digest:
                        raise TurnRouteProofRejectedError("Workspace replacement receipt changed")
                    return None, None, None, receipt, None, None, None, None, None, None
                raise TurnRouteProofRejectedError("Workspace replacement result schema is unknown")
            if record.route_id == "workspace_patch_bundle":
                schema_version = record.result_manifest.get("schema_version")
                if schema_version == "deskpilot.workspace-patch-preview.v1":
                    patch_preview = WorkspacePatchPreview.model_validate(record.result_manifest)
                    if record.result_digest != patch_preview.confirmation_digest:
                        raise TurnRouteProofRejectedError("Workspace patch preview changed")
                    return None, None, None, None, patch_preview, None, None, None, None, None
                if schema_version == "deskpilot.workspace-patch-receipt.v1":
                    patch_receipt = WorkspacePatchReceipt.model_validate(record.result_manifest)
                    if record.result_digest != patch_receipt.receipt_digest:
                        raise TurnRouteProofRejectedError("Workspace patch receipt changed")
                    return None, None, None, None, patch_receipt, None, None, None, None, None
                raise TurnRouteProofRejectedError("Workspace patch result schema is unknown")
            if record.route_id == "workspace_agent_patch_test":
                schema_version = record.result_manifest.get("schema_version")
                if schema_version == "deskpilot.workspace-patch-preview.v1":
                    agent_patch_preview = WorkspacePatchPreview.model_validate(
                        record.result_manifest
                    )
                    if record.result_digest != agent_patch_preview.confirmation_digest:
                        raise TurnRouteProofRejectedError("Agent patch preview changed")
                    return (
                        None,
                        None,
                        None,
                        None,
                        agent_patch_preview,
                        None,
                        None,
                        None,
                        None,
                        None,
                    )
                if schema_version == "deskpilot.workspace-patch-test.v1":
                    patch_test = WorkspacePatchTestRead.model_validate(record.result_manifest)
                    if record.result_digest != patch_test.result_digest:
                        raise TurnRouteProofRejectedError("Agent patch test result changed")
                    return (
                        None,
                        None,
                        None,
                        None,
                        patch_test.patch_receipt,
                        None,
                        None,
                        patch_test.python_test,
                        patch_test.node_test,
                        None,
                    )
                raise TurnRouteProofRejectedError("Agent patch result schema is unknown")
            if record.route_id == "workspace_dynamic_patch_test":
                schema_version = record.result_manifest.get("schema_version")
                if schema_version == "deskpilot.workspace-patch-preview.v1":
                    dynamic_preview = WorkspacePatchPreview.model_validate(
                        record.result_manifest
                    )
                    if record.result_digest != dynamic_preview.confirmation_digest:
                        raise TurnRouteProofRejectedError("Dynamic Patch approval proof changed")
                    return (
                        None,
                        None,
                        None,
                        None,
                        dynamic_preview,
                        None,
                        None,
                        None,
                        None,
                        None,
                    )
                if schema_version == "deskpilot.workspace-patch-test.v1":
                    patch_test = WorkspacePatchTestRead.model_validate(record.result_manifest)
                    if record.result_digest != patch_test.result_digest:
                        raise TurnRouteProofRejectedError("Dynamic Patch result proof changed")
                    return (
                        None,
                        None,
                        None,
                        None,
                        patch_test.patch_receipt,
                        None,
                        None,
                        patch_test.python_test,
                        patch_test.node_test,
                        None,
                    )
                if schema_version == "deskpilot.workspace-directory-read.v1":
                    directory = WorkspaceDirectoryRead.model_validate(record.result_manifest)
                    if record.result_digest != directory.result_digest:
                        raise TurnRouteProofRejectedError("Dynamic Patch output proof changed")
                    return None, None, None, None, None, directory, None, None, None, None
                raise TurnRouteProofRejectedError("Dynamic Patch result schema is unknown")
            if record.route_id in {"workspace_file_create", "workspace_file_rename"}:
                schema_version = record.result_manifest.get("schema_version")
                if schema_version == "deskpilot.workspace-path-operation-preview.v1":
                    operation_preview = WorkspacePathOperationPreview.model_validate(
                        record.result_manifest
                    )
                    if record.result_digest != operation_preview.confirmation_digest:
                        raise TurnRouteProofRejectedError(
                            "Workspace path operation preview changed"
                        )
                    return None, None, None, None, None, None, None, None, None, operation_preview
                if schema_version == "deskpilot.workspace-path-operation-receipt.v1":
                    operation_receipt = WorkspacePathOperationReceipt.model_validate(
                        record.result_manifest
                    )
                    if record.result_digest != operation_receipt.receipt_digest:
                        raise TurnRouteProofRejectedError(
                            "Workspace path operation receipt changed"
                        )
                    return None, None, None, None, None, None, None, None, None, operation_receipt
                raise TurnRouteProofRejectedError(
                    "Workspace path operation result schema is unknown"
                )
        except ValidationError as error:
            raise TurnRouteProofRejectedError("Turn Route result is invalid") from error
        raise TurnRouteProofRejectedError("Turn Route result does not match its binding")

    @staticmethod
    def _read(record: TurnRouteRecord) -> TurnRouteRead:
        created_at = (
            record.created_at.replace(tzinfo=UTC)
            if record.created_at.tzinfo is None
            else record.created_at
        )
        updated_at = (
            record.updated_at.replace(tzinfo=UTC)
            if record.updated_at.tzinfo is None
            else record.updated_at
        )
        return TurnRouteRead(
            task_id=record.task_id,
            conversation_id=record.conversation_id,
            user_message_id=record.user_message_id,
            decision=TurnRouteDecision(record.decision),
            route_id=cast(RouteId | None, record.route_id),
            route_version=cast(Literal["1"] | None, record.route_version),
            route_manifest_digest=record.route_manifest_digest,
            candidate_digest=record.candidate_digest,
            parameter_digest=record.parameter_digest,
            resolved_from_task_id=record.resolved_from_task_id,
            resolution_rule=record.resolution_rule,
            resolution_digest=record.resolution_digest,
            reason_code=record.reason_code,
            status=TurnRouteStatus(record.status),
            result_digest=record.result_digest,
            error_code=record.error_code,
            revision=record.revision,
            created_at=created_at,
            updated_at=updated_at,
        )
