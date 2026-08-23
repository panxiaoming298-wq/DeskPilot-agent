"""Fixed versioned capability declarations with explicit runtime activation."""

from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.task_plans import CapabilityPack
from deskpilot.domain.tool_contracts import ToolRiskLevel


class CapabilityCatalogError(LookupError):
    code = "CAPABILITY_NOT_REGISTERED"


def _pack(
    *,
    version: str = "1.0.0",
    runtime_enabled: bool = False,
    **values: object,
) -> CapabilityPack:
    material = {
        "schema_version": "deskpilot.capability-pack.v1",
        "version": version,
        "runtime_enabled": runtime_enabled,
        **values,
    }
    return CapabilityPack.model_validate({**material, "digest": sha256_digest(material)})


class CapabilityCatalog:
    def __init__(self, packs: tuple[CapabilityPack, ...]) -> None:
        self._packs = {pack.key: pack for pack in packs}
        if len(self._packs) != len(packs):
            raise ValueError("Capability Pack keys must be unique")

    def resolve(
        self, capability_id: str, version: str, digest: str | None = None
    ) -> CapabilityPack:
        try:
            pack = self._packs[(capability_id, version)]
        except KeyError as error:
            raise CapabilityCatalogError("Capability Pack is not registered") from error
        if digest is not None and digest != pack.digest:
            raise CapabilityCatalogError("Capability Pack digest does not match")
        return pack

    def list_public(self) -> tuple[CapabilityPack, ...]:
        return tuple(self._packs[key] for key in sorted(self._packs))

    def resolve_preferred(self, capability_id: str) -> CapabilityPack:
        matches = [pack for pack in self._packs.values() if pack.capability_id == capability_id]
        if not matches:
            raise CapabilityCatalogError("Capability Pack is not registered")
        return max(matches, key=lambda pack: tuple(int(item) for item in pack.version.split(".")))


