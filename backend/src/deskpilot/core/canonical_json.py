"""Canonical JSON helpers used by signed local protocols."""

import hashlib
import json
from typing import Any

from pydantic import BaseModel


def canonical_json_bytes(value: BaseModel | dict[str, Any]) -> bytes:
    serializable = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    return json.dumps(
        serializable,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_digest(value: BaseModel | dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()
