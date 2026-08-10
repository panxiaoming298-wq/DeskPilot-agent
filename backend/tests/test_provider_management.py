import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from deskpilot.application.model_gateway import ModelGateway
from deskpilot.application.provider_health_service import ProviderHealthService
from deskpilot.core.config import Settings
from deskpilot.domain.model_contracts import ProviderHealth, ProviderHealthStatus
from deskpilot.domain.provider_config import (
    CredentialReference,
    FakeProviderConfig,
    OpenAICompatibleChatProviderConfig,
)
from deskpilot.infrastructure.environment_credentials import (
    EnvironmentCredentialResolver,
)
from deskpilot.main import create_app
from deskpilot.model_providers.fake import FakeModelProvider

TEST_ORIGIN = "http://127.0.0.1:5173"
TEST_TOKEN = "provider-management-session-token-123456"
UPSTREAM_SECRET = "upstream-secret-must-not-appear"


class InstrumentedProvider(FakeModelProvider):
    def __init__(
        self,
        *,
        provider_id: str,
        delay_seconds: float = 0,
        tracker: dict[str, int] | None = None,
    ) -> None:
        super().__init__(provider_id=provider_id)
        self.health_calls = 0
        self._health_delay_seconds = delay_seconds
        self._tracker = tracker

    async def health(self) -> ProviderHealth:
        self.health_calls += 1
        if self._tracker is not None:
            self._tracker["active"] += 1
            self._tracker["max_active"] = max(
                self._tracker["max_active"], self._tracker["active"]
            )
        try:
            if self._health_delay_seconds:
                await asyncio.sleep(self._health_delay_seconds)
            return ProviderHealth(
                provider_id=self.descriptor.provider_id,
                status=ProviderHealthStatus.READY,
                latency_ms=1,
                detail=UPSTREAM_SECRET,
            )
        finally:
            if self._tracker is not None:
                self._tracker["active"] -= 1


def _authenticated_client(
    tmp_path: Path,
    *,
    provider: InstrumentedProvider,
) -> Iterator[TestClient]:
    settings = Settings(
        _env_file=None,
        database_url=(
            f"sqlite+aiosqlite:///{(tmp_path / f'{provider.descriptor.provider_id}.db').as_posix()}"
        ),
        session_token=SecretStr(TEST_TOKEN),
        cors_origins=[TEST_ORIGIN],
        model_default_provider_id=provider.descriptor.provider_id,
    )
    headers = {
        "Authorization": f"Bearer {TEST_TOKEN}",
        "Origin": TEST_ORIGIN,
        "X-DeskPilot-Client": "deskpilot-web-v1",
    }
    with TestClient(
        create_app(settings, model_provider=provider), headers=headers
    ) as client:
        yield client


def test_provider_list_is_authenticated_and_secret_free(
    client: TestClient,
) -> None:
    response = client.get("/api/v1/model-providers")

    assert response.status_code == 200
    body = response.json()
    assert body["catalog_version"] == 1
    assert body["imported_at"].endswith("Z")
    assert body["default_provider_id"] == "fake-local"
    assert body["providers"] == [
        {
            "descriptor": {
                "provider_id": "fake-local",
                "display_name": "DeskPilot Fake Model",
                "model": "deskpilot-fake-v1",
                "protocol": "fake",
                "location": "local",
                "capabilities": {
                    "streaming": True,
                    "structured_output": True,
                    "strict_json_schema": True,
                    "tool_calling": "none",
                    "parallel_tool_calls": False,
                    "vision": False,
                    "embeddings": False,
                    "max_context_tokens": 32768,
                },
            },
            "enabled": True,
            "is_default": True,
            "cached_health": None,
        }
    ]
    serialized = response.text.casefold()
    assert "base_url" not in serialized
    assert "credential" not in serialized
    assert "api_key" not in serialized
    assert "detail" not in serialized


