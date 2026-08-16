"""Process-local fair admission control for effect DAG Runner work."""

import asyncio
from collections import defaultdict, deque
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass, field
from typing import Protocol, TypeVar

from deskpilot.domain.effect_graph import EffectDagAdmissionProof

_T = TypeVar("_T")


class EffectDagAdmissionCancelledError(RuntimeError):
    """Raised when a graph cancellation removes work waiting for capacity."""


@dataclass(frozen=True, slots=True)
class EffectDagAdmissionRequest:
    """One candidate node that may consume a Runner concurrency slot."""

    node_id: str
    tool_name: str


@dataclass(frozen=True, slots=True)
class EffectDagAdmissionSnapshot:
    """Observable controller state used by health checks and scheduler tests."""

    active_total: int
    active_by_graph: dict[str, int]
    active_by_tool: dict[str, int]
    waiting_batches: int
    waiting_graphs: tuple[str, ...]


class EffectDagAdmissionPermitPort(Protocol):
    """Capacity permit shared by local and database-backed admission."""

    graph_id: str
    request: EffectDagAdmissionRequest

    @property
    def proof(self) -> EffectDagAdmissionProof | None: ...

    async def run(self, work: Awaitable[_T]) -> _T: ...

    async def release(self) -> None: ...


class EffectDagAdmissionControllerPort(Protocol):
    """Admission interface consumed by forward and compensation dispatchers."""

    @property
    def per_graph_limit(self) -> int: ...

    async def acquire_batch(
        self,
        graph_id: str,
        requests: tuple[EffectDagAdmissionRequest, ...],
    ) -> tuple[EffectDagAdmissionPermitPort, ...]: ...

    async def cancel_waiters(self, graph_id: str) -> None: ...

    async def snapshot(self) -> EffectDagAdmissionSnapshot: ...


@dataclass(slots=True)
class _AdmissionWaiter:
    graph_id: str
    remaining: list[EffectDagAdmissionRequest]
    future: asyncio.Future[tuple["EffectDagAdmissionPermit", ...]]
    granted: list["EffectDagAdmissionPermit"] = field(default_factory=list)


class EffectDagAdmissionPermit:
    """One idempotently releasable global/graph/tool capacity reservation."""

    def __init__(
        self,
        controller: "EffectDagAdmissionController",
        *,
        graph_id: str,
        request: EffectDagAdmissionRequest,
    ) -> None:
        self.graph_id = graph_id
        self.request = request
        self.proof: EffectDagAdmissionProof | None = None
        self._controller = controller
        self._released = False

    async def run(self, work: Awaitable[_T]) -> _T:
        return await work

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self._controller._release(self.graph_id, self.request.tool_name)


