"""Terminal-local state schema model; persistence is intentionally deferred."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from grim_dawn_sync.errors import SyncError


STATE_SCHEMA_VERSION = "1.0.0"
STATE_KEYS = frozenset(
    {"schema_version", "last_applied_remote_commit", "last_applied_manifest_root_hash", "session_id"}
)


@dataclass(frozen=True)
class SyncState:
    schema_version: str = STATE_SCHEMA_VERSION
    last_applied_remote_commit: str | None = None
    last_applied_manifest_root_hash: str | None = None
    session_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_state(payload: dict[str, Any]) -> SyncState:
    if not isinstance(payload, dict) or payload.get("schema_version") != STATE_SCHEMA_VERSION:
        raise SyncError("invalid_state", f"state schema_version must be {STATE_SCHEMA_VERSION}.")
    if set(payload) != STATE_KEYS:
        raise SyncError("invalid_state", "State must contain exactly the required schema fields.")
    values = {key: payload.get(key) for key in ("last_applied_remote_commit", "last_applied_manifest_root_hash", "session_id")}
    if any(value is not None and (not isinstance(value, str) or not value) for value in values.values()):
        raise SyncError("invalid_state", "Optional state identifiers must be non-empty strings or null.")
    return SyncState(**values)
