"""Encode validated Provider runtime bundles into protected payloads."""

from pydantic import TypeAdapter, ValidationError

from deskpilot.application.provider_runtime_store import (
    ProtectedRuntimeConfigPayload,
    ProviderRuntimeConfigInvalidError,
    RuntimeConfigProtector,
)
from deskpilot.core.canonical_json import canonical_json_bytes
from deskpilot.domain.provider_runtime import ProviderRuntimeConfigBundle

_BUNDLE_ADAPTER = TypeAdapter(ProviderRuntimeConfigBundle)
_MAX_PLAINTEXT_BYTES = 256 * 1024


class ProviderRuntimeConfigCodec:
    def __init__(self, protector: RuntimeConfigProtector) -> None:
        self._protector = protector

    def encode(
        self,
        bundle: ProviderRuntimeConfigBundle,
    ) -> ProtectedRuntimeConfigPayload:
        plaintext = bytearray(canonical_json_bytes(bundle))
        try:
            if not plaintext or len(plaintext) > _MAX_PLAINTEXT_BYTES:
                raise ProviderRuntimeConfigInvalidError(
                    "Provider runtime configuration has an invalid size"
                )
            return ProtectedRuntimeConfigPayload(
                scheme=self._protector.scheme,
                payload=self._protector.protect(
                    plaintext,
                    context=self._context(bundle.provider_id),
                ),
            )
        finally:
            self._zero(plaintext)

    def decode(
        self,
        *,
        provider_id: str,
        scheme: str,
        payload: bytes,
    ) -> ProviderRuntimeConfigBundle:
        if scheme != self._protector.scheme:
            raise ProviderRuntimeConfigInvalidError(
                "Provider runtime configuration protection scheme is unsupported"
            )
        plaintext = self._protector.unprotect(
            payload,
            context=self._context(provider_id),
        )
        try:
            if not plaintext or len(plaintext) > _MAX_PLAINTEXT_BYTES:
                raise ProviderRuntimeConfigInvalidError(
                    "Provider runtime configuration has an invalid size"
                )
            try:
                bundle = _BUNDLE_ADAPTER.validate_json(plaintext)
            except ValidationError as error:
                raise ProviderRuntimeConfigInvalidError(
                    "Provider runtime configuration payload is invalid"
                ) from error
            if bundle.provider_id != provider_id:
                raise ProviderRuntimeConfigInvalidError(
                    "Provider runtime configuration record identity does not match"
                )
            return bundle
        finally:
            self._zero(plaintext)

    @staticmethod
    def _context(provider_id: str) -> str:
        return f"DeskPilot/ProviderRuntime/{provider_id}/v1"

    @staticmethod
    def _zero(buffer: bytearray) -> None:
        buffer[:] = b"\x00" * len(buffer)