class EffectDagAdmissionController:
    """Round-robin, work-conserving admission before durable node claims.

    Callers submit one bounded page of candidates. The controller grants only the
    subset that fits all three limits. Waiting batches exert backpressure without
    creating database claims, and graph cancellation can remove those waiters.
    """

    def __init__(
        self,
        *,
        global_limit: int,
        per_graph_limit: int,
        default_tool_limit: int,
        tool_limits: Mapping[str, int] | None = None,
    ) -> None:
        if not 1 <= global_limit <= 1_024:
            raise ValueError("Effect DAG global concurrency is invalid")
        if not 1 <= per_graph_limit <= global_limit:
            raise ValueError("Effect DAG per-graph concurrency is invalid")
        if not 1 <= default_tool_limit <= global_limit:
            raise ValueError("Effect DAG per-tool concurrency is invalid")
        configured_tool_limits = dict(tool_limits or {})
        if any(
            not tool_name or not 1 <= limit <= global_limit
            for tool_name, limit in configured_tool_limits.items()
        ):
            raise ValueError("Effect DAG tool concurrency override is invalid")
        self._global_limit = global_limit
        self._per_graph_limit = per_graph_limit
        self._default_tool_limit = default_tool_limit
        self._tool_limits = configured_tool_limits
        self._lock = asyncio.Lock()
        self._active_total = 0
        self._active_by_graph: defaultdict[str, int] = defaultdict(int)
        self._active_by_tool: defaultdict[str, int] = defaultdict(int)
        self._queues: dict[str, deque[_AdmissionWaiter]] = {}
        self._graph_ring: deque[str] = deque()
        self._ring_members: set[str] = set()
        self._waiting_batches = 0

    @property
    def per_graph_limit(self) -> int:
        return self._per_graph_limit

    async def acquire_batch(
        self,
        graph_id: str,
        requests: tuple[EffectDagAdmissionRequest, ...],
    ) -> tuple[EffectDagAdmissionPermit, ...]:
        """Wait for and return a fair, non-empty subset of candidate permits."""
        if not graph_id:
            raise ValueError("Effect DAG admission graph ID is required")
        if not requests or len({request.node_id for request in requests}) != len(requests):
            raise ValueError("Effect DAG admission requires distinct candidates")
        if any(not request.node_id or not request.tool_name for request in requests):
            raise ValueError("Effect DAG admission candidate is invalid")
        loop = asyncio.get_running_loop()
        waiter = _AdmissionWaiter(
            graph_id=graph_id,
            remaining=list(requests),
            future=loop.create_future(),
        )
        async with self._lock:
            queue = self._queues.setdefault(graph_id, deque())
            queue.append(waiter)
            self._waiting_batches += 1
            self._add_graph_to_ring(graph_id)

        try:
            # Let concurrently-started graphs enter the ring before the first drain.
            await asyncio.sleep(0)
            async with self._lock:
                self._drain_locked()
            return await asyncio.shield(waiter.future)
        except BaseException:
            permits = await self._withdraw_waiter(waiter)
            await asyncio.gather(
                *(permit.release() for permit in permits),
                return_exceptions=True,
            )
            raise

    async def cancel_waiters(self, graph_id: str) -> None:
        """Wake all capacity waiters for one graph without touching active calls."""
        async with self._lock:
            queue = self._queues.pop(graph_id, deque())
            self._remove_graph_from_ring(graph_id)
            while queue:
                waiter = queue.popleft()
                self._waiting_batches -= 1
                if not waiter.future.done():
                    waiter.future.set_exception(EffectDagAdmissionCancelledError(graph_id))
            self._drain_locked()

    async def snapshot(self) -> EffectDagAdmissionSnapshot:
        async with self._lock:
            return EffectDagAdmissionSnapshot(
                active_total=self._active_total,
                active_by_graph=dict(self._active_by_graph),
                active_by_tool=dict(self._active_by_tool),
                waiting_batches=self._waiting_batches,
                waiting_graphs=tuple(self._graph_ring),
            )

    async def _withdraw_waiter(
        self,
        waiter: _AdmissionWaiter,
    ) -> tuple[EffectDagAdmissionPermit, ...]:
        async with self._lock:
            if waiter.future.done():
                if waiter.future.cancelled():
                    return ()
                exception = waiter.future.exception()
                return () if exception is not None else waiter.future.result()
            queue = self._queues.get(waiter.graph_id)
            if queue is not None:
                try:
                    queue.remove(waiter)
                except ValueError:
                    pass
                else:
                    self._waiting_batches -= 1
                if not queue:
                    self._queues.pop(waiter.graph_id, None)
                    self._remove_graph_from_ring(waiter.graph_id)
            waiter.future.cancel()
            self._drain_locked()
            return ()

    async def _release(self, graph_id: str, tool_name: str) -> None:
        async with self._lock:
            if (
                self._active_total < 1
                or self._active_by_graph[graph_id] < 1
                or self._active_by_tool[tool_name] < 1
            ):
                raise RuntimeError("Effect DAG admission permit was not active")
            self._active_total -= 1
            self._active_by_graph[graph_id] -= 1
            self._active_by_tool[tool_name] -= 1
            if self._active_by_graph[graph_id] == 0:
                del self._active_by_graph[graph_id]
            if self._active_by_tool[tool_name] == 0:
                del self._active_by_tool[tool_name]
            self._drain_locked()

    def _drain_locked(self) -> None:
        granted_waiters: dict[int, _AdmissionWaiter] = {}
        while self._active_total < self._global_limit and self._graph_ring:
            progress = False
            graph_count = len(self._graph_ring)
            for _ in range(graph_count):
                graph_id = self._graph_ring.popleft()
                queue = self._queues.get(graph_id)
                if not queue:
                    self._ring_members.discard(graph_id)
                    continue
                self._graph_ring.append(graph_id)
                if self._active_by_graph[graph_id] >= self._per_graph_limit:
                    continue
                waiter = queue[0]
                request_index = self._first_grantable_request(waiter)
                if request_index is None:
                    continue
                request = waiter.remaining.pop(request_index)
                permit = EffectDagAdmissionPermit(
                    self,
                    graph_id=graph_id,
                    request=request,
                )
                waiter.granted.append(permit)
                granted_waiters[id(waiter)] = waiter
                self._active_total += 1
                self._active_by_graph[graph_id] += 1
                self._active_by_tool[request.tool_name] += 1
                progress = True
                break
            if not progress:
                break

        for waiter in granted_waiters.values():
            queue = self._queues.get(waiter.graph_id)
            if queue is None or not queue or queue[0] is not waiter:
                raise RuntimeError("Effect DAG admission queue lost its head waiter")
            queue.popleft()
            self._waiting_batches -= 1
            if not queue:
                self._queues.pop(waiter.graph_id, None)
                self._remove_graph_from_ring(waiter.graph_id)
            if not waiter.future.done():
                waiter.future.set_result(tuple(waiter.granted))

    def _first_grantable_request(self, waiter: _AdmissionWaiter) -> int | None:
        for index, request in enumerate(waiter.remaining):
            tool_limit = self._tool_limits.get(
                request.tool_name,
                self._default_tool_limit,
            )
            if self._active_by_tool[request.tool_name] < tool_limit:
                return index
        return None

    def _add_graph_to_ring(self, graph_id: str) -> None:
        if graph_id not in self._ring_members:
            self._graph_ring.append(graph_id)
            self._ring_members.add(graph_id)

    def _remove_graph_from_ring(self, graph_id: str) -> None:
        if graph_id not in self._ring_members:
            return
        self._graph_ring = deque(item for item in self._graph_ring if item != graph_id)
        self._ring_members.discard(graph_id)


__all__ = [
    "EffectDagAdmissionCancelledError",
    "EffectDagAdmissionController",
    "EffectDagAdmissionControllerPort",
    "EffectDagAdmissionPermit",
    "EffectDagAdmissionPermitPort",
    "EffectDagAdmissionRequest",
    "EffectDagAdmissionSnapshot",
]
