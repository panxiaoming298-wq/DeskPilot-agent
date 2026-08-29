"""Manifest and local-mirror preflight for the offline-only 116C-A task suite."""

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from deskpilot.application.workspace_repository_evaluation import (
    WorkspaceRepositoryEvaluationError,
    WorkspaceRepositoryOfflinePreflight,
    WorkspaceRepositoryTaskSuiteLoader,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("manifest")
    preflight = commands.add_parser("preflight")
    preflight.add_argument("--mirror-root", type=Path, required=True)
    preflight.add_argument("--git-executable")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        bundle = WorkspaceRepositoryTaskSuiteLoader().load()
        if arguments.command == "manifest":
            suite = bundle.suite
            print(
                json.dumps(
                    {
                        "suite_id": suite.suite_id,
                        "suite_digest": bundle.suite_digest,
                        "repository_count": len(suite.repositories),
                        "task_count": len(suite.tasks),
                        "trial_count": (
                            len(suite.tasks) * suite.thresholds.repetitions_per_task
                        ),
                        "minimum_successful_trials": (
                            suite.thresholds.minimum_successful_trials
                        ),
                        "false_success_maximum": suite.thresholds.false_success_maximum,
                        "unauthorized_effect_maximum": (
                            suite.thresholds.unauthorized_effect_maximum
                        ),
                        "offline_boundary": suite.offline_boundary.model_dump(mode="json"),
                    },
                    sort_keys=True,
                )
            )
            return 0
        report = WorkspaceRepositoryOfflinePreflight(
            bundle,
            arguments.mirror_root,
            git_executable=arguments.git_executable,
        ).run()
        print(json.dumps(report.model_dump(mode="json"), sort_keys=True))
        return 0
    except WorkspaceRepositoryEvaluationError as error:
        print(json.dumps({"code": error.code, "detail": str(error)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
