"""CLI boundary for save synchronization; T0 performs no save or network mutation."""

from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import uuid
from typing import Any

from grim_dawn_sync import __version__
from grim_dawn_sync.config import default_config_path, load_config
from grim_dawn_sync.errors import EXIT_OK, SyncError
from grim_dawn_sync.manifest import stable_manifest
from grim_dawn_sync.discovery import cloud_candidates, game_candidates, inspect_path
from grim_dawn_sync.workflow import LaunchWorkflow
from grim_dawn_sync.shortcut import install_shortcut, migrate_shortcut
from grim_dawn_sync.git_vault import GitVault
from grim_dawn_sync.session_lock import (
    acquire_lock,
    inspect_remote_lock_readonly,
    mark_bootstrap_live_applied,
    prepare_bootstrap,
    push_bootstrap,
    recover_session,
    release_lock,
)
from grim_dawn_sync.snapshot import _copy_verified, restore_from_directory
from grim_dawn_sync.state import SyncState, load_state, save_state
from grim_dawn_sync.process_monitor import ProcessMonitor, WindowsProcessMonitor
from grim_dawn_sync.validation import validate_players
from grim_dawn_sync.version_catalog import VersionCatalogBuilder
from grim_dawn_sync.bookmarks import create_bookmark, create_live_bookmark_locked, make_bookmark_annotation
from grim_dawn_sync.selection import (
    CancelledSelection,
    ReconcileCase,
    SelectionDirective,
    SelectionRegistry,
    classify_reconciliation,
    selection_policy,
)
from grim_dawn_sync.selector_ui import (
    SelectionPresenter,
    SelectionRequest,
    TkSelectionPresenter,
    build_plan_from_request,
)
from grim_dawn_sync.catalog_capability import (
    configuration_identity,
    context_digest,
    issue_capability,
    read_remote_identity,
    safety_projection,
    verify_capability,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="grim-dawn-sync", allow_abbrev=False)
    parser.add_argument("--config", type=Path, default=default_config_path(), help="terminal-local config.local.json")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("doctor", help="read configuration and report readiness")
    subparsers.add_parser("status", help="report terminal-local readiness")
    subparsers.add_parser("recover", help="recover an interrupted session")
    restore = subparsers.add_parser("restore", help="restore a vault commit")
    restore.add_argument("--commit", required=True)
    restore.add_argument("--apply", action="store_true")
    restore.add_argument("--drill", action="store_true", help="with --apply, materialize into a tool-owned drill directory")
    subparsers.add_parser("snapshot", help="snapshot the current save")
    preserve = subparsers.add_parser("preserve", aliases=["archive-live"], help="make a verified local archive of the live save")
    preserve.add_argument("--apply", action="store_true", help="create the local archive")
    bootstrap = subparsers.add_parser("bootstrap", help="initialize from an explicit cloud source")
    bootstrap.add_argument("--source-cloud", type=Path, required=True); bootstrap.add_argument("--apply", action="store_true")
    enroll = subparsers.add_parser("enroll", aliases=["join"], help="adopt the current remote snapshot on a new terminal")
    enroll.add_argument("--apply", action="store_true")
    launch_parser = subparsers.add_parser("launch", help="select a verified save and run the synchronized DPYes launch workflow")
    launch_parser.add_argument("--select", help="auto-safe or an opaque candidate ID")
    launch_parser.add_argument("--catalog-token", help="short-lived catalog capability for --select")
    launch_parser.add_argument("--confirm", action="store_true", help="second confirmation for a history/bookmark selection")
    subparsers.add_parser("versions", help="list verified save-version candidates")
    bookmark_parser = subparsers.add_parser("bookmark", help="create a named immutable bookmark")
    bookmark_parser.add_argument("--candidate", required=True)
    bookmark_parser.add_argument("--catalog-token")
    bookmark_parser.add_argument("--name", required=True)
    bookmark_parser.add_argument("--note")
    bookmark_parser.add_argument("--apply", action="store_true")
    promote_parser = subparsers.add_parser("promote", help="make an explicitly selected save the remote latest without launching")
    promote_parser.add_argument("--candidate")
    promote_parser.add_argument("--catalog-token")
    promote_parser.add_argument("--apply", action="store_true")
    shortcut = subparsers.add_parser("install-shortcut", help="create the Save Sync desktop shortcut")
    shortcut.add_argument("--apply", action="store_true")
    migrate = subparsers.add_parser("migrate-shortcut", help="inspect the old shortcut and create the selection shortcut without replacement")
    migrate.add_argument("--apply", action="store_true")
    return parser


def _process_preflight(config: Any, monitor: ProcessMonitor | None = None) -> dict[str, Any]:
    """Do not mutate when process enumeration is incomplete or a target runs."""
    scan = (monitor or WindowsProcessMonitor()).scan()
    names = {name.casefold() for name in getattr(config, "game_process_names", ("Grim Dawn.exe",))} | {"dpyes.exe"}
    if not scan.complete:
        raise SyncError("process_scan_incomplete", "Game process status could not be verified; no change was made.", 5)
    matches = [item for item in scan.processes if item.name.casefold() in names]
    if matches:
        raise SyncError("game_already_running", "Grim Dawn or DPYes is running; no change was made.", 5)
    return {"status": "stopped", "complete": True}


def _process_status(config: Any, monitor: ProcessMonitor | None = None) -> dict[str, Any]:
    scan = (monitor or WindowsProcessMonitor()).scan()
    names = {name.casefold() for name in getattr(config, "game_process_names", ("Grim Dawn.exe",))} | {"dpyes.exe"}
    if not scan.complete:
        return {"status": "unknown", "complete": False}
    matches = [item for item in scan.processes if item.name.casefold() in names]
    return {"status": "running" if matches else "clear", "complete": True}


def doctor(config_path: Path, *, monitor: ProcessMonitor | None = None) -> dict[str, Any]:
    config = load_config(config_path)
    save = {"configured": True, "exists": config.save_root.exists(), "manifest": None}
    warnings: list[dict[str, str]] = []
    if config.save_root.is_dir():
        try:
            manifest = stable_manifest(config.save_root, machine_id=config.machine_id, retries=config.stable_scan_retries, window_seconds=config.stable_window_seconds)
            save["manifest"] = {key: manifest[key] for key in ("root_hash", "file_count", "total_bytes", "character_count")}
        except SyncError as error:
            save["validation"] = error.code
    else:
        warnings.append({"code": "save_root_missing", "message": "Configured save root was not found; nothing was created."})
    process = _process_status(config, monitor)
    vault: dict[str, Any] = inspect_path(config.vault_repo)
    try:
        checked = _vault(config); checked.preflight(); remote = checked.remote_oid()
        # Doctor must never fetch: its local tracking ref may be stale, so a
        # Vault supplied remote relation is the only meaningful comparison.
        relation = _live_remote_relation(checked, remote)
        vault.update({"git": "available", "remote_commit": remote, "relation": relation,
                      "active_lock": inspect_remote_lock_readonly(checked) is not None})
    except SyncError as error:
        vault.update({"git": "unavailable", "validation": error.code})
    return {
        "schema_version": "1.0.0",
        "tool_version": __version__,
        "command": "doctor",
        "read_only": True,
        "machine_id": config.machine_id,
        "checks": {
            "config": {"ok": True},
            "save_root": save,
            "cloud": cloud_candidates(config.game_install),
            "vault": vault,
            "launcher": game_candidates(config.game_install, config.launcher_path),
            "processes": process,
        },
        "warnings": warnings,
    }


