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
_TOKEN = re.compile(r"^c1_[0-9a-f]{64}$")


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


def _bucket(now: float) -> int:
    if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now) or now < 0:
        raise SyncError("catalog_clock_invalid", "Catalog capability clock is invalid.", EXIT_CONFLICT)
    return int(now) // CAPABILITY_SECONDS


def _read_bucket(clock: Callable[[], float]) -> int:
    try:
        return _bucket(clock())
    except SyncError:
        raise
    except Exception as error:
        raise SyncError("catalog_clock_invalid", "Catalog capability clock is unavailable.", EXIT_CONFLICT) from error


def issue_capability(projection: dict[str, Any], *, clock: Callable[[], float] = time.time,
                     monotonic_clock: Callable[[], float] = time.monotonic) -> str:
    wall_bucket = _read_bucket(clock)
    monotonic_bucket = _read_bucket(monotonic_clock)
    digest = hashlib.sha256(
        canonical_bytes(projection) + b"\0" + str(wall_bucket).encode("ascii")
        + b"\0" + str(monotonic_bucket).encode("ascii")
    ).hexdigest()
    return "c1_" + digest


def verify_capability(token: str, projection: dict[str, Any], *, clock: Callable[[], float] = time.time,
                      monotonic_clock: Callable[[], float] = time.monotonic) -> None:
    if not isinstance(token, str) or not _TOKEN.fullmatch(token):
        raise SyncError("invalid_catalog_token", "Catalog capability is malformed or altered.", EXIT_VALIDATION)
    expected = issue_capability(projection, clock=clock, monotonic_clock=monotonic_clock)
    if not hmac.compare_digest(token, expected):
        raise SyncError("catalog_expired", "Catalog capability expired or its verified context changed.", EXIT_CONFLICT)
