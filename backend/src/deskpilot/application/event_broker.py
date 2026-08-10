"""In-process live event fan-out with database replay as the source of truth."""

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from deskpilot.domain.schemas import TaskEventRead


class EventBroker:
    """Publishes committed events to active WebSocket subscribers."""

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[TaskEventRead]]] = defaultdict(set)
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def subscribe(self, task_id: str) -> AsyncIterator[asyncio.Queue[TaskEventRead]]:
        queue: asyncio.Queue[TaskEventRead] = asyncio.Queue(maxsize=256)
        async with self._lock:
            self._subscribers[task_id].add(queue)
        try:
            yield queue
        finally:
            async with self._lock:
                subscribers = self._subscribers.get(task_id)
                if subscribers is not None:
                    subscribers.discard(queue)
                    if not subscribers:
                        self._subscribers.pop(task_id, None)

    async def publish(self, event: TaskEventRead) -> None:
        async with self._lock:
            queues = tuple(self._subscribers.get(event.task_id, ()))

        for queue in queues:
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(event)
