"""Strict loader for the versioned, disposable Workspace Coding golden suite."""

from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError
from yaml.tokens import AliasToken, AnchorToken

from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.workspace_coding_evaluations import (
    WorkspaceCodingGoldenConcurrencySuite,
    WorkspaceCodingGoldenFrozenCommandRecoverySuite,
    WorkspaceCodingGoldenFrozenCommandTaskSuite,
    WorkspaceCodingGoldenFrozenConcurrencySuite,
    WorkspaceCodingGoldenFrozenReleaseSoakSuite,
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


@dataclass(frozen=True, slots=True)
class WorkspaceCodingGoldenConcurrencySuiteBundle:
    suite: WorkspaceCodingGoldenConcurrencySuite
    suite_digest: str
    sidecar: WorkspaceCodingGoldenSidecarSoakSuiteBundle


@dataclass(frozen=True, slots=True)
class WorkspaceCodingGoldenFrozenReleaseSoakSuiteBundle:
    suite: WorkspaceCodingGoldenFrozenReleaseSoakSuite
    suite_digest: str
    concurrency: WorkspaceCodingGoldenConcurrencySuiteBundle


@dataclass(frozen=True, slots=True)
class WorkspaceCodingGoldenFrozenCommandTaskSuiteBundle:
    suite: WorkspaceCodingGoldenFrozenCommandTaskSuite
    suite_digest: str
    frozen_release: WorkspaceCodingGoldenFrozenReleaseSoakSuiteBundle


@dataclass(frozen=True, slots=True)
class WorkspaceCodingGoldenFrozenCommandRecoverySuiteBundle:
    suite: WorkspaceCodingGoldenFrozenCommandRecoverySuite
    suite_digest: str
    frozen_command: WorkspaceCodingGoldenFrozenCommandTaskSuiteBundle


@dataclass(frozen=True, slots=True)
class WorkspaceCodingGoldenFrozenConcurrencySuiteBundle:
    suite: WorkspaceCodingGoldenFrozenConcurrencySuite
    suite_digest: str
    frozen_command_recovery: WorkspaceCodingGoldenFrozenCommandRecoverySuiteBundle


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


class WorkspaceCodingGoldenConcurrencySuiteLoader:
    """Bind one bounded scheduler canary to the exact sidecar soak contract."""

    def __init__(
        self,
        suite_path: Path | None = None,
        *,
        sidecar_loader: WorkspaceCodingGoldenSidecarSoakSuiteLoader | None = None,
    ) -> None:
        self._suite_path = suite_path or (
            Path(__file__).parents[1] / "evaluations" / "workspace_coding_concurrency_v1.yaml"
        )
        self._sidecar_loader = sidecar_loader or WorkspaceCodingGoldenSidecarSoakSuiteLoader()

    def load(self) -> WorkspaceCodingGoldenConcurrencySuiteBundle:
        sidecar = self._sidecar_loader.load()
        try:
            suite = WorkspaceCodingGoldenConcurrencySuite.model_validate(
                _read_strict_yaml(self._suite_path)
            )
        except WorkspaceCodingEvaluationError:
            raise
        except ValidationError as error:
            raise WorkspaceCodingEvaluationError(
                "Workspace Coding concurrency suite failed strict validation"
            ) from error
        scenario = suite.scenario
        bound = sidecar.suite.scenario
        if suite.sidecar_suite_digest != sidecar.suite_digest:
            raise WorkspaceCodingEvaluationError(
                "Workspace Coding concurrency suite crossed its sidecar digest"
            )
        if (
            scenario.no_automatic_replay != bound.no_automatic_replay
            or scenario.max_advances != bound.max_advances
        ):
            raise WorkspaceCodingEvaluationError(
                "Workspace Coding concurrency scenario crossed its sidecar safety contract"
            )
        return WorkspaceCodingGoldenConcurrencySuiteBundle(
            suite=suite,
            suite_digest=sha256_digest(suite.model_dump(mode="json")),
            sidecar=sidecar,
        )


class WorkspaceCodingGoldenFrozenReleaseSoakSuiteLoader:
    """Bind an opt-in installed-artifact canary to the exact concurrency suite."""

    def __init__(
        self,
        suite_path: Path | None = None,
        *,
        concurrency_loader: WorkspaceCodingGoldenConcurrencySuiteLoader | None = None,
    ) -> None:
        self._suite_path = suite_path or (
            Path(__file__).parents[1]
            / "evaluations"
            / "workspace_coding_frozen_release_soak_v1.yaml"
        )
        self._concurrency_loader = (
            concurrency_loader or WorkspaceCodingGoldenConcurrencySuiteLoader()
        )

    def load(self) -> WorkspaceCodingGoldenFrozenReleaseSoakSuiteBundle:
        concurrency = self._concurrency_loader.load()
        try:
            suite = WorkspaceCodingGoldenFrozenReleaseSoakSuite.model_validate(
                _read_strict_yaml(self._suite_path)
            )
        except WorkspaceCodingEvaluationError:
            raise
        except ValidationError as error:
            raise WorkspaceCodingEvaluationError(
                "Workspace Coding frozen release soak suite failed strict validation"
            ) from error
        scenario = suite.scenario
        bound = concurrency.suite.scenario
        if suite.concurrency_suite_digest != concurrency.suite_digest:
            raise WorkspaceCodingEvaluationError(
                "Workspace Coding frozen release soak suite crossed its concurrency digest"
            )
        if (
            scenario.concurrency_scenario_id != bound.scenario_id
            or scenario.no_automatic_replay != bound.no_automatic_replay
        ):
            raise WorkspaceCodingEvaluationError(
                "Workspace Coding frozen release soak scenario crossed "
                "its concurrency safety contract"
            )
        return WorkspaceCodingGoldenFrozenReleaseSoakSuiteBundle(
            suite=suite,
            suite_digest=sha256_digest(suite.model_dump(mode="json")),
            concurrency=concurrency,
        )


class WorkspaceCodingGoldenFrozenCommandTaskSuiteLoader:
    """Bind one installed real-Profile interruption to the exact release cohort."""

    def __init__(
        self,
        suite_path: Path | None = None,
        *,
        frozen_release_loader: WorkspaceCodingGoldenFrozenReleaseSoakSuiteLoader
        | None = None,
    ) -> None:
        self._suite_path = suite_path or (
            Path(__file__).parents[1]
            / "evaluations"
            / "workspace_coding_frozen_command_task_v1.yaml"
        )
        self._frozen_release_loader = (
            frozen_release_loader or WorkspaceCodingGoldenFrozenReleaseSoakSuiteLoader()
        )

    def load(self) -> WorkspaceCodingGoldenFrozenCommandTaskSuiteBundle:
        frozen_release = self._frozen_release_loader.load()
        try:
            suite = WorkspaceCodingGoldenFrozenCommandTaskSuite.model_validate(
                _read_strict_yaml(self._suite_path)
            )
        except WorkspaceCodingEvaluationError:
            raise
        except ValidationError as error:
            raise WorkspaceCodingEvaluationError(
                "Workspace Coding frozen command task suite failed strict validation"
            ) from error
        scenario = suite.scenario
        bound = frozen_release.suite.scenario
        if suite.frozen_release_suite_digest != frozen_release.suite_digest:
            raise WorkspaceCodingEvaluationError(
                "Workspace Coding frozen command task suite crossed its release digest"
            )
        if (
            scenario.frozen_release_scenario_id != bound.scenario_id
            or scenario.no_automatic_replay != bound.no_automatic_replay
            or not bound.health_only_canary
            or bound.replays_command_tasks
        ):
            raise WorkspaceCodingEvaluationError(
                "Workspace Coding frozen command task crossed its release safety boundary"
            )
        return WorkspaceCodingGoldenFrozenCommandTaskSuiteBundle(
            suite=suite,
            suite_digest=sha256_digest(suite.model_dump(mode="json")),
            frozen_release=frozen_release,
        )


class WorkspaceCodingGoldenFrozenCommandRecoverySuiteLoader:
    """Bind installed between-step recovery to the exact interruption cohort."""

    def __init__(
        self,
        suite_path: Path | None = None,
        *,
        frozen_command_loader: WorkspaceCodingGoldenFrozenCommandTaskSuiteLoader
        | None = None,
    ) -> None:
        self._suite_path = suite_path or (
            Path(__file__).parents[1]
            / "evaluations"
            / "workspace_coding_frozen_command_recovery_v1.yaml"
        )
        self._frozen_command_loader = (
            frozen_command_loader or WorkspaceCodingGoldenFrozenCommandTaskSuiteLoader()
        )

    def load(self) -> WorkspaceCodingGoldenFrozenCommandRecoverySuiteBundle:
        frozen_command = self._frozen_command_loader.load()
        try:
            suite = WorkspaceCodingGoldenFrozenCommandRecoverySuite.model_validate(
                _read_strict_yaml(self._suite_path)
            )
        except WorkspaceCodingEvaluationError:
            raise
        except ValidationError as error:
            raise WorkspaceCodingEvaluationError(
                "Workspace Coding frozen command recovery suite failed strict validation"
            ) from error
        scenario = suite.scenario
        bound = frozen_command.suite.scenario
        if suite.frozen_command_suite_digest != frozen_command.suite_digest:
            raise WorkspaceCodingEvaluationError(
                "Workspace Coding frozen command recovery crossed its command suite digest"
            )
        if (
            scenario.frozen_command_scenario_id != bound.scenario_id
            or scenario.command_profile_ids != bound.command_profile_ids
            or scenario.bundled_runtime_directory != bound.bundled_runtime_directory
            or scenario.bundled_runtime_digest != bound.bundled_runtime_digest
            or scenario.expected_isolation_mode != bound.expected_isolation_mode
            or scenario.production_fake_provider_unsupported
            != bound.production_fake_provider_unsupported
            or scenario.no_automatic_replay != bound.no_automatic_replay
        ):
            raise WorkspaceCodingEvaluationError(
                "Workspace Coding frozen command recovery crossed its command safety boundary"
            )
        return WorkspaceCodingGoldenFrozenCommandRecoverySuiteBundle(
            suite=suite,
            suite_digest=sha256_digest(suite.model_dump(mode="json")),
            frozen_command=frozen_command,
        )


class WorkspaceCodingGoldenFrozenConcurrencySuiteLoader:
    """Bind installed fair concurrency to the exact between-step recovery cohort."""

    def __init__(
        self,
        suite_path: Path | None = None,
        *,
        frozen_command_recovery_loader: WorkspaceCodingGoldenFrozenCommandRecoverySuiteLoader
        | None = None,
    ) -> None:
        self._suite_path = suite_path or (
            Path(__file__).parents[1]
            / "evaluations"
            / "workspace_coding_frozen_concurrency_v1.yaml"
        )
        self._frozen_command_recovery_loader = (
            frozen_command_recovery_loader
            or WorkspaceCodingGoldenFrozenCommandRecoverySuiteLoader()
        )

    def load(self) -> WorkspaceCodingGoldenFrozenConcurrencySuiteBundle:
        recovery = self._frozen_command_recovery_loader.load()
        try:
            suite = WorkspaceCodingGoldenFrozenConcurrencySuite.model_validate(
                _read_strict_yaml(self._suite_path)
            )
        except WorkspaceCodingEvaluationError:
            raise
        except ValidationError as error:
            raise WorkspaceCodingEvaluationError(
                "Workspace Coding frozen concurrency suite failed strict validation"
            ) from error
        scenario = suite.scenario
        bound = recovery.suite.scenario
        if (
            suite.frozen_command_recovery_suite_digest
            != recovery.suite_digest
        ):
            raise WorkspaceCodingEvaluationError(
                "Workspace Coding frozen concurrency crossed its recovery suite digest"
            )
        if (
            scenario.frozen_command_recovery_scenario_id != bound.scenario_id
            or any(
                item.command_profile_ids != bound.command_profile_ids
                for item in scenario.repositories
            )
            or scenario.bundled_runtime_directory != bound.bundled_runtime_directory
            or scenario.bundled_runtime_digest != bound.bundled_runtime_digest
            or scenario.production_fake_provider_unsupported
            != bound.production_fake_provider_unsupported
            or scenario.no_automatic_replay != bound.no_automatic_replay
            or not bound.reaches_final_delivery
        ):
            raise WorkspaceCodingEvaluationError(
                "Workspace Coding frozen concurrency crossed its recovery safety boundary"
            )
        return WorkspaceCodingGoldenFrozenConcurrencySuiteBundle(
            suite=suite,
            suite_digest=sha256_digest(suite.model_dump(mode="json")),
            frozen_command_recovery=recovery,
        )
