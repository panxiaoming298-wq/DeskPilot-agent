"""Provider administration use cases with validation before atomic persistence."""

import asyncio
import hashlib
import hmac
from collections.abc import Callable
from uuid import uuid4

from deskpilot.application.credential_resolver import CredentialResolver
from deskpilot.application.model_gateway import (
    ModelGateway,
    ModelProvider,
    UnknownModelProviderError,
)
from deskpilot.application.provider_health_service import ProviderHealthService
from deskpilot.application.provider_management_store import (
    PreparedProviderMutation,
    ProviderAlreadyExistsError,
    ProviderIdempotencyContext,
    ProviderManagementConflictError,
    ProviderManagementNotFoundError,
    ProviderManagementStore,
)
from deskpilot.core.canonical_json import canonical_json_bytes
from deskpilot.domain.provider_admin import (
    ProviderConfigAuditPage,
    ProviderManagementState,
    ProviderMutationAction,
    ProviderMutationResult,
)
from deskpilot.domain.provider_config import ProviderConfig
from deskpilot.domain.provider_management import (
    ProviderCatalogDefinition,
    ProviderCatalogDefinitionEntry,
)
from deskpilot.domain.provider_runtime import (
    ProviderConfigActorType,
    ProviderConfigAuditContext,
    ProviderConfigAuditSource,
    ProviderRuntimeConfigBundle,
)
from deskpilot.model_providers.factory import (
    create_model_providers,
    describe_model_provider_config,
)


