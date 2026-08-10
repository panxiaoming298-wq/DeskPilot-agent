"""Runner-side allowlist checks performed before any tool implementation executes."""

import hmac
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import BaseModel, ValidationError

from deskpilot.application.tool_registry import ToolRegistration, ToolRegistry
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.policy import PolicyResource, ToolAuthorizationRequest
from deskpilot.domain.tool_contracts import ToolIdempotency, ToolRiskLevel
from deskpilot.runner.ipc_protocol import (
    IpcProtocolError,
    IpcVerifier,
    SignedIpcEnvelope,
    ToolCallRequest,
    UnexpectedMessageError,
)


class ToolContractMismatchError(IpcProtocolError):
    code = "TOOL_CONTRACT_MISMATCH"


class MissingIdempotencyKeyError(IpcProtocolError):
    code = "TOOL_IDEMPOTENCY_KEY_REQUIRED"


class MissingPolicyAuthorizationError(IpcProtocolError):
    code = "POLICY_AUTHORIZATION_REQUIRED"


class PolicyAuthorizationMismatchError(IpcProtocolError):
    code = "POLICY_AUTHORIZATION_MISMATCH"


class PolicyAuthorizationExpiredError(IpcProtocolError):
    code = "POLICY_AUTHORIZATION_EXPIRED"


@dataclass(frozen=True, slots=True)
class AuthorizedToolCall:
    request: ToolCallRequest
    registration: ToolRegistration
    arguments: BaseModel


class ToolCallAuthorizer:
    def __init__(self, *, verifier: IpcVerifier, registry: ToolRegistry) -> None:
        self._verifier = verifier
        self._registry = registry

    def authorize(
        self,
        envelope: SignedIpcEnvelope,
        *,
        now: datetime | None = None,
    ) -> AuthorizedToolCall:
        payload = self._verifier.verify(envelope, now=now)
        if not isinstance(payload, ToolCallRequest):
            raise UnexpectedMessageError("Runner authorizer accepts only tool.call messages")

        registration = self._registry.resolve(payload.tool_name, payload.tool_version)
        if not hmac.compare_digest(payload.contract_digest, registration.contract.digest):
            raise ToolContractMismatchError("Signed tool contract digest does not match allowlist")
        if (
            registration.contract.execution.idempotency is ToolIdempotency.KEY_REQUIRED
            and payload.idempotency_key is None
        ):
            raise MissingIdempotencyKeyError("This tool contract requires an idempotency key")

        authorization = payload.authorization
        arguments = self._registry.validate_input(
            payload.tool_name,
            payload.tool_version,
            payload.arguments,
        )
        projected_resources = self._registry.project_resources(
            payload.tool_name,
            payload.tool_version,
            arguments,
        )
        reconstructed_request = self._reconstructed_request(
            payload,
            registration,
            projected_resources,
        )
        if (
            not hmac.compare_digest(
                authorization.authorization_id,
                authorization.expected_authorization_id,
            )
            or not hmac.compare_digest(
                authorization.request_digest,
                reconstructed_request.request_digest,
            )
            or authorization.task_id != payload.task_id
            or authorization.step_id != payload.step_id
            or authorization.call_id != payload.call_id
            or authorization.actor_id != payload.actor
            or authorization.tool_name != payload.tool_name
            or authorization.tool_version != payload.tool_version
            or not hmac.compare_digest(
                authorization.contract_digest,
                payload.contract_digest,
            )
            or not hmac.compare_digest(
                authorization.arguments_digest,
                sha256_digest(payload.arguments),
            )
            or not hmac.compare_digest(
                authorization.expected_resource_versions_digest,
                sha256_digest(payload.expected_resource_versions),
            )
            or authorization.resources != reconstructed_request.resources
        ):
            raise PolicyAuthorizationMismatchError(
                "Signed policy authorization does not match this Tool call"
            )
        contract = registration.contract
        contract_capabilities = tuple(sorted(set(contract.security.capabilities)))
        contract_side_effects = tuple(sorted(set(contract.side_effects)))
        scoped_operations = tuple(
            sorted(
                {
                    operation
                    for resource in authorization.resources
                    for operation in resource.operations
                }
            )
        )
        if (
            authorization.origin != "builtin"
            or authorization.capabilities != contract_capabilities
            or scoped_operations != contract_capabilities
            or authorization.network_access != contract.security.network_access
            or authorization.side_effects != contract_side_effects
            or authorization.reversible != contract.reversible
            or (authorization.data_egress and not authorization.network_access)
        ):
            raise PolicyAuthorizationMismatchError(
                "Signed policy facts do not match the registered Tool Contract"
            )
        if _risk_ordinal(authorization.effective_risk) < _risk_ordinal(contract.risk_level):
            raise PolicyAuthorizationMismatchError(
                "Policy authorization cannot lower the Tool Contract risk"
            )
        authorization_risk = max(
            contract.risk_level,
            authorization.effective_risk,
            key=_risk_ordinal,
        )
        if authorization_risk is not ToolRiskLevel.R0 and any(
            value is None
            for value in (
                authorization.approval_id,
                authorization.preview_hash,
                authorization.approved_at,
                authorization.grant_expires_at,
            )
        ):
            raise MissingPolicyAuthorizationError(
                "This Tool risk level requires an exact user approval"
            )
        current_time = now or datetime.now(UTC)
        if authorization.approved_at is not None and authorization.approved_at > current_time:
            raise PolicyAuthorizationMismatchError("The signed user approval time is in the future")
        if (
            authorization.grant_expires_at is not None
            and authorization.grant_expires_at <= current_time
        ):
            raise PolicyAuthorizationExpiredError("The signed user approval grant has expired")

        return AuthorizedToolCall(payload, registration, arguments)

    @staticmethod
    def _reconstructed_request(
        payload: ToolCallRequest,
        registration: ToolRegistration,
        projected_resources: tuple[PolicyResource, ...],
    ) -> ToolAuthorizationRequest:
        authorization = payload.authorization
        contract = registration.contract
        try:
            request = ToolAuthorizationRequest(
                task_id=payload.task_id,
                step_id=payload.step_id,
                call_id=payload.call_id,
                actor=payload.actor,
                origin=authorization.origin,
                tool_name=payload.tool_name,
                tool_version=payload.tool_version,
                contract_digest=payload.contract_digest,
                arguments_digest=sha256_digest(payload.arguments),
                risk_level=contract.risk_level,
                side_effects=contract.side_effects,
                reversible=contract.reversible,
                capabilities=contract.security.capabilities,
                network_access=contract.security.network_access,
                data_egress=authorization.data_egress,
                resources=projected_resources,
                expected_resource_versions_digest=sha256_digest(payload.expected_resource_versions),
                interactive=authorization.interactive,
                batch_count=authorization.batch_count,
            )
        except ValidationError as error:
            raise PolicyAuthorizationMismatchError(
                "Signed policy facts cannot reconstruct the authorization request"
            ) from error
        if not hmac.compare_digest(
            request.resource_scope_digest,
            authorization.resource_scope_digest,
        ):
            raise PolicyAuthorizationMismatchError(
                "Signed policy resources do not match the resource scope digest"
            )
        return request


def _risk_ordinal(risk: ToolRiskLevel) -> int:
    return {
        ToolRiskLevel.R0: 0,
        ToolRiskLevel.R1: 1,
        ToolRiskLevel.R2: 2,
        ToolRiskLevel.R3: 3,
        ToolRiskLevel.R4: 4,
    }[risk]
