"""Executable entry point for the independent DeskPilot Tool Runner."""

from deskpilot.runner.server import run_server_from_stdio
from deskpilot.tools import create_builtin_executor


def main() -> int:
    return run_server_from_stdio(
        create_builtin_executor(),
        worker_factory="deskpilot.tools:create_builtin_executor",
    )


if __name__ == "__main__":
    raise SystemExit(main())
