import json
from pathlib import Path
from threading import Thread

import pytest

from deskpilot.runner.profile_journal import (
    AppContainerProfileJournal,
    ProfileJournalError,
)


def _profile(index: int) -> str:
    return f"DeskPilot.Worker.{index:032x}"


def test_profile_journal_registers_concurrently_and_reaps_exact_names(
    tmp_path: Path,
) -> None:
    journal = AppContainerProfileJournal(tmp_path / "profiles.json")
    threads = [Thread(target=journal.register, args=(_profile(index),)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert journal.snapshot() == tuple(_profile(index) for index in range(8))
    deleted: list[str] = []
    assert journal.reap(deleted.append) == tuple(_profile(index) for index in range(8))
    assert deleted == list(_profile(index) for index in range(8))
    assert journal.snapshot() == ()


def test_profile_journal_retains_failed_deletions(tmp_path: Path) -> None:
    journal = AppContainerProfileJournal(tmp_path / "profiles.json")
    journal.register(_profile(1))
    journal.register(_profile(2))

    def delete(profile_name: str) -> None:
        if profile_name == _profile(2):
            raise OSError("profile is busy")

    with pytest.raises(ProfileJournalError, match="Could not reap"):
        journal.reap(delete)

    assert journal.snapshot() == (_profile(2),)


def test_profile_journal_rejects_foreign_and_corrupt_entries(tmp_path: Path) -> None:
    path = tmp_path / "profiles.json"
    journal = AppContainerProfileJournal(path)

    with pytest.raises(ProfileJournalError, match="outside DeskPilot scope"):
        journal.register("Foreign.Product.Profile")

    path.write_text(
        json.dumps(
            {
                "schema": "deskpilot.appcontainer-profile-journal.v1",
                "profiles": [_profile(2), _profile(1)],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ProfileJournalError, match="sorted unique"):
        journal.snapshot()