def test_provider_list_requires_session_authentication(raw_client: TestClient) -> None:
    response = raw_client.get("/api/v1/model-providers")

    assert response.status_code == 401
    assert response.json()["code"] == "SESSION_TOKEN_INVALID"


def test_provider_health_is_on_demand_and_then_cached(client: TestClient) -> None:
    first = client.get("/api/v1/model-providers/fake-local/health")
    second = client.get("/api/v1/model-providers/fake-local/health")
    catalog = client.get("/api/v1/model-providers")

    assert first.status_code == 200
    assert first.json()["status"] == "ready"
    assert first.json()["cache_status"] == "fresh"
    assert second.json()["cache_status"] == "cached"
    assert second.json()["checked_at"] == first.json()["checked_at"]
    cached = catalog.json()["providers"][0]["cached_health"]
    assert cached["cache_status"] == "cached"
    assert cached["checked_at"] == first.json()["checked_at"]
    assert "detail" not in first.json()


def test_unknown_provider_health_uses_stable_problem_code(client: TestClient) -> None:
    response = client.get("/api/v1/model-providers/missing-provider/health")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "MODEL_PROVIDER_NOT_FOUND"


def test_disabled_provider_is_listed_but_never_probed(tmp_path: Path) -> None:
    cloud = OpenAICompatibleChatProviderConfig(
        enabled=False,
        provider_id="disabled-cloud",
        display_name="Disabled Cloud",
        model="cloud-model",
        base_url="https://models.example.test/v1",
        location="cloud",
        credential_ref=CredentialReference(
            identifier="DESKPILOT_CREDENTIAL_DISABLED_CLOUD"
        ),
    )
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'disabled.db').as_posix()}",
        session_token=SecretStr(TEST_TOKEN),
        cors_origins=[TEST_ORIGIN],
        model_providers=(FakeProviderConfig(), cloud),
    )
    headers = {
        "Authorization": f"Bearer {TEST_TOKEN}",
        "Origin": TEST_ORIGIN,
        "X-DeskPilot-Client": "deskpilot-web-v1",
    }
    app = create_app(
        settings,
        credential_resolver=EnvironmentCredentialResolver({}),
    )

    with TestClient(app, headers=headers) as test_client:
        catalog = test_client.get("/api/v1/model-providers")
        health = test_client.get(
            "/api/v1/model-providers/disabled-cloud/health"
        )

    providers = {
        item["descriptor"]["provider_id"]: item for item in catalog.json()["providers"]
    }
    assert providers["disabled-cloud"]["enabled"] is False
    assert providers["disabled-cloud"]["is_default"] is False
    assert providers["disabled-cloud"]["cached_health"] is None
    assert "base_url" not in catalog.text
    assert "credential" not in catalog.text.casefold()
    assert health.status_code == 409
    assert health.json()["code"] == "MODEL_PROVIDER_DISABLED"


def test_listing_does_not_call_provider_health(tmp_path: Path) -> None:
    provider = InstrumentedProvider(provider_id="counted-provider")

    for test_client in _authenticated_client(tmp_path, provider=provider):
        listed = test_client.get("/api/v1/model-providers")
        assert listed.status_code == 200
        assert provider.health_calls == 0

        checked = test_client.get(
            "/api/v1/model-providers/counted-provider/health"
        )
        assert checked.status_code == 200
        assert provider.health_calls == 1


@pytest.mark.asyncio
async def test_same_provider_concurrent_health_uses_single_flight() -> None:
    provider = InstrumentedProvider(
        provider_id="single-flight",
        delay_seconds=0.03,
    )
    gateway = ModelGateway(default_provider_id=provider.descriptor.provider_id)
    gateway.register(provider)
    service = ProviderHealthService(
        gateway,
        cache_ttl_seconds=1,
        max_concurrency=4,
        probe_timeout_seconds=1,
    )

    try:
        results = await asyncio.gather(
            *(service.get(provider.descriptor.provider_id) for _ in range(8))
        )
        cached = await service.get(provider.descriptor.provider_id)
    finally:
        await service.shutdown()

    assert provider.health_calls == 1
    statuses = [result.cache_status.value for result in results]
    assert statuses.count("fresh") == 1
    assert statuses.count("coalesced") == 7
    assert cached.cache_status.value == "cached"


