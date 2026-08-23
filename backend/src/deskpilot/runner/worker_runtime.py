"""Content-addressed Python runtime bundle for AppContainer Tool workers."""

import hashlib
import importlib.metadata
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from uuid import uuid4

from deskpilot.runner.process_isolation import ProcessIsolationUnavailableError

WORKER_RUNTIME_SCHEMA = "deskpilot.worker-runtime.v1"
WORKER_RUNTIME_CAPABILITY = "DeskPilot.workerRuntime.v1"
WORKER_DISTRIBUTIONS = (
    "annotated-types",
    "pydantic-core",
    "pydantic",
    "typing-extensions",
    "typing-inspection",
)
RUNTIME_ROOT_FILES = (
    "python.exe",
    "python3.dll",
    f"python{sys.version_info.major}{sys.version_info.minor}.dll",
    "vcruntime140.dll",
    "vcruntime140_1.dll",
)
REPARSE_POINT_ATTRIBUTE = 0x00000400


class WorkerRuntimeError(ProcessIsolationUnavailableError):
    """The dedicated AppContainer worker runtime is unavailable or invalid."""


class WorkerRuntimeIntegrityError(WorkerRuntimeError):
    """An existing content-addressed worker runtime failed verification."""


@dataclass(frozen=True, slots=True)
class _SourceFile:
    source: Path
    destination: PurePosixPath
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class WorkerRuntimeBundle:
    root: Path
    executable: Path
    digest: str
    capability_name: str
    capability_sid: str


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1_048_576), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_reparse_point(path: Path) -> bool:
    attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
    return path.is_symlink() or bool(attributes & REPARSE_POINT_ATTRIBUTE)


def _iter_tree(
    source_root: Path,
    destination_root: PurePosixPath,
) -> list[tuple[Path, PurePosixPath]]:
    if _is_reparse_point(source_root):
        raise WorkerRuntimeError("Worker runtime source contains a reparse point")
    files: list[tuple[Path, PurePosixPath]] = []
    for source in sorted(source_root.rglob("*"), key=lambda item: item.as_posix()):
        relative = source.relative_to(source_root)
        if "__pycache__" in relative.parts or source.suffix in {".pyc", ".pyo"}:
            continue
        if _is_reparse_point(source):
            raise WorkerRuntimeError("Worker runtime source contains a reparse point")
        if source.is_file():
            files.append((source, destination_root / PurePosixPath(relative.as_posix())))
    return files


