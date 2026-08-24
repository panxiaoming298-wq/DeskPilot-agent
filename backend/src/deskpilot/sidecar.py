"""Frozen entry point for the supervised DeskPilot desktop backend."""

import multiprocessing
import runpy
import sys
from pathlib import Path

import uvicorn

from deskpilot.main import app


def _run_frozen_subprocess() -> bool:
    allowed_modules = {
        "deskpilot.runner.service",
        "deskpilot.runner.worker",
    }
    module = sys.argv[2] if sys.argv[1:2] == ["-m"] and len(sys.argv) >= 3 else None
    arguments = sys.argv[3:]
    if module not in allowed_modules:
        script = Path(sys.argv[1]).name if len(sys.argv) >= 2 else ""
        if script != "readonly_text_server.py":
            return False
        module = "deskpilot.mcp_servers.readonly_text_server"
        arguments = sys.argv[2:]
    sys.argv = [module, *arguments]
    runpy.run_module(module, run_name="__main__")
    return True


def main() -> None:
    multiprocessing.freeze_support()
    if _run_frozen_subprocess():
        return
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8000,
        access_log=False,
        server_header=False,
        workers=1,
    )


if __name__ == "__main__":
    main()
