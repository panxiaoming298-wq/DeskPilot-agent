"""Bounded, cached, single-flight Provider health probing."""

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from deskpilot.application.model_gateway import ModelGateway
from deskpilot.domain.model_contracts import ProviderHealth
from deskpilot.domain.provider_management import (
    ProviderHealthCacheStatus,
    ProviderHealthSnapshot,
)


@dataclass(frozen=True, slots=True)
class _CachedHealth:
    health: ProviderHealth
    expires_monotonic: float
    expires_at: datetime


class ProviderHealthService:
    """Probe only on demand while preventing request and dependency storms."""

    def __init__(
        self,
        gateway: ModelGateway,
        *,
        cache_ttl_seconds: float,
        max_concurrency: int,
        probe_timeout_seconds: float,
    ) -> None:
        if cache_ttl_seconds <= 0:
            raise ValueError("Provider health cache TTL must be positive")
        if max_concurrency < 1:
            raise ValueError("Provider health concurrency must be at least one")
        if probe_timeout_seconds <= 0:
            raise ValueError("Provider health timeout must be positive")
        self._gateway = gateway
        self._cache_ttl_seconds = cache_ttl_seconds
        self._probe_timeout_seconds = probe_timeout_seconds
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._lock = asyncio.Lock()
        self._cache: dict[str, _CachedHealth] = {}
        self._inflight: dict[str, asyncio.Task[_CachedHealth]] = {}
        self._closed = False

    async def get(self, provider_id: str) -> ProviderHealthSnapshot:
        """Return cached health or share exactly one probe for this Provider."""
        self._gateway.descriptor(provider_id)
        cache_status = ProviderHealthCacheStatus.CACHED
        async with self._lock:
            self._ensure_open()
            cached = self._valid_cached(provider_id)
            if cached is not None:
                return self._public_snapshot(cached, cache_status)

            task = self._inflight.get(provider_id)
            if task is None:
                task = asyncio.create_task(
                    self._probe_and_cache(provider_id),
                    name=f"provider-health:{provider_id}",
                )
                self._inflight[provider_id] = task
                cache_status = ProviderHealthCacheStatus.FRESH
            else:
                cache_status = ProviderHealthCacheStatus.COALESCED

        cached = await asyncio.shield(task)
        return self._public_snapshot(cached, cache_status)

    async def cached_snapshots(self) -> dict[str, ProviderHealthSnapshot]:
        """Read valid cache entries without starting network probes."""
        async with self._lock:
            self._ensure_open()
            snapshots: dict[str, ProviderHealthSnapshot] = {}
            for provider_id in tuple(self._cache):
                cached = self._valid_cached(provider_id)
                if cached is not None:
                    snapshots[provider_id] = self._public_snapshot(
                        cached,
                        ProviderHealthCacheStatus.CACHED,
                    )
            return snapshots

    async def invalidate(self, provider_ids: set[str] | None = None) -> None:
        """Drop stale cache and cancel probes that target replaced adapters."""
        async with self._lock:
            self._ensure_open()
            targets = set(self._cache) | set(self._inflight)
            if provider_ids is not None:
                targets &= provider_ids
            tasks = tuple(
                task
                for provider_id, task in self._inflight.items()
                if provider_id in targets
            )
            for provider_id in targets:
                self._cache.pop(provider_id, None)
                self._inflight.pop(provider_id, None)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def shutdown(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            tasks = tuple(self._inflight.values())
            self._inflight.clear()
            self._cache.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _probe_and_cache(self, provider_id: str) -> _CachedHealth:
        try:
            async with self._semaphore:
                health = await self._gateway.check_health(
                    provider_id,
                    timeout_seconds=self._probe_timeout_seconds,
                )
            expires_at = datetime.now(UTC) + timedelta(
                seconds=self._cache_ttl_seconds
            )
            cached = _CachedHealth(
                health=health,
                expires_monotonic=time.monotonic() + self._cache_ttl_seconds,
                expires_at=expires_at,
            )
            async with self._lock:
                if not self._closed:
                    self._cache[provider_id] = cached
            return cached
        finally:
            async with self._lock:
                current = self._inflight.get(provider_id)
                if current is asyncio.current_task():
                    self._inflight.pop(provider_id, None)

    def _valid_cached(self, provider_id: str) -> _CachedHealth | None:
        cached = self._cache.get(provider_id)
        if cached is None:
            return None
        if cached.expires_monotonic <= time.monotonic():
            self._cache.pop(provider_id, None)
            return None
        return cached

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("Provider health service is closed")

    @staticmethod
    def _public_snapshot(
        cached: _CachedHealth,
        cache_status: ProviderHealthCacheStatus,
    ) -> ProviderHealthSnapshot:
        return ProviderHealthSnapshot(
            provider_id=cached.health.provider_id,
            status=cached.health.status,
            checked_at=cached.health.checked_at,
            latency_ms=cached.health.latency_ms,
            cache_status=cache_status,
            expires_at=cached.expires_at,
        )
