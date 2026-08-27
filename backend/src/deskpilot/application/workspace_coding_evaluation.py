"""Strict loader for the versioned, disposable Workspace Coding golden suite."""

from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError
from yaml.tokens import AliasToken, AnchorToken

from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.workspace_coding_evaluations import WorkspaceCodingGoldenSuite

MAX_WORKSPACE_CODING_SUITE_BYTES = 131_072


class WorkspaceCodingEvaluationError(RuntimeError):
    code = "WORKSPACE_CODING_EVALUATION_REJECTED"


@dataclass(frozen=True, slots=True)
class WorkspaceCodingGoldenSuiteBundle:
    suite: WorkspaceCodingGoldenSuite
    suite_digest: str


class WorkspaceCodingGoldenSuiteLoader:
    """Load immutable task definitions without executing or granting authority."""

    def __init__(self, suite_path: Path | None = None) -> None:
        self._suite_path = suite_path or (
            Path(__file__).parents[1] / "evaluations" / "workspace_coding_v1.yaml"
        )

    def load(self) -> WorkspaceCodingGoldenSuiteBundle:
        try:
            if self._suite_path.is_symlink() or not self._suite_path.is_file():
                raise WorkspaceCodingEvaluationError(
                    "Workspace Coding golden suite must be one regular file"
                )
            payload = self._suite_path.read_bytes()
            if not payload or len(payload) > MAX_WORKSPACE_CODING_SUITE_BYTES:
                raise WorkspaceCodingEvaluationError(
                    "Workspace Coding golden suite is empty or exceeds its size limit"
                )
            text = payload.decode("utf-8")
            if any(isinstance(token, AnchorToken | AliasToken) for token in yaml.scan(text)):
                raise WorkspaceCodingEvaluationError(
                    "Workspace Coding golden suite YAML aliases are not allowed"
                )
            suite = WorkspaceCodingGoldenSuite.model_validate(yaml.safe_load(text))
        except WorkspaceCodingEvaluationError:
            raise
        except (OSError, UnicodeDecodeError, yaml.YAMLError, ValidationError) as error:
            raise WorkspaceCodingEvaluationError(
                "Workspace Coding golden suite failed strict validation"
            ) from error
        return WorkspaceCodingGoldenSuiteBundle(
            suite=suite,
            suite_digest=sha256_digest(suite.model_dump(mode="json")),
        )
