"""Immutable Agent release manifests, activation channels and audit events."""

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_contracts import AGENT_ID_PATTERN, DIGEST_PATTERN
from deskpilot.domain.tool_contracts import SEMVER_PATTERN

RELEASE_ID_PATTERN = r"^arl_[0-9a-f]{64}$"
RELEASE_EVENT_ID_PATTERN = r"^are_[0-9a-f]{64}$"
RELEASE_CHANNEL_PATTERN = r"^[a-z][a-z0-9_-]{0,31}$"
REQUIRED_RELEASE_ROLES = (
    "turn_planner",
    "dynamic_coordinator",
    "patch_planner",
)
MAX_AGENT_RELEASE_VALIDITY = timedelta(days=90)


class AgentReleaseEventKind(StrEnum):
    REGISTERED = "registered"
    ACTIVATED = "activated"
    DISABLED = "disabled"
    EXPIRED = "expired"
    REPLACED = "replaced"
    ROLLED_BACK = "rolled_back"


class AgentReleaseIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    agent_id: str = Field(pattern=AGENT_ID_PATTERN)
    agent_version: str = Field(pattern=SEMVER_PATTERN)
    agent_contract_digest: str = Field(pattern=DIGEST_PATTERN)
    prompt_package_digest: str = Field(pattern=DIGEST_PATTERN)

    @property
    def key(self) -> tuple[str, str]:
        return self.agent_id, self.agent_version


class AgentReleaseManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.agent-release-manifest.v1"]
    release_id: str = Field(pattern=RELEASE_ID_PATTERN)
    build_id: str = Field(min_length=1, max_length=200)
    cohort: tuple[AgentReleaseIdentity, ...] = Field(min_length=3, max_length=3)
    companions: tuple[AgentReleaseIdentity, ...] = Field(default=(), max_length=8)
    created_at: datetime
    valid_until: datetime
    supersedes_release_id: str | None = Field(default=None, pattern=RELEASE_ID_PATTERN)
    release_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def identity_and_digest_match(self) -> Self:
        if self.created_at.tzinfo is None or self.valid_until.tzinfo is None:
            raise ValueError("Agent release timestamps must be timezone-aware")
        if (
            self.valid_until <= self.created_at
            or self.valid_until - self.created_at > MAX_AGENT_RELEASE_VALIDITY
        ):
            raise ValueError("Agent release validity must be within 90 days")
        if tuple(item.role for item in self.cohort) != REQUIRED_RELEASE_ROLES:
            raise ValueError("Agent release cohort must contain the ordered three roles")
        identities = (*self.cohort, *self.companions)
        if len({item.key for item in identities}) != len(identities):
            raise ValueError("Agent release identities must be unique")
        if len({item.role for item in identities}) != len(identities):
            raise ValueError("Agent release roles must be unique")
        identity_material = {
            "build_id": self.build_id,
            "cohort": self.cohort,
            "companions": self.companions,
            "created_at": self.created_at,
            "valid_until": self.valid_until,
            "supersedes_release_id": self.supersedes_release_id,
        }
        if self.release_id != f"arl_{sha256_digest(identity_material)}":
            raise ValueError("Agent release ID does not match its identity")
        material = self.model_dump(mode="python", exclude={"release_digest"})
        if self.release_digest != sha256_digest(material):
            raise ValueError("Agent release digest does not match")
        return self


class AgentReleaseEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.agent-release-event.v1"]
    event_id: str = Field(pattern=RELEASE_EVENT_ID_PATTERN)
    sequence: int = Field(ge=1)
    channel: str = Field(pattern=RELEASE_CHANNEL_PATTERN)
    kind: AgentReleaseEventKind
    release_id: str = Field(pattern=RELEASE_ID_PATTERN)
    related_release_id: str | None = Field(default=None, pattern=RELEASE_ID_PATTERN)
    actor: str = Field(min_length=1, max_length=100)
    created_at: datetime
    previous_event_digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    event_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def event_identity_matches(self) -> Self:
        if self.created_at.tzinfo is None:
            raise ValueError("Agent release event timestamp must be timezone-aware")
        if (self.sequence == 1) != (self.previous_event_digest is None):
            raise ValueError("Agent release event chain boundary is invalid")
        requires_related = self.kind in {
            AgentReleaseEventKind.REPLACED,
            AgentReleaseEventKind.ROLLED_BACK,
        }
        if requires_related != (self.related_release_id is not None):
            raise ValueError("Agent release event related release binding is invalid")
        identity = {
            "sequence": self.sequence,
            "channel": self.channel,
            "kind": self.kind,
            "release_id": self.release_id,
            "related_release_id": self.related_release_id,
            "actor": self.actor,
            "created_at": self.created_at,
            "previous_event_digest": self.previous_event_digest,
        }
        if self.event_id != f"are_{sha256_digest(identity)}":
            raise ValueError("Agent release event ID does not match its identity")
        material = self.model_dump(mode="python", exclude={"event_digest"})
        if self.event_digest != sha256_digest(material):
            raise ValueError("Agent release event digest does not match")
        return self


class AgentActivationChannel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.agent-activation-channel.v1"]
    channel: str = Field(pattern=RELEASE_CHANNEL_PATTERN)
    revision: int = Field(ge=0)
    active_release_id: str | None = Field(default=None, pattern=RELEASE_ID_PATTERN)
    last_event_digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    channel_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def digest_matches(self) -> Self:
        if (self.revision == 0) != (self.last_event_digest is None):
            raise ValueError("Agent activation channel revision boundary is invalid")
        material = self.model_dump(mode="python", exclude={"channel_digest"})
        if self.channel_digest != sha256_digest(material):
            raise ValueError("Agent activation channel digest does not match")
        return self


class AgentReleaseBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.agent-release-bundle.v1"]
    releases: tuple[AgentReleaseManifest, ...] = Field(default=(), max_length=32)
    events: tuple[AgentReleaseEvent, ...] = Field(default=(), max_length=512)
    activation: AgentActivationChannel
    bundle_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def bundle_digest_matches(self) -> Self:
        if len({item.release_id for item in self.releases}) != len(self.releases):
            raise ValueError("Agent release bundle contains duplicate releases")
        if len({item.event_id for item in self.events}) != len(self.events):
            raise ValueError("Agent release bundle contains duplicate events")
        material = self.model_dump(mode="python", exclude={"bundle_digest"})
        if self.bundle_digest != sha256_digest(material):
            raise ValueError("Agent release bundle digest does not match")
        return self
