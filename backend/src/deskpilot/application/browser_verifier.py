"""Fail-closed isolated Chromium verifier for static Task Workspace HTML."""

import asyncio
import os
import re
import subprocess
import tempfile
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict

from deskpilot.core.canonical_json import sha256_digest


class BrowserVerifierError(RuntimeError):
    code = "BROWSER_VERIFIER_ERROR"


class BrowserUnavailableError(BrowserVerifierError):
    code = "BROWSER_VERIFIER_UNAVAILABLE"


class BrowserEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    passed: bool
    engine: str
    title: str
    heading_count: int
    link_count: int
    external_request_count: int
    console_error_count: int
    page_error_count: int
    issue_codes: tuple[str, ...]
    dom_digest: str
    screenshot_digest: str


class BrowserVerifier(Protocol):
    async def verify(self, entry_path: Path, html: str) -> BrowserEvidence: ...


class _StaticHtmlAudit(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.in_title = False
        self.heading_count = 0
        self.link_count = 0
        self.ids: set[str] = set()
        self.duplicates: set[str] = set()
        self.issues: set[str] = set()
        self.has_lang = False
        self.has_charset = False
        self.has_viewport = False
        self.has_csp = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name.lower(): value or "" for name, value in attrs}
        tag = tag.lower()
        if tag == "html":
            self.has_lang = bool(values.get("lang"))
        elif tag == "title":
            self.in_title = True
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.heading_count += 1
        elif tag == "a":
            self.link_count += 1
        elif tag == "script":
            self.issues.add("SCRIPT_FORBIDDEN")
        elif tag in {"iframe", "object", "embed"}:
            self.issues.add("EMBED_FORBIDDEN")
        if tag == "meta":
            self.has_charset |= values.get("charset", "").lower() == "utf-8"
            name = values.get("name", "").lower()
            self.has_viewport |= name == "viewport"
            self.has_csp |= (
                values.get("http-equiv", "").lower() == "content-security-policy"
                and "default-src 'none'" in values.get("content", "").lower()
            )
            if values.get("http-equiv", "").lower() == "refresh":
                self.issues.add("META_REFRESH_FORBIDDEN")
        element_id = values.get("id")
        if element_id:
            if element_id in self.ids:
                self.duplicates.add(element_id)
            self.ids.add(element_id)
        if any(name.startswith("on") for name in values):
            self.issues.add("INLINE_HANDLER_FORBIDDEN")
        resource_attribute = {
            "img": "src",
            "link": "href",
            "source": "src",
            "video": "src",
            "audio": "src",
            "form": "action",
        }.get(tag)
        if resource_attribute and _is_external(values.get(resource_attribute, "")):
            self.issues.add("EXTERNAL_RESOURCE_FORBIDDEN")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)

    def finish(self, html: str) -> tuple[str, tuple[str, ...]]:
        if not self.has_lang:
            self.issues.add("HTML_LANG_MISSING")
        if not self.has_charset:
            self.issues.add("CHARSET_MISSING")
        if not self.has_viewport:
            self.issues.add("VIEWPORT_MISSING")
        if not self.has_csp:
            self.issues.add("CSP_MISSING")
        if self.heading_count == 0:
            self.issues.add("HEADING_MISSING")
        title = "".join(self.title_parts).strip()
        if not title:
            self.issues.add("TITLE_MISSING")
        if self.duplicates:
            self.issues.add("DUPLICATE_ID")
        if re.search(r"(?:url\s*\(|@import)\s*['\"]?https?://", html, re.I):
            self.issues.add("EXTERNAL_CSS_RESOURCE_FORBIDDEN")
        return title, tuple(sorted(self.issues))


def audit_static_html(html: str) -> tuple[_StaticHtmlAudit, str, tuple[str, ...]]:
    parser = _StaticHtmlAudit()
    parser.feed(html)
    parser.close()
    title, issues = parser.finish(html)
    return parser, title, issues


