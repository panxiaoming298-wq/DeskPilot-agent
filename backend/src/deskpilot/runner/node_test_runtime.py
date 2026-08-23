"""Content-addressed Node runtime for fixed node:test files."""

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from deskpilot.runner.process_isolation import ProcessIsolationUnavailableError
from deskpilot.runner.worker_runtime import WORKER_RUNTIME_CAPABILITY, _RuntimeBuildLock

NODE_TEST_RUNTIME_SCHEMA = "deskpilot.node-test-runtime.v1"


class NodeTestRuntimeError(ProcessIsolationUnavailableError):
    """The fixed Node test runtime is unavailable or invalid."""


class NodeTestRuntimeIntegrityError(NodeTestRuntimeError):
    """A published Node test runtime failed verification."""


@dataclass(frozen=True, slots=True)
class NodeTestRuntimeBundle:
    root: Path
    executable: Path
    digest: str


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1_048_576), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _identity(executable: Path, capability_sid: str) -> dict[str, object]:
    return {
        "schema": NODE_TEST_RUNTIME_SCHEMA,
        "capability": WORKER_RUNTIME_CAPABILITY,
        "capability_sid": capability_sid,
        "files": [
            {
                "path": "node.exe",
                "size": executable.stat().st_size,
                "sha256": _hash_file(executable),
            }
        ],
    }


def _digest(identity: dict[str, object]) -> str:
    encoded = json.dumps(
        identity,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _verify(root: Path, identity: dict[str, object], digest: str) -> None:
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise NodeTestRuntimeIntegrityError("Node test runtime manifest is unreadable") from error
    if manifest != {**identity, "digest": digest} or _digest(identity) != digest:
        raise NodeTestRuntimeIntegrityError("Node test runtime manifest does not match")
    files = identity["files"]
    if not isinstance(files, list) or len(files) != 1 or not isinstance(files[0], dict):
        raise NodeTestRuntimeIntegrityError("Node test runtime file manifest is invalid")
    executable = root / "node.exe"
    actual_files = {
        item.name for item in root.iterdir() if item.is_file() and item.name != "manifest.json"
    }
    expected = files[0]
    if actual_files != {"node.exe"}:
        raise NodeTestRuntimeIntegrityError("Node test runtime file set changed")
    if executable.stat().st_size != expected.get("size") or _hash_file(executable) != expected.get(
        "sha256"
    ):
        raise NodeTestRuntimeIntegrityError("Node executable failed verification")


def _remove_staging(path: Path, root: Path) -> None:
    resolved = path.resolve(strict=False)
    if resolved.parent != root or not resolved.name.startswith(".node-staging-"):
        raise NodeTestRuntimeError("Refusing to remove an unexpected Node runtime path")
    shutil.rmtree(resolved, ignore_errors=True)


def prepare_node_test_runtime(
    runtime_root: Path,
    node_executable: Path,
) -> NodeTestRuntimeBundle:
    if os.name != "nt":
        raise NodeTestRuntimeError("Node test AppContainer runtime requires Windows")
    from deskpilot.runner.windows_acl import capability_sid_string, protect_worker_runtime

    executable = node_executable.resolve(strict=True)
    if executable.name.casefold() != "node.exe" or not executable.is_file():
        raise NodeTestRuntimeError("Configured Node executable is invalid")
    root = (runtime_root / "node-test").resolve(strict=False)
    capability_sid = capability_sid_string(WORKER_RUNTIME_CAPABILITY)
    identity = _identity(executable, capability_sid)
    digest = _digest(identity)
    bundle_root = root / digest[:32]
    with _RuntimeBuildLock(root / ".build.lock", timeout_seconds=120):
        if bundle_root.exists():
            _verify(bundle_root, identity, digest)
        else:
            staging = root / f".node-staging-{uuid4().hex}"
            try:
                staging.mkdir(parents=True, exist_ok=False)
                shutil.copyfile(executable, staging / "node.exe")
                (staging / "manifest.json").write_text(
                    json.dumps(
                        {**identity, "digest": digest},
                        ensure_ascii=True,
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                projected = protect_worker_runtime(staging, WORKER_RUNTIME_CAPABILITY)
                if projected != capability_sid:
                    raise NodeTestRuntimeError("Node runtime capability changed during publication")
                _verify(staging, identity, digest)
                os.replace(staging, bundle_root)
            except Exception:
                _remove_staging(staging, root)
                raise
        _verify(bundle_root, identity, digest)
    return NodeTestRuntimeBundle(
        root=bundle_root,
        executable=(bundle_root / "node.exe").resolve(strict=True),
        digest=digest,
    )


__all__ = [
    "NodeTestRuntimeBundle",
    "NodeTestRuntimeError",
    "NodeTestRuntimeIntegrityError",
    "prepare_node_test_runtime",
]
