from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from deskpilot.agents import create_builtin_agent_registry
from deskpilot.application.agent_verified_result_bridge import (
    AgentVerifiedResultBridge,
    AgentVerifiedResultPlanProof,
    AgentVerifiedResultProofRejectedError,
    AgentVerifiedResultSourceBindingError,
    ResearchAgentVerificationProof,
    WorkspaceReaderVerificationProof,
)
from deskpilot.application.artifact_delivery_runtime import POLICY_DIGEST, POLICY_ID
from deskpilot.application.capability_catalog import (
    CapabilityCatalog,
    create_builtin_capability_catalog,
)
from deskpilot.application.plan_compiler import (
    PlanCompiler,
    research_to_html_contract,
    research_to_html_draft,
    workspace_file_read_contract,
    workspace_file_read_draft,
)
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_runtime import AgentOutputResult, AgentResult
from deskpilot.domain.capability_execution import CapabilityResultKind
from deskpilot.domain.model_contracts import (
    ModelCapabilities,
    ModelLocation,
    ModelProtocol,
    ModelProviderDescriptor,
)
from deskpilot.domain.research import (
    CitationEvidence,
    PageSnapshot,
    ResearchClaim,
    SearchRequest,
)
from deskpilot.domain.task_loop import (
    MODEL_PLANNER_COMPOSER_VERSION,
    ModelPlannerNodeMapping,
    ModelPlannerStepBinding,
    TaskLoopSourceRef,
)
from deskpilot.domain.task_plans import DraftPlan, ExecutablePlan, PlanProducer, TaskContract
from deskpilot.domain.turn_planning import (
    TurnPlanningOfferRef,
    TurnPlanningParameterBinding,
    TurnPlanningRecipeRef,
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
from deskpilot.tools import create_builtin_registry

NOW = datetime(2026, 8, 24, tzinfo=UTC)
TASK_ID = f"tsk_{'1' * 32}"
RUN_ID = f"run_{'2' * 64}"


@dataclass(frozen=True, slots=True)
class _CompiledBinding:
    contract: TaskContract
    source_plan: ExecutablePlan
    composite_plan: ExecutablePlan
    step: ModelPlannerStepBinding


@dataclass(frozen=True, slots=True)
class _ResearchFixture:
    plan: AgentVerifiedResultPlanProof
    verification: ResearchAgentVerificationProof


@dataclass(frozen=True, slots=True)
class _WorkspaceFixture:
    plan: AgentVerifiedResultPlanProof
    verification: WorkspaceReaderVerificationProof


def _provider() -> ModelProviderDescriptor:
    return ModelProviderDescriptor(
        provider_id="local_test",
        display_name="Local test provider",
        model="test-model",
        protocol=ModelProtocol.FAKE,
        location=ModelLocation.LOCAL,
        capabilities=ModelCapabilities(
            structured_output=True,
            strict_json_schema=True,
        ),
    )


def _compiler() -> tuple[PlanCompiler, CapabilityCatalog]:
    tools = create_builtin_registry()
    capabilities = create_builtin_capability_catalog(research_runtime_enabled=True)
    agents = create_builtin_agent_registry(tools, (_provider(),))
    return PlanCompiler(agents, tools, capabilities), capabilities


def _namespaced_draft(source: DraftPlan, mapping: dict[str, str]) -> DraftPlan:
    return DraftPlan(
        task_id=source.task_id,
        contract_version=source.contract_version,
        producer=PlanProducer(
            kind="model_planner",
            producer_ref=MODEL_PLANNER_COMPOSER_VERSION,
        ),
        nodes=tuple(
            node.model_copy(
                update={
                    "local_key": mapping.get(node.local_key, node.local_key),
                    "depends_on": tuple(mapping.get(item, item) for item in node.depends_on),
                }
            )
            for node in source.nodes
        ),
    )


def _binding(
    *,
    route_id: str,
    source_local_key: str,
    composite_local_key: str,
    parameter_name: str,
    parameter_value: str,
) -> _CompiledBinding:
    compiler, capabilities = _compiler()
    if route_id == "research_to_html":
        contract = research_to_html_contract(TASK_ID, capabilities)
        draft = research_to_html_draft(TASK_ID)
        names = {
            "research": "s01_research",
            "build_html": "s01_build_html",
            "browser_verify": "s01_browser_verify",
        }
    else:
        contract = workspace_file_read_contract(TASK_ID, capabilities)
        draft = workspace_file_read_draft(TASK_ID)
        names = {"workspace_file_read": "s02_workspace_file_read"}
    source_plan = compiler.compile(contract, draft, generation=1)
    composite_plan = compiler.compile(
        contract,
        _namespaced_draft(draft, names),
        generation=1,
    )
    source_node = next(item for item in source_plan.nodes if item.local_key == source_local_key)
    composite_node = next(
        item for item in composite_plan.nodes if item.local_key == composite_local_key
    )
    offer = TurnPlanningOfferRef(
        offer_id=f"tpo_{'3' * 64}",
        offer_key=f"ofk_{'4' * 64}",
        offer_digest="5" * 64,
    )
    parameter = TurnPlanningParameterBinding.build(
        offer_key=offer.offer_key,
        parameter_name=parameter_name,
        value=parameter_value,
        source_start=0,
        source_end=len(parameter_value),
    )
    node_mapping = ModelPlannerNodeMapping.build(
        source_node_id=source_node.node_id,
        source_local_key=source_node.local_key,
        source_node_spec_digest=source_node.node_spec_digest,
        composite_node_id=composite_node.node_id,
        composite_local_key=composite_node.local_key,
        composite_node_spec_digest=composite_node.node_spec_digest,
    )
    step = ModelPlannerStepBinding.build(
        source=TaskLoopSourceRef(
            task_id=TASK_ID,
            user_message_id=f"msg_{'6' * 32}",
            user_message_digest="7" * 64,
            turn_planner_run_id=f"tpr_{'8' * 64}",
            turn_planner_run_digest="9" * 64,
            adjudication_id=f"tpa_{'a' * 64}",
            adjudication_digest="b" * 64,
            turn_plan_binding_id=f"tpb_{'c' * 64}",
            turn_plan_binding_digest="d" * 64,
        ),
        ordinal=1,
        offer=offer,
        recipe=TurnPlanningRecipeRef(
            route_id=route_id,
            route_version="2",
            route_manifest_digest="e" * 64,
        ),
        policy_snapshot_digest="f" * 64,
        source_plan_id=source_plan.plan_id,
        source_plan_manifest_digest=source_plan.plan_manifest_digest,
        source_plan_binding_snapshot_digest=source_plan.binding_snapshot_digest,
        budget=source_node.budget,
        parameter_bindings=(parameter,),
        node_mappings=(node_mapping,),
        created_at=NOW,
    )
    return _CompiledBinding(
        contract=contract,
        source_plan=source_plan,
        composite_plan=composite_plan,
        step=step,
    )


def _plan_records(
    binding: _CompiledBinding,
    *,
    local_key: str,
    result: AgentResult | AgentOutputResult,
) -> AgentVerifiedResultPlanProof:
    node = next(item for item in binding.composite_plan.nodes if item.local_key == local_key)
    assert node.bound_agent is not None
    assert node.capability is not None
    run = TaskExecutionRunRecord(
        run_id=RUN_ID,
        task_id=TASK_ID,
        plan_generation=1,
        plan_digest=binding.composite_plan.plan_manifest_digest,
        status="active",
        revision=1,
        created_at=NOW,
        updated_at=NOW,
    )
    node_record = TaskExecutionNodeRecord(
        node_id=node.node_id,
        run_id=RUN_ID,
        local_key=node.local_key,
        node_kind=node.kind.value,
        node_spec_digest=node.node_spec_digest,
        depends_on=list(node.depends_on),
        handoff_parent_node_id=node.handoff_parent_node_id,
        bound_agent=node.bound_agent.model_dump(mode="json"),
        capability=node.capability.model_dump(mode="json"),
        acceptance_refs=list(node.acceptance_refs),
        budget=node.budget.model_dump(mode="json"),
        runtime_enabled=True,
        status="verified",
        revision=3,
        attempt_count=1,
        claim_fencing_token=1,
        created_at=NOW,
        updated_at=NOW,
    )
    invocation = AgentInvocationRecord(
        invocation_id=result.invocation_id,
        run_id=RUN_ID,
        node_id=node.node_id,
        attempt=1,
        handoff_id=f"hnd_{'0' * 64}",
        parent_invocation_id=None,
        agent_id=node.bound_agent.agent_id,
        agent_version=node.bound_agent.version,
        agent_contract_digest=node.bound_agent.contract_digest,
        prompt_package_digest=node.bound_agent.prompt_package_digest,
        execution_status="result_submitted",
        verification_status="verified",
        result_id=result.result_id,
        revision=3,
        created_at=NOW,
        started_at=NOW,
        finished_at=NOW,
    )
    result_record = AgentResultRecord(
        result_id=result.result_id,
        invocation_id=result.invocation_id,
        manifest=result.model_dump(mode="json"),
        result_digest=result.result_digest,
        created_at=NOW,
    )
    return AgentVerifiedResultPlanProof(
        step_binding=binding.step,
        source_contract=binding.contract,
        source_plan=binding.source_plan,
        composite_plan=binding.composite_plan,
        run=run,
        node=node_record,
        invocation=invocation,
        result=result_record,
    )


def _research_fixture(*, query: str = "bound research goal") -> _ResearchFixture:
    binding = _binding(
        route_id="research_to_html",
        source_local_key="research",
        composite_local_key="s01_research",
        parameter_name="goal",
        parameter_value="bound research goal",
    )
    invocation_id = f"inv_{'1' * 64}"
    result_id = f"res_{'2' * 64}"
    research_id = f"rsr_{'3' * 64}"
    page_text = "Bound public evidence supports the claim."
    page_material = {
        "schema_version": "deskpilot.page-snapshot.v1",
        "page_snapshot_id": f"snp_{'4' * 64}",
        "task_id": TASK_ID,
        "research_session_id": research_id,
        "search_hit_id": f"sht_{'5' * 64}",
        "requested_url": "https://one.example/article",
        "final_url": "https://one.example/article",
        "status_code": 200,
        "media_type": "text/html",
        "title": "Source",
        "extracted_text": page_text,
        "content_digest": sha256_digest({"text": page_text}),
        "extractor_version": "deskpilot.html-text.v1",
        "origin": "external_untrusted",
        "fetched_at": NOW,
    }
    page = PageSnapshot.model_validate(
        {**page_material, "snapshot_digest": sha256_digest(page_material)}
    )
    citation_material = {
        "citation_id": f"cit_{'6' * 64}",
        "claim_id": f"clm_{'7' * 64}",
        "page_snapshot_id": page.page_snapshot_id,
        "locator_text": "Bound public evidence",
        "locator_digest": sha256_digest({"text": "Bound public evidence"}),
        "status": "awaiting_verification",
    }
    citation = CitationEvidence.model_validate(
        {**citation_material, "citation_digest": sha256_digest(citation_material)}
    )
    claim_material = {
        "claim_id": citation.claim_id,
        "task_id": TASK_ID,
        "research_session_id": research_id,
        "statement": "The source supports this claim.",
        "citation_ids": (citation.citation_id,),
        "status": "awaiting_verification",
    }
    claim = ResearchClaim.model_validate(
        {**claim_material, "claim_digest": sha256_digest(claim_material)}
    )
    result_material = {
        "schema_version": "deskpilot.agent-result.v1",
        "result_id": result_id,
        "invocation_id": invocation_id,
        "disposition": "candidate",
        "claim_ids": (claim.claim_id,),
        "citation_ids": (citation.citation_id,),
        "limitation_codes": (),
        "input_digest": "8" * 64,
        "model_response_digest": "9" * 64,
        "output_schema_digest": "a" * 64,
    }
    result = AgentResult.model_validate(
        {**result_material, "result_digest": sha256_digest(result_material)}
    )
    plan = _plan_records(binding, local_key="s01_research", result=result)
    research_policy = binding.contract.research
    assert research_policy is not None
    search_request = SearchRequest(
        query=query,
        max_results=research_policy.max_results_per_search,
        allowed_domains=research_policy.allowed_domains,
    )
    search = ResearchSearchCallRecord(
        search_call_id=f"src_{'b' * 64}",
        research_session_id=research_id,
        attempt=1,
        provider_id="test-search",
        query_digest=sha256_digest(search_request),
        hits=[],
        created_at=NOW,
    )
    research = ResearchSessionRecord(
        research_session_id=research_id,
        task_id=TASK_ID,
        invocation_id=invocation_id,
        status="verified",
        revision=3,
        created_at=NOW,
        updated_at=NOW,
    )
    claim_record = ResearchClaimRecord(
        claim_id=claim.claim_id,
        research_session_id=research_id,
        manifest=claim.model_dump(mode="json"),
        claim_digest=claim.claim_digest,
        created_at=NOW,
    )
    citation_record = ResearchCitationRecord(
        citation_id=citation.citation_id,
        research_session_id=research_id,
        claim_id=claim.claim_id,
        page_snapshot_id=page.page_snapshot_id,
        manifest=citation.model_dump(mode="json"),
        citation_digest=citation.citation_digest,
        created_at=NOW,
    )
    page_record = ResearchPageSnapshotRecord(
        page_snapshot_id=page.page_snapshot_id,
        research_session_id=research_id,
        search_hit_id=page.search_hit_id,
        manifest=page.model_dump(mode="json"),
        snapshot_digest=page.snapshot_digest,
        created_at=NOW,
    )
    verdict_material = {
        "claim_id": claim.claim_id,
        "outcome": "verified",
        "reason_code": "SUPPORTED",
        "citation_ids": (citation.citation_id,),
    }
    verdict = ClaimVerdictRecord(
        verification_run_id=f"vfy_{'c' * 64}",
        claim_id=claim.claim_id,
        outcome="verified",
        reason_code="SUPPORTED",
        citation_ids=[citation.citation_id],
        verdict_digest=sha256_digest(verdict_material),
        created_at=NOW,
    )
    snapshot_manifest = {
        "result_id": result.result_id,
        "claim_digests": [claim.claim_digest],
        "citation_digests": [citation.citation_digest],
        "page_snapshot_digests": [page.snapshot_digest],
    }
    evidence_snapshot = VerificationEvidenceSnapshotRecord(
        evidence_snapshot_id=f"ves_{'d' * 64}",
        verification_run_id=verdict.verification_run_id,
        manifest=snapshot_manifest,
        snapshot_digest=sha256_digest(snapshot_manifest),
        created_at=NOW,
    )
    input_digest = sha256_digest(
        {
            "result_digest": result.result_digest,
            "claim_digests": snapshot_manifest["claim_digests"],
            "citation_digests": snapshot_manifest["citation_digests"],
            "snapshot_digests": snapshot_manifest["page_snapshot_digests"],
            "policy_digest": POLICY_DIGEST,
        }
    )
    verification = VerificationRunRecord(
        verification_run_id=verdict.verification_run_id,
        run_id=RUN_ID,
        node_id=plan.node.node_id,
        result_id=result.result_id,
        attempt=1,
        policy_id=POLICY_ID,
        policy_digest=POLICY_DIGEST,
        status="completed",
        outcome="verified",
        evidence_snapshot_id=evidence_snapshot.evidence_snapshot_id,
        input_manifest_digest=input_digest,
        grader_request_digest="e" * 64,
        grader_output_digest="f" * 64,
        grader_provider_id="local_test",
        grader_model="test-model",
        created_at=NOW,
        completed_at=NOW,
    )
    return _ResearchFixture(
        plan=plan,
        verification=ResearchAgentVerificationProof(
            research=research,
            search_call=search,
            search_request=search_request,
            verification=verification,
            evidence_snapshot=evidence_snapshot,
            claims=(claim_record,),
            citations=(citation_record,),
            pages=(page_record,),
            verdicts=(verdict,),
        ),
    )


def _workspace_fixture(*, result_path: str = "README.md") -> _WorkspaceFixture:
    binding = _binding(
        route_id="workspace_file_read",
        source_local_key="workspace_file_read",
        composite_local_key="s02_workspace_file_read",
        parameter_name="path",
        parameter_value="README.md",
    )
    content = "verified workspace content"
    workspace_material = {
        "schema_version": "deskpilot.workspace-file-read.v1",
        "relative_path": result_path,
        "byte_count": len(content.encode("utf-8")),
        "content_digest": sha256_digest({"bytes_hex": content.encode("utf-8").hex()}),
        "version_digest": "1" * 64,
        "content": content,
    }
    workspace = WorkspaceFileRead.model_validate(
        {**workspace_material, "result_digest": sha256_digest(workspace_material)}
    )
    result_id = f"res_{'2' * 64}"
    invocation_id = f"inv_{'3' * 64}"
    result_material = {
        "schema_version": "deskpilot.agent-output-result.v1",
        "result_id": result_id,
        "invocation_id": invocation_id,
        "disposition": "candidate",
        "output": {
            "relative_path": workspace.relative_path,
            "result_digest": workspace.result_digest,
        },
        "evidence_refs": (f"workspace-file:{workspace.result_digest}",),
        "limitation_codes": (),
        "input_digest": "4" * 64,
        "model_response_digest": "5" * 64,
        "output_schema_digest": "6" * 64,
    }
    result = AgentOutputResult.model_validate(
        {**result_material, "result_digest": sha256_digest(result_material)}
    )
    plan = _plan_records(binding, local_key="s02_workspace_file_read", result=result)
    record = WorkspaceAgentResultRecord(
        invocation_id=invocation_id,
        run_id=RUN_ID,
        result_kind="file",
        manifest=workspace.model_dump(mode="json"),
        result_digest=workspace.result_digest,
        created_at=NOW,
    )
    return _WorkspaceFixture(
        plan=plan,
        verification=WorkspaceReaderVerificationProof(workspace_result=record),
    )


def test_research_bridge_requires_complete_verified_lineage() -> None:
    fixture = _research_fixture()

    result_ref = AgentVerifiedResultBridge.research(fixture.plan, fixture.verification)

    assert result_ref.task_id == TASK_ID
    assert result_ref.run_id == RUN_ID
    assert result_ref.producer_node_id == fixture.plan.node.node_id
    assert result_ref.result_kind is CapabilityResultKind.VERIFIED_CLAIMS
    assert result_ref.capability.capability_id == "research.read.v1"
    assert result_ref.result_digest == fixture.plan.result.result_digest


def test_workspace_reader_bridge_uses_only_bound_path() -> None:
    fixture = _workspace_fixture()

    result_ref = AgentVerifiedResultBridge.workspace_reader(
        fixture.plan,
        fixture.verification,
    )

    assert result_ref.result_kind is CapabilityResultKind.WORKSPACE_FILE
    assert result_ref.capability.capability_id == "workspace.file.read.v1"
    assert result_ref.result_digest == fixture.verification.workspace_result.result_digest

    mismatched = _workspace_fixture(result_path="OTHER.md")
    with pytest.raises(AgentVerifiedResultSourceBindingError, match="source-step binding"):
        AgentVerifiedResultBridge.workspace_reader(mismatched.plan, mismatched.verification)


def test_bridge_rejects_unverified_cross_scope_and_capability_drift() -> None:
    unverified = _workspace_fixture()
    unverified.plan.invocation.verification_status = "pending"
    with pytest.raises(AgentVerifiedResultProofRejectedError, match="verification proof"):
        AgentVerifiedResultBridge.workspace_reader(unverified.plan, unverified.verification)

    cross_run = _workspace_fixture()
    cross_run.plan.invocation.run_id = f"run_{'9' * 64}"
    with pytest.raises(AgentVerifiedResultProofRejectedError):
        AgentVerifiedResultBridge.workspace_reader(cross_run.plan, cross_run.verification)

    cross_generation = _workspace_fixture()
    cross_generation.plan.run.plan_generation = 2
    with pytest.raises(AgentVerifiedResultSourceBindingError):
        AgentVerifiedResultBridge.workspace_reader(
            cross_generation.plan,
            cross_generation.verification,
        )

    capability_drift = _workspace_fixture()
    assert capability_drift.plan.node.capability is not None
    capability_drift.plan.node.capability = {
        **capability_drift.plan.node.capability,
        "digest": "0" * 64,
    }
    with pytest.raises(AgentVerifiedResultProofRejectedError):
        AgentVerifiedResultBridge.workspace_reader(
            capability_drift.plan,
            capability_drift.verification,
        )


def test_bridge_rejects_result_digest_and_research_query_drift() -> None:
    result_drift = _workspace_fixture()
    result_drift.plan.result.result_digest = "0" * 64
    with pytest.raises(AgentVerifiedResultProofRejectedError, match="persistence digest"):
        AgentVerifiedResultBridge.workspace_reader(
            result_drift.plan,
            result_drift.verification,
        )

    query_drift = _research_fixture(query="fallback Task goal")
    with pytest.raises(AgentVerifiedResultSourceBindingError, match="source-step binding"):
        AgentVerifiedResultBridge.research(query_drift.plan, query_drift.verification)