def _runtime_sources(
    distributions: tuple[str, ...] = WORKER_DISTRIBUTIONS,
    *,
    include_deskpilot: bool = True,
) -> tuple[_SourceFile, ...]:
    base = Path(sys.base_prefix).resolve(strict=True)
    mappings: dict[PurePosixPath, Path] = {}

    def add(source: Path, destination: PurePosixPath) -> None:
        resolved = source.resolve(strict=True)
        if _is_reparse_point(source) or not resolved.is_file():
            raise WorkerRuntimeError(f"Worker runtime source is invalid: {source.name}")
        existing = mappings.get(destination)
        if existing is not None and existing != resolved:
            raise WorkerRuntimeError(f"Worker runtime destination collision: {destination}")
        mappings[destination] = resolved

    for name in RUNTIME_ROOT_FILES:
        source = base / name
        if source.exists():
            add(source, PurePosixPath(name))
    if not (base / "python.exe").exists() or not (base / "Lib").is_dir():
        raise WorkerRuntimeError("CPython base runtime layout is unsupported")
    for source, destination in _iter_tree(base / "DLLs", PurePosixPath("DLLs")):
        add(source, destination)
    for child in sorted((base / "Lib").iterdir(), key=lambda item: item.name):
        if child.name == "site-packages" or child.name == "__pycache__":
            continue
        if child.is_file():
            if child.suffix not in {".pyc", ".pyo"}:
                add(child, PurePosixPath("Lib") / child.name)
        elif child.is_dir():
            for source, destination in _iter_tree(
                child,
                PurePosixPath("Lib") / child.name,
            ):
                add(source, destination)

    for distribution_name in distributions:
        try:
            distribution = importlib.metadata.distribution(distribution_name)
        except importlib.metadata.PackageNotFoundError as error:
            raise WorkerRuntimeError(
                f"Required worker dependency is missing: {distribution_name}"
            ) from error
        distribution_root = Path(str(distribution.locate_file(""))).resolve(strict=True)
        for relative in distribution.files or ():
            relative_path = PurePosixPath(str(relative).replace("\\", "/"))
            if (
                relative_path.is_absolute()
                or ".." in relative_path.parts
                or "__pycache__" in relative_path.parts
                or relative_path.suffix in {".pyc", ".pyo"}
            ):
                continue
            source = distribution_root.joinpath(*relative_path.parts)
            if source.is_file():
                add(source, PurePosixPath("Lib/site-packages") / relative_path)

    if include_deskpilot:
        import deskpilot

        deskpilot_root = Path(deskpilot.__file__).resolve(strict=True).parent
        for source, destination in _iter_tree(
            deskpilot_root,
            PurePosixPath("Lib/site-packages/deskpilot"),
        ):
            add(source, destination)

    sources: list[_SourceFile] = []
    for destination, source in sorted(mappings.items(), key=lambda item: str(item[0])):
        size = source.stat().st_size
        sources.append(
            _SourceFile(
                source=source,
                destination=destination,
                size=size,
                sha256=_hash_file(source),
            )
        )
    return tuple(sources)


