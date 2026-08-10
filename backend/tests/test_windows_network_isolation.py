import os
import socket
from pathlib import Path
from threading import Event, Thread

import pytest

from deskpilot.runner.process_isolation import (
    IsolationMode,
    IsolationPolicy,
    NetworkIsolationMode,
    ProcessIsolationError,
    create_process_launcher,
)
from deskpilot.runner.profile_journal import AppContainerProfileJournal


@pytest.mark.skipif(os.name != "nt", reason="Windows AppContainer integration test")
def test_appcontainer_without_capabilities_cannot_reach_loopback() -> None:
    curl = Path(os.environ["SYSTEMROOT"]) / "System32" / "curl.exe"
    if not curl.exists():
        pytest.skip("Windows curl.exe is unavailable")

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
    try:
        launcher = create_process_launcher(
            IsolationPolicy(
                require_windows_sandbox=True,
                require_network_isolation=True,
            )
        )
        result = launcher.run(
            command=(
                str(curl),
                "--silent",
                "--show-error",
                "--connect-timeout",
                "2",
                f"http://127.0.0.1:{port}/",
            ),
            input_frame=b"",
            cancellation=Event(),
        )
    finally:
        listener.close()
        server.join(timeout=1)

    assert launcher.mode is IsolationMode.WINDOWS_APPCONTAINER
    assert launcher.network_isolation_mode is NetworkIsolationMode.APPCONTAINER
    assert result.return_code != 0
    assert accepted == []


@pytest.mark.skipif(os.name != "nt", reason="Windows AppContainer integration test")
def test_required_network_isolation_does_not_fallback_for_invalid_worker(
    tmp_path: Path,
) -> None:
    invalid_worker = tmp_path / "worker.exe"
    invalid_worker.write_bytes(b"not-a-portable-executable")
    launcher = create_process_launcher(
        IsolationPolicy(
            require_windows_sandbox=True,
            require_network_isolation=True,
        )
    )

    with pytest.raises(ProcessIsolationError):
        launcher.validate_command((str(invalid_worker),))

    assert launcher.mode is IsolationMode.WINDOWS_APPCONTAINER
    assert launcher.network_isolation_mode is NetworkIsolationMode.APPCONTAINER


@pytest.mark.skipif(os.name != "nt", reason="Windows AppContainer integration test")
def test_startup_reaps_a_profile_left_by_an_abrupt_runner_exit(
    tmp_path: Path,
) -> None:
    from deskpilot.runner import windows_sandbox

    journal = AppContainerProfileJournal(tmp_path / "profiles.json")
    profile = windows_sandbox._create_appcontainer_profile(journal)
    profile_name = profile.name
    profile_folder = Path(profile.local_app_data)
    windows_sandbox.advapi32.FreeSid(profile.sid)
    assert journal.snapshot() == (profile_name,)

    try:
        launcher = create_process_launcher(
            IsolationPolicy(
                require_windows_sandbox=True,
                require_network_isolation=True,
                appcontainer_profile_journal_path=str(journal.path),
            )
        )
        assert launcher.mode is IsolationMode.WINDOWS_APPCONTAINER
        assert journal.snapshot() == ()
        assert not profile_folder.exists()
    finally:
        windows_sandbox._delete_appcontainer_profile_name(profile_name)
