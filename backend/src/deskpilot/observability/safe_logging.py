"""Logging filter for safe diagnostic records only."""

import logging
import re

_SAFE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")


class SafeTelemetryLoggingFilter(logging.Filter):
    """Reject records that are not explicitly marked as content-free telemetry."""

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "telemetry_safe", False) is not True:
            return False
        code = getattr(record, "error_code", None)
        return code is None or (isinstance(code, str) and _SAFE_CODE.fullmatch(code) is not None)