def _render(payload: dict[str, Any], *, as_json: bool) -> str:
    if as_json:
        # Command results can contain paths supplied by adapters.  The CLI is
        # the serialization boundary, so JSON mode must remain machine-readable
        # rather than failing after a successful operation.
        return json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n"
    if "error" in payload:
        return f"{payload['error']['code']}: {payload['error']['message']}\n"
    if payload.get("command") == "doctor":
        return "doctor: configuration valid; read-only diagnostics complete.\n"
    return f"{payload.get('command', 'grim-dawn-sync')}: complete\n"


def _vault(config) -> GitVault:
    return GitVault(config.vault_repo, remote=config.remote, branch=config.branch)


def versions(config_path: Path) -> dict[str, Any]:
    config = load_config(config_path)
    _, catalog, _, _ = _fresh_catalog(config_path, config)
    selectable = [
        catalog.candidate(candidate_id)
        for item in catalog.candidates
        for candidate_id in (item.candidate_id, *(alias.candidate_id for alias in item.aliases))
    ]
    return {"schema_version": "1.0.0", "command": "versions", "catalog_token": catalog.token,
            "candidates": [{"candidate_id": item.candidate_id, "kind": item.kind,
                            "file_count": item.file_count, "character_count": item.character_count,
                            "total_bytes": item.total_bytes,
                            "diff": {"added": item.diff_from_live.added, "removed": item.diff_from_live.removed, "changed": item.diff_from_live.changed}}
                           for item in selectable]}


def bookmark(config_path: Path, candidate_id: str, name: str, note: str | None, *, apply: bool,
             catalog_token: str | None = None) -> dict[str, Any]:
    config = load_config(config_path)
    make_bookmark_annotation(name, note, created_by=config.machine_id)
    if catalog_token is None:
        raise SyncError("catalog_token_required", "Bookmark validation requires a current catalog capability.", 3)
    vault, catalog, state, projection = _fresh_catalog(config_path, config, supplied_token=catalog_token)
    candidate = catalog.candidate(candidate_id)
    if not apply:
        return {"schema_version": "1.0.0", "command": "bookmark", "dry_run": True, "verified": True}
    _assert_capability_boundary(config_path, config, state, catalog, projection)
    vault.bind_remote_identity((str(projection["config"]["remote_fetch_url"]), str(projection["config"]["remote_push_url"])))
    if candidate.commit is None:
        manifest = stable_manifest(
            config.save_root, machine_id=config.machine_id,
            retries=config.stable_scan_retries, window_seconds=config.stable_window_seconds,
        )
        if manifest.get("root_hash") != candidate.root_hash or catalog.remote_head is None:
            raise SyncError("selection_stale", "Live bookmark candidate changed before execution.", 3)

        def before_lock() -> None:
            _assert_capability_boundary(config_path, config, state, catalog, projection)

        def after_lock() -> None:
            _process_preflight(config)
            if load_config(config_path).public_dict() != config.public_dict():
                raise SyncError("catalog_context_changed", "Configuration changed during bookmark creation.", 3)

        create_live_bookmark_locked(
            vault, config.save_root, manifest,
            state_path=_state_path(config_path), expected_remote_head=catalog.remote_head,
            display_name=name, note=note, created_by=config.machine_id,
            retries=config.stable_scan_retries, window_seconds=config.stable_window_seconds,
            validator=validate_players, before_lock=before_lock, after_lock=after_lock,
        )
    else:
        create_bookmark(vault, candidate.commit, display_name=name, note=note, created_by=config.machine_id)
    return {"schema_version": "1.0.0", "command": "bookmark", "dry_run": False, "created": True}


_REMOTE_REF_SNAPSHOT_MAX_BYTES = 256 * 1024
_REMOTE_REF_SNAPSHOT_MAX_ROWS = 500


def _remote_ref_snapshot(vault: GitVault) -> str:
    """Hash the bounded remote refs that can affect selection semantics."""
    result = vault.runner.run(
        "ls-remote", vault.remote, vault.remote_head,
        "refs/tags/grim-dawn-save-*", "refs/tags/archive/*", "refs/tags/milestone/*",
        check=False,
    )
    encoded = result.stdout.encode("utf-8", "surrogatepass")
    if result.returncode or len(encoded) > _REMOTE_REF_SNAPSHOT_MAX_BYTES:
        raise SyncError("catalog_ref_snapshot_failed", "Remote save-version refs could not be verified.", 4)
    rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    for row in result.stdout.splitlines():
        fields = row.split("\t")
        if len(fields) != 2:
            raise SyncError("catalog_ref_snapshot_invalid", "Remote save-version refs were malformed.", 4)
        oid, ref = fields
        base = ref[:-3] if ref.endswith("^{}") else ref
        allowed = (
            ref == vault.remote_head
            or re.fullmatch(r"refs/tags/grim-dawn-save-[0-9a-f]{32}", base) is not None
            or re.fullmatch(r"refs/tags/(?:archive|milestone)/[^\x00-\x1f]+", base) is not None
        )
        if (
            not allowed
            or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", oid) is None
            or ref in seen
            or len(ref.encode("utf-8", "surrogatepass")) > 512
        ):
            raise SyncError("catalog_ref_snapshot_invalid", "Remote save-version refs were malformed.", 4)
        seen.add(ref)
        rows.append((ref, oid))
        if len(rows) > _REMOTE_REF_SNAPSHOT_MAX_ROWS:
            raise SyncError("catalog_ref_snapshot_failed", "Remote save-version ref count exceeded its safe bound.", 4)
    canonical = "".join(f"{ref}\t{oid}\n" for ref, oid in sorted(rows))
    return hashlib.sha256(canonical.encode("utf-8", "surrogatepass")).hexdigest()


def _execution_context_projection(config: Any, state: SyncState, *, remote_identity: tuple[str, str],
                                  live_root_hash: str, remote_head: str | None,
                                  remote_ref_snapshot: str) -> dict[str, Any]:
    """Canonical lightweight state that must still match immediately before locking."""
    return {
        "schema_version": "1.0.0",
        "config": configuration_identity(config, remote_identity=remote_identity),
        "state": state.as_dict(),
        "lock": None,
        "remote_head": remote_head,
        "live_root_hash": live_root_hash,
        "baseline_root_hash": state.last_applied_manifest_root_hash,
        "remote_ref_snapshot": remote_ref_snapshot,
    }


