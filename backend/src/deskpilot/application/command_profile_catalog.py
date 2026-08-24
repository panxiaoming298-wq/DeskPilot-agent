"""Server-owned command profiles; callers never provide process fields."""

from typing import cast

from deskpilot.domain.command_profiles import CommandProfile, CommandProfileId


class CommandProfileNotFoundError(LookupError):
    code = "COMMAND_PROFILE_NOT_FOUND"


class CommandProfileCatalog:
    def __init__(self) -> None:
        profiles = (
            self._profile("python.pytest.v1", "python", "test", 180, 1, "bundled"),
            self._profile("python.ruff.v1", "python", "lint", 120, 1, "bundled"),
            self._profile("python.mypy.v1", "python", "type_check", 180, 1, "bundled"),
            self._profile(
                "node.pnpm_test.v1", "node", "test", 180, 8, "offline_frozen"
            ),
            self._profile(
                "node.pnpm_typecheck.v1",
                "node",
                "type_check",
                180,
                8,
                "offline_frozen",
            ),
            self._profile(
                "node.pnpm_build.v1", "node", "build", 300, 8, "offline_frozen"
            ),
        )
        self._profiles = {item.command_profile_id: item for item in profiles}

    @staticmethod
    def _profile(
        command_profile_id: CommandProfileId,
        ecosystem: str,
        operation: str,
        timeout_seconds: int,
        max_processes: int,
        dependency_mode: str,
    ) -> CommandProfile:
        return CommandProfile.build(
            command_profile_id=command_profile_id,
            version="1.0.0",
            ecosystem=ecosystem,
            operation=operation,
            timeout_seconds=timeout_seconds,
            max_output_bytes=65_536,
            max_processes=max_processes,
            network_access=False,
            temporary_snapshot=True,
            model_selects_only_profile_id=True,
            caller_supplies_process_fields=False,
            dependency_mode=dependency_mode,
        )

    def resolve(self, command_profile_id: str) -> CommandProfile:
        try:
            return self._profiles[cast(CommandProfileId, command_profile_id)]
        except KeyError as error:
            raise CommandProfileNotFoundError("Command Profile is not registered") from error

    def list(self) -> tuple[CommandProfile, ...]:
        return tuple(self._profiles[key] for key in sorted(self._profiles))

    def ids(self) -> tuple[CommandProfileId, ...]:
        return tuple(item.command_profile_id for item in self.list())


__all__ = ["CommandProfileCatalog", "CommandProfileNotFoundError"]
