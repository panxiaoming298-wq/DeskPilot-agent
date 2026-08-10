"""Trusted parent-side resource preparation for isolated Tool workers."""

import os
import shutil
from pathlib import Path

from deskpilot.runner.authorization import AuthorizedToolCall
from deskpilot.runner.worker_protocol import (
    BrokeredFileMove,
    BrokeredFilesystemMetadata,
    BrokeredResource,
)

FILESYSTEM_METADATA_CAPABILITY = "filesystem.metadata.read"
FILE_MOVE_CAPABILITIES = (
    "filesystem.file.move_destination",
    "filesystem.file.move_source",
)


class ResourceBrokerError(RuntimeError):
    code = "TOOL_RESOURCE_BROKER_FAILED"


class ResourceBrokerUnavailableError(ResourceBrokerError):
    code = "TOOL_CAPABILITY_BROKER_UNAVAILABLE"


class ResourceBrokerScopeError(ResourceBrokerError):
    code = "TOOL_RESOURCE_SCOPE_MISMATCH"


class ToolResourceBroker:
    """Turns signed resource scope into immutable facts before worker launch."""

    def prepare(
        self,
        call: AuthorizedToolCall,
    ) -> tuple[BrokeredResource, ...]:
        contract = call.registration.contract
        capabilities = tuple(sorted(set(contract.security.capabilities)))
        if not capabilities:
            return ()
        if capabilities == FILE_MOVE_CAPABILITIES:
            return (prepare_brokered_file_move(call),)
        if capabilities != (FILESYSTEM_METADATA_CAPABILITY,):
            raise ResourceBrokerUnavailableError(
                "No trusted resource broker is registered for this Tool capability"
            )
        if contract.key != ("computer.disk_usage", "1.0.0"):
            raise ResourceBrokerUnavailableError(
                "The filesystem metadata broker is not registered for this Tool"
            )

        resources = call.request.authorization.resources
        if len(resources) != 1:
            raise ResourceBrokerScopeError(
                "Filesystem metadata requires exactly one authorized resource"
            )
        resource = resources[0]
        if (
            resource.kind != "filesystem_path"
            or resource.operations != (FILESYSTEM_METADATA_CAPABILITY,)
        ):
            raise ResourceBrokerScopeError(
                "Authorized resource does not match the filesystem metadata capability"
            )
        return (read_brokered_filesystem_metadata(resource.identifier),)


def read_brokered_filesystem_metadata(identifier: str) -> BrokeredFilesystemMetadata:
    if os.name == "nt":
        from deskpilot.runner.windows_resources import read_filesystem_metadata

        return read_filesystem_metadata(identifier)

    resolved = Path(identifier).resolve(strict=True)
    descriptor = os.open(resolved, os.O_RDONLY)
    try:
        os.fstat(descriptor)
        usage = shutil.disk_usage(resolved)
    finally:
        os.close(descriptor)
    return BrokeredFilesystemMetadata(
        identifier=str(resolved),
        total_bytes=usage.total,
        used_bytes=usage.used,
        free_bytes=usage.free,
    )


def prepare_brokered_file_move(call: AuthorizedToolCall) -> BrokeredFileMove:
    """Re-read exact move versions from authorized canonical resources."""
    if call.registration.contract.key != ("file.move", "1.0.0"):
        raise ResourceBrokerUnavailableError(
            "The file.move broker is not registered for this Tool"
        )
    resources_by_operation = {
        operation: resource
        for resource in call.request.authorization.resources
        for operation in resource.operations
    }
    if set(resources_by_operation) != set(FILE_MOVE_CAPABILITIES):
        raise ResourceBrokerScopeError(
            "Authorized resources do not cover the file.move source and destination"
        )
    source = resources_by_operation["filesystem.file.move_source"]
    destination = resources_by_operation["filesystem.file.move_destination"]
    if source.kind != "filesystem_path" or destination.kind != "filesystem_path":
        raise ResourceBrokerScopeError("file.move requires filesystem_path resources")
    if source.version_digest is None or destination.version_digest is not None:
        raise ResourceBrokerScopeError("file.move resource versions have an invalid shape")
    expected_versions = call.request.expected_resource_versions
    if expected_versions != {
        "destination": "absent",
        "source": source.version_digest,
    }:
        raise ResourceBrokerScopeError(
            "file.move expected versions do not match its authorized resource scope"
        )

    from deskpilot.tools.files import (
        FileMoveDestinationConflictError,
        FileMoveValidationError,
        read_file_version,
    )

    try:
        source_version = read_file_version(source.identifier)
        destination_path = Path(destination.identifier)
        if destination_path.exists() or destination_path.is_symlink():
            raise FileMoveDestinationConflictError(
                "file.move destination exists before worker prepare"
            )
        resolved_parent = destination_path.parent.resolve(strict=True)
        if resolved_parent / destination_path.name != destination_path:
            raise FileMoveValidationError("file.move destination is no longer canonical")
    except (OSError, FileMoveValidationError) as error:
        raise ResourceBrokerScopeError("file.move resource state is unavailable") from error
    if source_version != source.version_digest:
        raise ResourceBrokerScopeError(
            "file.move source version changed after authorization"
        )
    return BrokeredFileMove(
        source_identifier=source.identifier,
        destination_identifier=destination.identifier,
        source_version=source_version,
    )
