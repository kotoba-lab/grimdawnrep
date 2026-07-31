"""Deterministic, short-lived capabilities for a fully verified catalog."""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import time
from typing import Any, Callable

from grim_dawn_sync.errors import EXIT_CONFLICT, EXIT_VALIDATION, SyncError


CAPABILITY_SECONDS = 300
_CAPABILITY_MILLISECONDS = CAPABILITY_SECONDS * 1000
_MAX_TIMESTAMP_MILLISECONDS = (1 << 64) - 1
_TOKEN = re.compile(r"^c2_([0-9a-f]{1,16})_([0-9a-f]{1,16})_([0-9a-f]{64})$")


def read_remote_identity(vault: Any, remote: str) -> tuple[str, str]:
    """Return one immutable fetch/push destination pair, or fail closed."""
    def urls(*args: str) -> list[str]:
        result = vault.runner.run("remote", "get-url", *args, remote, check=False)
        values = result.stdout.splitlines()
        if result.returncode or len(values) != 1 or not values[0] or "\0" in values[0]:
            raise SyncError("catalog_remote_identity_invalid", "Remote identity could not be verified.", EXIT_CONFLICT)
        return values
    fetch = urls("--all")[0]
    push = urls("--push", "--all")[0]
    if fetch != push:
        raise SyncError("catalog_remote_identity_invalid", "Fetch and push destinations must be one identical remote.", EXIT_CONFLICT)
    return fetch, push


def configuration_identity(config: Any, *, remote_identity: tuple[str, str]) -> dict[str, Any]:
    """Full internal identity; it is hashed into a token and never rendered."""
    if len(remote_identity) != 2 or any(not isinstance(value, str) or not value or "\0" in value for value in remote_identity):
        raise SyncError("catalog_remote_identity_invalid", "Remote identity could not be verified.", EXIT_CONFLICT)
    return {
        **config.public_dict(),
        "save_root_normalized": os.path.normcase(os.path.abspath(Path(config.save_root))),
        "vault_repo_normalized": os.path.normcase(os.path.abspath(Path(config.vault_repo))),
        "remote_name": config.remote,
        "remote_fetch_url": remote_identity[0],
        "remote_push_url": remote_identity[1],
        "branch": config.branch,
        "machine_id": config.machine_id,
    }


def safety_projection(config: Any, state: Any, catalog: Any, *, remote_identity: tuple[str, str] = ("test://remote", "test://remote")) -> dict[str, Any]:
    """Return the exact canonical inputs authorized by a catalog token."""
    config_payload = configuration_identity(config, remote_identity=remote_identity)
    state_payload = state.as_dict()
    candidates = [
        {
            "candidate_id": item.candidate_id,
            "kind": item.kind,
            "display_name": item.display_name,
            "created_at": item.created_at,
            "machine_id": item.machine_id,
            "root_hash": item.root_hash,
            "commit": item.commit,
            "character_count": item.character_count,
            "file_count": item.file_count,
            "total_bytes": item.total_bytes,
            "character_labels": list(item.character_labels),
            "diff_from_live": {
                "added": item.diff_from_live.added,
                "removed": item.diff_from_live.removed,
                "changed": item.diff_from_live.changed,
                "character_dirs_added": list(item.diff_from_live.character_dirs_added),
                "character_dirs_removed": list(item.diff_from_live.character_dirs_removed),
                "character_dirs_changed": list(item.diff_from_live.character_dirs_changed),
            },
            "note": item.note,
            "aliases": [
                {
                    "candidate_id": alias.candidate_id,
                    "kind": alias.kind,
                    "display_name": alias.display_name,
                    "created_at": alias.created_at,
                    "commit": alias.commit,
                    "note": alias.note,
                }
                for alias in item.aliases
            ],
        }
        for item in catalog.candidates
    ]
    return {
        "schema_version": "1.0.0",
        "config": config_payload,
        "state": state_payload,
        "lock": None,
        "remote_head": catalog.remote_head,
        "live_root_hash": catalog.live_root_hash,
        "baseline_root_hash": catalog.baseline_root_hash,
        "candidates": candidates,
    }


