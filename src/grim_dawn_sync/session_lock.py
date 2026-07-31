"""Remote annotated-tag session locks and conservative recovery primitives.

This module deliberately has no launcher/DPYes knowledge.  It is safe to call
from a workflow only after its own preflight has established a clean vault.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json, os, re, tempfile, uuid
from pathlib import Path

from grim_dawn_sync.errors import EXIT_CONFLICT, EXIT_RECOVERY_REQUIRED, SyncError
from grim_dawn_sync.git_vault import GitRunner, GitVault, _OID
from grim_dawn_sync.state import SyncState, load_state, save_state, save_state_if_unchanged

LOCK_REF = "refs/tags/grim-dawn-sync-active"
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

@dataclass(frozen=True)
class Session:
    session_id: str
    machine_id: str
    base_commit: str
    started_at: str
    def payload(self) -> dict[str, str]:
        return {"schema_version":"1.0.0", "session_id":self.session_id, "machine_id":self.machine_id, "base_commit":self.base_commit, "started_at":self.started_at}

@dataclass(frozen=True)
class Lock:
    session: Session
    oid: str
    local_tag: str | None = None

def _fail(code: str, message: str, exit_code: int = EXIT_CONFLICT, details: dict | None = None) -> None:
    raise SyncError(code, message, exit_code, details or {})


def _recovery_guard(vault: GitVault, state_path: Path, expected: SyncState) -> None:
    vault.assert_remote_identity()
    if load_state(state_path) != expected:
        _fail("recovery_state_changed", "Recovery state changed before mutation.", EXIT_RECOVERY_REQUIRED)

def _session(payload: object) -> Session:
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "session_id", "machine_id", "base_commit", "started_at"} or payload.get("schema_version") != "1.0.0": _fail("invalid_lock", "Remote lock JSON is not canonical.")
    sid, machine, base, stamp = (payload[k] for k in ("session_id", "machine_id", "base_commit", "started_at"))
    if not isinstance(sid, str) or not isinstance(machine, str) or not isinstance(base, str) or not isinstance(stamp, str): _fail("invalid_lock", "Remote lock fields are invalid.")
    try:
        parsed_uuid = uuid.UUID(sid)
        if str(parsed_uuid) != sid: raise ValueError
    except (ValueError, AttributeError): _fail("invalid_lock", "Remote lock session ID is invalid.")
    if not _TOKEN.fullmatch(machine) or not _OID.fullmatch(base): _fail("invalid_lock", "Remote lock identifiers are invalid.")
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None or not stamp.endswith("Z"): raise ValueError
    except ValueError: _fail("invalid_lock", "Remote lock timestamp is invalid.")
    return Session(sid, machine, base, stamp)

def new_session(machine_id: str, base_commit: str) -> Session:
    if not _TOKEN.fullmatch(machine_id) or not _OID.fullmatch(base_commit): _fail("invalid_lock", "Local lock identifiers are invalid.")
    return Session(str(uuid.uuid4()), machine_id, base_commit, datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"))

def _remote_lock(vault: GitVault) -> str | None:
    result = vault.runner.run("ls-remote", "--refs", vault.remote, LOCK_REF)
    rows = [line.split("\t") for line in result.stdout.splitlines() if line]
    if not rows: return None
    if len(rows) != 1 or len(rows[0]) != 2 or rows[0][1] != LOCK_REF or not _OID.fullmatch(rows[0][0]): _fail("malformed_remote_lock", "Remote lock ref was malformed.")
    return rows[0][0]


def _inspect_fetched_lock(runner: GitRunner, ref: str, oid: str) -> Lock:
    try:
        kind = runner.run("cat-file", "-t", ref).stdout.strip()
        actual = runner.run("rev-parse", ref).stdout.strip()
        if kind != "tag" or actual != oid:
            _fail("invalid_lock", "Remote lock is not its advertised annotated tag.")
        message = runner.run("cat-file", "-p", ref).stdout
        marker = "\n\n"
        if marker not in message:
            _fail("invalid_lock", "Remote lock tag has no canonical message.")
        header, body = message.split(marker, 1)
        if not body.endswith("\n") or body.count("\n") != 1:
            _fail("invalid_lock", "Remote lock message is not canonical JSON.")
        fields = dict(line.split(" ", 1) for line in header.splitlines() if " " in line)
        session = _session(json.loads(body))
        canonical = json.dumps(session.payload(), sort_keys=True, separators=(",", ":")) + "\n"
        if fields.get("type") != "commit" or fields.get("object") != session.base_commit or body != canonical:
            _fail("invalid_lock", "Remote lock tag header or JSON is not canonical.")
        return Lock(session, oid)
    except (json.JSONDecodeError, UnicodeError) as error:
        raise SyncError("invalid_lock", "Remote lock message is invalid.", EXIT_CONFLICT) from error


def inspect_remote_lock(vault: GitVault, oid: str | None = None) -> Lock | None:
    oid = _remote_lock(vault) if oid is None else oid
    if oid is None: return None
    if not _OID.fullmatch(oid): _fail("invalid_lock", "Remote lock object ID is invalid.")
    # Fetch into a unique, non-user-facing local ref, then verify an annotated tag.
    temporary = f"refs/save-sync/inspect/{uuid.uuid4().hex}"
    try:
        vault.runner.run("fetch", "--no-tags", vault.remote, f"{LOCK_REF}:{temporary}")
        return _inspect_fetched_lock(vault.runner, temporary, oid)
    finally:
        vault.runner.run("update-ref", "-d", temporary, check=False)


def inspect_remote_lock_readonly(vault: GitVault) -> Lock | None:
    """Inspect remote tag metadata without changing the configured vault."""
    oid = _remote_lock(vault)
    if oid is None:
        return None
    remote_result = vault.runner.run("remote", "get-url", vault.remote, check=False)
    urls = remote_result.stdout.splitlines()
    if remote_result.returncode or len(urls) != 1 or not urls[0] or "\0" in urls[0]:
        _fail("remote_url_unavailable", "Remote lock metadata could not be read.")
    with tempfile.TemporaryDirectory(prefix="grim-dawn-sync-inspect-") as raw:
        runner = GitRunner(Path(raw), executable=getattr(vault.runner, "executable", "git"))
        runner.run("init", "--bare", ".")
        temporary = "refs/save-sync/inspect/lock"
        runner.run("fetch", "--no-tags", "--", urls[0], f"{LOCK_REF}:{temporary}")
        return _inspect_fetched_lock(runner, temporary, oid)


def _inspect_local_tag(vault: GitVault, tag: str, expected_oid: str) -> Session:
    ref = f"refs/tags/{tag}"
    oid = vault.runner.run("rev-parse", "--verify", "--quiet", ref, check=False).stdout.strip()
    if oid != expected_oid or vault.runner.run("cat-file", "-t", ref, check=False).stdout.strip() != "tag":
        _fail("recovery_local_tag_mismatch", "Local recovery tag does not match state.")
    raw = vault.runner.run("cat-file", "-p", ref).stdout
    if "\n\n" not in raw: _fail("recovery_local_tag_mismatch", "Local recovery tag is malformed.")
    header, body = raw.split("\n\n", 1)
    try: session = _session(json.loads(body))
    except (json.JSONDecodeError, UnicodeError) as error:
        raise SyncError("recovery_local_tag_mismatch", "Local recovery tag is malformed.", EXIT_CONFLICT) from error
    fields = dict(line.split(" ", 1) for line in header.splitlines() if " " in line)
    canonical = json.dumps(session.payload(), sort_keys=True, separators=(",", ":")) + "\n"
    if fields.get("object") != session.base_commit or fields.get("type") != "commit" or body != canonical:
        _fail("recovery_local_tag_mismatch", "Local recovery tag is malformed.")
    return session

def acquire_lock(vault: GitVault, machine_id: str, base_commit: str, *, state_path: Path | None = None,
                 expected_pre_state: SyncState | None = None) -> Lock:
    vault.assert_remote_identity()
    if state_path is None: _fail("state_required", "Lock acquisition requires terminal-local state.", EXIT_RECOVERY_REQUIRED)
    if vault.remote_oid() != base_commit: _fail("stale_lock_base", "Remote main no longer matches the lock base.")
    if _remote_lock(vault) is not None: _fail("lock_held", "A remote save-sync lock already exists.")
    session = new_session(machine_id, base_commit); tag = f"grim-dawn-sync-{session.session_id}"
    message = json.dumps(session.payload(), sort_keys=True, separators=(",", ":"))
    fd, raw = tempfile.mkstemp(prefix="grim-dawn-sync-lock-", suffix=".json", dir=vault.repo)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as out: out.write(message + "\n")
        vault.runner.run("tag", "-a", tag, base_commit, "-F", raw)
        oid = vault.runner.run("rev-parse", tag).stdout.strip()
        if not _OID.fullmatch(oid) or vault.runner.run("cat-file", "-t", tag).stdout.strip() != "tag": _fail("lock_create_failed", "Could not create an annotated session tag.", EXIT_RECOVERY_REQUIRED)
        intent = SyncState(session_id=session.session_id, machine_id=machine_id, base_commit=base_commit, lock_oid=oid, local_tag=tag, phase="lock_held")
        try:
            if expected_pre_state is None:
                save_state(state_path, intent)
            else:
                save_state_if_unchanged(state_path, expected_pre_state, intent)
        except SyncError as error:
            deleted = vault.runner.run("tag", "-d", tag, check=False)
            if deleted.returncode:
                _fail("lock_race_cleanup_incomplete", "Local lock cleanup is incomplete.", EXIT_RECOVERY_REQUIRED)
            if expected_pre_state is not None and error.code in {"selection_stale", "state_busy"}:
                raise SyncError("selection_stale", "Recovery state changed before lock acquisition.", EXIT_CONFLICT) from None
            raise
        def cleanup_unpushed_intent(code: str, message: str) -> None:
            """Restore the exact pre-state before deleting an unpublished local tag."""
            try:
                if expected_pre_state is None:
                    save_state(state_path, SyncState())
                else:
                    save_state_if_unchanged(state_path, intent, expected_pre_state)
            except SyncError:
                _fail("lock_race_cleanup_incomplete", "Lock state cleanup is incomplete.", EXIT_RECOVERY_REQUIRED)
            deleted = vault.runner.run("tag", "-d", tag, check=False)
            if deleted.returncode:
                _fail("lock_race_cleanup_incomplete", "Local lock cleanup is incomplete.", EXIT_RECOVERY_REQUIRED)
            _fail(code, message)
        if vault.remote_oid() != base_commit:
            cleanup_unpushed_intent("stale_lock_base", "Remote main changed before lock push.")
        vault.assert_remote_identity()
        pushed = vault.runner.run("push", vault.remote, f"{oid}:{LOCK_REF}", check=False)
        try: remote = _remote_lock(vault)
        except SyncError:
            _fail("lock_push_unknown", "Lock acquisition result is unknown; recovery artifacts were retained.", EXIT_RECOVERY_REQUIRED, {"session_id":session.session_id,"machine_id":machine_id,"base_commit":base_commit,"lock_oid":oid,"local_tag":tag})
        if remote == oid:
            lock = Lock(session, oid, tag)
            return lock
        if remote is not None:
            cleanup_unpushed_intent("lock_race_lost", "Another machine acquired the remote lock.")
        _fail("lock_push_unknown" if pushed.returncode else "lock_unconfirmed", "Lock acquisition was not confirmed; local recovery artifacts were retained.", EXIT_RECOVERY_REQUIRED, {"session_id":session.session_id,"machine_id":machine_id,"base_commit":base_commit,"lock_oid":oid,"local_tag":tag})
    finally:
        try: os.unlink(raw)
        except OSError: pass


def _resume_bootstrap(
    vault: GitVault,
    state: SyncState,
    machine_id: str,
    *,
    state_path: Path,
) -> str:
    """Finish one exact unborn-main push without locks, leases, or force."""
    if (
        state.phase != "bootstrap_pending"
        or state.machine_id != machine_id
        or not state.local_commit
        or not state.last_applied_manifest_root_hash
    ):
        _fail("bootstrap_recovery_mismatch", "Bootstrap recovery belongs to another machine or is incomplete.")
    if not state.bootstrap_live_applied:
        _fail(
            "bootstrap_apply_required",
            "Bootstrap live save was not confirmed; apply and verify it before pushing.",
            EXIT_RECOVERY_REQUIRED,
        )
    _recovery_guard(vault, state_path, state)
    commit = state.local_commit
    if vault.runner.run("cat-file", "-e", f"{commit}^{{commit}}", check=False).returncode:
        _fail("bootstrap_local_commit_missing", "Bootstrap recovery commit is unavailable.")
    try:
        remote = vault.remote_oid()
    except SyncError:
        _fail("bootstrap_push_incomplete", "Bootstrap remote state could not be confirmed.", EXIT_RECOVERY_REQUIRED)
    if remote is not None and remote != commit:
        _fail("bootstrap_remote_conflict", "Remote main no longer permits bootstrap recovery.")
    if remote is None:
        try:
            _recovery_guard(vault, state_path, state)
            vault.runner.run("push", vault.remote, f"{commit}:{vault.remote_head}", check=False)
        except SyncError:
            # The subprocess may have updated the remote before its result was
            # lost.  Resolve that ambiguity with the same live-ref check.
            pass
        try:
            remote = vault.remote_oid()
        except SyncError:
            _fail("bootstrap_push_incomplete", "Bootstrap push could not be confirmed.", EXIT_RECOVERY_REQUIRED)
        if remote != commit:
            if remote is not None:
                _fail("bootstrap_remote_conflict", "Remote main advanced during bootstrap recovery.")
            _fail("bootstrap_push_incomplete", "Bootstrap push was not confirmed.", EXIT_RECOVERY_REQUIRED)
        # A non-zero push result with the exact remote OID is an ambiguous
        # success.  Confirmation, not subprocess status, is authoritative.
    _recovery_guard(vault, state_path, state)
    save_state(
        state_path,
        SyncState(
            last_applied_remote_commit=commit,
            last_applied_manifest_root_hash=state.last_applied_manifest_root_hash,
            machine_id=machine_id,
        ),
    )
    return commit


def prepare_bootstrap(
    vault: GitVault,
    machine_id: str,
    local_commit: str,
    root_hash: str,
    *,
    state_path: Path | None = None,
) -> SyncState:
    """Persist the bootstrap intent before any live-save mutation."""
    if state_path is None:
        _fail("state_required", "Bootstrap preparation requires terminal-local state.", EXIT_RECOVERY_REQUIRED)
    if not _OID.fullmatch(local_commit) or vault.runner.run(
        "cat-file", "-e", f"{local_commit}^{{commit}}", check=False
    ).returncode:
        _fail("bootstrap_local_commit_missing", "Bootstrap recovery commit is unavailable.")
    pending = SyncState(
        last_applied_manifest_root_hash=root_hash,
        machine_id=machine_id,
        phase="bootstrap_pending",
        local_commit=local_commit,
        bootstrap_live_applied=False,
    )
    save_state(state_path, pending)
    return pending


def mark_bootstrap_live_applied(
    machine_id: str,
    local_commit: str,
    root_hash: str,
    observed_live_root: str,
    *,
    state_path: Path | None = None,
) -> SyncState:
    """Mark live apply complete only after an exact manifest-root match."""
    if state_path is None:
        _fail("state_required", "Bootstrap live confirmation requires terminal-local state.", EXIT_RECOVERY_REQUIRED)
    state = load_state(state_path)
    if (
        state.phase != "bootstrap_pending"
        or state.machine_id != machine_id
        or state.local_commit != local_commit
        or state.last_applied_manifest_root_hash != root_hash
    ):
        _fail("bootstrap_recovery_mismatch", "Bootstrap live confirmation does not match pending state.")
    if observed_live_root != root_hash:
        _fail(
            "bootstrap_live_mismatch",
            "Bootstrap live save does not match the prepared manifest.",
            EXIT_RECOVERY_REQUIRED,
        )
    applied = replace(state, bootstrap_live_applied=True)
    save_state(state_path, applied)
    return applied


def push_bootstrap(
    vault: GitVault,
    machine_id: str,
    local_commit: str,
    root_hash: str,
    *,
    state_path: Path | None = None,
) -> str:
    """Initialize remote main only from a prepared, verified live apply."""
    if state_path is None:
        _fail("state_required", "Bootstrap push requires terminal-local state.", EXIT_RECOVERY_REQUIRED)
    state = load_state(state_path)
    if (
        state.phase != "bootstrap_pending"
        or state.machine_id != machine_id
        or state.local_commit != local_commit
        or state.last_applied_manifest_root_hash != root_hash
    ):
        _fail("bootstrap_recovery_mismatch", "Bootstrap push does not match pending state.")
    return _resume_bootstrap(vault, state, machine_id, state_path=state_path)


def release_lock(
    vault: GitVault,
    lock: Lock,
    pushed_commit: str,
    *,
    state_path: Path | None = None,
    confirmed_root_hash: str | None = None,
    expected_state: SyncState | None = None,
) -> None:
    if state_path is None: _fail("state_required", "Lock release requires terminal-local state.", EXIT_RECOVERY_REQUIRED)
    if expected_state is not None:
        _recovery_guard(vault, state_path, expected_state)
    try:
        if vault.remote_oid() != pushed_commit: _fail("release_remote_main_mismatch", "Remote main does not match the pushed session commit.")
        remote = _remote_lock(vault)
    except SyncError as error:
        if error.code in {"release_remote_main_mismatch"}: raise
        _fail("release_incomplete", "Release preflight could not confirm remote state.", EXIT_RECOVERY_REQUIRED)
    if remote != lock.oid: _fail("release_lock_mismatch", "Remote lock no longer belongs to this session.")
    existing = inspect_remote_lock(vault, remote)
    if existing is None or existing.session != lock.session: _fail("release_lock_mismatch", "Remote lock session does not match.")
    pending = SyncState(
        last_applied_manifest_root_hash=confirmed_root_hash,
        session_id=lock.session.session_id,
        machine_id=lock.session.machine_id,
        base_commit=lock.session.base_commit,
        lock_oid=lock.oid,
        local_tag=lock.local_tag,
        phase="release_pending",
        pushed_commit=pushed_commit,
    )
    if expected_state is not None:
        _recovery_guard(vault, state_path, expected_state)
    else:
        vault.assert_remote_identity()
    save_state(state_path, pending)
    _recovery_guard(vault, state_path, pending)
    result = vault.runner.run("push", f"--force-with-lease={LOCK_REF}:{lock.oid}", vault.remote, f":{LOCK_REF}", check=False)
    try: after = _remote_lock(vault)
    except SyncError: _fail("release_incomplete", "Lock deletion could not be confirmed.", EXIT_RECOVERY_REQUIRED)
    if result.returncode or after is not None: _fail("release_incomplete", "Lock release was not confirmed; recovery is required.", EXIT_RECOVERY_REQUIRED)
    if lock.local_tag:
        _recovery_guard(vault, state_path, pending)
        deleted = vault.runner.run("tag", "-d", lock.local_tag, check=False)
        if deleted.returncode: _fail("release_cleanup_incomplete", "Remote lock was released but local cleanup is incomplete.", EXIT_RECOVERY_REQUIRED)
    _recovery_guard(vault, state_path, pending)
    save_state(
        state_path,
        SyncState(
            last_applied_remote_commit=pushed_commit,
            last_applied_manifest_root_hash=confirmed_root_hash,
            # Older direct callers may not have a verified manifest root;
            # retain their legacy baseline shape rather than persisting an
            # incomplete machine-owned baseline.
            machine_id=lock.session.machine_id if confirmed_root_hash is not None else None,
        ),
    )


def release_bookmark_lock(vault: GitVault, lock: Lock, original: SyncState, *, state_path: Path) -> None:
    """Release a tag-only session while atomically preserving its old baseline."""
    if original.phase is not None or original.last_applied_remote_commit is None or original.last_applied_manifest_root_hash is None:
        _fail("bookmark_release_state_invalid", "Bookmark session has no inactive baseline to restore.")
    if vault.remote_oid() != lock.session.base_commit or _remote_lock(vault) != lock.oid:
        _fail("bookmark_release_mismatch", "Bookmark session remote state changed before release.")
    current = load_state(state_path)
    if (
        current.phase not in {"lock_held", "bookmark_release_pending"}
        or current.session_id != lock.session.session_id
        or current.machine_id != lock.session.machine_id
        or current.base_commit != lock.session.base_commit
        or current.lock_oid != lock.oid
        or current.local_tag != lock.local_tag
        or (current.phase == "bookmark_release_pending" and (
            current.last_applied_remote_commit != original.last_applied_remote_commit
            or current.last_applied_manifest_root_hash != original.last_applied_manifest_root_hash
            or current.pushed_commit != lock.session.base_commit
        ))
    ):
        _fail("bookmark_release_state_invalid", "Bookmark session state does not match its lock.")
    pending = SyncState(
        last_applied_remote_commit=original.last_applied_remote_commit,
        last_applied_manifest_root_hash=original.last_applied_manifest_root_hash,
        session_id=lock.session.session_id, machine_id=lock.session.machine_id,
        base_commit=lock.session.base_commit, lock_oid=lock.oid, local_tag=lock.local_tag,
        phase="bookmark_release_pending", pushed_commit=lock.session.base_commit,
        bookmark_ref=current.bookmark_ref, bookmark_tag_oid=current.bookmark_tag_oid,
        bookmark_root_hash=current.bookmark_root_hash,
    )
    vault.assert_remote_identity()
    save_state(state_path, pending)
    _recovery_guard(vault, state_path, pending)
    result = vault.runner.run("push", f"--force-with-lease={LOCK_REF}:{lock.oid}", vault.remote, f":{LOCK_REF}", check=False)
    try: after = _remote_lock(vault)
    except SyncError: _fail("release_incomplete", "Bookmark lock deletion could not be confirmed.", EXIT_RECOVERY_REQUIRED)
    if result.returncode or after is not None: _fail("release_incomplete", "Bookmark lock release was not confirmed.", EXIT_RECOVERY_REQUIRED)
    if lock.local_tag:
        _recovery_guard(vault, state_path, pending)
        if vault.runner.run("tag", "-d", lock.local_tag, check=False).returncode:
            _fail("release_cleanup_incomplete", "Bookmark lock was released but local cleanup is incomplete.", EXIT_RECOVERY_REQUIRED)
    _recovery_guard(vault, state_path, pending)
    save_state(state_path, original)


def _adopt_verified_snapshot_head(
    vault: GitVault,
    state: SyncState,
    remote_main: str | None,
    *,
    state_path: Path,
) -> SyncState:
    """Promote a provable one-commit snapshot before any push or ref change."""
    if state.phase != "lock_held" or state.local_commit is not None or not state.base_commit:
        _fail("recovery_commit_unproven", "Recovery state cannot adopt a local snapshot commit.")
    # A dirty worktree/index can conceal an incomplete snapshot transaction.
    status = vault.runner.run(
        "status", "--porcelain=v1", "--untracked-files=all", check=False
    )
    if status.returncode or status.stdout:
        _fail("recovery_commit_unproven", "Vault is dirty; local snapshot adoption is forbidden.")
    branch = vault.runner.run("symbolic-ref", "--quiet", "HEAD", check=False)
    if branch.returncode or branch.stdout.strip() != f"refs/heads/{vault.branch}":
        _fail("recovery_commit_unproven", "Vault HEAD is not the configured local branch.")
    head = vault.runner.run("rev-parse", "--verify", "--quiet", "HEAD", check=False).stdout.strip()
    if not _OID.fullmatch(head) or head == state.base_commit:
        _fail("recovery_commit_unproven", "Vault HEAD has no unrecorded snapshot commit.")
    parents = vault.runner.run("rev-list", "--parents", "-n", "1", head, check=False)
    fields = parents.stdout.strip().split()
    if parents.returncode or fields != [head, state.base_commit]:
        _fail("recovery_commit_unproven", "Vault HEAD is not exactly one commit after the lock base.")
    try:
        manifest = vault.validate_commit_snapshot(head)
        metadata = vault.read_vault_metadata(head)
    except SyncError as error:
        raise SyncError(
            "recovery_commit_unproven",
            "Vault HEAD is not a valid committed snapshot.",
            EXIT_CONFLICT,
        ) from error
    if (
        metadata["machine_id"] != state.machine_id
        or metadata["session_id"] != state.session_id
        or metadata["root_hash"] != manifest["root_hash"]
        or manifest["machine_id"] != state.machine_id
        or (
            state.last_applied_manifest_root_hash is not None
            and state.last_applied_manifest_root_hash != manifest["root_hash"]
        )
    ):
        _fail("recovery_commit_unproven", "Committed snapshot provenance does not match recovery state.")
    if remote_main not in {state.base_commit, head}:
        _fail("recovery_remote_diverged", "Remote main does not match the snapshot base or commit.")
    adopted = replace(
        state,
        phase="committed",
        local_commit=head,
        last_applied_manifest_root_hash=manifest["root_hash"],
    )
    _recovery_guard(vault, state_path, state)
    save_state(state_path, adopted)
    return adopted


def _release_abandoned_lock_held_session(
    vault: GitVault,
    state: SyncState,
    lock: Lock,
    remote_main: str | None,
    *,
    state_path: Path,
) -> str:
    """Release a lock acquired before any snapshot transaction began.

    This is deliberately narrower than ordinary recovery.  It verifies the
    remote base snapshot before using it as the applied baseline: a live save
    may have changed after a launcher failure, but the verified base remains
    the correct *last applied* point for a later three-way reconciliation.
    """
    if (
        state.phase != "lock_held"
        or state.local_commit is not None
        or state.pushed_commit is not None
        or remote_main != state.base_commit
    ):
        _fail("recovery_abandon_unproven", "The abandoned lock has commit activity or a changed remote.")
    status = vault.runner.run("status", "--porcelain=v1", "--untracked-files=all", check=False)
    branch = vault.runner.run("symbolic-ref", "--quiet", "HEAD", check=False)
    head = vault.runner.run("rev-parse", "--verify", "--quiet", "HEAD", check=False).stdout.strip()
    if (
        status.returncode
        or status.stdout
        or branch.returncode
        or branch.stdout.strip() != f"refs/heads/{vault.branch}"
        or head != state.base_commit
    ):
        _fail("recovery_abandon_unproven", "The local vault is not exactly at the lock base.")

    try:
        manifest = vault.validate_commit_snapshot(state.base_commit)
    except SyncError as error:
        raise SyncError(
            "recovery_abandon_unproven",
            "The lock base is not a valid remote snapshot.",
            EXIT_CONFLICT,
        ) from error
    release_lock(
        vault,
        lock,
        state.base_commit,
        state_path=state_path,
        confirmed_root_hash=manifest["root_hash"],
        expected_state=state,
    )
    return "abandoned_lock_released"


def recover_session(vault: GitVault, state: SyncState, machine_id: str, *, state_path: Path | None = None) -> str:
    """Recover only an exactly matching local session; otherwise make no change."""
    if state_path is None: _fail("state_required", "Recovery requires terminal-local state.", EXIT_RECOVERY_REQUIRED)
    _recovery_guard(vault, state_path, state)
    if state.phase == "bootstrap_pending":
        _resume_bootstrap(vault, state, machine_id, state_path=state_path)
        return "bootstrap_complete"
    if not state.session_id or state.machine_id != machine_id or not state.base_commit or not state.lock_oid or not state.local_tag: _fail("recovery_state_invalid", "Recovery state is incomplete.")
    remote = _remote_lock(vault); main = vault.remote_oid()
    if remote is None:
        if state.phase == "bookmark_publish_pending" and main == state.base_commit:
            rows = {row[0]: row for row in vault._managed_bookmark_rows(remote=True)}
            row = rows.get(state.bookmark_ref or "")
            if row is not None and (row[1] != state.bookmark_tag_oid or row[2] != state.local_commit
                                    or vault.validate_commit_snapshot(row[2]).get("root_hash") != state.bookmark_root_hash):
                _fail("bookmark_recovery_mismatch", "Remote bookmark does not match the persisted publication intent.", EXIT_RECOVERY_REQUIRED)
            if row is None and state.bookmark_ref:
                _recovery_guard(vault, state_path, state)
                deleted = vault.runner.run("tag", "-d", state.bookmark_ref, check=False)
                if deleted.returncode:
                    _fail("release_cleanup_incomplete", "Unpublished local bookmark cleanup is incomplete.", EXIT_RECOVERY_REQUIRED)
            present = vault.runner.run("rev-parse", "--verify", "--quiet", f"refs/tags/{state.local_tag}", check=False).returncode == 0
            if present:
                _recovery_guard(vault, state_path, state)
                if vault.runner.run("tag", "-d", state.local_tag, check=False).returncode:
                    _fail("release_cleanup_incomplete", "Remote bookmark lock is absent but local cleanup is incomplete.", EXIT_RECOVERY_REQUIRED)
            _recovery_guard(vault, state_path, state)
            save_state(state_path, SyncState(
                last_applied_remote_commit=state.last_applied_remote_commit,
                last_applied_manifest_root_hash=state.last_applied_manifest_root_hash,
                machine_id=machine_id,
            ))
            return "bookmark_complete" if row is not None else "bookmark_not_published"
        if state.phase == "bookmark_release_pending" and main == state.base_commit:
            present = vault.runner.run("rev-parse", "--verify", "--quiet", f"refs/tags/{state.local_tag}", check=False).returncode == 0
            if present:
                _recovery_guard(vault, state_path, state)
                if vault.runner.run("tag", "-d", state.local_tag, check=False).returncode:
                    _fail("release_cleanup_incomplete", "Remote bookmark lock is absent but local cleanup is incomplete.", EXIT_RECOVERY_REQUIRED)
            _recovery_guard(vault, state_path, state)
            save_state(
                state_path,
                SyncState(
                    last_applied_remote_commit=state.last_applied_remote_commit,
                    last_applied_manifest_root_hash=state.last_applied_manifest_root_hash,
                    machine_id=machine_id,
                ),
            )
            return "bookmark_complete"
        if state.pushed_commit and main == state.pushed_commit:
            present = vault.runner.run("rev-parse", "--verify", "--quiet", f"refs/tags/{state.local_tag}", check=False).returncode == 0
            if present:
                _recovery_guard(vault, state_path, state)
                if vault.runner.run("tag", "-d", state.local_tag, check=False).returncode:
                    _fail("release_cleanup_incomplete", "Remote lock is absent but local cleanup is incomplete.", EXIT_RECOVERY_REQUIRED)
            _recovery_guard(vault, state_path, state)
            save_state(
                state_path,
                SyncState(
                    last_applied_remote_commit=state.pushed_commit,
                    last_applied_manifest_root_hash=state.last_applied_manifest_root_hash,
                    machine_id=machine_id if state.last_applied_manifest_root_hash is not None else None,
                ),
            )
            return "complete"
        _fail("recovery_lock_missing", "Recovery lock is absent without a confirmed push.")
    local_session = _inspect_local_tag(vault, state.local_tag, state.lock_oid)
    if local_session.session_id != state.session_id or local_session.machine_id != machine_id or local_session.base_commit != state.base_commit:
        _fail("recovery_local_tag_mismatch", "Local recovery tag payload does not match state.")
    lock = inspect_remote_lock_readonly(vault)
    if lock is None or remote != state.lock_oid or lock.session != local_session: _fail("recovery_lock_mismatch", "Remote lock belongs to another session.")
    if state.phase == "bookmark_publish_pending":
        def publication_guard() -> None:
            _recovery_guard(vault, state_path, state)
            if vault.remote_oid() != state.base_commit:
                _fail("bookmark_recovery_mismatch", "Remote main changed during bookmark recovery.", EXIT_RECOVERY_REQUIRED)
            owned = inspect_remote_lock_readonly(vault)
            if owned is None or owned.oid != state.lock_oid or owned.session != lock.session:
                _fail("bookmark_recovery_mismatch", "Remote lock changed during bookmark recovery.", EXIT_RECOVERY_REQUIRED)

        vault.publish_managed_bookmark_intent(
            state.bookmark_ref or "", state.bookmark_tag_oid or "", state.local_commit or "",
            state.bookmark_root_hash or "", expected_remote_head=state.base_commit,
            expected_lock_oid=state.lock_oid, publication_guard=publication_guard,
        )
        confirmed = replace(state, phase="bookmark_release_pending", local_commit=None,
                            pushed_commit=state.base_commit)
        _recovery_guard(vault, state_path, state)
        save_state(state_path, confirmed)
        state = confirmed
    if state.phase == "bookmark_release_pending":
        release_bookmark_lock(
            vault,
            Lock(lock.session, remote, state.local_tag),
            SyncState(
                last_applied_remote_commit=state.last_applied_remote_commit,
                last_applied_manifest_root_hash=state.last_applied_manifest_root_hash,
                machine_id=machine_id,
            ),
            state_path=state_path,
        )
        return "bookmark_released"
    if state.phase == "lock_held" and state.local_commit is None:
        # A clean vault still at the base proves that no snapshot commit was
        # started.  This is the only case in which a failed launch may abandon
        # its lock without changing main or adopting a commit.
        head = vault.runner.run("rev-parse", "--verify", "--quiet", "HEAD", check=False).stdout.strip()
        if head == state.base_commit:
            return _release_abandoned_lock_held_session(
                vault, state, Lock(lock.session, remote, state.local_tag), main,
                state_path=state_path,
            )
        state = _adopt_verified_snapshot_head(vault, state, main, state_path=state_path)
    if state.local_commit and not state.pushed_commit:
        if main == state.base_commit:
            try: pushed = vault.push(state.local_commit)
            except SyncError: raise
            state = replace(state, pushed_commit=pushed, phase="pushed")
            if state_path:
                _recovery_guard(vault, state_path, replace(state, pushed_commit=None, phase="committed"))
                save_state(state_path, state)
            main = vault.remote_oid()
        elif main == state.local_commit:
            previous = state
            state = replace(state, pushed_commit=state.local_commit, phase="pushed")
            _recovery_guard(vault, state_path, previous)
            save_state(state_path, state)
        else: _fail("recovery_remote_diverged", "Remote main advanced unexpectedly.")
    if state.pushed_commit:
        if main != state.pushed_commit: _fail("recovery_remote_diverged", "Remote main does not match the recovered commit.")
        release_lock(
            vault,
            Lock(lock.session, remote, state.local_tag),
            state.pushed_commit,
            state_path=state_path,
            confirmed_root_hash=state.last_applied_manifest_root_hash,
            expected_state=state,
        )
        return "released"
    _fail("recovery_no_commit", "Recovery has no local commit to push.")
