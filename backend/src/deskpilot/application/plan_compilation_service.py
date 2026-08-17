"""Atomic persistence and proof-checked reads for immutable planning manifests."""

from typing import Literal, cast

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from deskpilot.application.plan_compiler import PlanCompiler, PlanCompilerError
from deskpilot.domain.task_plans import (
    DraftPlan,
    ExecutablePlan,
    ExecutablePlanPage,
    ExecutablePlanRead,
    PlanningStateRead,
    TaskContract,
    TaskContractVersionPage,
    TaskContractVersionRead,
)
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import (
    TaskContractVersionRecord,
    TaskPlanGenerationRecord,
    TaskPlanningStateRecord,
    TaskRecord,
    utc_now,
)


class PlanningError(RuntimeError):
    code = "PLANNING_ERROR"


class PlanningNotFoundError(PlanningError):
    code = "PLANNING_NOT_FOUND"


class PlanningVersionConflictError(PlanningError):
    code = "PLANNING_VERSION_CONFLICT"


class PlanningProofRejectedError(PlanningError):
    code = "PLANNING_PROOF_REJECTED"


class PlanCompilationService:
    def __init__(self, database: Database, compiler: PlanCompiler) -> None:
        self._database = database
        self._compiler = compiler

    async def activate(
        self,
        contract: TaskContract,
        draft: DraftPlan,
    ) -> ExecutablePlanRead:
        """Compile and atomically advance one Task's immutable planning generation."""

        try:
            async with self._database.session() as session, session.begin():
                task = await session.scalar(
                    select(TaskRecord)
                    .where(TaskRecord.task_id == contract.task_id)
                    .with_for_update()
                )
                if task is None:
                    raise PlanningNotFoundError("Task does not exist")
                state = await session.scalar(
                    select(TaskPlanningStateRecord)
                    .where(TaskPlanningStateRecord.task_id == contract.task_id)
                    .with_for_update()
                )
                generation, insert_contract = await self._next_generation(
                    session,
                    contract,
                    state,
                )
                plan = self._compiler.compile(contract, draft, generation=generation)
                if insert_contract:
                    session.add(
                        TaskContractVersionRecord(
                            task_id=contract.task_id,
                            version=contract.version,
                            contract_id=contract.contract_id,
                            previous_contract_digest=contract.previous_contract_digest,
                            manifest=contract.model_dump(mode="json"),
                            contract_digest=contract.digest,
                            created_at=utc_now(),
                        )
                    )
                if state is not None:
                    previous = await session.get(
                        TaskPlanGenerationRecord,
                        (contract.task_id, state.active_plan_generation),
                    )
                    if previous is None or previous.status != "active":
                        raise PlanningProofRejectedError(
                            "Active plan pointer has no matching generation"
                        )
                    self._plan_from_record(previous)
                    previous.status = "superseded"
                session.add(
                    TaskPlanGenerationRecord(
                        task_id=contract.task_id,
                        generation=generation,
                        plan_id=plan.plan_id,
                        contract_version=contract.version,
                        contract_digest=contract.digest,
                        status="active",
                        manifest=plan.model_dump(mode="json"),
                        plan_manifest_digest=plan.plan_manifest_digest,
                        created_at=utc_now(),
                    )
                )
                if state is None:
                    state = TaskPlanningStateRecord(
                        task_id=contract.task_id,
                        active_contract_version=contract.version,
                        active_contract_digest=contract.digest,
                        active_plan_generation=generation,
                        active_plan_digest=plan.plan_manifest_digest,
                        revision=1,
                        updated_at=utc_now(),
                    )
                    session.add(state)
                else:
                    state.active_contract_version = contract.version
                    state.active_contract_digest = contract.digest
                    state.active_plan_generation = generation
                    state.active_plan_digest = plan.plan_manifest_digest
                    state.revision += 1
                    state.updated_at = utc_now()
                await session.flush()
                return ExecutablePlanRead(plan=plan, status="active")
        except IntegrityError as error:
            raise PlanningVersionConflictError(
                "Planning generation changed concurrently"
            ) from error

    async def get_state(self, task_id: str) -> PlanningStateRead:
        async with self._database.session() as session:
            state = await session.get(TaskPlanningStateRecord, task_id)
            if state is None:
                raise PlanningNotFoundError("Task has no planning state")
            await self._validate_active_state(session, state)
            return self._state_read(state)

    async def get_current_contract(self, task_id: str) -> TaskContractVersionRead:
        async with self._database.session() as session:
            state = await session.get(TaskPlanningStateRecord, task_id)
            if state is None:
                raise PlanningNotFoundError("Task has no Task Contract")
            await self._validate_active_state(session, state)
            record = await session.get(
                TaskContractVersionRecord,
                (task_id, state.active_contract_version),
            )
            if record is None:
                raise PlanningProofRejectedError("Active Task Contract is missing")
            return TaskContractVersionRead(
                contract=self._contract_from_record(record),
                contract_digest=record.contract_digest,
                active=True,
            )

    async def list_contracts(self, task_id: str) -> TaskContractVersionPage:
        async with self._database.session() as session:
            state = await session.get(TaskPlanningStateRecord, task_id)
            if state is None:
                raise PlanningNotFoundError("Task has no Task Contract")
            await self._validate_active_state(session, state)
            records = tuple(
                (
                    await session.scalars(
                        select(TaskContractVersionRecord)
                        .where(TaskContractVersionRecord.task_id == task_id)
                        .order_by(TaskContractVersionRecord.version)
                    )
                ).all()
            )
            return TaskContractVersionPage(
                contracts=tuple(
                    TaskContractVersionRead(
                        contract=self._contract_from_record(record),
                        contract_digest=record.contract_digest,
                        active=record.version == state.active_contract_version,
                    )
                    for record in records
                )
            )

    async def get_plan(self, task_id: str, generation: int) -> ExecutablePlanRead:
        async with self._database.session() as session:
            record = await session.get(TaskPlanGenerationRecord, (task_id, generation))
            if record is None:
                raise PlanningNotFoundError("Executable Plan generation does not exist")
            await self._validate_plan_contract(session, record)
            return ExecutablePlanRead(
                plan=self._plan_from_record(record),
                status=self._plan_status(record.status),
            )

    async def list_plans(self, task_id: str) -> ExecutablePlanPage:
        async with self._database.session() as session:
            state = await session.get(TaskPlanningStateRecord, task_id)
            if state is None:
                raise PlanningNotFoundError("Task has no Executable Plan")
            await self._validate_active_state(session, state)
            records = tuple(
                (
                    await session.scalars(
                        select(TaskPlanGenerationRecord)
                        .where(TaskPlanGenerationRecord.task_id == task_id)
                        .order_by(TaskPlanGenerationRecord.generation)
                    )
                ).all()
            )
            result: list[ExecutablePlanRead] = []
            for record in records:
                await self._validate_plan_contract(session, record)
                result.append(
                    ExecutablePlanRead(
                        plan=self._plan_from_record(record),
                        status=self._plan_status(record.status),
                    )
                )
            return ExecutablePlanPage(plans=tuple(result))

    async def _next_generation(
        self,
        session: AsyncSession,
        contract: TaskContract,
        state: TaskPlanningStateRecord | None,
    ) -> tuple[int, bool]:
        if state is None:
            if contract.version != 1 or contract.previous_contract_digest is not None:
                raise PlanningVersionConflictError(
                    "First Task Contract must be version 1 without a predecessor"
                )
            return 1, True
        await self._validate_active_state(session, state)
        active_record = await session.get(
            TaskContractVersionRecord,
            (contract.task_id, state.active_contract_version),
        )
        if active_record is None:
            raise PlanningProofRejectedError("Active Task Contract is missing")
        active_contract = self._contract_from_record(active_record)
        if contract.contract_id != active_contract.contract_id:
            raise PlanningVersionConflictError("Task Contract identity cannot change")
        if contract.version == state.active_contract_version:
            if contract.digest != state.active_contract_digest:
                raise PlanningVersionConflictError(
                    "Existing Task Contract version is immutable"
                )
            return state.active_plan_generation + 1, False
        if contract.version != state.active_contract_version + 1:
            raise PlanningVersionConflictError("Task Contract versions must be contiguous")
        if contract.previous_contract_digest != state.active_contract_digest:
            raise PlanningVersionConflictError("Task Contract predecessor digest is stale")
        return state.active_plan_generation + 1, True

    async def _validate_active_state(
        self,
        session: AsyncSession,
        state: TaskPlanningStateRecord,
    ) -> None:
        contract_record = await session.get(
            TaskContractVersionRecord,
            (state.task_id, state.active_contract_version),
        )
        plan_record = await session.get(
            TaskPlanGenerationRecord,
            (state.task_id, state.active_plan_generation),
        )
        if contract_record is None or plan_record is None or plan_record.status != "active":
            raise PlanningProofRejectedError("Planning state points to missing evidence")
        contract = self._contract_from_record(contract_record)
        plan = self._plan_from_record(plan_record)
        if (
            contract.digest != state.active_contract_digest
            or plan.plan_manifest_digest != state.active_plan_digest
            or plan.task_contract.digest != contract.digest
        ):
            raise PlanningProofRejectedError("Planning state digest does not match evidence")

    async def _validate_plan_contract(
        self,
        session: AsyncSession,
        record: TaskPlanGenerationRecord,
    ) -> None:
        contract_record = await session.get(
            TaskContractVersionRecord,
            (record.task_id, record.contract_version),
        )
        if contract_record is None:
            raise PlanningProofRejectedError("Executable Plan Task Contract is missing")
        contract = self._contract_from_record(contract_record)
        plan = self._plan_from_record(record)
        if (
            record.contract_digest != contract.digest
            or plan.task_contract.version != contract.version
            or plan.task_contract.digest != contract.digest
        ):
            raise PlanningProofRejectedError("Executable Plan Task Contract binding changed")

    @staticmethod
    def _contract_from_record(record: TaskContractVersionRecord) -> TaskContract:
        try:
            contract = TaskContract.model_validate(record.manifest)
        except ValidationError as error:
            raise PlanningProofRejectedError("Task Contract manifest is invalid") from error
        if (
            contract.task_id != record.task_id
            or contract.version != record.version
            or contract.contract_id != record.contract_id
            or contract.previous_contract_digest != record.previous_contract_digest
            or contract.digest != record.contract_digest
        ):
            raise PlanningProofRejectedError("Task Contract digest does not match")
        return contract

    def _plan_from_record(self, record: TaskPlanGenerationRecord) -> ExecutablePlan:
        try:
            plan = ExecutablePlan.model_validate(record.manifest)
            self._compiler.validate_manifest(plan)
        except (ValidationError, PlanCompilerError) as error:
            raise PlanningProofRejectedError("Executable Plan manifest is invalid") from error
        if (
            plan.task_id != record.task_id
            or plan.plan_generation != record.generation
            or plan.plan_id != record.plan_id
            or plan.task_contract.version != record.contract_version
            or plan.task_contract.digest != record.contract_digest
            or plan.plan_manifest_digest != record.plan_manifest_digest
        ):
            raise PlanningProofRejectedError("Executable Plan digest does not match")
        return plan

    @staticmethod
    def _state_read(state: TaskPlanningStateRecord) -> PlanningStateRead:
        return PlanningStateRead(
            task_id=state.task_id,
            active_contract_version=state.active_contract_version,
            active_contract_digest=state.active_contract_digest,
            active_plan_generation=state.active_plan_generation,
            active_plan_digest=state.active_plan_digest,
            revision=state.revision,
        )

    @staticmethod
    def _plan_status(value: str) -> Literal["active", "superseded"]:
        if value not in {"active", "superseded"}:
            raise PlanningProofRejectedError("Executable Plan status is invalid")
        return cast(Literal["active", "superseded"], value)
