"""Strict loader for the versioned, disposable Workspace Coding golden suite."""

from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError
from yaml.tokens import AliasToken, AnchorToken

from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.workspace_coding_evaluations import (
    WorkspaceCodingGoldenResilienceSuite,
    WorkspaceCodingGoldenSidecarSoakSuite,
    WorkspaceCodingGoldenSuite,
)

MAX_WORKSPACE_CODING_SUITE_BYTES = 131_072


class WorkspaceCodingEvaluationError(RuntimeError):
    code = "WORKSPACE_CODING_EVALUATION_REJECTED"


@dataclass(frozen=True, slots=True)
class WorkspaceCodingGoldenSuiteBundle:
    suite: WorkspaceCodingGoldenSuite
    suite_digest: str


@dataclass(frozen=True, slots=True)
class WorkspaceCodingGoldenResilienceSuiteBundle:
    suite: WorkspaceCodingGoldenResilienceSuite
    suite_digest: str
    workspace: WorkspaceCodingGoldenSuiteBundle


@dataclass(frozen=True, slots=True)
class WorkspaceCodingGoldenSidecarSoakSuiteBundle:
    suite: WorkspaceCodingGoldenSidecarSoakSuite
    suite_digest: str
    resilience: WorkspaceCodingGoldenResilienceSuiteBundle


def _read_strict_yaml(suite_path: Path) -> object:
    try:
        if suite_path.is_symlink() or not suite_path.is_file():
            raise WorkspaceCodingEvaluationError(
                "Workspace Coding evaluation suite must be one regular file"
            )
        payload = suite_path.read_bytes()
        if not payload or len(payload) > MAX_WORKSPACE_CODING_SUITE_BYTES:
            raise WorkspaceCodingEvaluationError(
                "Workspace Coding evaluation suite is empty or exceeds its size limit"
            )
        text = payload.decode("utf-8")
        if any(isinstance(token, AnchorToken | AliasToken) for token in yaml.scan(text)):
            raise WorkspaceCodingEvaluationError(
                "Workspace Coding evaluation suite YAML aliases are not allowed"
            )
        return yaml.safe_load(text)
    except WorkspaceCodingEvaluationError:
        raise
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise WorkspaceCodingEvaluationError(
            "Workspace Coding evaluation suite failed strict loading"
        ) from error


class WorkspaceCodingGoldenSuiteLoader:
    """Load immutable task definitions without executing or granting authority."""

    def __init__(self, suite_path: Path | None = None) -> None:
        self._suite_path = suite_path or (
            Path(__file__).parents[1] / "evaluations" / "workspace_coding_v1.yaml"
        )

    def load(self) -> WorkspaceCodingGoldenSuiteBundle:
        try:
            suite = WorkspaceCodingGoldenSuite.model_validate(
                _read_strict_yaml(self._suite_path)
            )
        except WorkspaceCodingEvaluationError:
            raise
        except ValidationError as error:
            raise WorkspaceCodingEvaluationError(
                "Workspace Coding golden suite failed strict validation"
            ) from error
        return WorkspaceCodingGoldenSuiteBundle(
            suite=suite,
            suite_digest=sha256_digest(suite.model_dump(mode="json")),
        )


class WorkspaceCodingGoldenResilienceSuiteLoader:
    """Bind the fault-injection plan to one exact golden workspace suite."""

    def __init__(
        self,
        suite_path: Path | None = None,
        *,
        workspace_loader: WorkspaceCodingGoldenSuiteLoader | None = None,
    ) -> None:
        self._suite_path = suite_path or (
            Path(__file__).parents[1]
            / "evaluations"
            / "workspace_coding_resilience_v1.yaml"
        )
        self._workspace_loader = workspace_loader or WorkspaceCodingGoldenSuiteLoader()

    def load(self) -> WorkspaceCodingGoldenResilienceSuiteBundle:
        workspace = self._workspace_loader.load()
        try:
            suite = WorkspaceCodingGoldenResilienceSuite.model_validate(
                _read_strict_yaml(self._suite_path)
            )
        except WorkspaceCodingEvaluationError:
            raise
        except ValidationError as error:
            raise WorkspaceCodingEvaluationError(
                "Workspace Coding resilience suite failed strict validation"
            ) from error
        if suite.workspace_suite_digest != workspace.suite_digest:
            raise WorkspaceCodingEvaluationError(
                "Workspace Coding resilience suite crossed its golden suite digest"
            )
        cases = {
            case.case_id: case
            for case in workspace.suite.cases
        }
        selected = cases.get(suite.scenario.workspace_case_id)
        if selected is None or selected.ecosystem != "python":
            raise WorkspaceCodingEvaluationError(
                "Workspace Coding resilience scenario requires its exact Python case"
            )
        return WorkspaceCodingGoldenResilienceSuiteBundle(
            suite=suite,
            suite_digest=sha256_digest(suite.model_dump(mode="json")),
            workspace=workspace,
        )


class WorkspaceCodingGoldenSidecarSoakSuiteLoader:
    """Bind one real-clock supervisor canary to the exact resilience suite."""

    def __init__(
        self,
        suite_path: Path | None = None,
        *,
        resilience_loader: WorkspaceCodingGoldenResilienceSuiteLoader | None = None,
    ) -> None:
        self._suite_path = suite_path or (
            Path(__file__).parents[1]
            / "evaluations"
            / "workspace_coding_sidecar_soak_v1.yaml"
        )
        self._resilience_loader = (
            resilience_loader or WorkspaceCodingGoldenResilienceSuiteLoader()
        )

    def load(self) -> WorkspaceCodingGoldenSidecarSoakSuiteBundle:
        resilience = self._resilience_loader.load()
        try:
            suite = WorkspaceCodingGoldenSidecarSoakSuite.model_validate(
                _read_strict_yaml(self._suite_path)
            )
        except WorkspaceCodingEvaluationError:
            raise
        except ValidationError as error:
            raise WorkspaceCodingEvaluationError(
                "Workspace Coding sidecar soak suite failed strict validation"
            ) from error
        scenario = suite.scenario
        bound = resilience.suite.scenario
        if suite.resilience_suite_digest != resilience.suite_digest:
            raise WorkspaceCodingEvaluationError(
                "Workspace Coding sidecar soak suite crossed its resilience digest"
            )
        if (
            scenario.resilience_scenario_id != bound.scenario_id
            or scenario.command_project_path != bound.command_project_path
            or scenario.command_profile_ids != bound.command_profile_ids
            or scenario.max_advances != bound.max_advances
            or scenario.no_automatic_replay != bound.no_automatic_replay
        ):
            raise WorkspaceCodingEvaluationError(
                "Workspace Coding sidecar soak scenario crossed its resilience authority"
            )
        return WorkspaceCodingGoldenSidecarSoakSuiteBundle(
            suite=suite,
            suite_digest=sha256_digest(suite.model_dump(mode="json")),
            resilience=resilience,
        )