def _is_external(value: str) -> bool:
    if not value:
        return False
    parsed = urlsplit(value)
    return value.startswith("//") or parsed.scheme.lower() in {
        "http",
        "https",
        "ftp",
        "ws",
        "wss",
    }


class IsolatedChromiumVerifier:
    def __init__(self, executable_path: str | None = None, *, timeout_seconds: int = 30):
        self._executable = self._resolve_executable(executable_path)
        self._timeout_seconds = timeout_seconds

    @property
    def available(self) -> bool:
        return self._executable is not None

    async def verify(self, entry_path: Path, html: str) -> BrowserEvidence:
        parser, title, issues = audit_static_html(html)
        if issues:
            digest = sha256_digest({"html": html, "issues": issues})
            return BrowserEvidence(
                passed=False,
                engine=self._engine_name(),
                title=title,
                heading_count=parser.heading_count,
                link_count=parser.link_count,
                external_request_count=sum("EXTERNAL" in item for item in issues),
                console_error_count=0,
                page_error_count=0,
                issue_codes=issues,
                dom_digest=digest,
                screenshot_digest=sha256_digest({"screenshot": "not-captured"}),
            )
        return await asyncio.to_thread(self._render, entry_path, html, parser, title)

    def _render(
        self, entry_path: Path, html: str, parser: _StaticHtmlAudit, title: str
    ) -> BrowserEvidence:
        if self._executable is None:
            raise BrowserUnavailableError("No supported Chromium or Edge executable found")
        resolved_entry = entry_path.resolve(strict=True)
        with tempfile.TemporaryDirectory(
            prefix="deskpilot-browser-", ignore_cleanup_errors=True
        ) as temporary:
            temp = Path(temporary)
            screenshot = temp / "page.png"
            common = [
                str(self._executable),
                "--headless",
                "--disable-gpu",
                "--no-first-run",
                "--disable-default-apps",
                "--disable-extensions",
                "--disable-sync",
                "--disable-background-networking",
                "--disable-component-update",
                "--disable-features=MediaRouter,Translate,OptimizationHints",
                "--disable-javascript",
                "--host-resolver-rules=MAP * 0.0.0.0",
                "--proxy-server=127.0.0.1:9",
                "--window-size=1280,720",
            ]
            startupinfo = None
            if os.name == "nt":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            screenshot_result = subprocess.run(
                [
                    *common,
                    f"--user-data-dir={temp / 'profile'}",
                    f"--screenshot={screenshot}",
                    resolved_entry.as_uri(),
                ],
                check=False,
                capture_output=True,
                timeout=self._timeout_seconds,
                startupinfo=startupinfo,
            )
            deadline = time.monotonic() + self._timeout_seconds
            while not screenshot.is_file() and time.monotonic() < deadline:
                time.sleep(0.05)
            if screenshot_result.returncode != 0 or not screenshot.is_file():
                raise BrowserVerifierError("Isolated browser render failed")
            screenshot_bytes = screenshot.read_bytes()
            if not screenshot_bytes:
                raise BrowserVerifierError("Browser evidence is incomplete")
            time.sleep(0.25)
            return BrowserEvidence(
                passed=True,
                engine=self._engine_name(),
                title=title,
                heading_count=parser.heading_count,
                link_count=parser.link_count,
                external_request_count=0,
                console_error_count=0,
                page_error_count=0,
                issue_codes=(),
                dom_digest=sha256_digest({"static_dom": html}),
                screenshot_digest=sha256_digest({"bytes_hex": screenshot_bytes.hex()}),
            )

    def _engine_name(self) -> str:
        return self._executable.name if self._executable is not None else "unavailable"

    @staticmethod
    def _resolve_executable(configured: str | None) -> Path | None:
        candidates = [Path(configured)] if configured else []
        if os.name == "nt":
            candidates.extend(
                Path(path)
                for path in (
                    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                )
            )
        else:
            candidates.extend(
                Path(path) for path in ("/usr/bin/chromium", "/usr/bin/google-chrome")
            )
        return next((item.resolve() for item in candidates if item.is_file()), None)
