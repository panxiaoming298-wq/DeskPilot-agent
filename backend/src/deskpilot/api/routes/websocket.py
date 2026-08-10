"""Authenticated task event WebSocket with durable backlog replay."""

import asyncio
from typing import cast

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from starlette.types import Message

from deskpilot.application.task_service import TaskNotFoundError
from deskpilot.core.security import WEBSOCKET_PROTOCOL, LocalSessionSecurity
from deskpilot.domain.schemas import TaskEventRead

router = APIRouter(tags=["events"])

TERMINAL_EVENTS = {"task.completed", "task.failed", "task.cancelled"}


async def _stream_live_events(
    websocket: WebSocket,
    queue: asyncio.Queue[TaskEventRead],
    cursor: int,
) -> None:
    event_waiter: asyncio.Task[TaskEventRead] = asyncio.create_task(queue.get())
    client_waiter: asyncio.Task[Message] = asyncio.create_task(websocket.receive())

    try:
        while True:
            done, _ = await asyncio.wait(
                {event_waiter, client_waiter},
                return_when=asyncio.FIRST_COMPLETED,
            )

            if client_waiter in done:
                message = client_waiter.result()
                if message["type"] == "websocket.disconnect":
                    return
                # Client data frames are unsupported. Keep the event waiter alive so an
                # event arriving in the same scheduling turn is still delivered.
                client_waiter = asyncio.create_task(websocket.receive())

            if event_waiter in done:
                event = event_waiter.result()
                if event.seq > cursor:
                    await websocket.send_json(event.model_dump(mode="json"))
                    cursor = event.seq
                    if event.type in TERMINAL_EVENTS:
                        await websocket.close(code=1000)
                        return
                event_waiter = asyncio.create_task(queue.get())
    finally:
        event_waiter.cancel()
        client_waiter.cancel()
        await asyncio.gather(event_waiter, client_waiter, return_exceptions=True)


@router.websocket("/ws/tasks/{task_id}")
async def task_events(
    websocket: WebSocket,
    task_id: str,
    after_seq: int = Query(default=0, ge=0),
) -> None:
    service = websocket.app.state.task_service
    broker = websocket.app.state.event_broker
    security = cast(LocalSessionSecurity, websocket.app.state.session_security)

    if not security.is_allowed_origin(websocket.headers.get("origin")):
        await websocket.close(code=4403, reason="Origin not allowed")
        return

    protocols = [
        protocol.strip()
        for protocol in websocket.headers.get("sec-websocket-protocol", "").split(",")
        if protocol.strip()
    ]
    if not security.authenticate_websocket_protocols(protocols):
        await websocket.close(code=4401, reason="Session token invalid")
        return

    try:
        await service.get_task(task_id)
    except TaskNotFoundError:
        await websocket.close(code=4404, reason="Task not found")
        return

    await websocket.accept(subprotocol=WEBSOCKET_PROTOCOL)
    cursor = after_seq
    try:
        async with broker.subscribe(task_id) as queue:
            backlog = await service.list_events(task_id, after_seq=cursor)
            for event in backlog:
                if event.seq <= cursor:
                    continue
                await websocket.send_json(event.model_dump(mode="json"))
                cursor = event.seq
                if event.type in TERMINAL_EVENTS:
                    await websocket.close(code=1000)
                    return

            await _stream_live_events(websocket, queue, cursor)
    except WebSocketDisconnect:
        return
