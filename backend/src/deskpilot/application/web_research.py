"""Search-provider boundary and DNS-pinned, no-script page reader."""

import asyncio
import http.client
import ipaddress
import json
import socket
import ssl
from collections.abc import Callable
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import SplitResult, urlencode, urljoin, urlsplit, urlunsplit

import httpx

from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.research import (
    PageSnapshot,
    SearchHit,
    SearchProviderResult,
    SearchRequest,
)


class SearchProvider(Protocol):
    provider_id: str

    async def search(self, request: SearchRequest) -> SearchProviderResult: ...


class ResearchIngressError(RuntimeError):
    code = "RESEARCH_INGRESS_REJECTED"


class SearchProviderError(ResearchIngressError):
    code = "SEARCH_PROVIDER_FAILED"


class PageReadRejectedError(ResearchIngressError):
    code = "PAGE_READ_REJECTED"


class SearxngSearchProvider:
    """Small SearXNG JSON adapter; model routing never passes through this port."""

    provider_id = "searxng"

    def __init__(self, base_url: str, *, timeout_seconds: float = 10.0) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("SearXNG base URL must be HTTP(S)")
        self._endpoint = base_url.rstrip("/") + "/search"
        self._timeout = timeout_seconds

    async def search(self, request: SearchRequest) -> SearchProviderResult:
        params = urlencode({"q": request.query, "format": "json"})
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                async with client.stream("GET", f"{self._endpoint}?{params}") as response:
                    response.raise_for_status()
                    if "application/json" not in response.headers.get("content-type", ""):
                        raise SearchProviderError("Search provider returned a non-JSON body")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 1_000_000:
                            raise SearchProviderError("Search provider response is too large")
        except SearchProviderError:
            raise
        except Exception as error:
            raise SearchProviderError("Search provider request failed") from error
        try:
            payload = json.loads(body)
            raw_results = payload["results"]
            if not isinstance(raw_results, list):
                raise TypeError
        except (KeyError, TypeError, ValueError) as error:
            raise SearchProviderError("Search provider response is invalid") from error
        hits: list[SearchHit] = []
        for raw in raw_results:
            if len(hits) >= request.max_results or not isinstance(raw, dict):
                break
            url = str(raw.get("url", ""))[:4_096]
            hostname = (urlsplit(url).hostname or "").lower()
            if request.allowed_domains and not any(
                hostname == domain.lower() or hostname.endswith("." + domain.lower())
                for domain in request.allowed_domains
            ):
                continue
            rank = len(hits) + 1
            material = {
                "rank": rank,
                "title": str(raw.get("title") or hostname or "Untitled")[:500],
                "url": url,
                "snippet": str(raw.get("content") or "")[:2_000],
                "origin": "external_untrusted",
            }
            hit_id = f"sht_{sha256_digest({'query': request.query, **material})}"
            hits.append(
                SearchHit.model_validate(
                    {
                        "hit_id": hit_id,
                        **material,
                        "hit_digest": sha256_digest({"hit_id": hit_id, **material}),
                    }
                )
            )
        return SearchProviderResult(provider_id=self.provider_id, hits=tuple(hits))


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, address: str, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._address = address

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._address, self.port), self.timeout
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, address: str, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._address = address
        self._ssl_context = ssl.create_default_context()

    def connect(self) -> None:
        raw_socket = socket.create_connection(
            (self._address, self.port), self.timeout
        )
        self.sock = self._ssl_context.wrap_socket(raw_socket, server_hostname=self.host)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self._suppressed = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in {"script", "style", "noscript", "template"}:
            self._suppressed += 1
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "template"} and self._suppressed:
            self._suppressed -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        text = " ".join(data.split())
        if not text or self._suppressed:
            return
        self.parts.append(text)
        if self._in_title:
            self.title_parts.append(text)


Resolver = Callable[[str, int], list[str]]


