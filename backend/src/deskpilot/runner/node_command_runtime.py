"""Content-addressed Node + pnpm bundle for fixed Command Profiles."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from uuid import uuid4

from deskpilot.runner.process_isolation import ProcessIsolationUnavailableError
from deskpilot.runner.worker_runtime import WORKER_RUNTIME_CAPABILITY, _RuntimeBuildLock

NODE_COMMAND_RUNTIME_SCHEMA = "deskpilot.node-command-runtime.v1"
NODE_COMMAND_HARNESS = r"""
const { spawnSync } = require("node:child_process")
const path = require("node:path")

if (process.argv[2] === "--probe") {
  process.stdout.write("ready\n")
  process.exit(0)
}

const scripts = Object.freeze({
  "node.pnpm_test.v1": "test",
  "node.pnpm_typecheck.v1": "type-check",
  "node.pnpm_build.v1": "build",
})
const script = scripts[process.argv[2]]
const store = process.argv[3]
if (!script || !path.isAbsolute(store)) process.exit(64)
const pnpm = path.join(__dirname, "pnpm", "bin", "pnpm.mjs")
const preservePathOptions = "--preserve-symlinks --preserve-symlinks-main"
const environment = Object.freeze({
  ...process.env,
  CI: "1",
  NODE_OPTIONS: preservePathOptions,
  NPM_CONFIG_USERCONFIG: "NUL",
  TEMP: ".",
  TMP: ".",
})

function run(args) {
  const preload = path.join(__dirname, "pnpm-appcontainer-preload.cjs")
  const result = spawnSync(process.execPath, [
    "--preserve-symlinks",
    "--preserve-symlinks-main",
    "--require",
    preload,
    pnpm,
    ...args,
  ], {
    cwd: process.cwd(),
    env: environment,
    shell: false,
    stdio: "inherit",
  })
  if (result.error) {
    process.stderr.write(`${result.error.name}: pnpm child process failed\n`)
    return 70
  }
  return typeof result.status === "number" ? result.status : 70
}

let code = run([
  "--offline",
  "--store-dir", store,
  "install",
  "--frozen-store",
  "--frozen-lockfile",
  "--ignore-scripts",
  "--package-import-method=copy",
  "--reporter=append-only",
])
if (code === 0) {
  code = run(["run", script])
}
process.exit(code)
""".strip()
NODE_COMMAND_PRELOAD = r"""
const fs = require("node:fs")
const path = require("node:path")

// The disposable workspace mirror is copied from a server-verified snapshot and
// contains no symlink or reparse point. Avoid Node's attempt to enumerate the
// Windows volume root when pnpm canonicalizes that exact trusted directory.
const trustedRoots = Object.freeze([
  path.resolve(process.cwd()),
  path.resolve(__dirname),
])
const originalRealpathSync = fs.realpathSync
const originalRealpath = fs.promises.realpath

function trustedLogicalPath(value) {
  const target = path.resolve(value)
  for (const root of trustedRoots) {
    const relative = path.relative(root, target)
    if (relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative))) {
      let current = root
      for (const part of relative.split(path.sep).filter(Boolean)) {
        current = path.join(current, part)
        try {
          if (fs.lstatSync(current).isSymbolicLink()) return undefined
        } catch (error) {
          if (error && error.code === "ENOENT") return target
          return undefined
        }
      }
      return target
    }
  }
  return undefined
}

