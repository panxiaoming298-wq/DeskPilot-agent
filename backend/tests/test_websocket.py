import asyncio
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from fastapi import WebSocket
from starlette.types import Message

from deskpilot.api.routes.websocket import _stream_live_events
from deskpilot.application.event_broker import EventBroker
from deskpilot.domain.schemas import TaskEventRead


class StubWebSocket:
    def __init__(self) -> None:
        self.incoming: asyncio.Queue[Message] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []
        self.message_sent = asyncio.Event()
        self.close_codes: list[int] = []

    async def receive(self) -> Message:
        return await self.incoming.get()

    async def send_json(self, data: dict[str, Any]) -> None:
        self.sent.append(data)
        self.message_sent.set()

    async def close(self, code: int = 1000) -> None:
        self.close_codes.append(code)


def _event(*, seq: int = 1, event_type: str = "task.status_changed") -> TaskEventRead:
    return TaskEventRead(
        event_id=f"evt_{seq}",
        task_id="tsk_websocket",
        seq=seq,
        type=event_type,
        timestamp=datetime.now(UTC),
        trace_id="trace_websocket",
        payload={"from": "paused", "to": "running"},
    )


@pytest.mark.asyncio
async def test_live_stream_disconnect_cancels_event_waiter() -> None:
    websocket = StubWebSocket()
    queue: asyncio.Queue[TaskEventRead] = asyncio.Queue()
    stream = asyncio.create_task(
        _stream_live_events(cast(WebSocket, websocket), queue, cursor=0)
    )

    await websocket.incoming.put({"type": "websocket.disconnect", "code": 1000})
    await asyncio.wait_for(stream, timeout=1)

    event = _event()
    await queue.put(event)
    assert queue.get_nowait() is event
    assert websocket.sent == []


@pytest.mark.asyncio
async def test_live_stream_ignores_client_frame_without_losing_ready_event() -> None:
    websocket = StubWebSocket()
    queue: asyncio.Queue[TaskEventRead] = asyncio.Queue()
    terminal_event = _event(event_type="task.completed")
    await websocket.incoming.put({"type": "websocket.receive", "text": "unsupported"})
    await queue.put(terminal_event)

    await asyncio.wait_for(
        _stream_live_events(cast(WebSocket, websocket), queue, cursor=0),
        timeout=1,
    )

    assert [message["event_id"] for message in websocket.sent] == [terminal_event.event_id]
    assert websocket.close_codes == [1000]
    disconnect: Message = {"type": "websocket.disconnect", "code": 1000}
    await websocket.incoming.put(disconnect)
    assert websocket.incoming.get_nowait() is disconnect


@pytest.mark.asyncio
async def test_disconnected_subscriber_does_not_lose_event_for_surviving_client() -> None:
    broker = EventBroker()
    departing = StubWebSocket()
    surviving = StubWebSocket()

    async with broker.subscribe("tsk_websocket") as surviving_queue:
        surviving_stream = asyncio.create_task(
            _stream_live_events(cast(WebSocket, surviving), surviving_queue, cursor=0)
        )
        async with broker.subscribe("tsk_websocket") as departing_queue:
            departing_stream = asyncio.create_task(
                _stream_live_events(cast(WebSocket, departing), departing_queue, cursor=0)
            )
            await departing.incoming.put({"type": "websocket.disconnect", "code": 1000})
            await asyncio.wait_for(departing_stream, timeout=1)

        event = _event()
        await broker.publish(event)
        await asyncio.wait_for(surviving.message_sent.wait(), timeout=1)

        await surviving.incoming.put({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(surviving_stream, timeout=1)

    assert [message["event_id"] for message in surviving.sent] == [event.event_id]
    assert departing.sent == []
