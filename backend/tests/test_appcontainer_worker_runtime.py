import asyncio
import os
import socket
from pathlib import Path
from threading import Event, Thread

import pytest

from deskpilot.application.runner_client import RunnerClient
from deskpilot.runner.process_isolation import IsolationPolicy, create_process_launcher
from deskpilot.runner.worker_runtime import (
    WorkerRuntimeIntegrityError,
    copy_worker_runtime_bundle,
    load_bundled_worker_runtime,
    load_worker_runtime,
    prepare_worker_runtime,
    publish_bundled_worker_runtime,
)
from deskpilot.tools import create_builtin_registry
from deskpilot.tools.computer import DISK_USAGE_CONTRACT
from tests.authorization_helpers import make_tool_authorization

pytestmark = pytest.mark.skipif(
    os.name != "nt",
    reason="Windows AppContainer worker runtime integration test",
)


@pytest.fixture(scope="module")
def appcontainer_bundle(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("appcontainer-worker")
    bundle = prepare_worker_runtime(root / "runtime")
    return bundle.root, root / "profiles.json"


@pytest.mark.asyncio
async def test_real_disk_tool_runs_in_forced_network_isolation(
    appcontainer_bundle: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    bundle_root, journal = appcontainer_bundle
    client = RunnerClient(
        registry=create_builtin_registry(),
        require_windows_sandbox=True,
        require_network_isolation=True,
        worker_runtime_root=str(bundle_root.parent),
        appcontainer_profile_journal_path=str(journal),
        heartbeat_interval_seconds=0.1,
        heartbeat_timeout_seconds=2,
        startup_timeout_seconds=10,
    )
    arguments = {"path": str(tmp_path)}
    try:
        await client.start()
        result = await client.call_tool(
            task_id="task-appcontainer-runtime",
            step_id="step-disk",
            tool_name=DISK_USAGE_CONTRACT.name,
            tool_version=DISK_USAGE_CONTRACT.version,
            arguments=arguments,
            actor="pytest",
            call_id="call-appcontainer-runtime",
            authorization=make_tool_authorization(
                DISK_USAGE_CONTRACT,
                task_id="task-appcontainer-runtime",
                step_id="step-disk",
                call_id="call-appcontainer-runtime",
                actor_id="pytest",
                arguments=arguments,
            ),
        )

        assert client.isolation_mode == "windows_appcontainer"
        assert client.network_isolation_mode == "appcontainer"
        assert result.status == "succeeded"
        assert result.output is not None
        assert result.output["resolved_path"] == str(tmp_path)
        assert int(result.output["total_bytes"]) > 0
    finally:
        await client.stop()


def test_worker_capability_is_read_execute_only_and_has_no_network(
    appcontainer_bundle: tuple[Path, Path],
) -> None:
    bundle_root, journal = appcontainer_bundle
    bundle = load_worker_runtime(bundle_root)
    launcher = create_process_launcher(
        IsolationPolicy(
            require_windows_sandbox=True,
            require_network_isolation=True,
            worker_runtime_bundle=str(bundle.root),
            appcontainer_profile_journal_path=str(journal),
        )
    )
    blocked_file = bundle.root / "appcontainer-write-must-fail.txt"
    write_probe = (
        "from pathlib import Path\n"
        "import sys\n"
        f"target=Path({str(blocked_file)!r})\n"
        "try:\n"
        " target.write_text('forbidden', encoding='utf-8')\n"
        "except OSError:\n"
        " raise SystemExit(0)\n"
        "raise SystemExit(9)\n"
    )
    write_result = launcher.run(
        command=(str(bundle.executable), "-I", "-c", write_probe),
        input_frame=b"",
        cancellation=Event(),
    )

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(4)
    port = int(listener.getsockname()[1])
    accepted: list[bool] = []

    def serve() -> None:
        try:
            connection, _ = listener.accept()
        except OSError:
            return
        accepted.append(True)
        connection.close()

    server = Thread(target=serve, daemon=True)
    server.start()
    network_probe = (
        "import socket\n"
        "connection=socket.socket()\n"
        "connection.settimeout(2)\n"
        "try:\n"
        f" connection.connect(('127.0.0.1',{port}))\n"
        "except OSError:\n"
        " raise SystemExit(0)\n"
        "raise SystemExit(9)\n"
    )
    try:
        network_result = launcher.run(
            command=(str(bundle.executable), "-I", "-c", network_probe),
            input_frame=b"",
            cancellation=Event(),
        )
    finally:
        listener.close()
        server.join(timeout=1)

    assert write_result.return_code == 0
    assert not blocked_file.exists()
    assert network_result.return_code == 0
    assert accepted == []


def test_published_bundle_tampering_fails_closed(
    appcontainer_bundle: tuple[Path, Path],
) -> None:
    bundle_root, _ = appcontainer_bundle
    target = bundle_root / "Lib" / "site-packages" / "deskpilot" / "tools" / "computer.py"
    original = target.read_bytes()
    try:
        target.write_bytes(original + b"\n# tampered\n")
        with pytest.raises(WorkerRuntimeIntegrityError, match="failed verification"):
            prepare_worker_runtime(bundle_root.parent)
    finally:
        target.write_bytes(original)
    load_worker_runtime(bundle_root)


def test_installed_bundle_is_strictly_loaded_and_republished_with_appcontainer_acl(
    appcontainer_bundle: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    bundle_root, journal = appcontainer_bundle
    resource_root = tmp_path / f"installed-resource-{'x' * 55}"
    resource_root.mkdir()
    copy_worker_runtime_bundle(
        load_worker_runtime(bundle_root),
        resource_root / bundle_root.name,
    )

    source = load_bundled_worker_runtime(resource_root)
    published = publish_bundled_worker_runtime(
        source,
        tmp_path / f"runtime-{'x' * 55}",
    )
    deep_runtime_file = (
        published.root
        / "Lib"
        / "site-packages"
        / "deskpilot"
        / "infrastructure"
        / "migrations"
        / "versions"
        / "0015_database_claims_and_dag_scheduler.py"
    )
    assert len(str(deep_runtime_file)) > 260
    launcher = create_process_launcher(
        IsolationPolicy(
            require_windows_sandbox=True,
            require_network_isolation=True,
            worker_runtime_bundle=str(published.root),
            appcontainer_profile_journal_path=str(journal),
        )
    )
    result = launcher.run(
        command=(str(published.executable), "-I", "-c", "print('bundled-ready')"),
        input_frame=b"",
        cancellation=Event(),
    )

    assert source.digest == published.digest
    assert result.return_code == 0
    assert result.stdout.strip() == b"bundled-ready"


def test_installed_bundle_rejects_extra_or_tampered_resources(
    appcontainer_bundle: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    bundle_root, _ = appcontainer_bundle
    resource_root = tmp_path / "installed-resource"
    copied = resource_root / bundle_root.name
    resource_root.mkdir()
    copy_worker_runtime_bundle(load_worker_runtime(bundle_root), copied)
    (resource_root / "unexpected.txt").write_text("not authorized", encoding="utf-8")
    with pytest.raises(WorkerRuntimeIntegrityError, match="exactly one digest"):
        load_bundled_worker_runtime(resource_root)

    (resource_root / "unexpected.txt").unlink()
    target = copied / "python.exe"
    target.write_bytes(target.read_bytes() + b"tampered")
    with pytest.raises(WorkerRuntimeIntegrityError, match="failed verification"):
        load_bundled_worker_runtime(resource_root)


@pytest.mark.asyncio
async def test_bundle_fixture_does_not_block_event_loop(
    appcontainer_bundle: tuple[Path, Path],
) -> None:
    bundle_root, _ = appcontainer_bundle
    loaded = await asyncio.to_thread(load_worker_runtime, bundle_root)
    assert loaded.root == bundle_root