class SafePageReader:
    """Fetch public HTTP(S) pages while pinning each validated DNS result."""

    def __init__(
        self,
        *,
        resolver: Resolver | None = None,
        timeout_seconds: float = 10.0,
        max_bytes: int = 1_000_000,
        max_redirects: int = 3,
    ) -> None:
        self._resolver = resolver or self._resolve
        self._timeout = timeout_seconds
        self._max_bytes = max_bytes
        self._max_redirects = max_redirects

    async def read(
        self,
        *,
        task_id: str,
        research_session_id: str,
        hit: SearchHit,
    ) -> PageSnapshot:
        return await asyncio.to_thread(
            self._read_sync,
            task_id,
            research_session_id,
            hit,
        )

    def _read_sync(
        self, task_id: str, research_session_id: str, hit: SearchHit
    ) -> PageSnapshot:
        requested_url = hit.url
        current_url = requested_url
        for redirect_count in range(self._max_redirects + 1):
            parsed, address, port = self._validated_target(current_url)
            connection: http.client.HTTPConnection
            if parsed.scheme == "https":
                connection = _PinnedHTTPSConnection(
                    parsed.hostname or "", port, address, self._timeout
                )
            else:
                connection = _PinnedHTTPConnection(
                    parsed.hostname or "", port, address, self._timeout
                )
            target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
            host = parsed.hostname or ""
            display_host = f"[{host}]" if ":" in host else host
            default_port = 443 if parsed.scheme == "https" else 80
            host_header = (
                display_host if port == default_port else f"{display_host}:{port}"
            )
            try:
                connection.request(
                    "GET",
                    target,
                    headers={
                        "Host": host_header,
                        "Accept": "text/html,text/plain;q=0.9",
                        "Accept-Encoding": "identity",
                        "User-Agent": "DeskPilot-SafePageReader/1",
                        "Connection": "close",
                    },
                )
                response = connection.getresponse()
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.getheader("Location")
                    response.read(0)
                    if not location or redirect_count >= self._max_redirects:
                        raise PageReadRejectedError("Redirect policy was exceeded")
                    current_url = urljoin(current_url, location)
                    continue
                if not 200 <= response.status <= 299:
                    raise PageReadRejectedError("Page returned a non-success status")
                media_type = response.getheader("Content-Type", "").split(";", 1)[0].lower()
                if media_type not in {"text/html", "text/plain"}:
                    raise PageReadRejectedError("Page media type is not allowed")
                if response.getheader("Content-Encoding", "identity").lower() != "identity":
                    raise PageReadRejectedError("Encoded page bodies are not allowed")
                declared = response.getheader("Content-Length")
                if declared is not None and int(declared) > self._max_bytes:
                    raise PageReadRejectedError("Page body is too large")
                body = response.read(self._max_bytes + 1)
                if len(body) > self._max_bytes:
                    raise PageReadRejectedError("Page body is too large")
            except PageReadRejectedError:
                raise
            except Exception as error:
                raise PageReadRejectedError("Page request failed") from error
            finally:
                connection.close()
            charset = response.headers.get_content_charset() or "utf-8"
            text = body.decode(charset, errors="replace")
            title: str | None = None
            if media_type == "text/html":
                extractor = _TextExtractor()
                extractor.feed(text)
                text = "\n".join(extractor.parts)
                title = " ".join(extractor.title_parts)[:500] or None
            text = text.strip()[:200_000]
            if not text:
                raise PageReadRejectedError("Page contains no readable text")
            fetched_at = datetime.now(UTC)
            content_digest = sha256_digest({"text": text})
            identity = {
                "task_id": task_id,
                "research_session_id": research_session_id,
                "search_hit_id": hit.hit_id,
                "final_url": current_url,
                "content_digest": content_digest,
                "fetched_at": fetched_at,
            }
            page_snapshot_id = f"snp_{sha256_digest(identity)}"
            material = {
                "schema_version": "deskpilot.page-snapshot.v1",
                "page_snapshot_id": page_snapshot_id,
                "task_id": task_id,
                "research_session_id": research_session_id,
                "search_hit_id": hit.hit_id,
                "requested_url": requested_url,
                "final_url": current_url,
                "status_code": response.status,
                "media_type": media_type,
                "title": title,
                "extracted_text": text,
                "content_digest": content_digest,
                "extractor_version": "deskpilot.html-text.v1",
                "origin": "external_untrusted",
                "fetched_at": fetched_at,
            }
            return PageSnapshot.model_validate(
                {**material, "snapshot_digest": sha256_digest(material)}
            )
        raise PageReadRejectedError("Redirect policy was exceeded")

    def _validated_target(self, url: str) -> tuple[SplitResult, str, int]:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise PageReadRejectedError("Only credential-free HTTP(S) URLs are allowed")
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError as error:
            raise PageReadRejectedError("Page URL port is invalid") from error
        addresses = self._resolver(parsed.hostname, port)
        if not addresses:
            raise PageReadRejectedError("Page hostname did not resolve")
        for address in addresses:
            try:
                ip = ipaddress.ip_address(address)
            except ValueError as error:
                raise PageReadRejectedError("Resolver returned an invalid address") from error
            if not ip.is_global:
                raise PageReadRejectedError("Page address is not public")
        return parsed, sorted(addresses)[0], port

    @staticmethod
    def _resolve(hostname: str, port: int) -> list[str]:
        return sorted(
            {
                str(item[4][0])
                for item in socket.getaddrinfo(
                    hostname, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
                )
            }
        )
