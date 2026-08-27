"""Test-only Uvicorn process with persistent command fault injection."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, cast

import uvicorn

from deskpilot.application.command_profile_catalog import CommandProfileCatalog
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.core.config import Settings
from deskpilot.domain.command_profiles import (
    CommandProfile,
    CommandProfileId,
    WorkspaceCommandRead,
    WorkspaceCommandSnapshot,
)
from deskpilot.domain.model_contracts import ModelRequest, ModelResponse
from deskpilot.main import create_app
from deskpilot.model_providers.fake import (
    TURN_PLANNER_DECISION_SCHEMA,
    FakeModelProvider,
)


def _required_path(name: str) -> Path:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required")
    return Path(value)


def _read_calls(path: Path) -> list[str]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not all(isinstance(item, str) for item in payload):
        raise RuntimeError("Fault-injection call state is invalid")
    return payload


def _append_call(path: Path, value: str) -> list[str]:
    calls = [*_read_calls(path), value]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(calls, separators=(",", ":")), encoding="utf-8")
    return calls


def _offer_key(request: ModelRequest, profile_id: str) -> str:
    for message in request.messages:
        try:
            payload = json.loads(message.content)
        except json.JSONDecodeError:
            continue
        offers = payload.get("offers") if isinstance(payload, dict) else None
        if not isinstance(offers, list):
            continue
        matches = [
            item["offer"]["offer_key"]
            for item in offers
            if isinstance(item, dict)
            and profile_id in str(item.get("intent_description", ""))
        ]
        if len(matches) == 1:
            return str(matches[0])
    raise RuntimeError("Command Profile offer disappeared from the Planner request")


class _CommandPlannerProvider(FakeModelProvider):
    def __init__(self, profile_ids: tuple[CommandProfileId, ...], calls_path: Path) -> None:
        super().__init__(provider_id="fake-local")
        self._profile_ids = profile_ids
        self._calls_path = calls_path

    async def complete(self, request: ModelRequest) -> ModelResponse:
        response = await super().complete(request)
        if request.output_schema is None or request.output_schema.name != (
            TURN_PLANNER_DECISION_SCHEMA
        ):
            return response
        _append_call(self._calls_path, request.request_id)
        decision: dict[str, Any] = {
            "schema_version": "deskpilot.turn-planner-decision.v1",
            "kind": "propose_steps",
            "steps": [
                {
                    "offer_key": _offer_key(request, profile_id),
                    "parameters": [
                        {
                            "name": "project_path",
                            "value": os.environ["DESKPILOT_GOLDEN_COMMAND_PROJECT"],
                        }
                    ],
                }
                for profile_id in self._profile_ids
            ],
        }
        return response.model_copy(update={"structured_output": decision})


class _PersistentFaultCommandRuntime:
    def __init__(
        self,
        profile_ids: tuple[CommandProfileId, ...],
        calls_path: Path,
        started_path: Path,
        mode: str,
    ) -> None:
        self.enabled_profile_ids = frozenset(profile_ids)
        self._calls_path = calls_path
        self._started_path = started_path
        self._mode = mode

    def run(self, snapshot: WorkspaceCommandSnapshot) -> WorkspaceCommandRead:
        calls = _append_call(
            self._calls_path,
            snapshot.command_profile.command_profile_id,
        )
        if self._mode == "block_once" and len(calls) == 1:
            self._started_path.write_text(snapshot.snapshot_digest, encoding="utf-8")
            while True:
                time.sleep(1)
        status = "failed" if self._mode == "fail_once" and len(calls) == 1 else "passed"
        output = "scripted check failure" if status == "failed" else "ok"
        material: dict[str, Any] = {
            "schema_version": "deskpilot.workspace-command-read.v1",
            "command_profile_id": snapshot.command_profile.command_profile_id,
            "profile_digest": snapshot.command_profile.profile_digest,
            "project_path": snapshot.project_path,
            "snapshot_digest": snapshot.snapshot_digest,
            "toolchain_digest": "2" * 64,
            "status": status,
            "exit_code": 1 if status == "failed" else 0,
            "duration_ms": 10,
            "output_summary": output,
            "output_digest": sha256_digest({"output": output}),
            "output_truncated": False,
            "termination_reason": "completed",
            "cancellation_receipt_digest": None,
            "isolation_mode": "windows_appcontainer",
            "network_access": False,
            "temporary_snapshot": True,
            "snapshot_mutations_discarded": True,
        }
        return WorkspaceCommandRead.model_validate(
            {**material, "result_digest": sha256_digest(material)}
        )


def _profile_catalog() -> CommandProfileCatalog:
    catalog = CommandProfileCatalog()
    drift = os.environ.get("DESKPILOT_GOLDEN_PROFILE_DRIFT", "none")
    if drift == "none":
        return catalog
    profile_id = "python.ruff.v1" if drift == "profile" else "python.pytest.v1"
    original = catalog.resolve(profile_id)
    values = original.model_dump(exclude={"profile_digest", "timeout_seconds"})
    catalog._profiles[cast(CommandProfileId, profile_id)] = CommandProfile.build(  # noqa: SLF001
        **values,
        timeout_seconds=original.timeout_seconds + 1,
    )
    return catalog


def _settings() -> Settings:
    control_path = os.environ.get("DESKPILOT_GOLDEN_WORKBENCH_RUNTIME_CONTROL_PATH")
    if not control_path:
        return Settings()
    value = Path(control_path).read_text(encoding="utf-8").strip().lower()
    if value not in {"true", "false"}:
        raise RuntimeError("Golden Workbench runtime control must be true or false")
    return Settings(
        workbench_runtime_enabled=value == "true",
        workbench_runtime_poll_interval_seconds=0.01,
        workbench_runtime_claim_ttl_seconds=5,
    )


def main() -> None:
    profile_ids = tuple(
        cast(CommandProfileId, value)
        for value in os.environ["DESKPILOT_GOLDEN_COMMAND_PROFILES"].split(",")
    )
    runtime = _PersistentFaultCommandRuntime(
        profile_ids,
        _required_path("DESKPILOT_GOLDEN_COMMAND_CALLS_PATH"),
        _required_path("DESKPILOT_GOLDEN_COMMAND_STARTED_PATH"),
        os.environ.get("DESKPILOT_GOLDEN_COMMAND_FAULT_MODE", "pass"),
    )
    provider = _CommandPlannerProvider(
        profile_ids,
        _required_path("DESKPILOT_GOLDEN_PROVIDER_CALLS_PATH"),
    )
    app = create_app(
        _settings(),
        model_provider=provider,
        command_profile_catalog=_profile_catalog(),
        workspace_command_runtime=runtime,
    )
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=int(os.environ["DESKPILOT_GOLDEN_API_PORT"]),
        access_log=False,
    )


if __name__ == "__main__":
    main()
