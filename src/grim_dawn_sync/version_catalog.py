"""Read-only, bounded save-version catalog construction."""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import math
from pathlib import Path, PurePosixPath
import re
import time
from typing import Callable, Literal
import uuid

from grim_dawn_sync.bookmarks import parse_bookmark_annotation
from grim_dawn_sync.errors import EXIT_VALIDATION, SyncError
from grim_dawn_sync.git_vault import GitVault
from grim_dawn_sync.manifest import stable_manifest


CandidateKind = Literal["live", "remote_head", "history", "bookmark", "legacy"]


@dataclass(frozen=True)
class ManifestDiff:
    added: int
    removed: int
    changed: int
    character_dirs_added: tuple[str, ...] = ()
    character_dirs_removed: tuple[str, ...] = ()
    character_dirs_changed: tuple[str, ...] = ()


@dataclass(frozen=True)
class CandidateAlias:
    candidate_id: str
    kind: CandidateKind
    display_name: str
    created_at: str
    commit: str | None
    note: str | None = None


@dataclass(frozen=True)
class SaveCandidate:
    candidate_id: str
    kind: CandidateKind
    display_name: str
    created_at: str
    machine_id: str
    root_hash: str
    commit: str | None
    character_count: int
    file_count: int
    total_bytes: int
    character_labels: tuple[str, ...]
    diff_from_live: ManifestDiff
    note: str | None = None
    aliases: tuple[CandidateAlias, ...] = ()


@dataclass(frozen=True)
class VersionCatalog:
    token: str
    remote_head: str | None
    live_root_hash: str
    candidates: tuple[SaveCandidate, ...]
    baseline_root_hash: str | None = None

    def candidate(self, candidate_id: str) -> SaveCandidate:
        for item in self.candidates:
            if item.candidate_id == candidate_id:
                return item
            for alias in item.aliases:
                if alias.candidate_id == candidate_id:
                    return replace(item, candidate_id=alias.candidate_id, kind=alias.kind,
                                   display_name=alias.display_name, created_at=alias.created_at,
                                   commit=alias.commit, note=alias.note, aliases=())
        raise SyncError("unknown_candidate", "Selected save candidate is not in this catalog.", EXIT_VALIDATION)


def _labels(manifest: dict) -> tuple[str, ...]:
    labels = {
        PurePosixPath(str(item["path"])).parts[1]
        for item in manifest["files"]
        if len(PurePosixPath(str(item["path"])).parts) == 3
        and PurePosixPath(str(item["path"])).parts[0].casefold() == "main"
        and PurePosixPath(str(item["path"])).parts[2].casefold() == "player.gdc"
    }
    return tuple(sorted(labels, key=str.casefold))


def _diff(left: dict, right: dict) -> ManifestDiff:
    left_files = {str(item["path"]): (int(item["size"]), str(item["sha256"])) for item in left["files"]}
    right_files = {str(item["path"]): (int(item["size"]), str(item["sha256"])) for item in right["files"]}
    left_labels, right_labels = set(_labels(left)), set(_labels(right))
    changed_paths = {path for path in left_files.keys() & right_files.keys() if left_files[path] != right_files[path]}
    changed_labels = {
        PurePosixPath(path).parts[1]
        for path in changed_paths
        if len(PurePosixPath(path).parts) >= 3 and PurePosixPath(path).parts[0].casefold() == "main"
    }
    return ManifestDiff(
        added=len(right_files.keys() - left_files.keys()),
        removed=len(left_files.keys() - right_files.keys()),
        changed=len(changed_paths),
        character_dirs_added=tuple(sorted(right_labels - left_labels, key=str.casefold)),
        character_dirs_removed=tuple(sorted(left_labels - right_labels, key=str.casefold)),
        character_dirs_changed=tuple(sorted(changed_labels & left_labels & right_labels, key=str.casefold)),
    )


_REPRESENTATIVE_PRIORITY: dict[CandidateKind, int] = {
    "live": 0, "remote_head": 1, "bookmark": 2, "history": 3, "legacy": 4,
}


def _coalesce(candidates: list[SaveCandidate]) -> tuple[SaveCandidate, ...]:
    """Keep one root representative while retaining every selectable provenance."""
    groups: dict[str, list[SaveCandidate]] = {}
    order: list[str] = []
    for item in candidates:
        if item.root_hash not in groups: order.append(item.root_hash)
        groups.setdefault(item.root_hash, []).append(item)
    result: list[SaveCandidate] = []
    for root in order:
        values = groups[root]
        representative = min(values, key=lambda item: (_REPRESENTATIVE_PRIORITY[item.kind], values.index(item)))
        aliases = tuple(
            CandidateAlias(item.candidate_id, item.kind, item.display_name, item.created_at, item.commit, item.note)
            for item in values if item is not representative
        )
        result.append(replace(representative, aliases=aliases))
    return tuple(result)