def _bundle_digest(sources: tuple[_SourceFile, ...], capability_sid: str) -> str:
    identity = {
        "schema": WORKER_RUNTIME_SCHEMA,
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "capability": WORKER_RUNTIME_CAPABILITY,
        "capability_sid": capability_sid,
        "files": [
            {"path": str(source.destination), "size": source.size, "sha256": source.sha256}
            for source in sources
        ],
    }
    encoded = json.dumps(
        identity,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _manifest_payload(
    sources: tuple[_SourceFile, ...], digest: str, capability_sid: str
) -> dict[str, object]:
    return {
        "schema": WORKER_RUNTIME_SCHEMA,
        "digest": digest,
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "capability": WORKER_RUNTIME_CAPABILITY,
        "capability_sid": capability_sid,
        "files": [
            {"path": str(source.destination), "size": source.size, "sha256": source.sha256}
            for source in sources
        ],
    }


def _manifest_identity(manifest: dict[str, object]) -> dict[str, object]:
    return {
        "schema": manifest.get("schema"),
        "python": manifest.get("python"),
        "capability": manifest.get("capability"),
        "capability_sid": manifest.get("capability_sid"),
        "files": manifest.get("files"),
    }


def _identity_digest(identity: dict[str, object]) -> str:
    encoded = json.dumps(
        identity,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_manifest(bundle_root: Path) -> dict[str, object]:
    try:
        raw = json.loads((bundle_root / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise WorkerRuntimeIntegrityError("Worker runtime manifest is unreadable") from error
    if not isinstance(raw, dict):
        raise WorkerRuntimeIntegrityError("Worker runtime manifest has an invalid shape")
    return raw


def _verify_bundle(
    bundle_root: Path,
    sources: tuple[_SourceFile, ...],
    digest: str,
) -> str:
    manifest = _load_manifest(bundle_root)
    files = manifest.get("files")
    expected_files = [
        {"path": str(source.destination), "size": source.size, "sha256": source.sha256}
        for source in sources
    ]
    if (
        manifest.get("schema") != WORKER_RUNTIME_SCHEMA
        or manifest.get("digest") != digest
        or manifest.get("capability") != WORKER_RUNTIME_CAPABILITY
        or files != expected_files
        or not isinstance(manifest.get("capability_sid"), str)
        or _identity_digest(_manifest_identity(manifest)) != digest
    ):
        raise WorkerRuntimeIntegrityError("Worker runtime manifest does not match its sources")
    expected_paths = {PurePosixPath(str(item["path"])) for item in expected_files}
    actual_paths: set[PurePosixPath] = set()
    for path in bundle_root.rglob("*"):
        relative = PurePosixPath(path.relative_to(bundle_root).as_posix())
        if _is_reparse_point(path):
            raise WorkerRuntimeIntegrityError("Worker runtime contains a reparse point")
        if path.is_file() and relative != PurePosixPath("manifest.json"):
            actual_paths.add(relative)
    if actual_paths != expected_paths:
        raise WorkerRuntimeIntegrityError("Worker runtime file set does not match its manifest")
    for source in sources:
        target = bundle_root.joinpath(*source.destination.parts)
        try:
            size = target.stat().st_size
        except OSError as error:
            raise WorkerRuntimeIntegrityError("Worker runtime file is unavailable") from error
        if size != source.size or _hash_file(target) != source.sha256:
            raise WorkerRuntimeIntegrityError(
                f"Worker runtime file failed verification: {source.destination}"
            )
    return str(manifest["capability_sid"])


def load_worker_runtime(bundle_root: Path) -> WorkerRuntimeBundle:
    """Verify a published bundle without trusting its manifest paths or digest."""
    resolved = bundle_root.resolve(strict=True)
    manifest = _load_manifest(resolved)
    files = manifest.get("files")
    digest = manifest.get("digest")
    capability_sid = manifest.get("capability_sid")
    if (
        manifest.get("schema") != WORKER_RUNTIME_SCHEMA
        or manifest.get("capability") != WORKER_RUNTIME_CAPABILITY
        or not isinstance(manifest.get("python"), str)
        or not isinstance(files, list)
        or not isinstance(digest, str)
        or len(digest) != 64
        or not isinstance(capability_sid, str)
        or resolved.name != digest
        or _identity_digest(_manifest_identity(manifest)) != digest
    ):
        raise WorkerRuntimeIntegrityError("Published worker runtime manifest is invalid")

    expected: dict[PurePosixPath, tuple[int, str]] = {}
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "size", "sha256"}:
            raise WorkerRuntimeIntegrityError("Published worker runtime entry is invalid")
        path_value = item["path"]
        size = item["size"]
        sha256 = item["sha256"]
        if not isinstance(path_value, str):
            raise WorkerRuntimeIntegrityError("Published worker runtime path is invalid")
        relative = PurePosixPath(path_value)
        if relative.is_absolute() or ".." in relative.parts or relative in expected:
            raise WorkerRuntimeIntegrityError("Published worker runtime path is unsafe")
        if (
            not isinstance(size, int)
            or size < 0
            or not isinstance(sha256, str)
            or len(sha256) != 64
        ):
            raise WorkerRuntimeIntegrityError("Published worker runtime hash is invalid")
        expected[relative] = (size, sha256)

    actual: set[PurePosixPath] = set()
    for path in resolved.rglob("*"):
        relative = PurePosixPath(path.relative_to(resolved).as_posix())
        if _is_reparse_point(path):
            raise WorkerRuntimeIntegrityError("Published worker runtime has a reparse point")
        if path.is_file() and relative != PurePosixPath("manifest.json"):
            actual.add(relative)
    if actual != set(expected):
        raise WorkerRuntimeIntegrityError("Published worker runtime file set is invalid")
    for relative, (size, sha256) in expected.items():
        target = resolved.joinpath(*relative.parts)
        if target.stat().st_size != size or _hash_file(target) != sha256:
            raise WorkerRuntimeIntegrityError(
                f"Published worker runtime file failed verification: {relative}"
            )
    executable = (resolved / "python.exe").resolve(strict=True)
    return WorkerRuntimeBundle(
        root=resolved,
        executable=executable,
        digest=digest,
        capability_name=WORKER_RUNTIME_CAPABILITY,
        capability_sid=capability_sid,
    )


class _RuntimeBuildLock:
    def __init__(self, path: Path, timeout_seconds: float = 30.0) -> None:
        self._path = path
        self._timeout_seconds = timeout_seconds
        self._stream: object | None = None

    def __enter__(self) -> None:
        if os.name != "nt":
            raise WorkerRuntimeError("AppContainer worker runtime requires Windows")
        import msvcrt

        self._path.parent.mkdir(parents=True, exist_ok=True)
        stream = self._path.open("a+b")
        stream.seek(0)
        if stream.read(1) != b"1":
            stream.seek(0)
            stream.write(b"1")
            stream.flush()
        deadline = time.monotonic() + self._timeout_seconds
        while True:
            try:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                self._stream = stream
                return
            except OSError as error:
                if time.monotonic() >= deadline:
                    stream.close()
                    raise WorkerRuntimeError(
                        "Timed out waiting for worker runtime build"
                    ) from error
                time.sleep(0.05)

    def __exit__(self, *_: object) -> None:
        import msvcrt

        stream = self._stream
        self._stream = None
        if stream is not None:
            stream.seek(0)  # type: ignore[attr-defined]
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
            stream.close()  # type: ignore[attr-defined]


def _remove_staging(path: Path, runtime_root: Path) -> None:
    resolved = path.resolve(strict=False)
    if resolved.parent != runtime_root or not resolved.name.startswith(".staging-"):
        raise WorkerRuntimeError("Refusing to remove an unexpected runtime path")
    shutil.rmtree(resolved, ignore_errors=True)


def prepare_worker_runtime(
    runtime_root: Path,
    *,
    distributions: tuple[str, ...] = WORKER_DISTRIBUTIONS,
    include_deskpilot: bool = True,
) -> WorkerRuntimeBundle:
    """Build, protect, publish, and verify the current worker source closure."""
    if os.name != "nt":
        raise WorkerRuntimeError("AppContainer worker runtime requires Windows")
    from deskpilot.runner.windows_acl import (
        capability_sid_string,
        protect_worker_runtime,
    )

    root = runtime_root.resolve(strict=False)
    sources = _runtime_sources(distributions, include_deskpilot=include_deskpilot)
    expected_capability_sid = capability_sid_string(WORKER_RUNTIME_CAPABILITY)
    digest = _bundle_digest(sources, expected_capability_sid)
    bundle_root = root / digest
    with _RuntimeBuildLock(root / ".build.lock"):
        if bundle_root.exists():
            capability_sid = _verify_bundle(bundle_root, sources, digest)
        else:
            staging = root / f".staging-{uuid4().hex}"
            try:
                staging.mkdir(parents=True, exist_ok=False)
                for source in sources:
                    destination = staging.joinpath(*source.destination.parts)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source.source, destination)
                capability_sid = expected_capability_sid
                payload = _manifest_payload(sources, digest, capability_sid)
                manifest = staging / "manifest.json"
                with manifest.open("x", encoding="utf-8", newline="\n") as stream:
                    json.dump(
                        payload,
                        stream,
                        ensure_ascii=True,
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                projected_sid = protect_worker_runtime(
                    staging,
                    WORKER_RUNTIME_CAPABILITY,
                )
                if projected_sid != capability_sid:
                    raise WorkerRuntimeError("Worker capability SID changed during publication")
                _verify_bundle(staging, sources, digest)
                os.replace(staging, bundle_root)
            except Exception:
                _remove_staging(staging, root)
                raise
        capability_sid = _verify_bundle(bundle_root, sources, digest)
    executable = (bundle_root / "python.exe").resolve(strict=True)
    return WorkerRuntimeBundle(
        root=bundle_root,
        executable=executable,
        digest=digest,
        capability_name=WORKER_RUNTIME_CAPABILITY,
        capability_sid=capability_sid,
    )