def _remote_lock_snapshot(vault: GitVault) -> str | None:
    """Read only the active-lock ref; the annotated OID binds its full payload."""
    lock_ref = "refs/tags/grim-dawn-sync-active"
    result = vault.runner.run("ls-remote", "--refs", vault.remote, lock_ref, check=False)
    rows = [row.split("\t") for row in result.stdout.splitlines() if row]
    if (
        result.returncode
        or len(rows) > 1
        or (rows and (len(rows[0]) != 2 or rows[0][1] != lock_ref
                      or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", rows[0][0]) is None))
    ):
        raise SyncError("catalog_lock_snapshot_invalid", "Remote save-sync lock could not be verified.", 4)
    return rows[0][0] if rows else None


def _capture_catalog(config_path: Path, expected_config: Any) -> tuple[GitVault, Any, SyncState, dict[str, Any]]:
    _process_preflight(expected_config)
    current_config = load_config(config_path)
    if current_config.public_dict() != expected_config.public_dict():
        raise SyncError("catalog_context_changed", "Configuration changed while versions were scanned.", 3)
    state = load_state(_state_path(config_path))
    if state.phase is not None:
        raise SyncError("recovery_required", "Recovery state exists; run recover before selecting a save.", 6)
    vault = _vault(current_config)
    if inspect_remote_lock_readonly(vault) is not None:
        raise SyncError("lock_held", "Another save-sync session is active; no selection was made.", 3)
    remote_identity = read_remote_identity(vault, current_config.remote)
    remote_refs = _remote_ref_snapshot(vault)
    catalog = VersionCatalogBuilder(
        vault, current_config.save_root, machine_id=current_config.machine_id,
        retries=current_config.stable_scan_retries, window_seconds=current_config.stable_window_seconds,
        baseline_root_hash=state.last_applied_manifest_root_hash,
    ).build()
    # Close the scan around config/state/lock/process too, so the catalog can
    # never be issued from a hybrid of values observed on opposite sides of I/O.
    _process_preflight(expected_config)
    after_config = load_config(config_path)
    after_state = load_state(_state_path(config_path))
    if (
        after_config.public_dict() != current_config.public_dict()
        or after_state.as_dict() != state.as_dict()
        or inspect_remote_lock_readonly(vault) is not None
    ):
        raise SyncError("catalog_context_changed", "Save selection context changed during verification; reload versions.", 3)
    if read_remote_identity(vault, current_config.remote) != remote_identity:
        raise SyncError("catalog_context_changed", "Remote identity changed during verification; reload versions.", 3)
    if _remote_ref_snapshot(vault) != remote_refs:
        raise SyncError("catalog_context_changed", "Remote save-version refs changed during verification; reload versions.", 3)
    manifest = stable_manifest(
        current_config.save_root, machine_id=current_config.machine_id,
        retries=current_config.stable_scan_retries, window_seconds=current_config.stable_window_seconds,
    )
    if manifest.get("root_hash") != catalog.live_root_hash or vault.remote_oid() != catalog.remote_head:
        raise SyncError("catalog_context_changed", "Live or remote save data changed during verification; reload versions.", 3)
    projection = safety_projection(current_config, state, catalog, remote_identity=remote_identity)
    projection["execution_context"] = _execution_context_projection(
        current_config, state, remote_identity=remote_identity,
        live_root_hash=catalog.live_root_hash, remote_head=catalog.remote_head,
        remote_ref_snapshot=remote_refs,
    )
    return vault, catalog, state, projection


def _fresh_catalog(config_path: Path, config: Any, *, supplied_token: str | None = None) -> tuple[GitVault, Any, SyncState, dict[str, Any]]:
    """Build one catalog bracketed by lightweight safety observations."""
    vault, catalog, state, projection = _capture_catalog(config_path, config)
    token = issue_capability(projection)
    if supplied_token is not None:
        verify_capability(supplied_token, projection)
        token = supplied_token
    catalog = replace(catalog, token=token)
    return vault, catalog, state, projection


def _capture_execution_context(config_path: Path, expected_config: Any, expected_state: SyncState,
                               vault: GitVault) -> dict[str, Any]:
    """Re-read execution inputs without rebuilding commits or candidates."""
    _process_preflight(expected_config)
    current_config = load_config(config_path)
    current_state = load_state(_state_path(config_path))
    if current_config.public_dict() != expected_config.public_dict() or current_state.as_dict() != expected_state.as_dict():
        raise SyncError("catalog_context_changed", "Selection context changed before lock acquisition.", 3)
    if current_state.phase is not None:
        raise SyncError("recovery_required", "Recovery state exists; run recover before selecting a save.", 6)
    if _remote_lock_snapshot(vault) is not None:
        raise SyncError("lock_held", "Another save-sync session is active; no selection was made.", 3)
    remote_identity = read_remote_identity(vault, current_config.remote)
    remote_refs = _remote_ref_snapshot(vault)
    manifest = stable_manifest(
        current_config.save_root, machine_id=current_config.machine_id,
        retries=current_config.stable_scan_retries, window_seconds=current_config.stable_window_seconds,
    )
    return _execution_context_projection(
        current_config, current_state, remote_identity=remote_identity,
        live_root_hash=str(manifest.get("root_hash")), remote_head=vault.remote_oid(),
        remote_ref_snapshot=remote_refs,
    )

def _assert_capability_boundary(config_path: Path, config: Any, state: SyncState, catalog: Any,
                                projection: dict[str, Any]) -> None:
    """Rebind state/config immediately before the workflow's atomic lock race."""
    current_config = load_config(config_path)
    current_state = load_state(_state_path(config_path))
    if current_config.public_dict() != config.public_dict() or current_state.as_dict() != state.as_dict():
        raise SyncError("catalog_context_changed", "Selection context changed before execution; reload versions.", 3)
    verify_capability(catalog.token, projection)


def _selection_case(config_path: Path, catalog: Any, vault: GitVault) -> ReconcileCase:
    if catalog.remote_head is None:
        raise SyncError("remote_main_missing", "Remote main is missing; run bootstrap first.", 3)
    try:
        state = load_state(_state_path(config_path))
    except SyncError as error:
        if error.code == "state_missing":
            raise SyncError("bootstrap_required", "No applied baseline exists; run bootstrap before launch.", 3) from None
        raise
    remote = vault.validate_commit_snapshot(catalog.remote_head)
    case = classify_reconciliation(
        live_root_hash=catalog.live_root_hash,
        remote_root_hash=remote.get("root_hash"),
        baseline_root_hash=state.last_applied_manifest_root_hash,
    )
    if case == ReconcileCase.BASELINE_MISSING:
        raise SyncError("bootstrap_required", "No applied baseline exists; run bootstrap before launch.", 3)
    return case


def _registry_for_catalog(catalog: Any) -> SelectionRegistry:
    # VersionCatalogBuilder has already verified managed and legacy tag targets.
    # Resolve every provenance ID through the catalog itself.  Coalesced
    # bookmark/legacy aliases are reconstructed canonical candidates and are
    # not equal to their root representative object.
    authorized_ids = {
        item.candidate_id
        for representative in catalog.candidates
        for item in (representative, *(catalog.candidate(alias.candidate_id) for alias in representative.aliases))
    }
    verified = {candidate_id: catalog.candidate(candidate_id) for candidate_id in authorized_ids}
    return SelectionRegistry(
        verified_bookmark_target=lambda candidate: verified.get(candidate.candidate_id) == candidate,
    )


def _automatic_request(catalog: Any, mode: str) -> SelectionRequest:
    candidate = next((item for item in catalog.candidates if item.kind == "live"), None)
    if candidate is None:
        raise SyncError("selection_required", "No verified automatic selection is available.", 3)
    return SelectionRequest(candidate.candidate_id, mode)


def _execute_selection_command(
    config_path: Path,
    *,
    command: str,
    as_json: bool,
    selected: str | None,
    catalog_token: str | None,
    presenter: SelectionPresenter | None = None,
    confirmed: bool = False,
) -> dict[str, Any]:
    config = load_config(config_path)
    _process_preflight(config)
    if selected and selected != "auto-safe" and catalog_token is None:
        raise SyncError("catalog_token_required", "Explicit selection requires a current catalog capability.", 3)
    ui = presenter or TkSelectionPresenter()
    while True:
        interactive = not as_json and selected is None
        request: SelectionRequest | CancelledSelection | None = None
        if interactive:
            captured: dict[str, Any] = {}
            def build_in_worker() -> Any:
                vault_value, catalog_value, state_value, projection_value = _fresh_catalog(config_path, config)
                case_value = _selection_case(config_path, catalog_value, vault_value)
                captured.update(vault=vault_value, catalog=catalog_value, state=state_value,
                                projection=projection_value, case=case_value)
                return catalog_value
            def directive_after_scan(_catalog: Any) -> SelectionDirective:
                base = selection_policy(captured["case"])
                if command == "promote":
                    return SelectionDirective(True, base.initial_selection, ("promote", "bookmark", "cancel", "reload"),
                                              base.bookmark_displaced_remote)
                return base
            builder_present = getattr(ui, "present_builder", None)
            if not callable(builder_present):
                raise SyncError("adapter_contract_invalid", "Selection presenter cannot load catalogs safely.", 2)
            request = builder_present(build_in_worker, directive_after_scan)
            try:
                vault, catalog, state, projection, case = (
                    captured["vault"], captured["catalog"], captured["state"], captured["projection"], captured["case"],
                )
            except KeyError:
                if isinstance(request, CancelledSelection):
                    return {"schema_version": "1.0.0", "command": command, "result": {"state": "CANCELLED"}}
                raise SyncError("selection_catalog_failed", "Verified save versions were not loaded.", 3) from None
        else:
            vault, catalog, state, projection = _fresh_catalog(
                config_path, config, supplied_token=(catalog_token if selected and selected != "auto-safe" else None),
            )
            case = _selection_case(config_path, catalog, vault)
        directive = selection_policy(case)
        registry = _registry_for_catalog(catalog); registry.register(catalog)

        if selected and selected != "auto-safe":
            request = SelectionRequest(
                selected, "promote-only" if command == "promote" else "launch",
                confirmation_granted=(command == "promote" or confirmed),
            )
        elif command == "promote" and as_json:
            raise SyncError("selection_required", "Promotion requires an explicit current selection.", 3)
        elif case == ReconcileCase.EQUAL:
            request = request or _automatic_request(catalog, "launch")
        elif selected == "auto-safe" or as_json:
            raise SyncError("selection_required", "Save versions differ; an explicit current selection is required.", 3)
        elif request is None:
            raise SyncError("selection_required", "An explicit selection is required.", 3)

        if isinstance(request, CancelledSelection):
            return {"schema_version": "1.0.0", "command": command, "result": {"state": "CANCELLED"}}
        if request.action == "reload":
            selected = None; catalog_token = None
            continue
        if request.action == "bookmark":
            if request.candidate_id is None or not request.display_name:
                raise SyncError("invalid_selection_request", "Bookmark selection is incomplete.", 3)
            candidate = catalog.candidate(request.candidate_id)
            _assert_capability_boundary(config_path, config, state, catalog, projection)
            vault.bind_remote_identity((str(projection["config"]["remote_fetch_url"]), str(projection["config"]["remote_push_url"])))
            if candidate.commit is None:
                manifest = stable_manifest(
                    config.save_root, machine_id=config.machine_id,
                    retries=config.stable_scan_retries, window_seconds=config.stable_window_seconds,
                )
                if manifest.get("root_hash") != candidate.root_hash or catalog.remote_head is None:
                    raise SyncError("selection_stale", "Live bookmark candidate changed before execution.", 3)

                def before_lock() -> None:
                    _assert_capability_boundary(config_path, config, state, catalog, projection)

                def after_lock() -> None:
                    _process_preflight(config)
                    if load_config(config_path).public_dict() != config.public_dict():
                        raise SyncError("catalog_context_changed", "Configuration changed during bookmark creation.", 3)

                create_live_bookmark_locked(
                    vault, config.save_root, manifest,
                    state_path=_state_path(config_path), expected_remote_head=catalog.remote_head,
                    display_name=request.display_name, note=request.note, created_by=config.machine_id,
                    retries=config.stable_scan_retries, window_seconds=config.stable_window_seconds,
                    validator=validate_players, before_lock=before_lock, after_lock=after_lock,
                )
            else:
                create_bookmark(vault, candidate.commit, display_name=request.display_name,
                                note=request.note, created_by=config.machine_id)
            return {"schema_version": "1.0.0", "command": command, "result": {"state": "BOOKMARKED"}}
        remote_identity = (
            str(projection["config"]["remote_fetch_url"]),
            str(projection["config"]["remote_push_url"]),
        )
        plan = build_plan_from_request(
            registry, request, catalog_token=catalog.token, case=case,
            expected_context_digest=context_digest(projection["execution_context"]),
            expected_remote_identity=remote_identity,
        )
        _assert_capability_boundary(config_path, config, state, catalog, projection)
        def revalidate_context(expected: str) -> None:
            fresh_context = _capture_execution_context(config_path, config, state, vault)
            verify_capability(catalog.token, projection)
            if context_digest(fresh_context) != expected:
                raise SyncError("catalog_context_changed", "Selection context changed before lock acquisition.", 3)
        try:
            result = LaunchWorkflow(config, config_path.parent).execute_selection_plan(
                plan, registry, context_revalidate=revalidate_context,
            )
        except SyncError as error:
            if interactive and error.code == "selection_stale":
                # A stale interactive choice is not a recovery event: no lock
                # or mutation has begun. Re-enter catalog construction so the
                # presenter can show fresh candidates and let the user reload,
                # cancel, or select again. Explicit/headless requests still
                # fail closed to their caller.
                selected = None
                catalog_token = None
                continue
            raise
        return {"schema_version": "1.0.0", "command": command, "result": result}


def launch(config_path: Path, *, as_json: bool = False, selected: str | None = None,
           catalog_token: str | None = None, presenter: SelectionPresenter | None = None,
           confirmed: bool = False) -> dict[str, Any]:
    return _execute_selection_command(config_path, command="launch", as_json=as_json, selected=selected,
                                      catalog_token=catalog_token, presenter=presenter, confirmed=confirmed)


def promote(config_path: Path, *, apply: bool, as_json: bool = False, selected: str | None = None,
            catalog_token: str | None = None, presenter: SelectionPresenter | None = None) -> dict[str, Any]:
    if not apply:
        return {"schema_version": "1.0.0", "command": "promote", "dry_run": True}
    return _execute_selection_command(config_path, command="promote", as_json=as_json, selected=selected,
                                      catalog_token=catalog_token, presenter=presenter)


def _state_path(config_path: Path) -> Path:
    return Path(config_path).parent / "state.json"


def _tree_usage(path: Path) -> dict[str, int]:
    """Best-effort, read-only usage summary; never follows links."""
    if not path.is_dir() or path.is_symlink():
        return {"files": 0, "bytes": 0}
    files = total = 0
    try:
        for item in path.rglob("*"):
            if item.is_file() and not item.is_symlink():
                files += 1; total += item.stat().st_size
    except OSError:
        return {"files": files, "bytes": total}
    return {"files": files, "bytes": total}


def _safe_archive_parent(path: Path) -> None:
    """Create a dedicated archive parent without traversing a link/reparse."""
    chain: list[Path] = []
    current = path
    while True:
        chain.append(current)
        if current.parent == current:
            break
        current = current.parent
    missing: list[Path] = []
    for candidate in reversed(chain):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            missing.append(candidate)
            continue
        if candidate.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & 0x400) or not stat.S_ISDIR(info.st_mode):
            raise SyncError("unsafe_archive_path", "Archive parent is not a safe directory.", 6)
    try:
        for candidate in missing:
            candidate.mkdir()
    except OSError as error:
        raise SyncError("unsafe_archive_path", "Archive parent could not be created safely.", 6) from error


def _live_remote_relation(vault: GitVault, remote: str | None) -> str:
    """Read-only relation; do not infer state from a stale tracking ref."""
    result = vault.runner.run("rev-parse", "--verify", "--quiet", "HEAD", check=False)
    local = result.stdout.strip() if result.returncode == 0 else None
    if remote is None:
        return "unborn" if local is None else "remote_missing"
    return "equal" if local == remote else "remote_changed_or_unknown"


def _local_head_oid(vault: GitVault) -> str | None:
    result = vault.runner.run("rev-parse", "--verify", "--quiet", "HEAD", check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def status(config_path: Path, *, monitor: ProcessMonitor | None = None) -> dict[str, Any]:
    config = load_config(config_path); vault = _vault(config); state_path = _state_path(config_path)
    try:
        state = load_state(state_path)
    except SyncError as error:
        if error.code != "state_missing": raise
        state = SyncState()
    vault.preflight()
    remote = vault.remote_oid(); lock = inspect_remote_lock_readonly(vault)
    relation = _live_remote_relation(vault, remote)
    recovery = state.phase is not None
    readiness = "recovery_required" if recovery else ("ready" if relation in {"equal", "behind"} and remote else "blocked")
    return {"schema_version":"1.0.0", "command":"status", "readiness":readiness,
            "remote_commit":remote, "last_pushed_commit":state.pushed_commit or state.last_applied_remote_commit,
            "vault_relation":relation, "active_lock": None if lock is None else {"machine_id":lock.session.machine_id, "session_id":lock.session.session_id, "started_at":lock.session.started_at},
            "recovery_phase":state.phase,
            "processes": _process_status(config, monitor),
            "archive_usage": _tree_usage(Path(config_path).parent / "archives"),
            "vault_usage": _tree_usage(Path(config.vault_repo) / "save")}


def recover(config_path: Path, *, monitor: ProcessMonitor | None = None) -> dict[str, Any]:
    config = load_config(config_path)
    # Recovery can delete a remote session lock; require the same complete,
    # stopped process view as every other mutating save operation.
    _process_preflight(config, monitor)
    vault = _vault(config); vault.preflight()
    identity = read_remote_identity(vault, config.remote)
    vault.bind_remote_identity(identity)
    state = load_state(_state_path(config_path))
    if load_config(config_path).public_dict() != config.public_dict() or load_state(_state_path(config_path)) != state:
        raise SyncError("recovery_context_changed", "Recovery configuration or state changed before execution.", 6)
    vault.assert_remote_identity()
    result = recover_session(vault, state, config.machine_id, state_path=_state_path(config_path))
    return {"schema_version":"1.0.0", "command":"recover", "result":result}


def restore(config_path: Path, commit: str, *, apply: bool, drill: bool = False) -> dict[str, Any]:
    config = load_config(config_path); vault = _vault(config); vault.preflight()
    root = Path(config_path).parent
    if drill and apply:
        # This path does not scan processes, acquire locks, change state,
        # alter live saves, update Git, or launch an executable.  extract_save
        # validates ancestry, blobs, manifest, and player data before publish.
        planned = _inspect_restore(vault, commit)
        drill_root = root / "restore-drills"
        _safe_archive_parent(drill_root)
        drill_id = f"restore-{commit[:16]}-{uuid.uuid4().hex}"
        destination = drill_root / drill_id
        try:
            destination.lstat()
        except FileNotFoundError:
            pass
        except OSError as error:
            raise SyncError("restore_drill_unavailable", "Restore drill destination could not be inspected.", 3) from error
        else:
            raise SyncError("snapshot_name_collision", "Restore drill destination already exists.", 3)
        vault.extract_save(commit, destination, machine_id=config.machine_id,
                           retries=config.stable_scan_retries,
                           window_seconds=config.stable_window_seconds)
        return {"schema_version": "1.0.0", "command": "restore", "commit": commit,
                "dry_run": False, "materialized": True,
                "root_hash": planned["root_hash"], "file_count": planned["file_count"],
                "total_bytes": planned["total_bytes"], "drill_id": drill_id}
    if not apply:
        # Do not use extract_save here: extraction necessarily creates a
        # staging directory.  A default restore is an inspection only.
        return {"schema_version": "1.0.0", "command": "restore", "commit": commit,
                **_inspect_restore(vault, commit)}
    _validate_restore_ancestry(vault, commit)
    _process_preflight(config)
    # Extract validates the historical manifest before any live-save operation.
    # ``extract_save`` publishes through a create-only staging sibling.  The
    # CLI owns its staging parent, so prepare it safely before extraction.
    _safe_archive_parent(root / "staging")
    # Keep every attempt create-only.  A prior successful restore may retain
    # its extracted tree for recovery inspection, so its destination must not
    # prevent the same historical commit from being restored again.
    destination = root / "staging" / f"restore-{commit}-{uuid.uuid4().hex}"
    vault.extract_save(commit, destination, machine_id=config.machine_id, retries=config.stable_scan_retries, window_seconds=config.stable_window_seconds)
    result = restore_from_directory(destination, config.save_root, root / "archives", root / "recovery", machine_id=config.machine_id, apply=apply, retries=config.stable_scan_retries, window_seconds=config.stable_window_seconds)
    return {"schema_version":"1.0.0", "command":"restore", "commit":commit, **result}


def snapshot(config_path: Path) -> dict[str, Any]:
    config = load_config(config_path); vault = _vault(config); state_path = _state_path(config_path)
    _process_preflight(config)
    # Preserve the exact live tree before Git/lock operations.  A later push or
    # release failure therefore always leaves a local, verified recovery copy.
    manifest = stable_manifest(config.save_root, machine_id=config.machine_id, retries=config.stable_scan_retries, window_seconds=config.stable_window_seconds)
    validation = validate_players(config.save_root, manifest)
    if not validation.get("ok"):
        raise SyncError(validation.get("classification", "save_invalid"), "Save validation did not permit snapshot.", 3)
    archive_parent = Path(config_path).parent / "archives"
    _safe_archive_parent(archive_parent)
    archive = archive_parent / f"save-before-snapshot-{manifest['root_hash'][:16]}-{uuid.uuid4().hex}"
    _copy_verified(config.save_root, archive, manifest, config.machine_id, config.stable_scan_retries, 0, validate_players)
    status_value = vault.update_fast_forward()
    if status_value.relation in {"ahead", "diverged", "unborn"}: raise SyncError("vault_not_reconciled", "Vault must be synchronized before snapshot.", 4)
    base = vault.remote_oid()
    if not base: raise SyncError("remote_main_missing", "Remote main is missing; run bootstrap first.", 4)
    lock = acquire_lock(vault, config.machine_id, base, state_path=state_path)
    oid = vault.snapshot(config.save_root, machine_id=config.machine_id, session_id=lock.session.session_id, expected_manifest=manifest, retries=config.stable_scan_retries, window_seconds=config.stable_window_seconds)
    save_state(state_path, SyncState(last_applied_manifest_root_hash=manifest["root_hash"], session_id=lock.session.session_id, machine_id=lock.session.machine_id, base_commit=lock.session.base_commit, lock_oid=lock.oid, local_tag=lock.local_tag, phase="committed", local_commit=oid))
    pushed = vault.push(oid); release_lock(vault, lock, pushed, state_path=state_path, confirmed_root_hash=manifest["root_hash"])
    return {"schema_version":"1.0.0", "command":"snapshot", "commit":pushed, "root_hash": manifest["root_hash"]}


def preserve(config_path: Path, *, apply: bool) -> dict[str, Any]:
    """Preserve live saves locally without consulting sync, state, or vault data."""
    config = load_config(config_path)
    if apply:
        # Do not even scan a live save while its writer is known to run.
        _process_preflight(config)
    manifest = stable_manifest(config.save_root, machine_id=config.machine_id,
                               retries=config.stable_scan_retries,
                               window_seconds=config.stable_window_seconds)
    validation = validate_players(config.save_root, manifest)
    if not validation.get("ok"):
        raise SyncError(validation.get("classification", "save_invalid"),
                        "Live save validation did not permit preservation.", 3)
    summary = {
        "schema_version": "1.0.0", "command": "preserve",
        "file_count": manifest["file_count"],
        "character_count": manifest["character_count"],
    }
    if not apply:
        return {**summary, "dry_run": True, "verified": False}
    archive_parent = Path(config_path).parent / "archives"
    _safe_archive_parent(archive_parent)
    archive_id = f"save-preserved-{manifest['root_hash'][:16]}-{uuid.uuid4().hex}"
    destination = archive_parent / archive_id
    # A finished-looking archive is never published until the second process
    # check has passed.  An interrupted copy can only leave this clearly
    # incomplete staging name behind for manual inspection/removal.
    stage = archive_parent / f".preserve-incomplete-{uuid.uuid4().hex}"
    _process_preflight(config)
    _copy_verified(config.save_root, stage, manifest, config.machine_id,
                   config.stable_scan_retries, config.stable_window_seconds,
                   validate_players)
    try:
        copied_info = stage.lstat()
    except OSError as error:
        raise SyncError("archive_publish_failed", "Verified local archive could not be inspected.", 3) from error
    copied_identity = (copied_info.st_dev, copied_info.st_ino)
    try:
        _process_preflight(config)
    except SyncError as error:
        # Keep an explicitly incomplete staging tree, but never advertise it
        # as a preservable archive after a process-start race.
        raise SyncError(error.code, error.message, error.exit_code) from error
    try:
        _safe_archive_parent(archive_parent)
        if stage.parent != archive_parent or not re.fullmatch(r"\.preserve-incomplete-[0-9a-f]{32}", stage.name):
            raise SyncError("unsafe_archive_path", "Incomplete archive staging is not safe to publish.", 3)
        stage_info = stage.lstat()
        if (stage.is_symlink() or bool(getattr(stage_info, "st_file_attributes", 0) & 0x400)
                or not stat.S_ISDIR(stage_info.st_mode)
                or (stage_info.st_dev, stage_info.st_ino) != copied_identity):
            raise SyncError("unsafe_archive_path", "Incomplete archive staging is not a safe directory.", 3)
        if destination.parent != archive_parent or destination.exists() or destination.is_symlink():
            raise SyncError("snapshot_name_collision", "Archive destination already exists.", 3)
        os.replace(stage, destination)
    except SyncError:
        raise
    except OSError as error:
        raise SyncError("archive_publish_failed", "Verified local archive could not be published.", 3) from error
    return {**summary, "dry_run": False, "verified": True, "archive_id": archive_id}


def bootstrap(config_path: Path, source: Path, *, apply: bool) -> dict[str, Any]:
    config = load_config(config_path); vault = _vault(config); root = Path(config_path).parent
    if not source.is_dir(): raise SyncError("cloud_source_missing", "Cloud save source does not exist.", 3)
    # Plan first; dry-run has no live or Git mutation.
    planned = restore_from_directory(source, config.save_root, root / "archives", root / "recovery", machine_id=config.machine_id, apply=False, retries=config.stable_scan_retries, window_seconds=config.stable_window_seconds)
    if not apply: return {"schema_version":"1.0.0", "command":"bootstrap", **planned}
    _process_preflight(config)
    vault.preflight()
    try:
        prior_state = load_state(_state_path(config_path))
    except SyncError as error:
        if error.code != "state_missing":
            raise
        prior_state = SyncState()
    resuming = prior_state.phase == "bootstrap_pending"
    if prior_state.phase is not None and not resuming:
        raise SyncError("recovery_required", "Another recovery session must be completed first.", 6)
    manifest = stable_manifest(source, machine_id=config.machine_id, retries=config.stable_scan_retries, window_seconds=config.stable_window_seconds)
    validation = validate_players(source, manifest)
    if not validation.get("ok"):
        raise SyncError(validation.get("classification", "save_invalid"), "Cloud save validation did not permit bootstrap.", 3)
    if resuming:
        return _resume_bootstrap_cli(config_path, config, vault, prior_state, manifest)
    if config.save_root.exists() or config.save_root.is_symlink():
        raise SyncError("bootstrap_live_not_empty", "Bootstrap requires a missing live save root.", 3)
    initial_remote = vault.remote_oid()
    if initial_remote is not None:
        raise SyncError("bootstrap_remote_not_empty", "Bootstrap requires an empty remote main.", 4)
    # Preserve the cloud input before creating a live save.  The archive name
    # is unique and _copy_verified is create-only/symlink-safe.
    archive_parent = root / "archives"
    _safe_archive_parent(archive_parent)
    archive = archive_parent / f"cloud-before-bootstrap-{manifest['root_hash'][:16]}-{uuid.uuid4().hex}"
    _copy_verified(source, archive, manifest, config.machine_id, config.stable_scan_retries, config.stable_window_seconds, validate_players)
    oid = vault.snapshot(archive, machine_id=config.machine_id, session_id="bootstrap", expected_manifest=manifest, retries=config.stable_scan_retries, window_seconds=config.stable_window_seconds)
    # Recheck after local commit creation so a remote bootstrap race cannot
    # proceed into live-save creation.
    remote = vault.remote_oid()
    if remote is not None:
        raise SyncError("bootstrap_remote_not_empty", "Bootstrap requires an empty or exactly matching pending remote.", 4)
    pending = prepare_bootstrap(vault, config.machine_id, oid, manifest["root_hash"], state_path=_state_path(config_path))
    return _resume_bootstrap_cli(config_path, config, vault, pending, manifest)


def _enroll_existing_state(state_path: Path, config: Any, commit: str, root_hash: str) -> bool:
    """Return true only for this terminal's already-complete exact baseline."""
    try:
        state = load_state(state_path)
    except SyncError as error:
        if error.code == "state_missing":
            return False
        raise
    if state.phase is not None:
        raise SyncError("recovery_required", "An interrupted session must be recovered before enrollment.", 6)
    if (
        state.machine_id == config.machine_id
        and state.last_applied_remote_commit == commit
        and state.last_applied_manifest_root_hash == root_hash
    ):
        return True
    raise SyncError("enroll_state_exists", "Existing terminal state does not match this remote snapshot.", 6)


def enroll(config_path: Path, *, apply: bool) -> dict[str, Any]:
    """Join a non-empty vault without pushing or overwriting an existing save.

    Dry-run deliberately does not fetch: it reports the advertised remote OID
    but cannot attest its snapshot until ``--apply`` has fetched it locally.
    """
    config = load_config(config_path); vault = _vault(config); state_path = _state_path(config_path)
    vault.preflight()
    remote = vault.remote_oid()
    lock = inspect_remote_lock_readonly(vault)
    if lock is not None:
        raise SyncError("lock_held", "Another terminal holds the remote sync lock.", 6)
    if remote is None:
        raise SyncError("remote_main_missing", "Remote main is missing; enrollment cannot continue.", 4)
    if not apply:
        if _local_head_oid(vault) != remote:
            raise SyncError("vault_not_reconciled", "Dry-run requires local HEAD to equal advertised remote main.", 4)
        return {"schema_version": "1.0.0", "command": "enroll", "dry_run": True,
                "remote_commit": remote, "remote_snapshot_verified": False,
                "note": "Dry-run does not fetch or create staging; use --apply to verify and enroll."}
    _process_preflight(config)
    # Refuse an interrupted local session before fetch can advance this clone.
    # A missing state is the normal first-enrollment condition.
    try:
        preliminary_state = load_state(state_path)
    except SyncError as error:
        if error.code != "state_missing":
            raise
    else:
        if preliminary_state.phase is not None:
            raise SyncError("recovery_required", "An interrupted session must be recovered before enrollment.", 6)
    status_value = vault.update_fast_forward()
    # ``update_fast_forward`` reports the pre-merge relation, so ``behind``
    # is acceptable only when the subsequent HEAD equality check proves its
    # fast-forward completed.  All other non-equal relations are unsafe.
    if status_value.relation in {"ahead", "diverged", "unborn"}:
        raise SyncError("vault_not_reconciled", "Vault must fast-forward to remote main before enrollment.", 4)
    remote = vault.remote_oid()
    if remote is None:
        raise SyncError("remote_main_missing", "Remote main disappeared during enrollment.", 4)
    if _local_head_oid(vault) != remote:
        raise SyncError("vault_not_reconciled", "Vault HEAD did not match fetched remote main.", 4)
    if inspect_remote_lock_readonly(vault) is not None:
        raise SyncError("lock_held", "Another terminal holds the remote sync lock.", 6)
    manifest = vault.validate_commit_snapshot(remote)
    root_hash = manifest["root_hash"]
    idempotent = _enroll_existing_state(state_path, config, remote, root_hash)
    live_manifest: dict[str, Any] | None = None
    if config.save_root.exists() or config.save_root.is_symlink():
        if config.save_root.is_symlink() or not config.save_root.is_dir():
            raise SyncError("enroll_live_unsafe", "Existing live save path is not a safe directory.", 6)
        live_manifest = stable_manifest(config.save_root, machine_id=config.machine_id,
                                        retries=config.stable_scan_retries, window_seconds=config.stable_window_seconds)
        if live_manifest.get("file_count", 0) == 0:
            raise SyncError("enroll_live_conflict", "Existing live save is empty; no overwrite was made.", 4)
        validation = validate_players(config.save_root, live_manifest)
        if not validation.get("ok"):
            raise SyncError(validation.get("classification", "save_invalid"), "Existing live save validation failed.", 3)
        if live_manifest["root_hash"] != root_hash:
            raise SyncError("enroll_live_conflict", "Existing live save differs from remote; no overwrite was made.", 4)
    if idempotent:
        if live_manifest is None:
            raise SyncError("enroll_live_missing", "Enrolled live save is missing; recovery is required.", 6)
        return {"schema_version": "1.0.0", "command": "enroll", "dry_run": False,
                "commit": remote, "root_hash": root_hash, "idempotent": True}
    if live_manifest is None:
        root = Path(config_path).parent
        _safe_archive_parent(root / "staging")
        destination = root / "staging" / f"enroll-{remote}-{uuid.uuid4().hex}"
        vault.extract_save(remote, destination, machine_id=config.machine_id,
                           retries=config.stable_scan_retries, window_seconds=config.stable_window_seconds)
        if vault.remote_oid() != remote or inspect_remote_lock_readonly(vault) is not None:
            raise SyncError("enroll_remote_changed", "Remote changed or became locked before restore; no live save was changed.", 4)
        _process_preflight(config)
        result = restore_from_directory(destination, config.save_root, root / "archives", root / "recovery",
                                        machine_id=config.machine_id, apply=True,
                                        retries=config.stable_scan_retries, window_seconds=config.stable_window_seconds)
        if result.get("root_hash") != root_hash:
            raise SyncError("enroll_live_mismatch", "Restored live save did not match remote snapshot.", 6)
    observed = stable_manifest(config.save_root, machine_id=config.machine_id,
                               retries=config.stable_scan_retries, window_seconds=config.stable_window_seconds)
    if observed["root_hash"] != root_hash:
        raise SyncError("enroll_live_mismatch", "Live save did not match remote snapshot.", 6)
    if vault.remote_oid() != remote or inspect_remote_lock_readonly(vault) is not None:
        raise SyncError("enroll_remote_changed", "Remote changed or became locked before state was saved.", 4)
    save_state(state_path, SyncState(last_applied_remote_commit=remote,
                                     last_applied_manifest_root_hash=root_hash,
                                     machine_id=config.machine_id))
    return {"schema_version": "1.0.0", "command": "enroll", "dry_run": False,
            "commit": remote, "root_hash": root_hash, "idempotent": False}


def _resume_bootstrap_cli(
    config_path: Path,
    config: Any,
    vault: GitVault,
    pending: SyncState,
    source_manifest: dict[str, Any],
) -> dict[str, Any]:
    """Resume only the exact prepared commit; never create a replacement."""
    oid = pending.local_commit
    root_hash = pending.last_applied_manifest_root_hash
    if (
        pending.phase != "bootstrap_pending"
        or pending.machine_id != config.machine_id
        or not isinstance(oid, str)
        or not isinstance(root_hash, str)
        or source_manifest.get("root_hash") != root_hash
    ):
        raise SyncError("bootstrap_recovery_mismatch", "Bootstrap retry does not match pending recovery.", 4)
    committed = vault.validate_commit_snapshot(oid)
    if committed.get("root_hash") != root_hash:
        raise SyncError("bootstrap_recovery_mismatch", "Pending bootstrap commit does not match recovery state.", 4)
    head = vault.runner.run("rev-parse", "--verify", "--quiet", "HEAD", check=False)
    if head.returncode or head.stdout.strip() != oid:
        raise SyncError("bootstrap_recovery_mismatch", "Pending bootstrap commit is not the current local HEAD.", 4)
    remote = vault.remote_oid()
    if remote not in {None, oid}:
        raise SyncError("bootstrap_remote_not_empty", "Bootstrap remote does not match pending recovery.", 4)

    live_ready = False
    if config.save_root.is_dir() and not config.save_root.is_symlink():
        try:
            live_ready = next(config.save_root.iterdir(), None) is not None
        except OSError as error:
            raise SyncError("bootstrap_live_unreadable", "Bootstrap live save could not be inspected.", 6) from error
    elif config.save_root.exists() or config.save_root.is_symlink():
        raise SyncError("bootstrap_live_mismatch", "Bootstrap live path is not a safe save directory.", 6)

    if live_ready:
        observed = stable_manifest(config.save_root, machine_id=config.machine_id, retries=config.stable_scan_retries, window_seconds=config.stable_window_seconds)
        if observed["root_hash"] != root_hash:
            raise SyncError("bootstrap_live_mismatch", "Existing bootstrap live save does not match pending recovery.", 6)
    elif pending.bootstrap_live_applied:
        raise SyncError("bootstrap_live_mismatch", "Applied bootstrap live save is missing.", 6)
    else:
        root = Path(config_path).parent
        # The CLI owns its state-local staging parent.  GitVault owns only
        # the create-only extraction destination below it, so prepare this
        # ordinary directory without following links/reparse points first.
        staging_root = root / "staging"
        _safe_archive_parent(staging_root)
        extracted = staging_root / f"bootstrap-{oid}-{uuid.uuid4().hex}"
        vault.extract_save(oid, extracted, machine_id=config.machine_id, retries=config.stable_scan_retries, window_seconds=config.stable_window_seconds)
        applied = restore_from_directory(extracted, config.save_root, root / "archives", root / "recovery", machine_id=config.machine_id, apply=True, retries=config.stable_scan_retries, window_seconds=config.stable_window_seconds)
        observed = stable_manifest(config.save_root, machine_id=config.machine_id, retries=config.stable_scan_retries, window_seconds=config.stable_window_seconds)
        if observed["root_hash"] != root_hash or applied.get("root_hash") != root_hash:
            raise SyncError("bootstrap_live_mismatch", "Applied bootstrap live save failed exact verification.", 6)

    if not pending.bootstrap_live_applied:
        mark_bootstrap_live_applied(config.machine_id, oid, root_hash, root_hash, state_path=_state_path(config_path))
    pushed = push_bootstrap(vault, config.machine_id, oid, root_hash, state_path=_state_path(config_path))
    return {"schema_version": "1.0.0", "command": "bootstrap", "commit": pushed, "root_hash": root_hash, "dry_run": False}


_OID = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z", re.IGNORECASE)


def _inspect_restore(vault: GitVault, commit: str) -> dict[str, Any]:
    """Validate the requested historical save without materializing it.

    GitVault deliberately keeps extraction and validation together.  The CLI
    needs this small read-only counterpart so its default action cannot create
    a staging, archive, journal, or state artifact.
    """
    _validate_restore_ancestry(vault, commit)
    try:
        # This is deliberately a single strict Vault operation.  It validates
        # the canonical manifest and every committed save-tree entry without
        # materialising a staging directory.
        manifest = vault.validate_commit_snapshot(commit)
        root_hash = manifest["root_hash"]
        file_count = manifest["file_count"]
        total_bytes = manifest["total_bytes"]
    except (KeyError, TypeError, ValueError, SyncError) as error:
        raise SyncError("restore_manifest_invalid", "Restore commit has an invalid save manifest.", 3) from error
    if not isinstance(root_hash, str) or not _OID.fullmatch(root_hash) or not isinstance(file_count, int) or not isinstance(total_bytes, int):
        raise SyncError("restore_manifest_invalid", "Restore commit has an invalid save manifest.", 3)
    return {"dry_run": True, "root_hash": root_hash, "file_count": file_count, "total_bytes": total_bytes}


def _validate_restore_ancestry(vault: GitVault, commit: str) -> None:
    branch_ref = f"refs/heads/{vault.branch}"
    result = vault.runner.run("merge-base", "--is-ancestor", commit, branch_ref, check=False)
    if result.returncode == 1:
        raise SyncError("restore_commit_not_in_history", "Restore commit is not in configured branch history.", 3)
    if result.returncode != 0:
        raise SyncError("git_command_failed", "Restore commit ancestry could not be verified.", 4)


_SAFE_DETAIL_KEYS = {
    "safe_oid", "safe_root_hash", "archive_root", "quarantine_root",
    "archive_id", "quarantine_id", "machine_id", "session_id",
    "last_state", "next_command", "local_commit", "root_hash",
    "archive", "quarantine",
}


def _safe_error_payload(error: SyncError) -> dict[str, Any]:
    """Keep CLI failures useful without reflecting paths, remotes, or stderr."""
    details: dict[str, Any] = {}
    for key, value in error.details.items():
        if key not in _SAFE_DETAIL_KEYS:
            continue
        if key in {"safe_oid", "local_commit"} and isinstance(value, str) and _OID.fullmatch(value):
            details[key] = value
        elif key in {"safe_root_hash", "root_hash", "archive_root", "quarantine_root"} and isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value):
            details[key] = value
        elif key in {"archive_id", "quarantine_id", "archive", "quarantine", "machine_id", "session_id", "last_state"} and isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", value):
            details[key] = value
        elif key == "next_command" and value in {"grim-dawn-sync recover", "grim-dawn-sync status"}:
            details[key] = value
    payload = {"error": {"code": error.code, "message": error.message, "details": details}}
    payload["error"]["next_command"] = details.get("next_command") or ("grim-dawn-sync recover" if error.exit_code == 6 or error.code == "lock_held" else "grim-dawn-sync status")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor": payload = doctor(args.config)
        elif args.command == "status": payload = status(args.config)
        elif args.command == "recover": payload = recover(args.config)
        elif args.command == "restore": payload = restore(args.config, args.commit, apply=args.apply, drill=args.drill)
        elif args.command == "snapshot": payload = snapshot(args.config)
        elif args.command in {"preserve", "archive-live"}: payload = preserve(args.config, apply=args.apply)
        elif args.command == "bootstrap": payload = bootstrap(args.config, args.source_cloud, apply=args.apply)
        elif args.command in {"enroll", "join"}: payload = enroll(args.config, apply=args.apply)
        elif args.command == "versions": payload = versions(args.config)
        elif args.command == "bookmark": payload = bookmark(args.config, args.candidate, args.name, args.note, apply=args.apply, catalog_token=args.catalog_token)
        elif args.command == "launch": payload = launch(args.config, as_json=args.json, selected=args.select, catalog_token=args.catalog_token, confirmed=args.confirm)
        elif args.command == "promote": payload = promote(args.config, apply=args.apply, as_json=args.json, selected=args.candidate, catalog_token=args.catalog_token)
        elif args.command == "install-shortcut":
            if not args.apply: payload = {"schema_version": "1.0.0", "command": "install-shortcut", "dry_run": True}
            else:
                desktop = Path.home() / "Desktop"
                install_shortcut(desktop, config_path=args.config)
                payload = {"schema_version": "1.0.0", "command": "install-shortcut", "created": True}
        elif args.command == "migrate-shortcut":
            if not args.apply:
                payload = {"schema_version": "1.0.0", "command": "migrate-shortcut", "dry_run": True}
            else:
                destination, old_current = migrate_shortcut(Path.home() / "Desktop", config_path=args.config)
                payload = {"schema_version": "1.0.0", "command": "migrate-shortcut", "created": True,
                           "old_shortcut": "current" if old_current is True else ("stale" if old_current is False else "absent")}
        exit_code = EXIT_OK
    except SyncError as error:
        payload = _safe_error_payload(error)
        exit_code = error.exit_code
    except Exception:
        # The command boundary must not leak a Python exception, Git stderr,
        # or local save path when an adapter violates its error contract.
        payload = _safe_error_payload(SyncError(
            "operation_failed", "The operation stopped safely; run recovery before trying again.", 6,
        ))
        exit_code = 6
    sys.stdout.write(_render(payload, as_json=args.json))
    return exit_code
