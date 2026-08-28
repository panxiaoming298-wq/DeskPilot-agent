"""Build the immutable Python Command Profile resource for desktop packaging."""

from __future__ import annotations

import json
import os
import sys
import sysconfig
from pathlib import Path

from deskpilot.application.workspace_command_runtime import COMMAND_RUNTIME_DISTRIBUTIONS
from deskpilot.runner.worker_runtime import WorkerRuntimeBundle, prepare_worker_runtime


def build_python_command_runtime_resource(root: Path) -> WorkerRuntimeBundle:
    """Publish the exact local Python/pytest/Ruff/mypy closure under ``root``."""

    if os.name != "nt":
        raise RuntimeError("The desktop Python Command Profile resource requires Windows")
    scripts = Path(sysconfig.get_path("scripts"))
    ruff_executable = scripts / "ruff.exe"
    if not ruff_executable.is_file():
        raise RuntimeError("The frozen Python Command Profile resource requires Ruff")
    return prepare_worker_runtime(
        root,
        distributions=COMMAND_RUNTIME_DISTRIBUTIONS,
        include_deskpilot=False,
        additional_executables=(ruff_executable,),
    )


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m deskpilot.runner.python_command_runtime_resource ROOT")
    bundle = build_python_command_runtime_resource(Path(sys.argv[1]))
    print(
        json.dumps(
            {
                "bundle_root": str(bundle.root),
                "digest": bundle.digest,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()


__all__ = ["build_python_command_runtime_resource"]