def create_builtin_capability_catalog(
    *, research_runtime_enabled: bool = False
) -> CapabilityCatalog:
    return CapabilityCatalog(
        (
            # Phase 69's declaration remains immutable and permanently disabled.
            _pack(
                capability_id="research.read.v1",
                description="受控搜索、页面读取与待验证 Claim/Citation 写入。",
                allowed_operations=("web.search", "web.page.read", "research.claim.write"),
                max_risk_level=ToolRiskLevel.R0,
                external_ingress=True,
                external_egress=True,
                workspace_write=False,
            ),
            _pack(
                capability_id="research.read.v1",
                version="1.1.0",
                runtime_enabled=research_runtime_enabled,
                description="受控搜索、无脚本页面读取与待验证 Claim/Citation 写入。",
                allowed_operations=("web.search", "web.page.read", "research.claim.write"),
                max_risk_level=ToolRiskLevel.R0,
                external_ingress=True,
                external_egress=True,
                workspace_write=False,
            ),
            _pack(
                capability_id="artifact.html.v1",
                description="仅在绑定 Task Workspace 中创建、读取和修订静态 HTML/CSS。",
                allowed_operations=(
                    "workspace.file.create",
                    "workspace.file.patch",
                    "workspace.file.read",
                ),
                max_risk_level=ToolRiskLevel.R1,
                external_ingress=False,
                external_egress=False,
                workspace_write=True,
            ),
            _pack(
                capability_id="artifact.html.v1",
                version="1.1.0",
                runtime_enabled=True,
                description=(
                    "在绑定 Task Workspace 中以不可变 revision 和 receipt 写静态 HTML，"
                    "并生成同源 Markdown 伴生交付。"
                ),
                allowed_operations=(
                    "workspace.file.create",
                    "workspace.file.patch",
                    "workspace.file.read",
                ),
                max_risk_level=ToolRiskLevel.R1,
                external_ingress=False,
                external_egress=False,
                workspace_write=True,
            ),
            _pack(
                capability_id="artifact.html.v1",
                version="1.2.0",
                runtime_enabled=True,
                description=(
                    "在绑定 Task Workspace 中以不可变 revision 和 receipt 写静态 HTML、"
                    "同源 Markdown，并以隔离打印和逐页栅格化生成已验收 PDF。"
                ),
                allowed_operations=(
                    "workspace.file.create",
                    "workspace.file.patch",
                    "workspace.file.read",
                    "document.render",
                ),
                max_risk_level=ToolRiskLevel.R1,
                external_ingress=False,
                external_egress=False,
                workspace_write=True,
            ),
            _pack(
                capability_id="browser.verify.v1",
                description="无登录、默认断网的新浏览器上下文只读验收。",
                allowed_operations=("browser.render", "browser.evidence.read"),
                max_risk_level=ToolRiskLevel.R0,
                external_ingress=False,
                external_egress=False,
                workspace_write=False,
            ),
            _pack(
                capability_id="browser.verify.v1",
                version="1.1.0",
                runtime_enabled=True,
                description="新建无登录、断网浏览器 profile 验收绑定 HTML revision。",
                allowed_operations=("browser.render", "browser.evidence.read"),
                max_risk_level=ToolRiskLevel.R0,
                external_ingress=False,
                external_egress=False,
                workspace_write=False,
            ),
            _pack(
                capability_id="knowledge.local.v1",
                runtime_enabled=True,
                description="检索已显式导入且来源版本仍有效的本地文本知识。",
                allowed_operations=("knowledge.search", "knowledge.citation.read"),
                max_risk_level=ToolRiskLevel.R0,
                external_ingress=True,
                external_egress=False,
                workspace_write=False,
            ),
            _pack(
                capability_id="mcp.text.metrics.v1",
                runtime_enabled=True,
                description="调用固定内置 MCP Server 计算只读文本指标。",
                allowed_operations=("mcp.text.metrics",),
                max_risk_level=ToolRiskLevel.R0,
                external_ingress=True,
                external_egress=False,
                workspace_write=False,
            ),
            _pack(
                capability_id="workspace.file.read.v1",
                runtime_enabled=True,
                description="只读取显式配置工作区内的受限 UTF-8 文本文件。",
                allowed_operations=("conversation-workspace.file.read",),
                max_risk_level=ToolRiskLevel.R0,
                external_ingress=True,
                external_egress=False,
                workspace_write=False,
            ),
            _pack(
                capability_id="workspace.directory.read.v1",
                runtime_enabled=True,
                description="只列出显式配置工作区目录中的受限直接子项。",
                allowed_operations=("conversation-workspace.directory.read",),
                max_risk_level=ToolRiskLevel.R0,
                external_ingress=True,
                external_egress=False,
                workspace_write=False,
            ),
            _pack(
                capability_id="workspace.snapshot.check.v1",
                runtime_enabled=True,
                description="在断网隔离进程中执行固定 Python/JSON 快照解析检查。",
                allowed_operations=("conversation-workspace.snapshot.check",),
                max_risk_level=ToolRiskLevel.R0,
                external_ingress=True,
                external_egress=False,
                workspace_write=False,
            ),
            _pack(
                capability_id="workspace.python.test.v1",
                runtime_enabled=True,
                description=("在断网 AppContainer 中对有界项目快照运行一个显式 pytest 文件。"),
                allowed_operations=("conversation-workspace.python.test",),
                max_risk_level=ToolRiskLevel.R0,
                external_ingress=True,
                external_egress=False,
                workspace_write=False,
            ),
            _pack(
                capability_id="workspace.node.test.v1",
                runtime_enabled=True,
                description=("在断网 AppContainer 中对有界项目快照运行一个显式 node:test 文件。"),
                allowed_operations=("conversation-workspace.node.test",),
                max_risk_level=ToolRiskLevel.R0,
                external_ingress=True,
                external_egress=False,
                workspace_write=False,
            ),
            _pack(
                capability_id="workspace.patch.propose.v1",
                runtime_enabled=True,
                description=(
                    "基于服务器绑定的单文件观察提出一次精确替换；提案本身不授予写权限。"
                ),
                allowed_operations=("conversation-workspace.patch.propose",),
                max_risk_level=ToolRiskLevel.R0,
                external_ingress=True,
                external_egress=False,
                workspace_write=False,
            ),
            _pack(
                capability_id="workspace.file.replace.v1",
                runtime_enabled=True,
                description="经预览确认后精确替换一个文本片段，并保留安全备份与回执。",
                allowed_operations=("conversation-workspace.file.replace",),
                max_risk_level=ToolRiskLevel.R1,
                external_ingress=True,
                external_egress=False,
                workspace_write=True,
            ),
            _pack(
                capability_id="workspace.file.create.v1",
                runtime_enabled=True,
                description="经预览确认后创建一个不存在的受限 UTF-8 文件，并保留恢复证明与回执。",
                allowed_operations=("conversation-workspace.file.create",),
                max_risk_level=ToolRiskLevel.R1,
                external_ingress=True,
                external_egress=False,
                workspace_write=True,
            ),
            _pack(
                capability_id="workspace.file.rename.v1",
                runtime_enabled=True,
                description="经预览确认后原子重命名一个受限文件，并绑定原版本与目标目录。",
                allowed_operations=("conversation-workspace.file.rename",),
                max_risk_level=ToolRiskLevel.R1,
                external_ingress=True,
                external_egress=False,
                workspace_write=True,
            ),
            _pack(
                capability_id="workspace.patch.bundle.v1",
                runtime_enabled=True,
                description="隔离预演 2–8 个精确文本替换，经一次确认后按序提交并逐项保留备份。",
                allowed_operations=("conversation-workspace.patch.bundle",),
                max_risk_level=ToolRiskLevel.R1,
                external_ingress=True,
                external_egress=False,
                workspace_write=True,
            ),
        )
    )
