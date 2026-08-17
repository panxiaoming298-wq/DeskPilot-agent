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
                    "在绑定 Task Workspace 中以不可变 revision 和 receipt 写静态 HTML/CSS。"
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
        )
    )
