"""Persistent stage-112A bridge from deferred Offers to a sealed DraftPlan.

The runtime never calls a model.  It appends an Observe -> Plan proof after the
immutable stage-111 ``multi_step_deferred`` binding, revalidates every selected
Offer against current server recipes, composes one least-authority draft, and
stores a generation-1 preview without activating execution.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import ValidationError
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError

from deskpilot.application.model_planner_composer import (
    ModelPlannerComposer,
    ModelPlannerComposition,
    ModelPlannerCompositionError,
    ModelPlannerDomainLimitError,
    ModelPlannerOfferRejectedError,
    ModelPlannerPrivacyConflictError,
)
from deskpilot.application.route_recipe_catalog import RouteRecipeCatalog
from deskpilot.application.turn_planner_runtime import (
    RevalidatedDeferredPlan,
    TurnPlannerRuntime,
    TurnPlannerRuntimeError,
)
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.task_loop import (
    ModelPlannerDraft,
    ModelPlannerFailureCode,
    ModelPlannerFailureProof,
    ModelPlannerNodeMapping,
    ModelPlannerStepBinding,
    TaskLoop,
    TaskLoopEvent,
    TaskLoopSourceRef,
)
from deskpilot.domain.turn_planning import TurnPlanningRead
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import (
    ModelPlannerDraftRecord,
    ModelPlannerStepBindingRecord,
    TaskLoopEventRecord,
    TaskLoopRecord,
    TurnPlanBindingRecord,
    TurnRouteRecord,
)


class MultiStepPlanRuntimeError(RuntimeError):
    code = "MULTI_STEP_PLAN_ERROR"


class MultiStepPlanNotFoundError(MultiStepPlanRuntimeError):
    code = "MULTI_STEP_PLAN_NOT_FOUND"


class MultiStepPlanNotEligibleError(MultiStepPlanRuntimeError):
    code = "MULTI_STEP_PLAN_NOT_ELIGIBLE"


class MultiStepPlanProofRejectedError(MultiStepPlanRuntimeError):
    code = "MULTI_STEP_PLAN_PROOF_REJECTED"


class MultiStepPlanConflictError(MultiStepPlanRuntimeError):
    code = "MULTI_STEP_PLAN_CONFLICT"


@dataclass(frozen=True, slots=True)
class ModelPlannerTaskLoopBundle:
    """Validated internal projection used by later task-loop milestones."""

    loop: TaskLoop
    events: tuple[TaskLoopEvent, ...]
    draft: ModelPlannerDraft | None = None
    steps: tuple[ModelPlannerStepBinding, ...] = ()


class MultiStepPlanRuntime:
    """Append and recover one deterministic model-planner composition per Turn."""

    def __init__(
        self,
        database: Database,
        turn_planner: TurnPlannerRuntime,
        composer: ModelPlannerComposer,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._database = database
        self._turn_planner = turn_planner
        self._composer = composer
        self._clock = clock or (lambda: datetime.now(UTC))

    async def plan(self, task_id: str) -> TaskLoop:
        """Create or recover a sealed multi-step Draft without model replay."""

        existing = await self.get_bundle(task_id)
        if existing is not None and existing.loop.status != "observed":
            return existing.loop

        if existing is None:
            planning = await self._turn_planner.get(task_id)
            source = self._source_from_planning(planning)
            observed_event = TaskLoopEvent.observe(
                source=source,
                created_at=self._now(),
            )
            loop = await self._persist_observed(TaskLoop.observed(observed_event), observed_event)
            if loop.status != "observed":
                return loop
        else:
            loop = existing.loop

        try:
            deferred = await self._turn_planner.revalidate_task_loop_plan(task_id)
            if self._source_from_planning(deferred.planning) != loop.source:
                raise MultiStepPlanProofRejectedError(
                    "Deferred Turn Planner lineage changed after Observe"
                )
            composition = self._composer.compose(task_id, deferred.steps)
            draft, steps = self._build_draft(loop, deferred, composition)
        except (
            TurnPlannerRuntimeError,
            ModelPlannerCompositionError,
            MultiStepPlanRuntimeError,
            ValidationError,
            ValueError,
        ) as error:
            return await self._persist_failure(loop, error)

        return await self._persist_plan(loop, draft, steps)

    async def get(self, task_id: str) -> TaskLoop | None:
        bundle = await self.get_bundle(task_id)
        return bundle.loop if bundle is not None else None

    async def get_bundle(self, task_id: str) -> ModelPlannerTaskLoopBundle | None:
        """Load and validate duplicated columns, manifests, event chain and Draft."""

        async with self._database.session() as session:
            records = tuple(
                (
                    await session.scalars(
                        select(TaskLoopRecord)
                        .where(TaskLoopRecord.task_id == task_id)
                        .order_by(TaskLoopRecord.created_at, TaskLoopRecord.loop_id)
                    )
                ).all()
            )
            if not records:
                return None
            if len(records) != 1:
                raise MultiStepPlanProofRejectedError(
                    "Task has more than one Task Loop source binding"
                )
            loop = self._loop_from_record(records[0])
            event_records = tuple(
                (
                    await session.scalars(
                        select(TaskLoopEventRecord)
                        .where(TaskLoopEventRecord.loop_id == loop.loop_id)
                        .order_by(TaskLoopEventRecord.sequence)
                    )
                ).all()
            )
            events = tuple(self._event_from_record(item) for item in event_records)
            self._validate_event_chain(loop, events)
            if loop.status != "planned":
                return ModelPlannerTaskLoopBundle(loop=loop, events=events)

            draft_record = await session.get(
                ModelPlannerDraftRecord,
                loop.active_draft.draft_id if loop.active_draft is not None else "",
            )
            if draft_record is None:
                raise MultiStepPlanProofRejectedError("Planned Task Loop lost its sealed Draft")
            draft = self._draft_from_record(draft_record)
            step_records = tuple(
                (
                    await session.scalars(
                        select(ModelPlannerStepBindingRecord)
                        .where(ModelPlannerStepBindingRecord.draft_id == draft.draft_id)
                        .order_by(ModelPlannerStepBindingRecord.ordinal)
                    )
                ).all()
            )
            steps = tuple(self._step_from_record(item) for item in step_records)
            self._validate_draft_bundle(loop, draft, steps)
            return ModelPlannerTaskLoopBundle(
                loop=loop,
                events=events,
                draft=draft,
                steps=steps,
            )

    async def recoverable_task_ids(self, *, limit: int = 100) -> tuple[str, ...]:
        """Return observed loops plus unobserved Task-Loop-eligible bindings."""

        if not 1 <= limit <= 1_000:
            raise ValueError("Task Loop recovery limit is invalid")
        async with self._database.session() as session:
            observed = tuple(
                (
                    await session.scalars(
                        select(TaskLoopRecord.task_id)
                        .where(TaskLoopRecord.status == "observed")
                        .order_by(TaskLoopRecord.updated_at, TaskLoopRecord.task_id)
                        .limit(limit)
                    )
                ).all()
            )
            remaining = limit - len(observed)
            if remaining <= 0:
                return tuple(dict.fromkeys(observed))
            eligible_bindings = tuple(
                (
                    await session.scalars(
                        select(TurnPlanBindingRecord.task_id)
                        .join(
                            TurnRouteRecord,
                            TurnRouteRecord.task_id == TurnPlanBindingRecord.task_id,
                        )
                        .outerjoin(
                            TaskLoopRecord,
                            TaskLoopRecord.source_turn_plan_binding_id
                            == TurnPlanBindingRecord.binding_id,
                        )
                        .where(
                            or_(
                                TurnPlanBindingRecord.status == "multi_step_deferred",
                                and_(
                                    TurnPlanBindingRecord.status == "bound",
                                    TurnRouteRecord.route_id.in_(
                                        RouteRecipeCatalog.planner_only_route_ids()
                                    ),
                                ),
                            ),
                            TaskLoopRecord.loop_id.is_(None),
                        )
                        .order_by(
                            TurnPlanBindingRecord.created_at,
                            TurnPlanBindingRecord.task_id,
                        )
                        .limit(remaining)
                    )
                ).all()
            )
        return tuple(dict.fromkeys((*observed, *eligible_bindings)))

    async def _persist_observed(
        self,
        loop: TaskLoop,
        event: TaskLoopEvent,
    ) -> TaskLoop:
        try:
            async with self._database.session() as session, session.begin():
                existing = await session.scalar(
                    select(TaskLoopRecord).where(
                        TaskLoopRecord.source_turn_plan_binding_id
                        == loop.source.turn_plan_binding_id
                    )
                )
                if existing is not None:
                    return self._loop_from_record(existing)
                session.add(self._loop_record(loop))
                await session.flush()
                session.add(self._event_record(event))
        except IntegrityError:
            persisted = await self.get(loop.source.task_id)
            if persisted is not None:
                return persisted
            raise MultiStepPlanConflictError(
                "Concurrent Task Loop Observe did not converge"
            ) from None
        persisted = await self.get(loop.source.task_id)
        if persisted is None:
            raise MultiStepPlanConflictError("Task Loop Observe was not persisted")
        return persisted

    async def _persist_plan(
        self,
        loop: TaskLoop,
        draft: ModelPlannerDraft,
        steps: tuple[ModelPlannerStepBinding, ...],
    ) -> TaskLoop:
        event = TaskLoopEvent.plan(
            observed=self._observed_event(loop),
            draft=draft.ref,
            created_at=max(self._now(), loop.updated_at),
        )
        settled = loop.settle_plan(event)
        try:
            async with self._database.session() as session, session.begin():
                record = await session.scalar(
                    select(TaskLoopRecord)
                    .where(TaskLoopRecord.loop_id == loop.loop_id)
                    .with_for_update()
                )
                if record is None:
                    raise MultiStepPlanNotFoundError("Observed Task Loop disappeared")
                current = self._loop_from_record(record)
                if current.status != "observed":
                    return current
                if current.loop_digest != loop.loop_digest:
                    raise MultiStepPlanConflictError("Observed Task Loop revision changed")
                session.add(self._draft_record(draft, loop.loop_id))
                await session.flush()
                session.add_all(
                    self._step_record(item, draft.draft_id, loop.loop_id) for item in steps
                )
                await session.flush()
                session.add(self._event_record(event))
                self._apply_loop(record, settled)
        except IntegrityError:
            persisted = await self.get(loop.source.task_id)
            if persisted is not None and persisted.status != "observed":
                return persisted
            raise MultiStepPlanConflictError("Concurrent Task Loop Plan did not converge") from None
        persisted = await self.get(loop.source.task_id)
        if persisted is None or persisted.status != "planned":
            raise MultiStepPlanConflictError("Task Loop Plan was not persisted")
        return persisted

    async def _persist_failure(self, loop: TaskLoop, error: Exception) -> TaskLoop:
        failure = ModelPlannerFailureProof.build(
            error_code=self._failure_code(error),
            reason_code=self._failure_reason(error),
            detail_digest=sha256_digest(
                {
                    "error_type": type(error).__name__,
                    "error_code": getattr(error, "code", None),
                }
            ),
        )
        observed = self._observed_event(loop)
        event = TaskLoopEvent.plan(
            observed=observed,
            failure=failure,
            created_at=max(self._now(), loop.updated_at),
        )
        settled = loop.settle_plan(event)
        try:
            async with self._database.session() as session, session.begin():
                record = await session.scalar(
                    select(TaskLoopRecord)
                    .where(TaskLoopRecord.loop_id == loop.loop_id)
                    .with_for_update()
                )
                if record is None:
                    raise MultiStepPlanNotFoundError("Observed Task Loop disappeared")
                current = self._loop_from_record(record)
                if current.status != "observed":
                    return current
                if current.loop_digest != loop.loop_digest:
                    raise MultiStepPlanConflictError("Observed Task Loop revision changed")
                session.add(self._event_record(event))
                self._apply_loop(record, settled)
        except IntegrityError:
            persisted = await self.get(loop.source.task_id)
            if persisted is not None and persisted.status != "observed":
                return persisted
            raise MultiStepPlanConflictError(
                "Concurrent Task Loop failure did not converge"
            ) from None
        persisted = await self.get(loop.source.task_id)
        if persisted is None or persisted.status != "failed":
            raise MultiStepPlanConflictError("Task Loop failure was not persisted")
        return persisted

    def _build_draft(
        self,
        loop: TaskLoop,
        deferred: RevalidatedDeferredPlan,
        composition: ModelPlannerComposition,
    ) -> tuple[ModelPlannerDraft, tuple[ModelPlannerStepBinding, ...]]:
        adjudication = deferred.planning.adjudication
        if adjudication is None:
            raise MultiStepPlanProofRejectedError("Deferred Turn Planner adjudication disappeared")
        composite_nodes = {item.local_key: item for item in composition.expected_plan.nodes}
        bindings: list[ModelPlannerStepBinding] = []
        for source_step, composed in zip(
            deferred.steps,
            composition.step_bindings,
            strict=True,
        ):
            if (
                composed.offer_key != source_step.offer.offer_key
                or composed.route_id != source_step.route.route_id
                or composed.parameter_binding_digest != source_step.parameter_binding_digest
            ):
                raise MultiStepPlanProofRejectedError(
                    "Composer step binding changed its revalidated Offer"
                )
            source_nodes = {item.local_key: item for item in source_step.offer.expected_plan.nodes}
            mappings: list[ModelPlannerNodeMapping] = []
            for source_key, composite_key in composed.source_to_composite_keys:
                source_node = source_nodes.get(source_key)
                composite_node = composite_nodes.get(composite_key)
                if source_node is None or composite_node is None:
                    raise MultiStepPlanProofRejectedError(
                        "Composer node mapping does not resolve exactly"
                    )
                mappings.append(
                    ModelPlannerNodeMapping.build(
                        source_node_id=source_node.node_id,
                        source_local_key=source_node.local_key,
                        source_node_spec_digest=source_node.node_spec_digest,
                        composite_node_id=composite_node.node_id,
                        composite_local_key=composite_node.local_key,
                        composite_node_spec_digest=composite_node.node_spec_digest,
                    )
                )
            parameter_bindings = tuple(
                item
                for item in adjudication.parameter_bindings
                if item.offer_key == source_step.offer.offer_key
            )
            bindings.append(
                ModelPlannerStepBinding.build(
                    source=loop.source,
                    ordinal=composed.step_index,
                    offer=source_step.offer.ref,
                    recipe=source_step.offer.trusted_recipe,
                    policy_snapshot_digest=source_step.offer.policy_snapshot_digest,
                    source_plan_id=source_step.offer.expected_plan.plan_id,
                    source_plan_manifest_digest=(
                        source_step.offer.expected_plan.plan_manifest_digest
                    ),
                    source_plan_binding_snapshot_digest=(
                        source_step.offer.expected_plan.binding_snapshot_digest
                    ),
                    budget=source_step.offer.budget,
                    parameter_bindings=parameter_bindings,
                    node_mappings=tuple(mappings),
                    created_at=loop.created_at,
                )
            )
        draft = ModelPlannerDraft.build(
            source=loop.source,
            steps=tuple(item.ref for item in bindings),
            task_contract=composition.contract,
            draft_plan=composition.draft,
            expected_plan=composition.expected_plan,
            created_at=loop.created_at,
        )
        return draft, tuple(bindings)

    @staticmethod
    def _source_from_planning(planning: TurnPlanningRead | None) -> TaskLoopSourceRef:
        if planning is None:
            raise MultiStepPlanNotFoundError("Deferred Turn Planner proof is missing")
        adjudication = planning.adjudication
        binding = planning.binding
        if (
            planning.run.status != "succeeded"
            or adjudication is None
            or binding is None
            or not (
                (
                    adjudication.outcome == "multi_step_deferred"
                    and binding.status == "multi_step_deferred"
                    and binding.reason_code == "MULTI_STEP_PLAN_DEFERRED"
                )
                or (
                    adjudication.outcome == "single_step"
                    and binding.status == "bound"
                    and binding.reason_code == "MODEL_PLANNER_SINGLE_STEP"
                    and len(adjudication.selected_offers) == 1
                    and any(
                        item.ref == adjudication.selected_offers[0]
                        and RouteRecipeCatalog.is_planner_only_route(
                            item.trusted_recipe.route_id
                        )
                        for item in planning.offers
                    )
                )
            )
        ):
            raise MultiStepPlanNotEligibleError(
                "Turn Planner outcome is not eligible for the generic Task Loop"
            )
        return TaskLoopSourceRef(
            task_id=planning.task_id,
            user_message_id=planning.user_message_id,
            user_message_digest=planning.user_message_digest,
            turn_planner_run_id=planning.run.run_id,
            turn_planner_run_digest=planning.run.run_digest,
            adjudication_id=adjudication.adjudication_id,
            adjudication_digest=adjudication.adjudication_digest,
            turn_plan_binding_id=binding.binding_id,
            turn_plan_binding_digest=binding.binding_digest,
        )

    @staticmethod
    def _failure_code(error: Exception) -> ModelPlannerFailureCode:
        if isinstance(error, ModelPlannerDomainLimitError):
            return "MULTI_STEP_BUDGET_EXCEEDED"
        if isinstance(error, ModelPlannerPrivacyConflictError):
            return "MULTI_STEP_CONTRACT_REJECTED"
        if isinstance(error, ModelPlannerOfferRejectedError):
            return "MULTI_STEP_OFFER_REJECTED"
        if isinstance(error, (TurnPlannerRuntimeError, MultiStepPlanProofRejectedError)):
            return "MULTI_STEP_BINDING_REJECTED"
        if isinstance(error, ValidationError):
            return "MULTI_STEP_PLAN_REJECTED"
        return "MULTI_STEP_PLAN_REJECTED"

    @classmethod
    def _failure_reason(cls, error: Exception) -> str:
        return cls._failure_code(error)

    @staticmethod
    def _observed_event(loop: TaskLoop) -> TaskLoopEvent:
        if loop.status != "observed":
            raise MultiStepPlanConflictError("Task Loop is no longer observable")
        event = TaskLoopEvent.observe(source=loop.source, created_at=loop.created_at)
        if (
            event.event_id != loop.latest_event_id
            or event.event_digest != loop.latest_event_digest
            or event.progress_digest != loop.progress_digest
        ):
            raise MultiStepPlanProofRejectedError("Task Loop Observe proof changed")
        return event

    @staticmethod
    def _loop_record(loop: TaskLoop) -> TaskLoopRecord:
        source = loop.source
        return TaskLoopRecord(
            loop_id=loop.loop_id,
            task_id=source.task_id,
            user_message_id=source.user_message_id,
            user_message_digest=source.user_message_digest,
            source_run_id=source.turn_planner_run_id,
            source_run_digest=source.turn_planner_run_digest,
            source_adjudication_id=source.adjudication_id,
            source_adjudication_digest=source.adjudication_digest,
            source_turn_plan_binding_id=source.turn_plan_binding_id,
            source_turn_plan_binding_digest=source.turn_plan_binding_digest,
            phase=loop.phase,
            status=loop.status,
            revision=loop.revision,
            event_count=loop.event_count,
            latest_event_id=loop.latest_event_id,
            latest_event_digest=loop.latest_event_digest,
            progress_digest=loop.progress_digest,
            active_draft_id=(loop.active_draft.draft_id if loop.active_draft is not None else None),
            active_draft_record_digest=(
                loop.active_draft.draft_record_digest if loop.active_draft is not None else None
            ),
            failure_manifest=(
                loop.failure.model_dump(mode="json") if loop.failure is not None else None
            ),
            failure_digest=(loop.failure.failure_digest if loop.failure is not None else None),
            manifest=loop.model_dump(mode="json"),
            loop_digest=loop.loop_digest,
            created_at=loop.created_at,
            updated_at=loop.updated_at,
        )

    @classmethod
    def _apply_loop(cls, record: TaskLoopRecord, loop: TaskLoop) -> None:
        replacement = cls._loop_record(loop)
        for field in (
            "phase",
            "status",
            "revision",
            "event_count",
            "latest_event_id",
            "latest_event_digest",
            "progress_digest",
            "active_draft_id",
            "active_draft_record_digest",
            "failure_manifest",
            "failure_digest",
            "manifest",
            "loop_digest",
            "updated_at",
        ):
            setattr(record, field, getattr(replacement, field))

    @staticmethod
    def _event_record(event: TaskLoopEvent) -> TaskLoopEventRecord:
        source = event.source
        return TaskLoopEventRecord(
            event_id=event.event_id,
            loop_id=event.loop_id,
            task_id=source.task_id,
            user_message_id=source.user_message_id,
            user_message_digest=source.user_message_digest,
            sequence=event.sequence,
            previous_event_digest=event.previous_event_digest,
            phase=event.phase,
            kind=event.kind,
            draft_id=event.draft.draft_id if event.draft is not None else None,
            draft_record_digest=(
                event.draft.draft_record_digest if event.draft is not None else None
            ),
            failure_manifest=(
                event.failure.model_dump(mode="json") if event.failure is not None else None
            ),
            failure_digest=(event.failure.failure_digest if event.failure is not None else None),
            progress_digest=event.progress_digest,
            manifest=event.model_dump(mode="json"),
            event_digest=event.event_digest,
            created_at=event.created_at,
        )

    @staticmethod
    def _draft_record(draft: ModelPlannerDraft, loop_id: str) -> ModelPlannerDraftRecord:
        source = draft.source
        expected = draft.expected_plan
        return ModelPlannerDraftRecord(
            draft_id=draft.draft_id,
            loop_id=loop_id,
            task_id=source.task_id,
            user_message_id=source.user_message_id,
            user_message_digest=source.user_message_digest,
            source_run_id=source.turn_planner_run_id,
            source_run_digest=source.turn_planner_run_digest,
            source_adjudication_id=source.adjudication_id,
            source_adjudication_digest=source.adjudication_digest,
            source_turn_plan_binding_id=source.turn_plan_binding_id,
            source_turn_plan_binding_digest=source.turn_plan_binding_digest,
            composer_version=draft.composer_version,
            step_count=draft.step_count,
            ordered_steps_manifest=[item.model_dump(mode="json") for item in draft.steps],
            step_set_digest=draft.step_set_digest,
            task_contract_manifest=draft.task_contract.model_dump(mode="json"),
            task_contract_digest=draft.task_contract_digest,
            draft_plan_manifest=draft.draft_plan.model_dump(mode="json"),
            draft_plan_digest=draft.draft_plan_digest,
            expected_plan_manifest=expected.model_dump(mode="json"),
            expected_plan_id=expected.plan_id,
            expected_plan_generation=expected.plan_generation,
            expected_plan_manifest_digest=expected.plan_manifest_digest,
            expected_plan_binding_snapshot_digest=expected.binding_snapshot_digest,
            manifest=draft.model_dump(mode="json"),
            draft_record_digest=draft.draft_record_digest,
            created_at=draft.created_at,
        )

    @staticmethod
    def _step_record(
        step: ModelPlannerStepBinding,
        draft_id: str,
        loop_id: str,
    ) -> ModelPlannerStepBindingRecord:
        source = step.source
        return ModelPlannerStepBindingRecord(
            step_binding_id=step.step_binding_id,
            draft_id=draft_id,
            loop_id=loop_id,
            task_id=source.task_id,
            user_message_id=source.user_message_id,
            user_message_digest=source.user_message_digest,
            ordinal=step.ordinal,
            offer_id=step.offer.offer_id,
            offer_key=step.offer.offer_key,
            offer_digest=step.offer.offer_digest,
            recipe_id=step.recipe.route_id,
            recipe_version=step.recipe.route_version,
            recipe_digest=step.recipe.route_manifest_digest,
            policy_snapshot_digest=step.policy_snapshot_digest,
            source_plan_id=step.source_plan_id,
            source_plan_manifest_digest=step.source_plan_manifest_digest,
            source_plan_binding_snapshot_digest=(step.source_plan_binding_snapshot_digest),
            budget_manifest=step.budget.model_dump(mode="json"),
            budget_digest=step.budget_digest,
            parameter_bindings_manifest=[
                item.model_dump(mode="json") for item in step.parameter_bindings
            ],
            parameter_bindings_digest=step.parameter_bindings_digest,
            node_mappings_manifest=[item.model_dump(mode="json") for item in step.node_mappings],
            node_mappings_digest=step.node_mappings_digest,
            manifest=step.model_dump(mode="json"),
            step_binding_digest=step.step_binding_digest,
            created_at=step.created_at,
        )

    @classmethod
    def _loop_from_record(cls, record: TaskLoopRecord) -> TaskLoop:
        try:
            loop = TaskLoop.model_validate(record.manifest)
        except ValidationError as error:
            raise MultiStepPlanProofRejectedError(
                "Persisted Task Loop manifest is invalid"
            ) from error
        source = loop.source
        expected = cls._loop_record(loop)
        fields = (
            "loop_id",
            "task_id",
            "user_message_id",
            "user_message_digest",
            "source_run_id",
            "source_run_digest",
            "source_adjudication_id",
            "source_adjudication_digest",
            "source_turn_plan_binding_id",
            "source_turn_plan_binding_digest",
            "phase",
            "status",
            "revision",
            "event_count",
            "latest_event_id",
            "latest_event_digest",
            "progress_digest",
            "active_draft_id",
            "active_draft_record_digest",
            "failure_manifest",
            "failure_digest",
            "manifest",
            "loop_digest",
        )
        if any(getattr(record, field) != getattr(expected, field) for field in fields) or (
            cls._aware(record.created_at) != loop.created_at
            or cls._aware(record.updated_at) != loop.updated_at
            or source.task_id != record.task_id
        ):
            raise MultiStepPlanProofRejectedError(
                "Persisted Task Loop columns diverge from its manifest"
            )
        return loop

    @classmethod
    def _event_from_record(cls, record: TaskLoopEventRecord) -> TaskLoopEvent:
        try:
            event = TaskLoopEvent.model_validate(record.manifest)
        except ValidationError as error:
            raise MultiStepPlanProofRejectedError(
                "Persisted Task Loop event manifest is invalid"
            ) from error
        expected = cls._event_record(event)
        fields = (
            "event_id",
            "loop_id",
            "task_id",
            "user_message_id",
            "user_message_digest",
            "sequence",
            "previous_event_digest",
            "phase",
            "kind",
            "draft_id",
            "draft_record_digest",
            "failure_manifest",
            "failure_digest",
            "progress_digest",
            "manifest",
            "event_digest",
        )
        if any(getattr(record, field) != getattr(expected, field) for field in fields) or (
            cls._aware(record.created_at) != event.created_at
        ):
            raise MultiStepPlanProofRejectedError(
                "Persisted Task Loop event columns diverge from its manifest"
            )
        return event

    @classmethod
    def _draft_from_record(cls, record: ModelPlannerDraftRecord) -> ModelPlannerDraft:
        try:
            draft = ModelPlannerDraft.model_validate(record.manifest)
        except ValidationError as error:
            raise MultiStepPlanProofRejectedError(
                "Persisted model Planner Draft manifest is invalid"
            ) from error
        expected = cls._draft_record(draft, record.loop_id)
        fields = (
            "draft_id",
            "loop_id",
            "task_id",
            "user_message_id",
            "user_message_digest",
            "source_run_id",
            "source_run_digest",
            "source_adjudication_id",
            "source_adjudication_digest",
            "source_turn_plan_binding_id",
            "source_turn_plan_binding_digest",
            "composer_version",
            "step_count",
            "ordered_steps_manifest",
            "step_set_digest",
            "task_contract_manifest",
            "task_contract_digest",
            "draft_plan_manifest",
            "draft_plan_digest",
            "expected_plan_manifest",
            "expected_plan_id",
            "expected_plan_generation",
            "expected_plan_manifest_digest",
            "expected_plan_binding_snapshot_digest",
            "manifest",
            "draft_record_digest",
        )
        if any(getattr(record, field) != getattr(expected, field) for field in fields) or (
            cls._aware(record.created_at) != draft.created_at
        ):
            raise MultiStepPlanProofRejectedError(
                "Persisted model Planner Draft columns diverge from its manifest"
            )
        return draft

    @classmethod
    def _step_from_record(
        cls,
        record: ModelPlannerStepBindingRecord,
    ) -> ModelPlannerStepBinding:
        try:
            step = ModelPlannerStepBinding.model_validate(record.manifest)
        except ValidationError as error:
            raise MultiStepPlanProofRejectedError(
                "Persisted model Planner step manifest is invalid"
            ) from error
        expected = cls._step_record(step, record.draft_id, record.loop_id)
        fields = (
            "step_binding_id",
            "draft_id",
            "loop_id",
            "task_id",
            "user_message_id",
            "user_message_digest",
            "ordinal",
            "offer_id",
            "offer_key",
            "offer_digest",
            "recipe_id",
            "recipe_version",
            "recipe_digest",
            "policy_snapshot_digest",
            "source_plan_id",
            "source_plan_manifest_digest",
            "source_plan_binding_snapshot_digest",
            "budget_manifest",
            "budget_digest",
            "parameter_bindings_manifest",
            "parameter_bindings_digest",
            "node_mappings_manifest",
            "node_mappings_digest",
            "manifest",
            "step_binding_digest",
        )
        if any(getattr(record, field) != getattr(expected, field) for field in fields) or (
            cls._aware(record.created_at) != step.created_at
        ):
            raise MultiStepPlanProofRejectedError(
                "Persisted model Planner step columns diverge from its manifest"
            )
        return step

    @staticmethod
    def _validate_event_chain(
        loop: TaskLoop,
        events: tuple[TaskLoopEvent, ...],
    ) -> None:
        if len(events) != loop.event_count or not events:
            raise MultiStepPlanProofRejectedError("Task Loop event count changed")
        first = events[0]
        if (
            first.kind != "observed"
            or first.sequence != 1
            or first.loop_id != loop.loop_id
            or first.source != loop.source
        ):
            raise MultiStepPlanProofRejectedError("Task Loop Observe event changed")
        previous = first
        for event in events[1:]:
            if (
                event.loop_id != loop.loop_id
                or event.source != loop.source
                or event.sequence != previous.sequence + 1
                or event.previous_event_digest != previous.event_digest
            ):
                raise MultiStepPlanProofRejectedError("Task Loop event chain changed")
            previous = event
        if (
            previous.event_id != loop.latest_event_id
            or previous.event_digest != loop.latest_event_digest
            or previous.progress_digest != loop.progress_digest
        ):
            raise MultiStepPlanProofRejectedError("Task Loop event head changed")
        if loop.status == "planned" and previous.draft != loop.active_draft:
            raise MultiStepPlanProofRejectedError("Task Loop Draft event changed")
        if loop.status == "failed" and previous.failure != loop.failure:
            raise MultiStepPlanProofRejectedError("Task Loop failure event changed")

    @staticmethod
    def _validate_draft_bundle(
        loop: TaskLoop,
        draft: ModelPlannerDraft,
        steps: tuple[ModelPlannerStepBinding, ...],
    ) -> None:
        if (
            loop.active_draft != draft.ref
            or draft.source != loop.source
            or len(steps) != draft.step_count
            or tuple(item.ref for item in steps) != draft.steps
            or any(item.source != loop.source for item in steps)
        ):
            raise MultiStepPlanProofRejectedError(
                "Model Planner Draft bundle changed its source or ordered steps"
            )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise MultiStepPlanProofRejectedError("Task Loop clock must be timezone-aware")
        return value.astimezone(UTC)

    @staticmethod
    def _aware(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
