"""Pure proof bridge from verified Agent results to generic capability ResultRefs.

The bridge performs no database I/O and grants no authority.  Callers must load
the immutable model-planner source-step binding, both compiled Plans, and the
existing Agent proof records in one consistent snapshot.  In particular, the
research query and Workspace path are recovered only from the source-step
parameter binding; there is deliberately no TurnRoute fallback.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import ValidationError

from deskpilot.application.artifact_delivery_runtime import POLICY_DIGEST, POLICY_ID
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_runtime import AgentOutputResult, AgentResult
from deskpilot.domain.artifact_runtime import ClaimVerdictRead
from deskpilot.domain.capability_execution import (
    CapabilityResultKind,
    VerifiedCapabilityResultRef,
)
from deskpilot.domain.research import (
    CitationEvidence,
    PageSnapshot,
    ResearchClaim,
    SearchRequest,
)
from deskpilot.domain.task_loop import ModelPlannerNodeMapping, ModelPlannerStepBinding
from deskpilot.domain.task_plans import (
    CapabilityRef,
    DraftNodeKind,
    ExecutablePlan,
    ExecutablePlanNode,
    TaskContract,
)
from deskpilot.domain.workspace_files import WorkspaceFileRead
from deskpilot.infrastructure.models import (
    AgentInvocationRecord,
    AgentResultRecord,
    ClaimVerdictRecord,
    ResearchCitationRecord,
    ResearchClaimRecord,
    ResearchPageSnapshotRecord,
    ResearchSearchCallRecord,
    ResearchSessionRecord,
    TaskExecutionNodeRecord,
    TaskExecutionRunRecord,
    VerificationEvidenceSnapshotRecord,
    VerificationRunRecord,
    WorkspaceAgentResultRecord,
)


class AgentVerifiedResultBridgeError(RuntimeError):
    code = "AGENT_VERIFIED_RESULT_BRIDGE_ERROR"


class AgentVerifiedResultProofRejectedError(AgentVerifiedResultBridgeError):
    code = "AGENT_VERIFIED_RESULT_PROOF_REJECTED"


class AgentVerifiedResultSourceBindingError(AgentVerifiedResultBridgeError):
    code = "AGENT_VERIFIED_RESULT_SOURCE_BINDING_REJECTED"


@dataclass(frozen=True, slots=True)
class AgentVerifiedResultPlanProof:
    """Existing immutable authority and execution records for one Agent node."""

    step_binding: ModelPlannerStepBinding
    source_contract: TaskContract
    source_plan: ExecutablePlan
    composite_plan: ExecutablePlan
    run: TaskExecutionRunRecord
    node: TaskExecutionNodeRecord
    invocation: AgentInvocationRecord
    result: AgentResultRecord


@dataclass(frozen=True, slots=True)
class ResearchAgentVerificationProof:
    """Existing citation-verification records required for a Research bridge."""

    research: ResearchSessionRecord
    search_call: ResearchSearchCallRecord
    search_request: SearchRequest
    verification: VerificationRunRecord
    evidence_snapshot: VerificationEvidenceSnapshotRecord
    claims: tuple[ResearchClaimRecord, ...]
    citations: tuple[ResearchCitationRecord, ...]
    pages: tuple[ResearchPageSnapshotRecord, ...]
    verdicts: tuple[ClaimVerdictRecord, ...]


@dataclass(frozen=True, slots=True)
class WorkspaceReaderVerificationProof:
    """Existing deterministic Workspace Reader result proof."""

    workspace_result: WorkspaceAgentResultRecord


@dataclass(frozen=True, slots=True)
class _ResolvedPlanProof:
    mapping: ModelPlannerNodeMapping
    source_node: ExecutablePlanNode
    composite_node: ExecutablePlanNode
    capability: CapabilityRef


class AgentVerifiedResultBridge:
    """Construct generic ResultRefs only after independently rechecking proof lineage."""

    @classmethod
    def research(
        cls,
        plan_proof: AgentVerifiedResultPlanProof,
        verification_proof: ResearchAgentVerificationProof,
        *,
        allow_pending_node_transition: bool = False,
    ) -> VerifiedCapabilityResultRef:
        resolved = cls._resolve_plan_proof(
            plan_proof,
            expected_route_id="research_to_html",
            expected_source_local_key="research",
            expected_agent_id="builtin.web_researcher",
            expected_capability_id="research.read.v1",
            allow_pending_node_transition=allow_pending_node_transition,
        )
        try:
            result = AgentResult.model_validate(plan_proof.result.manifest)
            claims = tuple(
                ResearchClaim.model_validate(item.manifest) for item in verification_proof.claims
            )
            citations = tuple(
                CitationEvidence.model_validate(item.manifest)
                for item in verification_proof.citations
            )
            pages = tuple(
                PageSnapshot.model_validate(item.manifest) for item in verification_proof.pages
            )
            verdicts = tuple(
                ClaimVerdictRead.model_validate(
                    {
                        "claim_id": item.claim_id,
                        "outcome": item.outcome,
                        "reason_code": item.reason_code,
                        "citation_ids": item.citation_ids,
                        "verdict_digest": item.verdict_digest,
                    }
                )
                for item in verification_proof.verdicts
            )
        except (ValidationError, ValueError) as error:
            raise AgentVerifiedResultProofRejectedError(
                "Research Agent result or evidence Schema was rejected"
            ) from error
        cls._assert_agent_result_record(plan_proof, result)
        cls._assert_research_source_binding(plan_proof, verification_proof)
        cls._assert_research_evidence(
            plan_proof,
            verification_proof,
            result,
            claims,
            citations,
            pages,
            verdicts,
        )
        verification_digest = cls._research_verification_digest(
            verification_proof,
            verdicts,
        )
        return VerifiedCapabilityResultRef.build(
            task_id=plan_proof.run.task_id,
            run_id=plan_proof.run.run_id,
            plan_generation=plan_proof.run.plan_generation,
            producer_node_id=plan_proof.node.node_id,
            producer_attempt=plan_proof.invocation.attempt,
            capability=resolved.capability,
            result_kind=CapabilityResultKind.VERIFIED_CLAIMS,
            result_schema_digest=sha256_digest(AgentResult.model_json_schema()),
            result_digest=result.result_digest,
            verification_digest=verification_digest,
        )

    @classmethod
    def workspace_reader(
        cls,
        plan_proof: AgentVerifiedResultPlanProof,
        verification_proof: WorkspaceReaderVerificationProof,
        *,
        allow_pending_node_transition: bool = False,
        expected_route_id: str = "workspace_file_read",
        expected_source_local_key: str = "workspace_file_read",
        source_parameter_name: str = "path",
    ) -> VerifiedCapabilityResultRef:
        resolved = cls._resolve_plan_proof(
            plan_proof,
            expected_route_id=expected_route_id,
            expected_source_local_key=expected_source_local_key,
            expected_agent_id="builtin.workspace_reader",
            expected_capability_id="workspace.file.read.v1",
            allow_pending_node_transition=allow_pending_node_transition,
        )
        try:
            result = AgentOutputResult.model_validate(plan_proof.result.manifest)
            workspace = WorkspaceFileRead.model_validate(
                verification_proof.workspace_result.manifest
            )
        except ValidationError as error:
            raise AgentVerifiedResultProofRejectedError(
                "Workspace Reader result Schema was rejected"
            ) from error
        cls._assert_agent_result_record(plan_proof, result)
        bound_path = cls._parameter(plan_proof.step_binding, source_parameter_name)
        if workspace.relative_path != bound_path:
            raise AgentVerifiedResultSourceBindingError(
                "Workspace path does not match the immutable source-step binding"
            )
        persisted = verification_proof.workspace_result
        expected_output = {
            "relative_path": workspace.relative_path,
            "result_digest": workspace.result_digest,
        }
        if (
            persisted.invocation_id != plan_proof.invocation.invocation_id
            or persisted.run_id != plan_proof.run.run_id
            or persisted.result_kind != "file"
            or persisted.result_digest != workspace.result_digest
            or result.output != expected_output
            or result.evidence_refs != (f"workspace-file:{workspace.result_digest}",)
            or result.limitation_codes
        ):
            raise AgentVerifiedResultProofRejectedError(
                "Workspace Reader deterministic verification proof changed"
            )
        verification_digest = sha256_digest(
            {
                "schema_version": "deskpilot.workspace-reader-verification-proof.v1",
                "step_binding_digest": plan_proof.step_binding.step_binding_digest,
                "plan_manifest_digest": plan_proof.composite_plan.plan_manifest_digest,
                "node_spec_digest": plan_proof.node.node_spec_digest,
                "invocation_id": plan_proof.invocation.invocation_id,
                "invocation_attempt": plan_proof.invocation.attempt,
                "agent_result_digest": result.result_digest,
                "workspace_result_digest": workspace.result_digest,
                "bound_path_digest": sha256_digest({"value": bound_path}),
            }
        )
        return VerifiedCapabilityResultRef.build(
            task_id=plan_proof.run.task_id,
            run_id=plan_proof.run.run_id,
            plan_generation=plan_proof.run.plan_generation,
            producer_node_id=plan_proof.node.node_id,
            producer_attempt=plan_proof.invocation.attempt,
            capability=resolved.capability,
            result_kind=CapabilityResultKind.WORKSPACE_FILE,
            result_schema_digest=sha256_digest(WorkspaceFileRead.model_json_schema()),
            result_digest=workspace.result_digest,
            verification_digest=verification_digest,
        )

    @classmethod
    def _resolve_plan_proof(
        cls,
        proof: AgentVerifiedResultPlanProof,
        *,
        expected_route_id: str,
        expected_source_local_key: str,
        expected_agent_id: str,
        expected_capability_id: str,
        allow_pending_node_transition: bool,
    ) -> _ResolvedPlanProof:
        step = proof.step_binding
        source_plan = proof.source_plan
        composite_plan = proof.composite_plan
        run = proof.run
        node = proof.node
        invocation = proof.invocation
        if (
            step.source.task_id != run.task_id
            or proof.source_contract.task_id != run.task_id
            or source_plan.task_id != run.task_id
            or composite_plan.task_id != run.task_id
            or source_plan.task_contract.digest != proof.source_contract.digest
            or step.source_plan_id != source_plan.plan_id
            or step.source_plan_manifest_digest != source_plan.plan_manifest_digest
            or step.source_plan_binding_snapshot_digest != source_plan.binding_snapshot_digest
            or step.recipe.route_id != expected_route_id
            or composite_plan.producer.kind != "model_planner"
            or run.plan_generation != composite_plan.plan_generation
            or run.plan_digest != composite_plan.plan_manifest_digest
            or run.status not in {"active", "awaiting_verification", "paused", "succeeded"}
        ):
            raise AgentVerifiedResultSourceBindingError(
                "Agent bridge Plan or source-step lineage changed"
            )
        mapping = next(
            (
                item
                for item in step.node_mappings
                if item.composite_node_id == node.node_id
                and item.composite_local_key == node.local_key
            ),
            None,
        )
        if mapping is None or mapping.source_local_key != expected_source_local_key:
            raise AgentVerifiedResultSourceBindingError(
                "Agent node has no exact source-to-composite mapping"
            )
        source_node = cls._plan_node(source_plan, mapping.source_node_id)
        composite_node = cls._plan_node(composite_plan, mapping.composite_node_id)
        if (
            source_node.local_key != mapping.source_local_key
            or source_node.node_spec_digest != mapping.source_node_spec_digest
            or composite_node.local_key != mapping.composite_local_key
            or composite_node.node_spec_digest != mapping.composite_node_spec_digest
            or source_node.kind is not DraftNodeKind.AGENT
            or composite_node.kind is not DraftNodeKind.AGENT
            or source_node.bound_agent is None
            or composite_node.bound_agent != source_node.bound_agent
            or source_node.bound_agent.agent_id != expected_agent_id
            or source_node.capability is None
            or source_node.capability.capability_id != expected_capability_id
            or composite_node.capability != source_node.capability
        ):
            raise AgentVerifiedResultSourceBindingError(
                "Agent, Capability, or node-mapping digest drifted"
            )
        if (
            node.run_id != run.run_id
            or node.node_id != composite_node.node_id
            or node.local_key != composite_node.local_key
            or node.node_kind != composite_node.kind.value
            or node.node_spec_digest != composite_node.node_spec_digest
            or tuple(node.depends_on) != composite_node.depends_on
            or node.bound_agent != composite_node.bound_agent.model_dump(mode="json")
            or node.capability != composite_node.capability.model_dump(mode="json")
            or tuple(node.acceptance_refs) != composite_node.acceptance_refs
            or node.budget != composite_node.budget.model_dump(mode="json")
            or node.runtime_enabled is not composite_node.runtime_enabled
            or node.status
            not in (
                {"verified", "awaiting_verification"}
                if allow_pending_node_transition
                else {"verified"}
            )
            or node.attempt_count < 1
            or invocation.run_id != run.run_id
            or invocation.node_id != node.node_id
            or invocation.attempt != node.attempt_count
            or invocation.parent_invocation_id is not None
            or invocation.agent_id != composite_node.bound_agent.agent_id
            or invocation.agent_version != composite_node.bound_agent.version
            or invocation.agent_contract_digest != composite_node.bound_agent.contract_digest
            or invocation.prompt_package_digest != composite_node.bound_agent.prompt_package_digest
            or invocation.execution_status != "result_submitted"
            or invocation.verification_status != "verified"
            or invocation.result_id != proof.result.result_id
        ):
            raise AgentVerifiedResultProofRejectedError(
                "Agent invocation or execution-node verification proof changed"
            )
        return _ResolvedPlanProof(
            mapping=mapping,
            source_node=source_node,
            composite_node=composite_node,
            capability=source_node.capability,
        )

    @staticmethod
    def _plan_node(plan: ExecutablePlan, node_id: str) -> ExecutablePlanNode:
        node = next((item for item in plan.nodes if item.node_id == node_id), None)
        if node is None:
            raise AgentVerifiedResultSourceBindingError(
                "Source-step node mapping references a missing Plan node"
            )
        return node

    @staticmethod
    def _parameter(binding: ModelPlannerStepBinding, name: str) -> str:
        matches = tuple(
            item.value for item in binding.parameter_bindings if item.parameter_name == name
        )
        if len(matches) != 1:
            raise AgentVerifiedResultSourceBindingError(
                f"Source-step binding requires exactly one {name} parameter"
            )
        return matches[0]

    @staticmethod
    def _assert_agent_result_record(
        proof: AgentVerifiedResultPlanProof,
        result: AgentResult | AgentOutputResult,
    ) -> None:
        if (
            proof.result.invocation_id != proof.invocation.invocation_id
            or proof.result.result_digest != result.result_digest
            or proof.result.manifest != result.model_dump(mode="json")
            or result.result_id != proof.result.result_id
            or result.invocation_id != proof.invocation.invocation_id
        ):
            raise AgentVerifiedResultProofRejectedError(
                "Agent Result persistence digest or Invocation lineage changed"
            )

    @classmethod
    def _assert_research_source_binding(
        cls,
        plan_proof: AgentVerifiedResultPlanProof,
        proof: ResearchAgentVerificationProof,
    ) -> None:
        goal = cls._parameter(plan_proof.step_binding, "goal")
        policy = plan_proof.source_contract.research
        if policy is None:
            raise AgentVerifiedResultSourceBindingError(
                "Research source Task Contract has no research policy"
            )
        if (
            proof.search_request.query != goal[:500]
            or proof.search_request.max_results != policy.max_results_per_search
            or proof.search_request.allowed_domains != policy.allowed_domains
            or proof.search_call.query_digest != sha256_digest(proof.search_request)
        ):
            raise AgentVerifiedResultSourceBindingError(
                "Research query does not match the immutable source-step binding"
            )

    @staticmethod
    def _assert_research_evidence(
        plan_proof: AgentVerifiedResultPlanProof,
        proof: ResearchAgentVerificationProof,
        result: AgentResult,
        claims: tuple[ResearchClaim, ...],
        citations: tuple[CitationEvidence, ...],
        pages: tuple[PageSnapshot, ...],
        verdicts: tuple[ClaimVerdictRead, ...],
    ) -> None:
        research = proof.research
        verification = proof.verification
        snapshot = proof.evidence_snapshot
        if (
            research.task_id != plan_proof.run.task_id
            or research.invocation_id != plan_proof.invocation.invocation_id
            or research.status != "verified"
            or proof.search_call.research_session_id != research.research_session_id
            or proof.search_call.attempt != 1
            or verification.run_id != plan_proof.run.run_id
            or verification.node_id != plan_proof.node.node_id
            or verification.result_id != result.result_id
            or verification.status != "completed"
            or verification.outcome != "verified"
            or verification.policy_id != POLICY_ID
            or verification.policy_digest != POLICY_DIGEST
            or verification.attempt != 1
            or verification.evidence_snapshot_id != snapshot.evidence_snapshot_id
            or verification.grader_output_digest is None
            or snapshot.verification_run_id != verification.verification_run_id
            or snapshot.snapshot_digest != sha256_digest(snapshot.manifest)
        ):
            raise AgentVerifiedResultProofRejectedError(
                "Research citation VerificationRun proof changed"
            )
        claim_ids = {item.claim_id for item in claims}
        citation_ids = {item.citation_id for item in citations}
        page_ids = {item.page_snapshot_id for item in pages}
        if (
            not claims
            or not citations
            or not pages
            or len(claim_ids) != len(claims)
            or len(citation_ids) != len(citations)
            or len(page_ids) != len(pages)
            or len({item.claim_id for item in verdicts}) != len(verdicts)
            or claim_ids != set(result.claim_ids)
            or citation_ids != set(result.citation_ids)
            or {item.claim_id for item in verdicts} != claim_ids
            or any(item.outcome != "verified" for item in verdicts)
            or any(item.task_id != plan_proof.run.task_id for item in claims)
            or any(item.research_session_id != research.research_session_id for item in claims)
            or any(item.research_session_id != research.research_session_id for item in pages)
        ):
            raise AgentVerifiedResultProofRejectedError(
                "Research Claim, Citation, or Snapshot scope changed"
            )
        if (
            any(
                record.claim_id != item.claim_id
                or record.research_session_id != research.research_session_id
                or record.claim_digest != item.claim_digest
                for record, item in zip(proof.claims, claims, strict=True)
            )
            or any(
                record.citation_id != item.citation_id
                or record.research_session_id != research.research_session_id
                or record.claim_id != item.claim_id
                or record.page_snapshot_id != item.page_snapshot_id
                or record.citation_digest != item.citation_digest
                for record, item in zip(proof.citations, citations, strict=True)
            )
            or any(
                record.page_snapshot_id != item.page_snapshot_id
                or record.research_session_id != research.research_session_id
                or record.search_hit_id != item.search_hit_id
                or record.snapshot_digest != item.snapshot_digest
                for record, item in zip(proof.pages, pages, strict=True)
            )
            or any(
                record.verification_run_id != verification.verification_run_id
                or record.claim_id != item.claim_id
                or record.outcome != item.outcome
                or record.reason_code != item.reason_code
                or tuple(record.citation_ids) != item.citation_ids
                or record.verdict_digest != item.verdict_digest
                for record, item in zip(proof.verdicts, verdicts, strict=True)
            )
        ):
            raise AgentVerifiedResultProofRejectedError(
                "Research evidence persistence fields or digests changed"
            )
        citations_by_id = {item.citation_id: item for item in citations}
        pages_by_id = {item.page_snapshot_id: item for item in pages}
        for claim in claims:
            if set(claim.citation_ids) - citation_ids:
                raise AgentVerifiedResultProofRejectedError(
                    "Research Claim references an unbound Citation"
                )
            for citation_id in claim.citation_ids:
                citation = citations_by_id[citation_id]
                page = pages_by_id.get(citation.page_snapshot_id)
                if (
                    citation.claim_id != claim.claim_id
                    or page is None
                    or page.page_snapshot_id not in page_ids
                    or page.task_id != plan_proof.run.task_id
                    or citation.locator_text not in page.extracted_text
                ):
                    raise AgentVerifiedResultProofRejectedError(
                        "Research Citation lineage or locator changed"
                    )
        snapshot_manifest = snapshot.manifest
        if (
            set(snapshot_manifest)
            != {
                "result_id",
                "claim_digests",
                "citation_digests",
                "page_snapshot_digests",
            }
            or snapshot_manifest.get("result_id") != result.result_id
            or set(snapshot_manifest.get("claim_digests", ()))
            != {item.claim_digest for item in claims}
            or set(snapshot_manifest.get("citation_digests", ()))
            != {item.citation_digest for item in citations}
            or set(snapshot_manifest.get("page_snapshot_digests", ()))
            != {item.snapshot_digest for item in pages}
        ):
            raise AgentVerifiedResultProofRejectedError(
                "Research verification evidence snapshot changed"
            )
        expected_input_digest = sha256_digest(
            {
                "result_digest": result.result_digest,
                "claim_digests": snapshot_manifest["claim_digests"],
                "citation_digests": snapshot_manifest["citation_digests"],
                "snapshot_digests": snapshot_manifest["page_snapshot_digests"],
                "policy_digest": POLICY_DIGEST,
            }
        )
        if verification.input_manifest_digest != expected_input_digest:
            raise AgentVerifiedResultProofRejectedError(
                "Research verification input digest changed"
            )
        verdict_by_claim = {item.claim_id: item for item in verdicts}
        for claim in claims:
            if set(verdict_by_claim[claim.claim_id].citation_ids) != set(claim.citation_ids):
                raise AgentVerifiedResultProofRejectedError(
                    "Research verified Claim Citation set changed"
                )

    @staticmethod
    def _research_verification_digest(
        proof: ResearchAgentVerificationProof,
        verdicts: tuple[ClaimVerdictRead, ...],
    ) -> str:
        verification = proof.verification
        return sha256_digest(
            {
                "schema_version": "deskpilot.research-verification-proof.v1",
                "verification_run_id": verification.verification_run_id,
                "result_id": verification.result_id,
                "attempt": verification.attempt,
                "policy_id": verification.policy_id,
                "policy_digest": verification.policy_digest,
                "outcome": verification.outcome,
                "evidence_snapshot_id": proof.evidence_snapshot.evidence_snapshot_id,
                "evidence_snapshot_digest": proof.evidence_snapshot.snapshot_digest,
                "input_manifest_digest": verification.input_manifest_digest,
                "grader_request_digest": verification.grader_request_digest,
                "grader_output_digest": verification.grader_output_digest,
                "grader_provider_id": verification.grader_provider_id,
                "grader_model": verification.grader_model,
                "verdict_digests": sorted(item.verdict_digest for item in verdicts),
            }
        )


__all__ = [
    "AgentVerifiedResultBridge",
    "AgentVerifiedResultBridgeError",
    "AgentVerifiedResultPlanProof",
    "AgentVerifiedResultProofRejectedError",
    "AgentVerifiedResultSourceBindingError",
    "ResearchAgentVerificationProof",
    "WorkspaceReaderVerificationProof",
]
