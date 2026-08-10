"""Fail-closed built-in policy evaluation for Tool Runner admission."""

from collections.abc import Iterable
from typing import Protocol

from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.policy import (
    PolicyDecision,
    PolicyEffect,
    ToolAuthorizationRequest,
)
from deskpilot.domain.tool_contracts import ToolRiskLevel
from deskpilot.tools.files import (
    FILE_MOVE_CONTRACT,
    FILE_MOVE_DESTINATION_CAPABILITY,
    FILE_MOVE_SOURCE_CAPABILITY,
)

DEFAULT_POLICY_REVISION = "builtin-tool-policy-v1"
DEFAULT_APPROVAL_TTL_SECONDS = 300


class PolicyEngine(Protocol):
    def evaluate(self, request: ToolAuthorizationRequest) -> PolicyDecision: ...


class BuiltinPolicyEngine:
    """Evaluate trusted contract facts without consulting a model or free-form text."""

    def __init__(
        self,
        *,
        allowed_capabilities: Iterable[str] = ("filesystem.metadata.read",),
        allowed_resource_scopes: Iterable[tuple[str, str]] = (),
        allow_user_selected_file_move: bool = False,
        require_approval_for_r0: bool = False,
        enable_r3: bool = False,
        policy_revision: str = DEFAULT_POLICY_REVISION,
        approval_ttl_seconds: int = DEFAULT_APPROVAL_TTL_SECONDS,
    ) -> None:
        self._allowed_capabilities = frozenset(allowed_capabilities)
        self._allowed_resource_scopes = frozenset(allowed_resource_scopes)
        self._allow_user_selected_file_move = allow_user_selected_file_move
        self._require_approval_for_r0 = require_approval_for_r0
        self._enable_r3 = enable_r3
        self._policy_revision = policy_revision
        self._approval_ttl_seconds = approval_ttl_seconds

        if any(not capability for capability in self._allowed_capabilities):
            raise ValueError("allowed capabilities must not contain empty values")
        if any(not kind or not identifier for kind, identifier in self._allowed_resource_scopes):
            raise ValueError("allowed resource scopes require non-empty kind and identifier")
        if not 1 <= approval_ttl_seconds <= 86_400:
            raise ValueError("approval_ttl_seconds must be between 1 and 86400")

    @property
    def policy_revision(self) -> str:
        return self._policy_revision

    def evaluate(self, request: ToolAuthorizationRequest) -> PolicyDecision:
        effective_risk = self._effective_risk(request)

        if request.risk_level is ToolRiskLevel.R4:
            return self._decision(
                request,
                effect=PolicyEffect.DENY,
                effective_risk=ToolRiskLevel.R4,
                rule_id="project.r4-prohibited",
                reason_code="RISK_LEVEL_PROHIBITED",
            )

        if not request.interactive:
            return self._decision(
                request,
                effect=PolicyEffect.DENY,
                effective_risk=effective_risk,
                rule_id="interaction.interactive-only",
                reason_code="NON_INTERACTIVE_NOT_SUPPORTED",
            )

        if request.batch_count != 1:
            return self._decision(
                request,
                effect=PolicyEffect.DENY,
                effective_risk=effective_risk,
                rule_id="batch.single-call-only",
                reason_code="BATCH_NOT_SUPPORTED",
            )

        if request.origin != "builtin":
            return self._decision(
                request,
                effect=PolicyEffect.DENY,
                effective_risk=effective_risk,
                rule_id="origin.builtin-only",
                reason_code="TOOL_ORIGIN_NOT_ALLOWED",
            )

        requested_capabilities = frozenset(request.capabilities)
        if not requested_capabilities.issubset(self._allowed_capabilities):
            return self._decision(
                request,
                effect=PolicyEffect.DENY,
                effective_risk=effective_risk,
                rule_id="capability.allowlist",
                reason_code="CAPABILITY_NOT_ALLOWED",
            )

        scoped_operations = frozenset(
            operation for resource in request.resources for operation in resource.operations
        )
        if scoped_operations != requested_capabilities:
            return self._decision(
                request,
                effect=PolicyEffect.DENY,
                effective_risk=effective_risk,
                rule_id="resource.capability-binding",
                reason_code="RESOURCE_CAPABILITY_MISMATCH",
            )

        if not self._resource_scope_allowed(request):
            return self._decision(
                request,
                effect=PolicyEffect.DENY,
                effective_risk=effective_risk,
                rule_id="resource.exact-scope",
                reason_code="RESOURCE_SCOPE_DENIED",
            )

        if request.risk_level is ToolRiskLevel.R0 and (
            request.side_effects
            or request.reversible
            or request.network_access
            or request.data_egress
        ):
            return self._decision(
                request,
                effect=PolicyEffect.DENY,
                effective_risk=effective_risk,
                rule_id="contract.r0-consistency",
                reason_code="R0_CONTRACT_INCONSISTENT",
            )

        if effective_risk is ToolRiskLevel.R0:
            if self._require_approval_for_r0:
                return self._decision(
                    request,
                    effect=PolicyEffect.REQUIRE_APPROVAL,
                    effective_risk=effective_risk,
                    rule_id="default.r0-explicit-approval",
                    reason_code="R0_APPROVAL_REQUIRED",
                    approval_ttl_seconds=self._approval_ttl_seconds,
                )
            return self._decision(
                request,
                effect=PolicyEffect.ALLOW,
                effective_risk=effective_risk,
                rule_id="default.r0-allow",
                reason_code="DEFAULT_R0_ALLOW",
            )

        if effective_risk in {ToolRiskLevel.R1, ToolRiskLevel.R2}:
            return self._decision(
                request,
                effect=PolicyEffect.REQUIRE_APPROVAL,
                effective_risk=effective_risk,
                rule_id="default.interactive-approval",
                reason_code="APPROVAL_REQUIRED",
                approval_ttl_seconds=self._approval_ttl_seconds,
            )

        if effective_risk is ToolRiskLevel.R3 and self._enable_r3:
            return self._decision(
                request,
                effect=PolicyEffect.REQUIRE_APPROVAL,
                effective_risk=effective_risk,
                rule_id="default.r3-explicit-approval",
                reason_code="R3_APPROVAL_REQUIRED",
                approval_ttl_seconds=self._approval_ttl_seconds,
            )

        return self._decision(
            request,
            effect=PolicyEffect.DENY,
            effective_risk=effective_risk,
            rule_id="default.r3-disabled",
            reason_code="R3_DISABLED",
        )

    def _resource_scope_allowed(self, request: ToolAuthorizationRequest) -> bool:
        if all(
            resource.scope_key in self._allowed_resource_scopes
            for resource in request.resources
        ):
            return True
        if not self._allow_user_selected_file_move:
            return False
        if (
            request.actor != "local_user"
            or (request.tool_name, request.tool_version) != FILE_MOVE_CONTRACT.key
            or request.contract_digest != FILE_MOVE_CONTRACT.digest
            or request.risk_level is not ToolRiskLevel.R1
            or request.side_effects != ("filesystem_write",)
            or not request.reversible
            or request.network_access
            or request.data_egress
            or not request.interactive
            or request.batch_count != 1
            or request.capabilities
            != (
                FILE_MOVE_DESTINATION_CAPABILITY,
                FILE_MOVE_SOURCE_CAPABILITY,
            )
            or len(request.resources) != 2
        ):
            return False
        resources_by_operation = {
            operation: resource
            for resource in request.resources
            for operation in resource.operations
        }
        if set(resources_by_operation) != set(request.capabilities):
            return False
        source = resources_by_operation[FILE_MOVE_SOURCE_CAPABILITY]
        destination = resources_by_operation[FILE_MOVE_DESTINATION_CAPABILITY]
        return (
            source.kind == "filesystem_path"
            and destination.kind == "filesystem_path"
            and source.identifier != destination.identifier
            and source.operations == (FILE_MOVE_SOURCE_CAPABILITY,)
            and destination.operations == (FILE_MOVE_DESTINATION_CAPABILITY,)
            and source.version_digest is not None
            and destination.version_digest is None
        )

    @staticmethod
    def _effective_risk(request: ToolAuthorizationRequest) -> ToolRiskLevel:
        declared = request.risk_level
        if declared is ToolRiskLevel.R4:
            return ToolRiskLevel.R4
        if request.data_egress or request.origin != "builtin":
            return ToolRiskLevel.R3
        if request.network_access or request.side_effects or request.reversible:
            return max(declared, ToolRiskLevel.R1, key=_risk_ordinal)
        return declared

    def _decision(
        self,
        request: ToolAuthorizationRequest,
        *,
        effect: PolicyEffect,
        effective_risk: ToolRiskLevel,
        rule_id: str,
        reason_code: str,
        approval_ttl_seconds: int | None = None,
    ) -> PolicyDecision:
        facts = {
            "request_digest": request.request_digest,
            "resource_scope_digest": request.resource_scope_digest,
            "effect": effect.value,
            "effective_risk": effective_risk.value,
            "policy_revision": self._policy_revision,
            "rule_id": rule_id,
            "reason_code": reason_code,
            "approval_ttl_seconds": approval_ttl_seconds,
        }
        return PolicyDecision(
            decision_id=f"pdec_{sha256_digest(facts)}",
            effect=effect,
            effective_risk=effective_risk,
            policy_revision=self._policy_revision,
            rule_id=rule_id,
            reason_code=reason_code,
            request_digest=request.request_digest,
            resource_scope_digest=request.resource_scope_digest,
            approval_ttl_seconds=approval_ttl_seconds,
        )


def _risk_ordinal(risk: ToolRiskLevel) -> int:
    return {
        ToolRiskLevel.R0: 0,
        ToolRiskLevel.R1: 1,
        ToolRiskLevel.R2: 2,
        ToolRiskLevel.R3: 3,
        ToolRiskLevel.R4: 4,
    }[risk]