class ProviderManagementService:
    def __init__(
        self,
        *,
        store: ProviderManagementStore,
        credential_resolver: CredentialResolver,
        gateway: ModelGateway,
        health_service: ProviderHealthService,
    ) -> None:
        self._store = store
        self._credential_resolver = credential_resolver
        self._gateway = gateway
        self._health_service = health_service
        self._mutation_lock = asyncio.Lock()

    async def initialize(
        self,
        configs: tuple[ProviderConfig, ...],
        *,
        default_provider_id: str,
    ) -> ProviderManagementState:
        definition = self._definition(configs, default_provider_id)
        state = await self._store.bootstrap(
            definition=definition,
            bundles=tuple(
                ProviderRuntimeConfigBundle.from_config(config) for config in configs
            ),
            audit=ProviderConfigAuditContext(
                source=ProviderConfigAuditSource.STARTUP_IMPORT,
                actor_type=ProviderConfigActorType.SYSTEM,
                correlation_id="provider-startup-import",
            ),
        )
        persisted_configs = self._configs(state)
        providers = self._build_validated_adapters(
            persisted_configs,
            state.default_provider_id,
        )
        self._gateway.reconfigure(
            providers,
            default_provider_id=state.default_provider_id,
        )
        return state

    async def create(
        self,
        config: ProviderConfig,
        *,
        expected_catalog_version: int,
        idempotency_key: str,
    ) -> ProviderMutationResult:
        return await self._mutate(
            action=ProviderMutationAction.CREATED,
            provider_id=config.provider_id,
            expected_catalog_version=expected_catalog_version,
            idempotency_key=idempotency_key,
            request_payload=config.model_dump(mode="json"),
            transform=lambda state: self._create_candidate(state, config),
        )

    async def update(
        self,
        provider_id: str,
        config: ProviderConfig,
        *,
        expected_catalog_version: int,
        idempotency_key: str,
    ) -> ProviderMutationResult:
        if config.provider_id != provider_id:
            raise ProviderManagementConflictError(
                "Path and body Provider IDs must match",
                provider_id=provider_id,
            )
        return await self._mutate(
            action=ProviderMutationAction.UPDATED,
            provider_id=provider_id,
            expected_catalog_version=expected_catalog_version,
            idempotency_key=idempotency_key,
            request_payload=config.model_dump(mode="json"),
            transform=lambda state: self._replace_candidate(
                state,
                provider_id,
                config,
            ),
        )

    async def enable(
        self,
        provider_id: str,
        *,
        expected_catalog_version: int,
        idempotency_key: str,
    ) -> ProviderMutationResult:
        return await self._toggle(
            provider_id,
            enabled=True,
            expected_catalog_version=expected_catalog_version,
            idempotency_key=idempotency_key,
        )

    async def disable(
        self,
        provider_id: str,
        *,
        expected_catalog_version: int,
        idempotency_key: str,
    ) -> ProviderMutationResult:
        return await self._toggle(
            provider_id,
            enabled=False,
            expected_catalog_version=expected_catalog_version,
            idempotency_key=idempotency_key,
        )

    async def make_default(
        self,
        provider_id: str,
        *,
        expected_catalog_version: int,
        idempotency_key: str,
    ) -> ProviderMutationResult:
        return await self._mutate(
            action=ProviderMutationAction.DEFAULT_CHANGED,
            provider_id=provider_id,
            expected_catalog_version=expected_catalog_version,
            idempotency_key=idempotency_key,
            request_payload={"provider_id": provider_id},
            transform=lambda state: self._default_candidate(state, provider_id),
        )

    async def delete(
        self,
        provider_id: str,
        *,
        expected_catalog_version: int,
        idempotency_key: str,
    ) -> ProviderMutationResult:
        return await self._mutate(
            action=ProviderMutationAction.DELETED,
            provider_id=provider_id,
            expected_catalog_version=expected_catalog_version,
            idempotency_key=idempotency_key,
            request_payload={"provider_id": provider_id},
            transform=lambda state: self._delete_candidate(state, provider_id),
        )

    async def audit_page(
        self,
        *,
        provider_id: str | None = None,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> ProviderConfigAuditPage:
        events = await self._store.list_audit_events(
            provider_id=provider_id,
            after_sequence=after_sequence,
            limit=limit,
        )
        return ProviderConfigAuditPage(
            events=events,
            next_sequence=events[-1].sequence if events else after_sequence,
        )

    async def _toggle(
        self,
        provider_id: str,
        *,
        enabled: bool,
        expected_catalog_version: int,
        idempotency_key: str,
    ) -> ProviderMutationResult:
        action = (
            ProviderMutationAction.ENABLED
            if enabled
            else ProviderMutationAction.DISABLED
        )
        return await self._mutate(
            action=action,
            provider_id=provider_id,
            expected_catalog_version=expected_catalog_version,
            idempotency_key=idempotency_key,
            request_payload={"provider_id": provider_id, "enabled": enabled},
            transform=lambda state: self._toggle_candidate(
                state,
                provider_id,
                enabled,
            ),
        )

    async def _mutate(
        self,
        *,
        action: ProviderMutationAction,
        provider_id: str,
        expected_catalog_version: int,
        idempotency_key: str,
        request_payload: dict[str, object],
        transform: Callable[
            [ProviderManagementState],
            tuple[tuple[ProviderConfig, ...], str],
        ],
    ) -> ProviderMutationResult:
        operation = f"model-providers:{action.value}:{provider_id}"
        idempotency = self._idempotency(
            idempotency_key,
            operation=operation,
            request_payload=request_payload,
        )
        async with self._mutation_lock:
            replay = await self._store.replay(idempotency)
            if replay is not None:
                return replay
            state = await self._store.get_state()
            candidate_configs, candidate_default = transform(state)
            definition = self._definition(candidate_configs, candidate_default)
            providers = self._build_validated_adapters(
                candidate_configs,
                candidate_default,
            )
            commit = await self._store.commit(
                PreparedProviderMutation(
                    action=action,
                    provider_id=provider_id,
                    definition=definition,
                    bundles=tuple(
                        ProviderRuntimeConfigBundle.from_config(config)
                        for config in candidate_configs
                    ),
                    expected_catalog_version=expected_catalog_version,
                    audit=ProviderConfigAuditContext(
                        source=ProviderConfigAuditSource.LOCAL_API,
                        actor_type=ProviderConfigActorType.LOCAL_USER,
                        correlation_id=f"pmc_{uuid4().hex}",
                    ),
                    idempotency=idempotency,
                )
            )
            if (
                not commit.replayed
                and commit.result.catalog_version != state.catalog_version
            ):
                self._gateway.reconfigure(
                    providers,
                    default_provider_id=candidate_default,
                )
                await self._health_service.invalidate()
            return commit.result

    def _build_validated_adapters(
        self,
        configs: tuple[ProviderConfig, ...],
        default_provider_id: str,
    ) -> tuple[ModelProvider, ...]:
        providers = create_model_providers(configs, self._credential_resolver)
        candidate_gateway = ModelGateway(
            default_provider_id=default_provider_id,
            policy=self._gateway.policy,
        )
        for provider in providers:
            candidate_gateway.register(provider)
        candidate_gateway.validate_configuration()
        return providers

    @staticmethod
    def _definition(
        configs: tuple[ProviderConfig, ...],
        default_provider_id: str,
    ) -> ProviderCatalogDefinition:
        if default_provider_id not in {
            config.provider_id for config in configs if config.enabled
        }:
            raise UnknownModelProviderError(
                "Default model Provider is missing or disabled",
                provider_id=default_provider_id,
            )
        return ProviderCatalogDefinition(
            default_provider_id=default_provider_id,
            providers=tuple(
                ProviderCatalogDefinitionEntry(
                    descriptor=describe_model_provider_config(config),
                    enabled=config.enabled,
                )
                for config in sorted(
                    configs,
                    key=lambda item: item.provider_id,
                )
            ),
        )

    @staticmethod
    def _configs(state: ProviderManagementState) -> tuple[ProviderConfig, ...]:
        return tuple(item.bundle.config for item in state.providers)

    @classmethod
    def _create_candidate(
        cls,
        state: ProviderManagementState,
        config: ProviderConfig,
    ) -> tuple[tuple[ProviderConfig, ...], str]:
        configs = cls._configs(state)
        if any(item.provider_id == config.provider_id for item in configs):
            raise ProviderAlreadyExistsError(config.provider_id)
        if len(configs) >= 32:
            raise ProviderManagementConflictError(
                "Provider catalog cannot contain more than 32 entries",
                provider_id=config.provider_id,
            )
        return (*configs, config), state.default_provider_id

    @classmethod
    def _replace_candidate(
        cls,
        state: ProviderManagementState,
        provider_id: str,
        config: ProviderConfig,
    ) -> tuple[tuple[ProviderConfig, ...], str]:
        configs = cls._configs(state)
        if not any(item.provider_id == provider_id for item in configs):
            raise ProviderManagementNotFoundError(provider_id)
        if provider_id == state.default_provider_id and not config.enabled:
            raise ProviderManagementConflictError(
                "Default Provider cannot be disabled",
                provider_id=provider_id,
            )
        return tuple(
            config if item.provider_id == provider_id else item for item in configs
        ), state.default_provider_id

    @classmethod
    def _toggle_candidate(
        cls,
        state: ProviderManagementState,
        provider_id: str,
        enabled: bool,
    ) -> tuple[tuple[ProviderConfig, ...], str]:
        configs = cls._configs(state)
        target = next(
            (item for item in configs if item.provider_id == provider_id),
            None,
        )
        if target is None:
            raise ProviderManagementNotFoundError(provider_id)
        if not enabled and provider_id == state.default_provider_id:
            raise ProviderManagementConflictError(
                "Default Provider cannot be disabled",
                provider_id=provider_id,
            )
        replacement = target.model_copy(update={"enabled": enabled})
        return tuple(
            replacement if item.provider_id == provider_id else item
            for item in configs
        ), state.default_provider_id

    @classmethod
    def _default_candidate(
        cls,
        state: ProviderManagementState,
        provider_id: str,
    ) -> tuple[tuple[ProviderConfig, ...], str]:
        configs = cls._configs(state)
        target = next(
            (item for item in configs if item.provider_id == provider_id),
            None,
        )
        if target is None:
            raise ProviderManagementNotFoundError(provider_id)
        if not target.enabled:
            raise ProviderManagementConflictError(
                "Disabled Provider cannot become default",
                provider_id=provider_id,
            )
        return configs, provider_id

    @classmethod
    def _delete_candidate(
        cls,
        state: ProviderManagementState,
        provider_id: str,
    ) -> tuple[tuple[ProviderConfig, ...], str]:
        configs = cls._configs(state)
        if not any(item.provider_id == provider_id for item in configs):
            raise ProviderManagementNotFoundError(provider_id)
        if provider_id == state.default_provider_id:
            raise ProviderManagementConflictError(
                "Default Provider cannot be deleted",
                provider_id=provider_id,
            )
        if len(configs) == 1:
            raise ProviderManagementConflictError(
                "The last Provider cannot be deleted",
                provider_id=provider_id,
            )
        return tuple(
            item for item in configs if item.provider_id != provider_id
        ), state.default_provider_id

    @staticmethod
    def _idempotency(
        key: str,
        *,
        operation: str,
        request_payload: dict[str, object],
    ) -> ProviderIdempotencyContext:
        key_bytes = key.encode("ascii")
        request_bytes = canonical_json_bytes(request_payload)
        return ProviderIdempotencyContext(
            key_digest=hashlib.sha256(key_bytes).hexdigest(),
            operation=operation,
            request_fingerprint=hmac.new(
                key_bytes,
                request_bytes,
                hashlib.sha256,
            ).hexdigest(),
        )
