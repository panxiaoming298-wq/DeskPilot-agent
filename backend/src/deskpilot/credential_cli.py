"""Safe local CLI for DeskPilot's Windows Credential Manager entries."""

import argparse
import getpass
import sys
from collections.abc import Callable, Sequence
from typing import TextIO

from pydantic import SecretStr, ValidationError

from deskpilot.application.credential_resolver import (
    CredentialNotFoundError,
    CredentialResolutionError,
    ManagedCredentialStore,
)
from deskpilot.domain.provider_config import CredentialReference
from deskpilot.infrastructure.windows_credentials import WindowsCredentialManager


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m deskpilot.credential_cli",
        description=(
            "Manage DeskPilot Provider secrets in the current user's "
            "Windows Credential Manager."
        ),
    )
    parser.add_argument("action", choices=("store", "status", "delete"))
    parser.add_argument(
        "identifier",
        help="Uppercase DeskPilot identifier, for example CLOUD_CHAT.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Required confirmation for delete.",
    )
    return parser


def run(
    argv: Sequence[str],
    *,
    manager: ManagedCredentialStore | None = None,
    secret_reader: Callable[[str], str] = getpass.getpass,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    arguments = _parser().parse_args(list(argv))
    try:
        reference = CredentialReference(
            backend="windows_credential_manager",
            identifier=arguments.identifier,
        )
    except ValidationError:
        print("CREDENTIAL_REFERENCE_INVALID", file=stderr)
        return 2

    try:
        resolved_manager = manager or WindowsCredentialManager()
    except OSError:
        print("CREDENTIAL_BACKEND_UNAVAILABLE", file=stderr)
        return 2

    try:
        if arguments.action == "store":
            first = secret_reader("Provider secret: ")
            second = secret_reader("Confirm secret: ")
            if first != second:
                print("CREDENTIAL_CONFIRMATION_MISMATCH", file=stderr)
                return 2
            resolved_manager.store(reference, SecretStr(first))
            print("Credential stored.", file=stdout)
            return 0

        if arguments.action == "status":
            try:
                resolved_manager.resolve(reference)
            except CredentialNotFoundError:
                print("Credential is not available.", file=stdout)
                return 1
            print("Credential is available.", file=stdout)
            return 0

        if not arguments.yes:
            print("CREDENTIAL_DELETE_CONFIRMATION_REQUIRED", file=stderr)
            return 2
        deleted = resolved_manager.delete(reference)
        print(
            "Credential deleted." if deleted else "Credential was already absent.",
            file=stdout,
        )
        return 0
    except CredentialResolutionError as error:
        print(error.code, file=stderr)
        return 1


def main() -> int:
    return run(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
