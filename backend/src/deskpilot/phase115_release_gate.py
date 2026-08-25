"""Offline immutable Agent release-manifest and activation-channel gate."""

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from deskpilot.agents import create_builtin_agent_registry
from deskpilot.application.agent_release_lifecycle import (
    MAX_AGENT_RELEASE_BUNDLE_BYTES,
    AgentReleaseError,
    AgentReleaseLifecycle,
    build_agent_release_manifest,
    empty_agent_release_bundle,
    load_agent_release_bundle,
)
from deskpilot.domain.agent_releases import AgentReleaseBundle, AgentReleaseManifest
from deskpilot.tools import create_builtin_registry

MAX_AGENT_RELEASE_MANIFEST_BYTES = 512 * 1024


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise argparse.ArgumentTypeError("timestamp must be ISO-8601") from error
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    manifest = commands.add_parser("manifest")
    manifest.add_argument("--build-id", required=True)
    manifest.add_argument("--created-at", type=_timestamp, required=True)
    manifest.add_argument("--valid-until", type=_timestamp, required=True)
    manifest.add_argument("--supersedes-release-id")
    manifest.add_argument("--output", type=Path, required=True)

    register = commands.add_parser("register")
    register.add_argument("--input", type=Path)
    register.add_argument("--channel", default="production")
    register.add_argument("--manifest", type=Path, required=True)
    _event_arguments(register)

    for name in ("activate", "disable", "expire"):
        command = commands.add_parser(name)
        command.add_argument("--input", type=Path, required=True)
        command.add_argument("--release-id", required=True)
        _event_arguments(command)

    replace = commands.add_parser("replace")
    replace.add_argument("--input", type=Path, required=True)
    replace.add_argument("--current-release-id", required=True)
    replace.add_argument("--manifest", type=Path, required=True)
    _event_arguments(replace)

    rollback = commands.add_parser("rollback")
    rollback.add_argument("--input", type=Path, required=True)
    rollback.add_argument("--current-release-id", required=True)
    rollback.add_argument("--target-release-id", required=True)
    _event_arguments(rollback)

    inspect = commands.add_parser("inspect")
    inspect.add_argument("--input", type=Path, required=True)
    return parser


def _event_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--actor", required=True)
    parser.add_argument("--at", type=_timestamp, required=True)
    parser.add_argument("--output", type=Path, required=True)


def _load_manifest(path: Path) -> AgentReleaseManifest:
    try:
        if path.is_symlink():
            raise AgentReleaseError("Agent release manifest cannot be a symbolic link")
        payload = path.resolve(strict=True).read_bytes()
        if not payload or len(payload) > MAX_AGENT_RELEASE_MANIFEST_BYTES:
            raise AgentReleaseError("Agent release manifest size is invalid")
        json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
        return AgentReleaseManifest.model_validate_json(payload, strict=True)
    except AgentReleaseError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError) as error:
        raise AgentReleaseError("Agent release manifest failed strict loading") from error


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AgentReleaseError("Agent release artifact contains a duplicate JSON key")
        result[key] = value
    return result


def _dump(path: Path, value: AgentReleaseBundle | AgentReleaseManifest) -> None:
    if path.exists():
        raise AgentReleaseError("Agent release artifact output is immutable")
    payload = value.model_dump_json(indent=2)
    if len(payload.encode("utf-8")) > MAX_AGENT_RELEASE_BUNDLE_BYTES:
        raise AgentReleaseError("Agent release artifact output is too large")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload + "\n", encoding="utf-8", newline="\n")
    except OSError as error:
        raise AgentReleaseError("Agent release artifact output failed") from error


def _summary(bundle: AgentReleaseBundle) -> dict[str, object]:
    return {
        "bundle_digest": bundle.bundle_digest,
        "channel": bundle.activation.channel,
        "revision": bundle.activation.revision,
        "active_release_id": bundle.activation.active_release_id,
        "release_count": len(bundle.releases),
        "event_count": len(bundle.events),
        "last_event_kind": bundle.events[-1].kind if bundle.events else None,
    }


def _mutate(arguments: argparse.Namespace) -> AgentReleaseBundle:
    if arguments.command == "register" and arguments.input is None:
        bundle = empty_agent_release_bundle(arguments.channel)
    else:
        bundle = load_agent_release_bundle(arguments.input)
    lifecycle = AgentReleaseLifecycle(bundle)
    if arguments.command == "register":
        return lifecycle.register(
            _load_manifest(arguments.manifest),
            actor=arguments.actor,
            now=arguments.at,
        )
    if arguments.command == "activate":
        return lifecycle.activate(
            arguments.release_id,
            actor=arguments.actor,
            now=arguments.at,
        )
    if arguments.command == "disable":
        return lifecycle.disable(
            arguments.release_id,
            actor=arguments.actor,
            now=arguments.at,
        )
    if arguments.command == "expire":
        return lifecycle.expire(
            arguments.release_id,
            actor=arguments.actor,
            now=arguments.at,
        )
    if arguments.command == "replace":
        return lifecycle.replace(
            arguments.current_release_id,
            _load_manifest(arguments.manifest),
            actor=arguments.actor,
            now=arguments.at,
        )
    return lifecycle.rollback(
        arguments.current_release_id,
        arguments.target_release_id,
        actor=arguments.actor,
        now=arguments.at,
    )


def main() -> int:
    arguments = _parser().parse_args()
    try:
        if arguments.command == "manifest":
            registry = create_builtin_agent_registry(create_builtin_registry(), ())
            result = build_agent_release_manifest(
                registry,
                build_id=arguments.build_id,
                created_at=arguments.created_at,
                valid_until=arguments.valid_until,
                supersedes_release_id=arguments.supersedes_release_id,
            )
            _dump(arguments.output, result)
            summary: dict[str, object] = {
                "release_id": result.release_id,
                "release_digest": result.release_digest,
                "cohort": [item.key for item in result.cohort],
                "companions": [item.key for item in result.companions],
            }
        elif arguments.command == "inspect":
            summary = _summary(load_agent_release_bundle(arguments.input))
        else:
            bundle = _mutate(arguments)
            _dump(arguments.output, bundle)
            summary = _summary(bundle)
        print(json.dumps(summary, sort_keys=True))
        return 0
    except (AgentReleaseError, ValidationError, ValueError, TypeError) as error:
        print(json.dumps({"code": "AGENT_RELEASE_REJECTED", "detail": str(error)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
