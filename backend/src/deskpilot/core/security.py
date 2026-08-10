"""Local session authentication and browser-origin policy."""

import secrets
from dataclasses import dataclass

from pydantic import SecretStr

WEBSOCKET_PROTOCOL = "deskpilot.v1"
WEBSOCKET_AUTH_PREFIX = "deskpilot.auth."
WEB_CLIENT_HEADER = "deskpilot-web-v1"


@dataclass(frozen=True, slots=True)
class LocalSessionSecurity:
    """Owns the process-scoped token and exact browser origin allowlist."""

    token: str
    allowed_origins: frozenset[str]

    @classmethod
    def create(
        cls,
        configured_token: SecretStr | None,
        allowed_origins: list[str],
    ) -> "LocalSessionSecurity":
        token = (
            configured_token.get_secret_value()
            if configured_token
            else secrets.token_urlsafe(32)
        )
        if len(token) < 32:
            raise ValueError("DeskPilot session token must contain at least 32 characters")
        return cls(token=token, allowed_origins=frozenset(allowed_origins))

    def is_allowed_origin(self, origin: str | None) -> bool:
        return origin is not None and origin in self.allowed_origins

    def is_trusted_browser_request(
        self,
        *,
        origin: str | None,
        fetch_site: str | None,
        client_header: str | None,
    ) -> bool:
        if origin is not None:
            return self.is_allowed_origin(origin)
        return fetch_site == "same-origin" and client_header == WEB_CLIENT_HEADER

    def authenticate_bearer(self, authorization: str | None) -> bool:
        if authorization is None:
            return False
        scheme, separator, credential = authorization.partition(" ")
        return (
            separator == " "
            and scheme.casefold() == "bearer"
            and bool(credential)
            and secrets.compare_digest(credential, self.token)
        )

    def authenticate_websocket_protocols(self, protocols: list[str]) -> bool:
        if WEBSOCKET_PROTOCOL not in protocols:
            return False
        credentials = [
            protocol.removeprefix(WEBSOCKET_AUTH_PREFIX)
            for protocol in protocols
            if protocol.startswith(WEBSOCKET_AUTH_PREFIX)
        ]
        return len(credentials) == 1 and secrets.compare_digest(credentials[0], self.token)