@pytest.mark.asyncio
async def test_cancelling_one_waiter_does_not_cancel_shared_probe() -> None:
    provider = InstrumentedProvider(
        provider_id="shielded-probe",
        delay_seconds=0.03,
    )
    gateway = ModelGateway(default_provider_id=provider.descriptor.provider_id)
    gateway.register(provider)
    service = ProviderHealthService(
        gateway,
        cache_ttl_seconds=1,
        max_concurrency=1,
        probe_timeout_seconds=1,
    )

    try:
        cancelled_waiter = asyncio.create_task(
            service.get(provider.descriptor.provider_id)
        )
        await asyncio.sleep(0)
        surviving_waiter = asyncio.create_task(
            service.get(provider.descriptor.provider_id)
        )
        cancelled_waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled_waiter
        result = await surviving_waiter
    finally:
        await service.shutdown()

    assert result.cache_status.value == "coalesced"
    assert result.status is ProviderHealthStatus.READY
    assert provider.health_calls == 1


@pytest.mark.asyncio
async def test_expired_health_cache_starts_a_new_probe() -> None:
    provider = InstrumentedProvider(provider_id="short-cache")
    gateway = ModelGateway(default_provider_id=provider.descriptor.provider_id)
    gateway.register(provider)
    service = ProviderHealthService(
        gateway,
        cache_ttl_seconds=0.01,
        max_concurrency=1,
        probe_timeout_seconds=1,
    )

    try:
        first = await service.get(provider.descriptor.provider_id)
        await asyncio.sleep(0.02)
        second = await service.get(provider.descriptor.provider_id)
    finally:
        await service.shutdown()

    assert first.cache_status.value == "fresh"
    assert second.cache_status.value == "fresh"
    assert provider.health_calls == 2


@pytest.mark.asyncio
async def test_health_probes_have_a_global_concurrency_limit() -> None:
    tracker = {"active": 0, "max_active": 0}
    first = InstrumentedProvider(
        provider_id="bounded-one",
        delay_seconds=0.02,
        tracker=tracker,
    )
    second = InstrumentedProvider(
        provider_id="bounded-two",
        delay_seconds=0.02,
        tracker=tracker,
    )
    gateway = ModelGateway(default_provider_id=first.descriptor.provider_id)
    gateway.register(first)
    gateway.register(second)
    service = ProviderHealthService(
        gateway,
        cache_ttl_seconds=1,
        max_concurrency=1,
        probe_timeout_seconds=1,
    )

    try:
        await asyncio.gather(
            service.get(first.descriptor.provider_id),
            service.get(second.descriptor.provider_id),
        )
    finally:
        await service.shutdown()

    assert tracker["max_active"] == 1
    assert first.health_calls == 1
    assert second.health_calls == 1


@pytest.mark.asyncio
async def test_timeout_is_unavailable_without_exposing_upstream_detail() -> None:
    provider = InstrumentedProvider(
        provider_id="timed-provider",
        delay_seconds=0.05,
    )
    gateway = ModelGateway(default_provider_id=provider.descriptor.provider_id)
    gateway.register(provider)
    service = ProviderHealthService(
        gateway,
        cache_ttl_seconds=1,
        max_concurrency=1,
        probe_timeout_seconds=0.005,
    )

    try:
        result = await service.get(provider.descriptor.provider_id)
    finally:
        await service.shutdown()

    serialized = result.model_dump_json()
    assert result.status is ProviderHealthStatus.UNAVAILABLE
    assert "detail" not in serialized
    assert UPSTREAM_SECRET not in serialized