fs.realpathSync = function deskpilotRealpathSync(value, options) {
  const trusted = trustedLogicalPath(value)
  if (trusted !== undefined) return trusted
  return originalRealpathSync.call(fs, value, options)
}
fs.promises.realpath = async function deskpilotRealpath(value, options) {
  const trusted = trustedLogicalPath(value)
  if (trusted !== undefined) return trusted
  return originalRealpath.call(fs.promises, value, options)
}
global[Symbol.for("__RESOLVED_TEMP_DIRECTORY__")] = trustedRoots[0]
""".strip()


class NodeCommandRuntimeError(ProcessIsolationUnavailableError):
    pass


class NodeCommandRuntimeIntegrityError(NodeCommandRuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _SourceFile:
    source: Path | None
    destination: PurePosixPath
    content: bytes | None
    size: int
    digest: str


@dataclass(frozen=True, slots=True)
class NodeCommandRuntimeBundle:
    root: Path
    executable: Path
    harness: Path
    digest: str


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1_048_576), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_reparse(path: Path) -> bool:
    value = path.stat(follow_symlinks=False)
    return stat.S_ISLNK(value.st_mode) or bool(
        getattr(value, "st_file_attributes", 0) & 0x00000400
    )


def _sources(node_executable: Path, pnpm_root: Path) -> tuple[_SourceFile, ...]:
    node = node_executable.resolve(strict=True)
    pnpm = pnpm_root.resolve(strict=True)
    if node.name.casefold() != "node.exe" or not node.is_file() or _is_reparse(node):
        raise NodeCommandRuntimeError("Configured Node executable is invalid")
    if not pnpm.is_dir() or _is_reparse(pnpm):
        raise NodeCommandRuntimeError("Configured pnpm package is invalid")
    required = (pnpm / "bin" / "pnpm.mjs", pnpm / "dist" / "pnpm.mjs")
    if not all(item.is_file() for item in required):
        raise NodeCommandRuntimeError("Configured pnpm package is incomplete")
    result = [
        _SourceFile(
            source=node,
            destination=PurePosixPath("node.exe"),
            content=None,
            size=node.stat().st_size,
            digest=_hash_file(node),
        )
    ]
    for source in sorted(pnpm.rglob("*"), key=lambda item: item.as_posix().casefold()):
        if _is_reparse(source):
            raise NodeCommandRuntimeError("pnpm bundle rejects links and reparse points")
        if source.is_file():
            result.append(
                _SourceFile(
                    source=source,
                    destination=PurePosixPath("pnpm")
                    / PurePosixPath(source.relative_to(pnpm).as_posix()),
                    content=None,
                    size=source.stat().st_size,
                    digest=_hash_file(source),
                )
            )
    harness = (NODE_COMMAND_HARNESS + "\n").encode("utf-8")
    result.append(
        _SourceFile(
            source=None,
            destination=PurePosixPath("command-harness.cjs"),
            content=harness,
            size=len(harness),
            digest=hashlib.sha256(harness).hexdigest(),
        )
    )
    preload = (NODE_COMMAND_PRELOAD + "\n").encode("utf-8")
    result.append(
        _SourceFile(
            source=None,
            destination=PurePosixPath("pnpm-appcontainer-preload.cjs"),
            content=preload,
            size=len(preload),
            digest=hashlib.sha256(preload).hexdigest(),
        )
    )
    return tuple(result)


def _identity(sources: tuple[_SourceFile, ...], capability_sid: str) -> dict[str, object]:
    return {
        "schema": NODE_COMMAND_RUNTIME_SCHEMA,
        "capability": WORKER_RUNTIME_CAPABILITY,
        "capability_sid": capability_sid,
        "files": [
            {"path": str(item.destination), "size": item.size, "sha256": item.digest}
            for item in sources
        ],
    }


def _digest(identity: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            identity,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _verify(root: Path, identity: dict[str, object], digest: str) -> None:
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise NodeCommandRuntimeIntegrityError("Node command manifest is unreadable") from error
    if manifest != {**identity, "digest": digest} or _digest(identity) != digest:
        raise NodeCommandRuntimeIntegrityError("Node command manifest changed")
    files = identity.get("files")
    if not isinstance(files, list):
        raise NodeCommandRuntimeIntegrityError("Node command file manifest is invalid")
    expected_paths: set[str] = set()
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise NodeCommandRuntimeIntegrityError("Node command file identity is invalid")
        relative = PurePosixPath(item["path"])
        target = root.joinpath(*relative.parts)
        expected_paths.add(relative.as_posix())
        if (
            _is_reparse(target)
            or not target.is_file()
            or target.stat().st_size != item.get("size")
            or _hash_file(target) != item.get("sha256")
        ):
            raise NodeCommandRuntimeIntegrityError("Node command toolchain failed verification")
    actual_paths = {
        item.relative_to(root).as_posix()
        for item in root.rglob("*")
        if item.is_file() and item.name != "manifest.json"
    }
    if actual_paths != expected_paths:
        raise NodeCommandRuntimeIntegrityError("Node command toolchain file set changed")


def _remove_staging(path: Path, root: Path) -> None:
    resolved = path.resolve(strict=False)
    if resolved.parent != root or not resolved.name.startswith(".node-command-staging-"):
        raise NodeCommandRuntimeError("Refusing to remove unexpected Node command staging")
    shutil.rmtree(resolved, ignore_errors=True)


def prepare_node_command_runtime(
    runtime_root: Path,
    node_executable: Path,
    pnpm_root: Path,
) -> NodeCommandRuntimeBundle:
    if os.name != "nt":
        raise NodeCommandRuntimeError("Node Command Profiles require Windows")
    from deskpilot.runner.windows_acl import capability_sid_string, protect_worker_runtime

    root = (runtime_root / "node-command").resolve(strict=False)
    sources = _sources(node_executable, pnpm_root)
    capability_sid = capability_sid_string(WORKER_RUNTIME_CAPABILITY)
    identity = _identity(sources, capability_sid)
    digest = _digest(identity)
    bundle_root = root / digest
    with _RuntimeBuildLock(root / ".build.lock", timeout_seconds=120):
        if bundle_root.exists():
            _verify(bundle_root, identity, digest)
        else:
            staging = root / f".node-command-staging-{uuid4().hex}"
            try:
                staging.mkdir(parents=True, exist_ok=False)
                for item in sources:
                    destination = staging.joinpath(*item.destination.parts)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if item.source is not None:
                        shutil.copyfile(item.source, destination)
                    elif item.content is not None:
                        destination.write_bytes(item.content)
                    else:
                        raise NodeCommandRuntimeError("Node command source is incomplete")
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
                if protect_worker_runtime(staging, WORKER_RUNTIME_CAPABILITY) != capability_sid:
                    raise NodeCommandRuntimeError("Node command capability SID changed")
                _verify(staging, identity, digest)
                os.replace(staging, bundle_root)
            except Exception:
                _remove_staging(staging, root)
                raise
        _verify(bundle_root, identity, digest)
    return NodeCommandRuntimeBundle(
        root=bundle_root,
        executable=(bundle_root / "node.exe").resolve(strict=True),
        harness=(bundle_root / "command-harness.cjs").resolve(strict=True),
        digest=digest,
    )


__all__ = [
    "NodeCommandRuntimeBundle",
    "NodeCommandRuntimeError",
    "NodeCommandRuntimeIntegrityError",
    "prepare_node_command_runtime",
]
