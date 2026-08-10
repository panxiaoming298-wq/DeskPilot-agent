"""Durable, secret-free evidence emitted by brokered Tool commits."""

import re
from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from deskpilot.domain.tool_contracts import SEMVER_PATTERN, TOOL_NAME_PATTERN

SHA256_PATTERN = r"^[0-9a-f]{64}$"
VERSION_VALUE_PATTERN = r"^(?:[0-9a-f]{64}|absent)$"


def _ensure_timezone_aware(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError("Commit receipt timestamps must be timezone-aware")
    return value


AwareDateTime = Annotated[datetime, AfterValidator(_ensure_timezone_aware)]


class ToolCommitReceipt(BaseModel):
    """A committed external effect bound to one exact authorization and prepare."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    receipt_id: str = Field(pattern=r"^cmt_[0-9a-f]{64}$")
    call_id: str = Field(min_length=1, max_length=128)
    tool_name: str = Field(pattern=TOOL_NAME_PATTERN)
    tool_version: str = Field(pattern=SEMVER_PATTERN)
    status: Literal["committed"] = "committed"
    authorization_id: str = Field(pattern=r"^auth_[0-9a-f]{64}$")
    approval_id: str = Field(min_length=1, max_length=128)
    preview_hash: str = Field(pattern=SHA256_PATTERN)
    prepare_digest: str = Field(pattern=SHA256_PATTERN)
    idempotency_key_digest: str = Field(pattern=SHA256_PATTERN)
    resource_versions_before: dict[str, str]
    resource_versions_after: dict[str, str]
    commit_started_at: AwareDateTime
    receipt_recorded_at: AwareDateTime

    @model_validator(mode="after")
    def validate_versions_and_time(self) -> Self:
        expected_keys = {"destination", "source"}
        if set(self.resource_versions_before) != expected_keys:
            raise ValueError("Commit receipt before-versions must cover source and destination")
        if set(self.resource_versions_after) != expected_keys:
            raise ValueError("Commit receipt after-versions must cover source and destination")
        for versions in (
            self.resource_versions_before,
            self.resource_versions_after,
        ):
            if any(
                re.fullmatch(VERSION_VALUE_PATTERN, value) is None
                for value in versions.values()
            ):
                raise ValueError("Commit receipt resource version is invalid")
        if self.resource_versions_before["destination"] != "absent":
            raise ValueError("file.move requires an absent destination before commit")
        if self.resource_versions_after["source"] != "absent":
            raise ValueError("file.move requires an absent source after commit")
        if (
            self.resource_versions_before["source"]
            != self.resource_versions_after["destination"]
        ):
            raise ValueError("file.move must preserve the external file version")
        if self.receipt_recorded_at < self.commit_started_at:
            raise ValueError("Commit receipt cannot precede its commit boundary")
        return self


__all__ = ["ToolCommitReceipt"]
