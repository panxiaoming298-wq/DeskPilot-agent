"""Durable journal for per-invocation AppContainer profile cleanup."""

import json
import os
import re
from collections.abc import Callable
from pathlib import Path
from threading import RLock
from uuid import uuid4

PROFILE_JOURNAL_SCHEMA = "deskpilot.appcontainer-profile-journal.v1"
PROFILE_NAME_PATTERN = re.compile(r"^DeskPilot\.Worker\.[0-9a-f]{32}$")
MAX_JOURNALED_PROFILES = 1_024


class ProfileJournalError(RuntimeError):
    """The durable AppContainer cleanup journal is invalid or unavailable."""


class AppContainerProfileJournal:
    """Tracks exact profile monikers so a replacement Runner can reap them."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve(strict=False)
        self._lock = RLock()

    def register(self, profile_name: str) -> None:
        self._validate_name(profile_name)
        with self._lock:
            profiles = set(self._load_unlocked())
            profiles.add(profile_name)
            if len(profiles) > MAX_JOURNALED_PROFILES:
                raise ProfileJournalError("AppContainer profile journal is full")
            self._write_unlocked(tuple(sorted(profiles)))

    def unregister(self, profile_name: str) -> None:
        self._validate_name(profile_name)
        with self._lock:
            profiles = set(self._load_unlocked())
            profiles.discard(profile_name)
            self._write_unlocked(tuple(sorted(profiles)))

    def reap(self, delete_profile: Callable[[str], None]) -> tuple[str, ...]:
        """Delete every previously journaled profile, retaining failed entries."""
        with self._lock:
            profiles = self._load_unlocked()
            failures: list[tuple[str, Exception]] = []
            deleted: list[str] = []
            for profile_name in profiles:
                try:
                    delete_profile(profile_name)
                except Exception as error:
                    failures.append((profile_name, error))
                else:
                    deleted.append(profile_name)
            self._write_unlocked(tuple(name for name, _ in failures))
            if failures:
                first_name, first_error = failures[0]
                raise ProfileJournalError(
                    f"Could not reap AppContainer profile {first_name}"
                ) from first_error
            return tuple(deleted)

    def snapshot(self) -> tuple[str, ...]:
        with self._lock:
            return self._load_unlocked()

    @staticmethod
    def _validate_name(profile_name: str) -> None:
        if PROFILE_NAME_PATTERN.fullmatch(profile_name) is None:
            raise ProfileJournalError("AppContainer profile name is outside DeskPilot scope")

    def _load_unlocked(self) -> tuple[str, ...]:
        if not self.path.exists():
            return ()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ProfileJournalError("AppContainer profile journal is unreadable") from error
        if not isinstance(raw, dict) or set(raw) != {"schema", "profiles"}:
            raise ProfileJournalError("AppContainer profile journal has an invalid shape")
        if raw["schema"] != PROFILE_JOURNAL_SCHEMA or not isinstance(
            raw["profiles"], list
        ):
            raise ProfileJournalError("AppContainer profile journal has an invalid schema")
        profiles = raw["profiles"]
        if len(profiles) > MAX_JOURNALED_PROFILES or any(
            not isinstance(name, str) for name in profiles
        ):
            raise ProfileJournalError("AppContainer profile journal entries are invalid")
        validated = tuple(profiles)
        for profile_name in validated:
            self._validate_name(profile_name)
        if tuple(sorted(set(validated))) != validated:
            raise ProfileJournalError(
                "AppContainer profile journal must contain sorted unique entries"
            )
        return validated

    def _write_unlocked(self, profiles: tuple[str, ...]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        payload = json.dumps(
            {"schema": PROFILE_JOURNAL_SCHEMA, "profiles": list(profiles)},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(payload)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except OSError as error:
            temporary.unlink(missing_ok=True)
            raise ProfileJournalError(
                "Could not atomically update AppContainer profile journal"
            ) from error
