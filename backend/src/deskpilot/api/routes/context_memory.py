"""User-visible conversation, working-memory, and Context Manifest routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Response, status

from deskpilot.api.dependencies import get_context_memory_runtime
from deskpilot.api.problem_details import ProblemException
from deskpilot.application.context_memory_runtime import (
    ContextMemoryConflictError,
    ContextMemoryError,
    ContextMemoryNotFoundError,
    ContextMemoryRuntime,
)
from deskpilot.domain.agent_runtime import INVOCATION_ID_PATTERN
from deskpilot.domain.context_memory import (
    WORKING_MEMORY_ID_PATTERN,
    ContextManifest,
    ConversationMessageRead,
    ConversationRead,
    CreateConversationMessageRequest,
    CreateConversationRequest,
    CreateWorkingMemoryRequest,
    CurrentContextRead,
    WorkingMemoryItemRead,
)
from deskpilot.domain.task_plans import (
    CONVERSATION_ID_PATTERN,
    MESSAGE_ID_PATTERN,
    TASK_ID_PATTERN,
)

router = APIRouter(tags=["context-memory"])
RuntimeDependency = Annotated[ContextMemoryRuntime, Depends(get_context_memory_runtime)]
ConversationId = Annotated[str, Path(pattern=CONVERSATION_ID_PATTERN)]
MessageId = Annotated[str, Path(pattern=MESSAGE_ID_PATTERN)]
TaskId = Annotated[str, Path(pattern=TASK_ID_PATTERN)]
MemoryItemId = Annotated[str, Path(pattern=WORKING_MEMORY_ID_PATTERN)]
InvocationId = Annotated[str, Path(pattern=INVOCATION_ID_PATTERN)]


@router.post(
    "/conversations",
    response_model=ConversationRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_conversation(
    request: CreateConversationRequest,
    runtime: RuntimeDependency,
    response: Response,
) -> ConversationRead:
    response.headers["Cache-Control"] = "no-store"
    return await runtime.create_conversation(request)


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=ConversationMessageRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_message(
    conversation_id: ConversationId,
    request: CreateConversationMessageRequest,
    runtime: RuntimeDependency,
    response: Response,
) -> ConversationMessageRead:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await runtime.add_message(conversation_id, request)
    except ContextMemoryError as error:
        raise _problem(error) from error


@router.delete(
    "/conversation-messages/{message_id}", response_model=ConversationMessageRead
)
async def delete_message(
    message_id: MessageId,
    runtime: RuntimeDependency,
    response: Response,
) -> ConversationMessageRead:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await runtime.delete_message(message_id)
    except ContextMemoryError as error:
        raise _problem(error) from error


@router.post(
    "/tasks/{task_id}/working-memory",
    response_model=WorkingMemoryItemRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_working_memory(
    task_id: TaskId,
    request: CreateWorkingMemoryRequest,
    runtime: RuntimeDependency,
    response: Response,
) -> WorkingMemoryItemRead:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await runtime.add_working_memory(task_id, request)
    except ContextMemoryError as error:
        raise _problem(error) from error


@router.delete(
    "/working-memory/{memory_item_id}", response_model=WorkingMemoryItemRead
)
async def delete_working_memory(
    memory_item_id: MemoryItemId,
    runtime: RuntimeDependency,
    response: Response,
) -> WorkingMemoryItemRead:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await runtime.delete_working_memory(memory_item_id)
    except ContextMemoryError as error:
        raise _problem(error) from error


@router.get("/tasks/{task_id}/context", response_model=CurrentContextRead)
async def get_current_context(
    task_id: TaskId,
    runtime: RuntimeDependency,
    response: Response,
) -> CurrentContextRead:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await runtime.current(task_id)
    except ContextMemoryError as error:
        raise _problem(error) from error


@router.get(
    "/agent-invocations/{invocation_id}/context-manifest",
    response_model=ContextManifest,
)
async def get_context_manifest(
    invocation_id: InvocationId,
    runtime: RuntimeDependency,
    response: Response,
) -> ContextManifest:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await runtime.get_manifest_for_invocation(invocation_id)
    except ContextMemoryError as error:
        raise _problem(error) from error


def _problem(error: ContextMemoryError) -> ProblemException:
    return ProblemException(
        status_code=(
            404
            if isinstance(error, ContextMemoryNotFoundError)
            else 409
            if isinstance(error, ContextMemoryConflictError)
            else 400
        ),
        code=error.code,
        title="上下文或工作记忆命令被拒绝",
        detail=str(error),
    )

