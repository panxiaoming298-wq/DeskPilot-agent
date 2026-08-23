"""Isolated HTML-to-PDF rendering with fail-closed Poppler verification."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import struct
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.artifact_runtime import PdfRenderVerificationRead, digested


class PdfArtifactRendererError(RuntimeError):
    code = "PDF_ARTIFACT_RENDERER_ERROR"


class PdfArtifactRendererUnavailableError(PdfArtifactRendererError):
    code = "PDF_ARTIFACT_RENDERER_UNAVAILABLE"


@dataclass(frozen=True)
class RenderedPdf:
    content: bytes
    verification: PdfRenderVerificationRead


class PdfArtifactRenderer(Protocol):
    async def render(self, entry_path: Path) -> RenderedPdf: ...


class IsolatedPdfArtifactRenderer:
    """Print static HTML with Chromium, then render every PDF page with Poppler."""

    def __init__(
        self,
        browser_executable_path: str | None = None,
        pdfinfo_executable_path: str | None = None,
        pdftoppm_executable_path: str | None = None,
        *,
        timeout_seconds: int = 30,
        render_dpi: int = 144,
    ) -> None:
        self._browser = self._resolve_browser(browser_executable_path)
        self._pdfinfo = self._resolve_tool(pdfinfo_executable_path, "pdfinfo")
        self._pdftoppm = self._resolve_tool(pdftoppm_executable_path, "pdftoppm")
        self._timeout_seconds = timeout_seconds
        self._render_dpi = render_dpi

    @property
    def available(self) -> bool:
        return all((self._browser, self._pdfinfo, self._pdftoppm))

    async def render(self, entry_path: Path) -> RenderedPdf:
        return await asyncio.to_thread(self._render, entry_path)

    def _render(self, entry_path: Path) -> RenderedPdf:
        if not self.available:
            raise PdfArtifactRendererUnavailableError(
                "Chromium, pdfinfo, and pdftoppm are required for verified PDF delivery"
            )
        browser = self._browser
        pdfinfo = self._pdfinfo
        pdftoppm = self._pdftoppm
        if browser is None or pdfinfo is None or pdftoppm is None:
            raise PdfArtifactRendererUnavailableError("Verified PDF renderer is unavailable")
        resolved_entry = entry_path.resolve(strict=True)
        with tempfile.TemporaryDirectory(
            prefix="deskpilot-pdf-", ignore_cleanup_errors=True
        ) as temporary:
            temp = Path(temporary)
            print_entry = temp / "index.html"
            print_entry.write_bytes(resolved_entry.read_bytes())
            pdf_path = temp / "report.pdf"
            common = [
                str(browser),
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
            ]
            result = self._run(
                [
                    *common,
                    f"--user-data-dir={temp / 'profile'}",
                    "--no-pdf-header-footer",
                    f"--print-to-pdf={pdf_path}",
                    print_entry.as_uri(),
                ]
            )
            deadline = time.monotonic() + self._timeout_seconds
            while not pdf_path.is_file() and time.monotonic() < deadline:
                time.sleep(0.05)
            if result.returncode != 0 or not pdf_path.is_file():
                raise PdfArtifactRendererError("Isolated Chromium PDF print failed")
            time.sleep(0.25)
            content = self._canonicalize_pdf(pdf_path.read_bytes())
            if not content.startswith(b"%PDF-") or len(content) < 512:
                raise PdfArtifactRendererError("Chromium produced an invalid PDF")
            pdf_path.write_bytes(content)

            info = self._run([str(pdfinfo), str(pdf_path)])
            if info.returncode != 0:
                raise PdfArtifactRendererError("pdfinfo rejected generated PDF")
            page_count = self._parse_positive_int(info.stdout, "Pages")
            page_width, page_height = self._parse_page_size(info.stdout)

            prefix = temp / "page"
            raster = self._run(
                [
                    str(pdftoppm),
                    "-png",
                    "-r",
                    str(self._render_dpi),
                    str(pdf_path),
                    str(prefix),
                ]
            )
            if raster.returncode != 0:
                raise PdfArtifactRendererError("pdftoppm failed to render generated PDF")
            pages = tuple(sorted(temp.glob("page-*.png"), key=self._page_number))
            if len(pages) != page_count:
                raise PdfArtifactRendererError("Rendered PDF page count is incomplete")
            dimensions = tuple(self._png_dimensions(path) for path in pages)
            if any(width < 2 or height < 2 for width, height in dimensions):
                raise PdfArtifactRendererError("Rendered PDF contains an invalid page image")
            page_digests = tuple(
                sha256_digest({"bytes_hex": path.read_bytes().hex()}) for path in pages
            )
            source_digest = sha256_digest({"bytes_hex": content.hex()})
            material: dict[str, object] = {
                "profile_id": "deskpilot.pdf-render.v1",
                "status": "passed",
                "engine": "chromium-print+poppler-pdftoppm",
                "source_digest": source_digest,
                "page_count": page_count,
                "page_width_points": page_width,
                "page_height_points": page_height,
                "render_dpi": self._render_dpi,
                "rendered_page_digests": page_digests,
                "rendered_page_dimensions": dimensions,
                "issue_codes": (),
            }
            verification = PdfRenderVerificationRead.model_validate(
                digested(material, "evidence_digest")
            )
            return RenderedPdf(content=content, verification=verification)

    def _run(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        startupinfo = None
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        try:
            return subprocess.run(
                argv,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._timeout_seconds,
                startupinfo=startupinfo,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise PdfArtifactRendererError("PDF renderer subprocess failed") from error

    @staticmethod
    def _canonicalize_pdf(content: bytes) -> bytes:
        fixed_date = b"D:20000101000000+00'00'"
        content = re.sub(
            rb"D:\d{14}[+-]\d{2}'\d{2}'",
            fixed_date,
            content,
        )
        return re.sub(
            rb"/ID(\s*\[\s*)<[0-9A-Fa-f]{32}>(\s*)<[0-9A-Fa-f]{32}>(\s*\])",
            lambda match: (
                b"/ID"
                + match.group(1)
                + b"<00000000000000000000000000000000>"
                + match.group(2)
                + b"<00000000000000000000000000000000>"
                + match.group(3)
            ),
            content,
        )

    @staticmethod
    def _parse_positive_int(output: str, label: str) -> int:
        match = re.search(rf"^{re.escape(label)}:\s*(\d+)\s*$", output, re.MULTILINE)
        value = int(match.group(1)) if match else 0
        if value < 1:
            raise PdfArtifactRendererError(f"pdfinfo omitted {label}")
        return value

    @staticmethod
    def _parse_page_size(output: str) -> tuple[float, float]:
        match = re.search(
            r"^Page size:\s*([0-9.]+)\s+x\s+([0-9.]+)\s+pts",
            output,
            re.MULTILINE,
        )
        if match is None:
            raise PdfArtifactRendererError("pdfinfo omitted Page size")
        width, height = float(match.group(1)), float(match.group(2))
        if width <= 0 or height <= 0:
            raise PdfArtifactRendererError("pdfinfo returned an invalid Page size")
        return width, height

    @staticmethod
    def _png_dimensions(path: Path) -> tuple[int, int]:
        header = path.read_bytes()[:24]
        if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
            raise PdfArtifactRendererError("Poppler produced an invalid PNG page")
        return struct.unpack(">II", header[16:24])

    @staticmethod
    def _page_number(path: Path) -> int:
        match = re.search(r"-(\d+)\.png$", path.name)
        return int(match.group(1)) if match else 0

    @staticmethod
    def _resolve_tool(configured: str | None, name: str) -> Path | None:
        candidates = [configured, shutil.which(name)]
        if os.name == "nt":
            candidates.extend(
                (
                    str(Path(r"C:\Program Files\poppler\Library\bin") / f"{name}.exe"),
                    str(Path(r"C:\Program Files\poppler\bin") / f"{name}.exe"),
                )
            )
        for value in candidates:
            if not value:
                continue
            path = Path(value)
            if path.suffix.lower() in {".cmd", ".bat"}:
                bundled = (
                    path.parent
                    / ".."
                    / ".."
                    / "native"
                    / "poppler"
                    / "Library"
                    / "bin"
                    / f"{name}.exe"
                ).resolve()
                if bundled.is_file():
                    return bundled
                continue
            if path.is_file():
                return path.resolve()
        return None

    @staticmethod
    def _resolve_browser(configured: str | None) -> Path | None:
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