class VersionCatalogBuilder:
    """Build candidates without checkout, extraction, state, or ref mutation."""

    def __init__(self, vault: GitVault, live_root: Path, *, machine_id: str, retries: int = 1, window_seconds: float = 0,
                 baseline_root_hash: str | None = None, clock: Callable[[], float] = time.time) -> None:
        self.vault = vault
        self.live_root = Path(live_root)
        self.machine_id = machine_id
        self.retries = retries
        self.window_seconds = window_seconds
        if baseline_root_hash is not None and not re.fullmatch(r"[0-9a-f]{64}", baseline_root_hash):
            raise SyncError("invalid_baseline_root", "Catalog baseline root is invalid.", EXIT_VALIDATION)
        self.baseline_root_hash = baseline_root_hash
        self.clock = clock

    def build(self, *, history_limit: int = 10) -> VersionCatalog:
        # Fetch only updates the local remote-tracking ref; it neither checks
        # out nor changes live saves, sync state, locks, commits, or tags.
        live = stable_manifest(self.live_root, machine_id=self.machine_id, retries=self.retries, window_seconds=self.window_seconds)
        self.vault.preflight()
        self.vault.fetch()
        remote_head = self.vault._oid(self.vault.remote_ref)
        commits = self.vault.remote_history(limit=history_limit)
        if remote_head is not None and (not commits or commits[0] != remote_head):
            raise SyncError("malformed_remote_history", "Remote history does not begin at remote main.")

        candidates: list[SaveCandidate] = [self._candidate("live", "This device's current data", None, live, live)]
        for index, commit in enumerate(commits):
            manifest = self.vault.validate_commit_snapshot(commit)
            metadata = self.vault.read_vault_metadata(commit)
            if metadata["root_hash"] != manifest["root_hash"]:
                raise SyncError("invalid_vault_metadata", "Committed provenance does not match its manifest.", EXIT_VALIDATION)
            kind: CandidateKind = "remote_head" if index == 0 else "history"
            name = "Sync destination latest" if kind == "remote_head" else "Remote main snapshot"
            candidates.append(self._candidate(kind, name, commit, manifest, live))
        for ref, commit, annotation in self.vault.managed_bookmarks():
            metadata = parse_bookmark_annotation(annotation)
            manifest = self.vault.validate_commit_snapshot(commit)
            candidates.append(self._candidate("bookmark", str(metadata["display_name"]), commit, manifest, live, note=metadata["note"]))
        for name, commit in self.vault.legacy_annotated_tags():
            manifest = self.vault.validate_commit_snapshot(commit)
            candidates.append(self._candidate("legacy", name, commit, manifest, live))
        try:
            now = self.clock()
        except Exception as error:
            raise SyncError("catalog_clock_invalid", "Catalog clock is unavailable.", EXIT_VALIDATION) from error
        if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now) or now < 0:
            raise SyncError("catalog_clock_invalid", "Catalog clock is invalid.", EXIT_VALIDATION)
        scope = hashlib.sha256(
            (f"{int(now) // 300}\0{self.machine_id}\0{live['root_hash']}\0{remote_head or ''}\0"
             f"{self.baseline_root_hash or ''}").encode("ascii")
        ).hexdigest()

        def scoped_id(candidate_id: str) -> str:
            return hashlib.sha256(f"{scope}\0{candidate_id}".encode("ascii")).hexdigest()[:32]

        scoped = tuple(
            replace(
                item,
                candidate_id=scoped_id(item.candidate_id),
                aliases=tuple(replace(alias, candidate_id=scoped_id(alias.candidate_id)) for alias in item.aliases),
            )
            for item in _coalesce(candidates)
        )
        return VersionCatalog(token=uuid.uuid4().hex, remote_head=remote_head, live_root_hash=str(live["root_hash"]),
                              candidates=scoped, baseline_root_hash=self.baseline_root_hash)

    @staticmethod
    def _candidate(kind: CandidateKind, display_name: str, commit: str | None, manifest: dict, live: dict, *, note: str | None = None) -> SaveCandidate:
        seed = f"{kind}\0{commit or manifest['root_hash']}\0{manifest['root_hash']}".encode("ascii")
        candidate_id = hashlib.sha256(seed).hexdigest()[:32]
        return SaveCandidate(candidate_id, kind, display_name, str(manifest["created_at"]), str(manifest["machine_id"]), str(manifest["root_hash"]), commit, int(manifest["character_count"]), int(manifest["file_count"]), int(manifest["total_bytes"]), _labels(manifest), _diff(live, manifest), note)