def canonical_bytes(projection: dict[str, Any]) -> bytes:
    try:
        return json.dumps(projection, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as error:
        raise SyncError("catalog_capability_invalid", "Verified catalog context is not canonical.", EXIT_VALIDATION) from error


def context_digest(projection: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(projection)).hexdigest()


def _milliseconds(now: float) -> int:
    if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now) or now < 0:
        raise SyncError("catalog_clock_invalid", "Catalog capability clock is invalid.", EXIT_CONFLICT)
    value = int(now * 1000)
    if value > _MAX_TIMESTAMP_MILLISECONDS:
        raise SyncError("catalog_clock_invalid", "Catalog capability clock is invalid.", EXIT_CONFLICT)
    return value


def _read_milliseconds(clock: Callable[[], float]) -> int:
    try:
        return _milliseconds(clock())
    except SyncError:
        raise
    except Exception as error:
        raise SyncError("catalog_clock_invalid", "Catalog capability clock is unavailable.", EXIT_CONFLICT) from error


def issue_capability(projection: dict[str, Any], *, clock: Callable[[], float] = time.time,
                     monotonic_clock: Callable[[], float] = time.monotonic) -> str:
    wall_millis = _read_milliseconds(clock)
    monotonic_millis = _read_milliseconds(monotonic_clock)
    digest = hashlib.sha256(
        canonical_bytes(projection) + b"\0" + str(wall_millis).encode("ascii")
        + b"\0" + str(monotonic_millis).encode("ascii")
    ).hexdigest()
    return f"c2_{wall_millis:x}_{monotonic_millis:x}_{digest}"


def verify_capability(token: str, projection: dict[str, Any], *, clock: Callable[[], float] = time.time,
                      monotonic_clock: Callable[[], float] = time.monotonic) -> None:
    match = _TOKEN.fullmatch(token) if isinstance(token, str) else None
    if match is None:
        raise SyncError("invalid_catalog_token", "Catalog capability is malformed or altered.", EXIT_VALIDATION)
    issued_wall = int(match.group(1), 16)
    issued_monotonic = int(match.group(2), 16)
    current_wall = _read_milliseconds(clock)
    current_monotonic = _read_milliseconds(monotonic_clock)
    if current_wall < issued_wall or current_monotonic < issued_monotonic:
        raise SyncError("catalog_clock_invalid", "Catalog capability clock moved backwards.", EXIT_CONFLICT)
    wall_age = current_wall - issued_wall
    monotonic_age = current_monotonic - issued_monotonic
    wall_bucket = current_wall // _CAPABILITY_MILLISECONDS
    monotonic_bucket = current_monotonic // _CAPABILITY_MILLISECONDS
    issued_wall_bucket = issued_wall // _CAPABILITY_MILLISECONDS
    issued_monotonic_bucket = issued_monotonic // _CAPABILITY_MILLISECONDS
    if (
        issued_wall_bucket not in {wall_bucket, wall_bucket - 1}
        or issued_monotonic_bucket not in {monotonic_bucket, monotonic_bucket - 1}
        or wall_age >= _CAPABILITY_MILLISECONDS
        or monotonic_age >= _CAPABILITY_MILLISECONDS
    ):
        raise SyncError("catalog_expired", "Catalog capability expired or its verified context changed.", EXIT_CONFLICT)
    digest = hashlib.sha256(
        canonical_bytes(projection) + b"\0" + str(issued_wall).encode("ascii")
        + b"\0" + str(issued_monotonic).encode("ascii")
    ).hexdigest()
    expected = f"c2_{issued_wall:x}_{issued_monotonic:x}_{digest}"
    if not hmac.compare_digest(token, expected):
        raise SyncError("catalog_expired", "Catalog capability expired or its verified context changed.", EXIT_CONFLICT)
