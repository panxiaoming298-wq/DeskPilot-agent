"""Trusted binding from untrusted Agent selectors to exact immutable references."""

from deskpilot.application.agent_registry import AgentRegistry, AgentRegistryError
from deskpilot.domain.agent_contracts import (
    AgentPlanDraftStep,
    BoundAgentPlanStep,
    BoundAgentRef,
)


class AgentPlanBindingError(AgentRegistryError):
    code = "AGENT_PLAN_BINDING_REJECTED"


class AgentToolNotAllowedError(AgentPlanBindingError):
    code = "AGENT_TOOL_NOT_ALLOWED"


class AgentBudgetExceededError(AgentPlanBindingError):
    code = "AGENT_BUDGET_EXCEEDED"


class AgentPlanBinder:
    def __init__(self, registry: AgentRegistry) -> None:
        self._registry = registry

    def bind(self, draft: AgentPlanDraftStep) -> BoundAgentPlanStep:
        registration = self._registry.resolve_preferred(draft.agent_selector)
        contract = registration.contract
        tool = None
        if draft.tool_name is not None and draft.tool_version is not None:
            tool = next(
                (
                    grant
                    for grant in contract.tool_policy.grants
                    if grant.key == (draft.tool_name, draft.tool_version)
                ),
                None,
            )
            if tool is None:
                raise AgentToolNotAllowedError("Draft Tool is outside the Agent Contract")
        limits = contract.budget_policy
        requested = draft.budget
        if (
            requested.model_calls > limits.max_model_calls
            or requested.tool_calls > limits.max_tool_calls
            or requested.input_tokens > limits.max_input_tokens
            or requested.output_tokens > limits.max_output_tokens
            or requested.wall_seconds > limits.max_wall_seconds
            or requested.retries > limits.max_retries
            or requested.cost_micros > limits.max_cost_micros
            or requested.handoffs > limits.max_handoffs
        ):
            raise AgentBudgetExceededError("Draft budget exceeds the Agent Contract")
        return BoundAgentPlanStep(
            step_id=draft.step_id,
            agent=BoundAgentRef(
                agent_id=contract.agent_id,
                version=contract.version,
                contract_digest=contract.digest,
                prompt_package_digest=registration.prompt_package.digest,
            ),
            tool=tool,
            budget=requested,
        )

    def validate_bound(self, bound: BoundAgentPlanStep) -> None:
        registration = self._registry.resolve_exact(
            bound.agent.agent_id,
            bound.agent.version,
            contract_digest=bound.agent.contract_digest,
            prompt_package_digest=bound.agent.prompt_package_digest,
        )
        if bound.tool is not None and bound.tool not in registration.contract.tool_policy.grants:
            raise AgentToolNotAllowedError("Bound Tool no longer matches the Agent Contract")
