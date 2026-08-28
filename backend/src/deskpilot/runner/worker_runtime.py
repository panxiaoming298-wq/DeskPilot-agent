"""Content-addressed Python runtime bundle for AppContainer Tool workers."""

import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import stat
import sys
import time
from concurrent.futures import ThreadPoolExecutor
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
RUNTIME_FILE_IO_WORKERS = min(16, max(4, os.cpu_count() or 4))


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


def _io_path(path: Path) -> str:
    """Return an extended-length absolute Windows path for bundle file I/O."""

    value = str(path.absolute())
    if os.name != "nt" or value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return f"\\\\?\\UNC\\{value[2:]}"
    return f"\\\\?\\{value}"


def _file_stat(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
    return os.stat(_io_path(path), follow_symlinks=follow_symlinks)


def _is_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(_file_stat(path).st_mode)
    except OSError:
        return False


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(_io_path(path), "rb") as stream:
        for chunk in iter(lambda: stream.read(1_048_576), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _files_match(
    entries: tuple[tuple[Path, int, str], ...],
) -> tuple[bool, ...]:
    """Verify independent runtime files concurrently without weakening hashes."""

    def matches(entry: tuple[Path, int, str]) -> bool:
        target, size, sha256 = entry
        try:
            return _file_stat(target).st_size == size and _hash_file(target) == sha256
        except OSError:
            return False

    with ThreadPoolExecutor(max_workers=RUNTIME_FILE_IO_WORKERS) as executor:
        return tuple(executor.map(matches, entries))


def _is_reparse_point(path: Path) -> bool:
    attributes = getattr(
        _file_stat(path, follow_symlinks=False),
        "st_file_attributes",
        0,
    )
    return bool(attributes & REPARSE_POINT_ATTRIBUTE)


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
    additional_executables: tuple[Path, ...] = (),
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

    for executable in additional_executables:
        if executable.suffix.casefold() != ".exe":
            raise WorkerRuntimeError("Additional worker executable must be one .exe file")
        add(executable, PurePosixPath(executable.name))

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
        with open(_io_path(bundle_root / "manifest.json"), encoding="utf-8") as stream:
            raw = json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise WorkerRuntimeIntegrityError("Worker runtime manifest is unreadable") from error
    if not isinstance(raw, dict):
        raise WorkerRuntimeIntegrityError("Worker runtime manifest has an invalid shape")
    return raw


def _runtime_tree_files(bundle_root: Path) -> set[PurePosixPath]:
    """Enumerate one runtime tree without the Windows legacy MAX_PATH limit."""

    actual: set[PurePosixPath] = set()
    io_root = _io_path(bundle_root)

    def reject_walk_error(error: OSError) -> None:
        raise WorkerRuntimeIntegrityError(
            "Worker runtime file set is unreadable"
        ) from error

    for current, directories, files in os.walk(
        io_root,
        topdown=True,
        onerror=reject_walk_error,
        followlinks=False,
    ):
        relative_current_value = os.path.relpath(current, io_root)
        relative_current = (
            PurePosixPath()
            if relative_current_value == "."
            else PurePosixPath(relative_current_value.replace("\\", "/"))
        )
        for name in (*directories, *files):
            relative = relative_current / name
            target = bundle_root.joinpath(*relative.parts)
            if _is_reparse_point(target):
                raise WorkerRuntimeIntegrityError(
                    "Worker runtime contains a reparse point"
                )
        for name in files:
            relative = relative_current / name
            target = bundle_root.joinpath(*relative.parts)
            if _is_file(target) and relative != PurePosixPath("manifest.json"):
                actual.add(relative)
    return actual


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
    actual_paths = _runtime_tree_files(bundle_root)
    if actual_paths != expected_paths:
        raise WorkerRuntimeIntegrityError("Worker runtime file set does not match its manifest")
    entries = tuple(
        (
            bundle_root.joinpath(*source.destination.parts),
            source.size,
            source.sha256,
        )
        for source in sources
    )
    for source, matches in zip(sources, _files_match(entries), strict=True):
        if not matches:
            raise WorkerRuntimeIntegrityError(
                f"Worker runtime file failed verification: {source.destination}"
            )
    return str(manifest["capability_sid"])


def _load_worker_runtime(
    bundle_root: Path,
    *,
    require_digest_directory: bool,
) -> WorkerRuntimeBundle:
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
        or (require_digest_directory and resolved.name != digest)
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

    actual = _runtime_tree_files(resolved)
    if actual != set(expected):
        raise WorkerRuntimeIntegrityError("Published worker runtime file set is invalid")
    entries = tuple(
        (resolved.joinpath(*relative.parts), size, sha256)
        for relative, (size, sha256) in expected.items()
    )
    for relative, matches in zip(expected, _files_match(entries), strict=True):
        if not matches:
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


def load_worker_runtime(bundle_root: Path) -> WorkerRuntimeBundle:
    """Verify a published bundle without trusting its manifest paths or digest."""

    return _load_worker_runtime(bundle_root, require_digest_directory=True)


def load_bundled_worker_runtime(resource_root: Path) -> WorkerRuntimeBundle:
    """Load exactly one content-addressed bundle from an installed resource root."""

    try:
        resolved = resource_root.resolve(strict=True)
        children = tuple(resolved.iterdir())
    except OSError as error:
        raise WorkerRuntimeIntegrityError(
            "Bundled worker runtime resource is unreadable"
        ) from error
    if _is_reparse_point(resolved) or not resolved.is_dir():
        raise WorkerRuntimeIntegrityError(
            "Bundled worker runtime resource must be one regular directory"
        )
    if (
        len(children) != 1
        or not children[0].is_dir()
        or _is_reparse_point(children[0])
        or re.fullmatch(r"[0-9a-f]{64}", children[0].name) is None
    ):
        raise WorkerRuntimeIntegrityError(
            "Bundled worker runtime resource must contain exactly one digest directory"
        )
    return load_worker_runtime(children[0])


def _copy_worker_runtime_tree(source: WorkerRuntimeBundle, destination: Path) -> None:
    if destination.exists():
        raise WorkerRuntimeError("Worker runtime copy destination already exists")
    manifest = _load_manifest(source.root)
    files = manifest.get("files")
    if not isinstance(files, list):
        raise WorkerRuntimeIntegrityError("Worker runtime manifest file set is invalid")
    copy_entries: list[tuple[Path, Path]] = []
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise WorkerRuntimeIntegrityError("Worker runtime manifest entry is invalid")
        relative = PurePosixPath(item["path"])
        source_file = source.root.joinpath(*relative.parts)
        destination_file = destination.joinpath(*relative.parts)
        copy_entries.append((source_file, destination_file))
    os.makedirs(_io_path(destination), exist_ok=False)
    parents = sorted(
        {destination_file.parent for _, destination_file in copy_entries},
        key=lambda path: (len(path.parts), str(path)),
    )
    for parent in parents:
        os.makedirs(_io_path(parent), exist_ok=True)

    def copy_file(entry: tuple[Path, Path]) -> None:
        source_file, destination_file = entry
        shutil.copyfile(_io_path(source_file), _io_path(destination_file))

    with ThreadPoolExecutor(max_workers=RUNTIME_FILE_IO_WORKERS) as executor:
        tuple(executor.map(copy_file, copy_entries))
    shutil.copyfile(
        _io_path(source.root / "manifest.json"),
        _io_path(destination / "manifest.json"),
    )


def copy_worker_runtime_bundle(
    source: WorkerRuntimeBundle,
    destination: Path,
) -> WorkerRuntimeBundle:
    """Copy one verified bundle with extended-length Windows file operations."""

    if destination.name != source.digest:
        raise WorkerRuntimeError("Worker runtime copy destination lost its digest name")
    _copy_worker_runtime_tree(source, destination)
    return load_worker_runtime(destination)


def publish_bundled_worker_runtime(
    source: WorkerRuntimeBundle,
    runtime_root: Path,
) -> WorkerRuntimeBundle:
    """Copy one verified installed bundle into a protected mutable runtime root."""

    if os.name != "nt":
        raise WorkerRuntimeError("AppContainer worker runtime requires Windows")
    from deskpilot.runner.windows_acl import protect_worker_runtime

    root = runtime_root.resolve(strict=False)
    bundle_root = root / source.digest
    with _RuntimeBuildLock(root / ".build.lock"):
        if bundle_root.exists():
            loaded = load_worker_runtime(bundle_root)
        else:
            staging = root / f".staging-{uuid4().hex}"
            try:
                _copy_worker_runtime_tree(source, staging)
                projected_sid = protect_worker_runtime(
                    staging,
                    WORKER_RUNTIME_CAPABILITY,
                )
                if projected_sid != source.capability_sid:
                    raise WorkerRuntimeIntegrityError(
                        "Bundled worker capability SID changed during publication"
                    )
                os.replace(staging, bundle_root)
                loaded = load_worker_runtime(bundle_root)
            except Exception:
                _remove_staging(staging, root)
                raise
        projected_sid = protect_worker_runtime(
            loaded.root,
            WORKER_RUNTIME_CAPABILITY,
        )
        if projected_sid != loaded.capability_sid:
            raise WorkerRuntimeIntegrityError(
                "Published worker capability SID changed during verification"
            )
        return loaded


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
    additional_executables: tuple[Path, ...] = (),
) -> WorkerRuntimeBundle:
    """Build, protect, publish, and verify the current worker source closure."""
    if os.name != "nt":
        raise WorkerRuntimeError("AppContainer worker runtime requires Windows")
    from deskpilot.runner.windows_acl import (
        capability_sid_string,
        protect_worker_runtime,
    )

    root = runtime_root.resolve(strict=False)
    sources = _runtime_sources(
        distributions,
        include_deskpilot=include_deskpilot,
        additional_executables=additional_executables,
    )
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
