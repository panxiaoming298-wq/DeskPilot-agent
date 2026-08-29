"""Trusted composition factory for validated Provider configuration."""

from deskpilot.application.credential_resolver import CredentialResolver
from deskpilot.application.model_gateway import ModelProvider
from deskpilot.core.config import Settings
from deskpilot.domain.model_contracts import ModelProviderDescriptor
from deskpilot.domain.provider_config import (
    FakeProviderConfig,
    OpenAICompatibleChatProviderConfig,
    OpenAICompatibleResponsesProviderConfig,
    ProviderConfig,
)
from deskpilot.model_providers.fake import FakeModelProvider
from deskpilot.model_providers.openai_compatible_chat import (
    OpenAICompatibleChatProvider,
)
from deskpilot.model_providers.openai_compatible_responses import (
    OpenAICompatibleResponsesProvider,
)


def effective_model_provider_configs(
    settings: Settings,
) -> tuple[ProviderConfig, ...]:
    if settings.model_providers:
        return settings.model_providers
    return (
        FakeProviderConfig(
            provider_id=settings.fake_model_provider_id,
            model=settings.fake_model_name,
            delay_seconds=settings.fake_model_delay_seconds,
        ),
    )


def describe_model_provider_config(
    config: ProviderConfig,
) -> ModelProviderDescriptor:
    """Build the public descriptor without resolving any credential."""
    if isinstance(config, FakeProviderConfig):
        return FakeModelProvider(
            provider_id=config.provider_id,
            display_name=config.display_name,
            model=config.model,
            delay_seconds=config.delay_seconds,
        ).descriptor
    if isinstance(config, OpenAICompatibleChatProviderConfig):
        return OpenAICompatibleChatProvider(
            provider_id=config.provider_id,
            display_name=config.display_name,
            model=config.model,
            base_url=config.base_url,
            location=config.location,
            supports_streaming=config.supports_streaming,
            supports_structured_output=config.supports_structured_output,
            supports_strict_json_schema=config.supports_strict_json_schema,
            max_context_tokens=config.max_context_tokens,
            max_tokens_field=config.max_tokens_field,
            max_response_bytes=config.max_response_bytes,
            health_timeout_seconds=config.health_timeout_seconds,
        ).descriptor
    if isinstance(config, OpenAICompatibleResponsesProviderConfig):
        return OpenAICompatibleResponsesProvider(
            provider_id=config.provider_id,
            display_name=config.display_name,
            model=config.model,
            base_url=config.base_url,
            location=config.location,
            supports_streaming=config.supports_streaming,
            supports_structured_output=config.supports_structured_output,
            supports_strict_json_schema=config.supports_strict_json_schema,
            max_context_tokens=config.max_context_tokens,
            max_response_bytes=config.max_response_bytes,
            health_timeout_seconds=config.health_timeout_seconds,
        ).descriptor
    raise TypeError(f"Unsupported Provider config: {type(config).__name__}")


def create_configured_model_providers(
    settings: Settings,
    credential_resolver: CredentialResolver,
) -> tuple[ModelProvider, ...]:
    """Build the allowlisted adapter set; no dynamic imports are permitted."""
    return create_model_providers(
        effective_model_provider_configs(settings),
        credential_resolver,
    )


def create_model_providers(
    configs: tuple[ProviderConfig, ...],
    credential_resolver: CredentialResolver,
) -> tuple[ModelProvider, ...]:
    """Build adapters from persisted, validated Provider configuration."""
    providers: list[ModelProvider] = []
    for config in configs:
        if not config.enabled:
            continue
        if isinstance(config, FakeProviderConfig):
            providers.append(
                FakeModelProvider(
                    provider_id=config.provider_id,
                    display_name=config.display_name,
                    model=config.model,
                    delay_seconds=config.delay_seconds,
                )
            )
            continue
        if isinstance(config, OpenAICompatibleChatProviderConfig):
            credential = (
                credential_resolver.resolve(config.credential_ref)
                if config.credential_ref is not None
                else None
            )
            providers.append(
                OpenAICompatibleChatProvider(
                    provider_id=config.provider_id,
                    display_name=config.display_name,
                    model=config.model,
                    base_url=config.base_url,
                    api_key=credential,
                    location=config.location,
                    supports_streaming=config.supports_streaming,
                    supports_structured_output=config.supports_structured_output,
                    supports_strict_json_schema=config.supports_strict_json_schema,
                    max_context_tokens=config.max_context_tokens,
                    max_tokens_field=config.max_tokens_field,
                    max_response_bytes=config.max_response_bytes,
                    health_timeout_seconds=config.health_timeout_seconds,
                )
            )
            continue
        if isinstance(config, OpenAICompatibleResponsesProviderConfig):
            credential = (
                credential_resolver.resolve(config.credential_ref)
                if config.credential_ref is not None
                else None
            )
            providers.append(
                OpenAICompatibleResponsesProvider(
                    provider_id=config.provider_id,
                    display_name=config.display_name,
                    model=config.model,
                    base_url=config.base_url,
                    api_key=credential,
                    location=config.location,
                    supports_streaming=config.supports_streaming,
                    supports_structured_output=config.supports_structured_output,
                    supports_strict_json_schema=config.supports_strict_json_schema,
                    max_context_tokens=config.max_context_tokens,
                    max_response_bytes=config.max_response_bytes,
                    health_timeout_seconds=config.health_timeout_seconds,
                )
            )
            continue
        raise TypeError(f"Unsupported Provider config: {type(config).__name__}")
    return tuple(providers)
