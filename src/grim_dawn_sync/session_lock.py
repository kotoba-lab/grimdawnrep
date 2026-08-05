"""Remote annotated-tag session locks and conservative recovery primitives.

This module deliberately has no launcher/DPYes knowledge.  It is safe to call
from a workflow only after its own preflight has established a clean vault.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json, re, tempfile, time, uuid
from pathlib import Path

from grim_dawn_sync.errors import EXIT_CONFLICT, EXIT_RECOVERY_REQUIRED, SyncError
from grim_dawn_sync.git_vault import GitRunner, GitVault, _OID
from grim_dawn_sync.state import SyncState, load_state, save_state_if_unchanged

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


def _save_recovery_transition(state_path: Path, expected: SyncState, replacement: SyncState) -> None:
    """Perform one exact recovery-state CAS and normalize every race to exit 6."""
    try:
        save_state_if_unchanged(state_path, expected, replacement)
    except SyncError as error:
        raise SyncError(
            "recovery_state_changed",
            "Recovery state changed before its exact transition.",
            EXIT_RECOVERY_REQUIRED,
            {**error.details, "session_id": expected.session_id},
        ) from error

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
    if oid != expected_oid:
        _fail("recovery_local_tag_mismatch", "Local recovery tag does not match state.")
    return _inspect_local_tag_object(vault, tag, expected_oid)


def _inspect_local_tag_object(vault: GitVault, tag: str, expected_oid: str) -> Session:
    if vault.runner.run("cat-file", "-t", expected_oid, check=False).stdout.strip() != "tag":
        _fail("recovery_local_tag_mismatch", "Local recovery tag object does not match state.")
    raw = vault.runner.run("cat-file", "-p", expected_oid, check=False).stdout
    if "\n\n" not in raw: _fail("recovery_local_tag_mismatch", "Local recovery tag is malformed.")
    header, body = raw.split("\n\n", 1)
    try: session = _session(json.loads(body))
    except (json.JSONDecodeError, UnicodeError) as error:
        raise SyncError("recovery_local_tag_mismatch", "Local recovery tag is malformed.", EXIT_CONFLICT) from error
    fields = dict(line.split(" ", 1) for line in header.splitlines() if " " in line)
    canonical = json.dumps(session.payload(), sort_keys=True, separators=(",", ":")) + "\n"
    if (fields.get("object") != session.base_commit or fields.get("type") != "commit"
            or fields.get("tag") != tag or body != canonical):
        _fail("recovery_local_tag_mismatch", "Local recovery tag is malformed.")
    return session


def _delete_exact_local_session_ref(
    vault: GitVault,
    tag: str | None,
    expected_oid: str,
    *,
    expected_identity: tuple[str | None, str | None, str | None] | None = None,
) -> None:
    """Idempotently delete only the exact canonical local session ref."""
    if not tag:
        return
    ref = f"refs/tags/{tag}"
    resolved = vault.runner.run("rev-parse", "--verify", "--quiet", ref, check=False)
    actual = resolved.stdout.strip()
    if resolved.returncode == 1 and not actual:
        return
    if resolved.returncode or actual != expected_oid:
        _fail(
            "recovery_local_tag_mismatch",
            "Local recovery ref does not match its persisted lock object.",
            EXIT_RECOVERY_REQUIRED,
        )
    session = _inspect_local_tag_object(vault, tag, expected_oid)
    if expected_identity is not None and (
        session.session_id, session.machine_id, session.base_commit
    ) != expected_identity:
        _fail(
            "recovery_local_tag_mismatch",
            "Local recovery tag payload does not match persisted state.",
            EXIT_RECOVERY_REQUIRED,
        )
    deleted = vault.runner.run("update-ref", "-d", ref, expected_oid, check=False)
    remaining = vault.runner.run("rev-parse", "--verify", "--quiet", ref, check=False).stdout.strip()
    if deleted.returncode and remaining != expected_oid:
        _fail(
            "recovery_local_tag_mismatch",
            "Local recovery ref changed before exact cleanup.",
            EXIT_RECOVERY_REQUIRED,
        )
    if deleted.returncode or remaining:
        _fail("release_cleanup_incomplete", "Local recovery ref cleanup is incomplete.", EXIT_RECOVERY_REQUIRED)

def acquire_lock(vault: GitVault, machine_id: str, base_commit: str, *, state_path: Path | None = None,
                 expected_pre_state: SyncState | None = None) -> Lock:
    vault.assert_remote_identity()
    if state_path is None: _fail("state_required", "Lock acquisition requires terminal-local state.", EXIT_RECOVERY_REQUIRED)
    if vault.remote_oid() != base_commit: _fail("stale_lock_base", "Remote main no longer matches the lock base.")
    if _remote_lock(vault) is not None: _fail("lock_held", "A remote save-sync lock already exists.")
    session = new_session(machine_id, base_commit); tag = f"grim-dawn-sync-{session.session_id}"
    message = json.dumps(session.payload(), sort_keys=True, separators=(",", ":"))
    tag_object = (
        f"object {base_commit}\n"
        "type commit\n"
        f"tag {tag}\n"
        f"tagger Grim Dawn Save Sync <grim-dawn-sync@localhost> {int(time.time())} +0000\n"
        f"\n{message}\n"
    )
    made = vault.runner.run_bytes_input_result("mktag", input_bytes=tag_object.encode("utf-8"))
    oid = made.stdout.strip()
    if made.returncode or not _OID.fullmatch(oid):
        _fail("lock_create_failed", "Could not create an annotated session tag object.", EXIT_RECOVERY_REQUIRED)
    _inspect_local_tag_object(vault, tag, oid)
    original_state = expected_pre_state if expected_pre_state is not None else SyncState()
    intent = SyncState(
        last_applied_remote_commit=original_state.last_applied_remote_commit,
        last_applied_manifest_root_hash=original_state.last_applied_manifest_root_hash,
        session_id=session.session_id, machine_id=machine_id, base_commit=base_commit,
        lock_oid=oid, local_tag=tag, phase="lock_held",
    )
    try:
        save_state_if_unchanged(state_path, original_state, intent)
    except SyncError as error:
        if error.code in {"selection_stale", "state_busy"}:
            raise SyncError("selection_stale", "Recovery state changed before lock acquisition.", EXIT_CONFLICT) from None
        raise
    ref = f"refs/tags/{tag}"
    created = vault.runner.run("update-ref", ref, oid, "0" * len(oid), check=False)
    actual = vault.runner.run("rev-parse", "--verify", "--quiet", ref, check=False).stdout.strip()
    if created.returncode or actual != oid:
        _fail("lock_create_failed", "Could not create the persisted session tag ref.", EXIT_RECOVERY_REQUIRED)
    _inspect_local_tag(vault, tag, oid)

    def cleanup_unpushed_intent(code: str, detail: str) -> None:
        """Delete only the exact local ref, then restore the exact pre-state."""
        try:
            _delete_exact_local_session_ref(
                vault, tag, oid,
                expected_identity=(session.session_id, session.machine_id, session.base_commit),
            )
        except SyncError as error:
            raise SyncError(
                "lock_race_cleanup_incomplete", "Local lock cleanup is incomplete.",
                EXIT_RECOVERY_REQUIRED, {**error.details, "session_id": session.session_id},
            ) from error
        try:
            save_state_if_unchanged(state_path, intent, original_state)
        except SyncError:
            _fail("lock_race_cleanup_incomplete", "Lock state cleanup is incomplete.", EXIT_RECOVERY_REQUIRED)
        _fail(code, detail)
    if vault.remote_oid() != base_commit:
        cleanup_unpushed_intent("stale_lock_base", "Remote main changed before lock push.")
    vault.assert_remote_identity()
    pushed = vault.runner.run("push", vault.remote, f"{oid}:{LOCK_REF}", check=False)
    try: remote = _remote_lock(vault)
    except SyncError:
        _fail("lock_push_unknown", "Lock acquisition result is unknown; recovery artifacts were retained.", EXIT_RECOVERY_REQUIRED, {"session_id":session.session_id,"machine_id":machine_id,"base_commit":base_commit,"lock_oid":oid,"local_tag":tag})
    if remote == oid:
        return Lock(session, oid, tag)
    if remote is not None:
        cleanup_unpushed_intent("lock_race_lost", "Another machine acquired the remote lock.")
    _fail("lock_push_unknown" if pushed.returncode else "lock_unconfirmed", "Lock acquisition was not confirmed; local recovery artifacts were retained.", EXIT_RECOVERY_REQUIRED, {"session_id":session.session_id,"machine_id":machine_id,"base_commit":base_commit,"lock_oid":oid,"local_tag":tag})


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
    _save_recovery_transition(
        state_path,
        state,
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
    save_state_if_unchanged(state_path, SyncState(), pending)
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
    save_state_if_unchanged(state_path, state, applied)
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
    current = expected_state if expected_state is not None else load_state(state_path)
    _recovery_guard(vault, state_path, current)
    _save_recovery_transition(state_path, current, pending)
    _recovery_guard(vault, state_path, pending)
    result = vault.runner.run("push", f"--force-with-lease={LOCK_REF}:{lock.oid}", vault.remote, f":{LOCK_REF}", check=False)
    try: after = _remote_lock(vault)
    except SyncError: _fail("release_incomplete", "Lock deletion could not be confirmed.", EXIT_RECOVERY_REQUIRED)
    if result.returncode or after is not None: _fail("release_incomplete", "Lock release was not confirmed; recovery is required.", EXIT_RECOVERY_REQUIRED)
    if lock.local_tag:
        _recovery_guard(vault, state_path, pending)
        _delete_exact_local_session_ref(
            vault, lock.local_tag, lock.oid,
            expected_identity=(lock.session.session_id, lock.session.machine_id, lock.session.base_commit),
        )
    _recovery_guard(vault, state_path, pending)
    _save_recovery_transition(
        state_path,
        pending,
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
    original_has_baseline = original.last_applied_remote_commit is not None
    if (
        original.phase is not None
        or original_has_baseline != (original.last_applied_manifest_root_hash is not None)
        or (original_has_baseline and original.machine_id != lock.session.machine_id)
        or (not original_has_baseline and original != SyncState())
    ):
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
        or current.last_applied_remote_commit != original.last_applied_remote_commit
        or current.last_applied_manifest_root_hash != original.last_applied_manifest_root_hash
        or (current.phase == "bookmark_release_pending" and current.pushed_commit != lock.session.base_commit)
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
    try:
        save_state_if_unchanged(state_path, current, pending)
    except SyncError as error:
        raise SyncError(
            "bookmark_release_state_changed",
            "Bookmark release state changed before lock deletion; recovery artifacts were retained.",
            EXIT_RECOVERY_REQUIRED,
            {**error.details, "session_id": lock.session.session_id},
        ) from error
    _recovery_guard(vault, state_path, pending)
    result = vault.runner.run("push", f"--force-with-lease={LOCK_REF}:{lock.oid}", vault.remote, f":{LOCK_REF}", check=False)
    try: after = _remote_lock(vault)
    except SyncError: _fail("release_incomplete", "Bookmark lock deletion could not be confirmed.", EXIT_RECOVERY_REQUIRED)
    if result.returncode or after is not None: _fail("release_incomplete", "Bookmark lock release was not confirmed.", EXIT_RECOVERY_REQUIRED)
    if lock.local_tag:
        _recovery_guard(vault, state_path, pending)
        _delete_exact_local_session_ref(
            vault, lock.local_tag, lock.oid,
            expected_identity=(lock.session.session_id, lock.session.machine_id, lock.session.base_commit),
        )
    _recovery_guard(vault, state_path, pending)
    try:
        save_state_if_unchanged(state_path, pending, original)
    except SyncError as error:
        raise SyncError(
            "bookmark_release_state_changed",
            "Bookmark release state changed before baseline restoration.",
            EXIT_RECOVERY_REQUIRED,
            {**error.details, "session_id": lock.session.session_id},
        ) from error


def release_without_publish(vault: GitVault, lock: Lock, original: SyncState, *, state_path: Path) -> None:
    """Release a lock that made no Git commit, without publishing anything.

    This is the exit-disposition counterpart to ``release_bookmark_lock``: it
    is used when a launch's ``local-only`` or ``restore-startup`` disposition
    ends the session after ``archive_after_game`` (a local-only archive) but
    deliberately never commits or pushes.  ``original`` must be the exact
    pre-lock baseline, restored byte-for-byte; ``last_applied_remote_commit``
    and ``last_applied_manifest_root_hash`` are left untouched so the next
    launch on this terminal correctly observes ``LIVE_AHEAD``.
    """
    original_has_baseline = original.last_applied_remote_commit is not None
    if (
        original.phase is not None
        or original_has_baseline != (original.last_applied_manifest_root_hash is not None)
        or (original_has_baseline and original.machine_id != lock.session.machine_id)
        or (not original_has_baseline and original != SyncState())
    ):
        _fail("unpublished_release_state_invalid", "Unpublished session has no inactive baseline to restore.")
    if vault.remote_oid() != lock.session.base_commit or _remote_lock(vault) != lock.oid:
        _fail("unpublished_release_mismatch", "Unpublished session remote state changed before release.")
    current = load_state(state_path)
    if (
        current.phase not in {"lock_held", "unpublished_release_pending"}
        or current.session_id != lock.session.session_id
        or current.machine_id != lock.session.machine_id
        or current.base_commit != lock.session.base_commit
        or current.lock_oid != lock.oid
        or current.local_tag != lock.local_tag
        or current.last_applied_remote_commit != original.last_applied_remote_commit
        or current.last_applied_manifest_root_hash != original.last_applied_manifest_root_hash
        or (current.phase == "unpublished_release_pending" and current.pushed_commit != lock.session.base_commit)
    ):
        _fail("unpublished_release_state_invalid", "Unpublished session state does not match its lock.")
    pending = SyncState(
        last_applied_remote_commit=original.last_applied_remote_commit,
        last_applied_manifest_root_hash=original.last_applied_manifest_root_hash,
        session_id=lock.session.session_id, machine_id=lock.session.machine_id,
        base_commit=lock.session.base_commit, lock_oid=lock.oid, local_tag=lock.local_tag,
        phase="unpublished_release_pending", pushed_commit=lock.session.base_commit,
    )
    vault.assert_remote_identity()
    try:
        save_state_if_unchanged(state_path, current, pending)
    except SyncError as error:
        raise SyncError(
            "unpublished_release_state_changed",
            "Unpublished release state changed before lock deletion; recovery artifacts were retained.",
            EXIT_RECOVERY_REQUIRED,
            {**error.details, "session_id": lock.session.session_id},
        ) from error
    _recovery_guard(vault, state_path, pending)
    result = vault.runner.run("push", f"--force-with-lease={LOCK_REF}:{lock.oid}", vault.remote, f":{LOCK_REF}", check=False)
    try: after = _remote_lock(vault)
    except SyncError: _fail("release_incomplete", "Unpublished lock deletion could not be confirmed.", EXIT_RECOVERY_REQUIRED)
    if result.returncode or after is not None: _fail("release_incomplete", "Unpublished lock release was not confirmed.", EXIT_RECOVERY_REQUIRED)
    if lock.local_tag:
        _recovery_guard(vault, state_path, pending)
        _delete_exact_local_session_ref(
            vault, lock.local_tag, lock.oid,
            expected_identity=(lock.session.session_id, lock.session.machine_id, lock.session.base_commit),
        )
    _recovery_guard(vault, state_path, pending)
    try:
        save_state_if_unchanged(state_path, pending, original)
    except SyncError as error:
        raise SyncError(
            "unpublished_release_state_changed",
            "Unpublished release state changed before baseline restoration.",
            EXIT_RECOVERY_REQUIRED,
            {**error.details, "session_id": lock.session.session_id},
        ) from error


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
    _save_recovery_transition(state_path, state, adopted)
    return adopted


def _original_baseline(state: SyncState) -> SyncState:
    """Recover the exact inactive baseline carried through an active lock."""
    has_commit = state.last_applied_remote_commit is not None
    if has_commit != (state.last_applied_manifest_root_hash is not None):
        _fail("recovery_state_invalid", "Recovery baseline is incomplete.", EXIT_RECOVERY_REQUIRED)
    if not has_commit:
        return SyncState()
    return SyncState(
        last_applied_remote_commit=state.last_applied_remote_commit,
        last_applied_manifest_root_hash=state.last_applied_manifest_root_hash,
        machine_id=state.machine_id,
    )


def _delete_exact_unpublished_bookmark(vault: GitVault, state: SyncState) -> None:
    """Delete only the exact local managed tag named by a persisted intent."""
    name = state.bookmark_ref or ""
    ref = f"refs/tags/{name}"
    resolved = vault.runner.run("rev-parse", "--verify", "--quiet", ref, check=False)
    actual_tag = resolved.stdout.strip()
    if resolved.returncode == 1 and not actual_tag:
        # Intent persistence deliberately precedes local ref creation.  A hard
        # crash in that gap leaves only the tracked object and needs no ref
        # cleanup when the remote publication also did not happen.
        return
    if resolved.returncode:
        _fail("release_cleanup_incomplete", "Local bookmark ref could not be inspected.", EXIT_RECOVERY_REQUIRED)
    actual_target = vault.runner.run("rev-parse", "--verify", "--quiet", f"{ref}^{{}}", check=False).stdout.strip()
    actual_type = vault.runner.run("cat-file", "-t", ref, check=False).stdout.strip()
    shown = vault.runner.run("cat-file", "tag", ref, check=False)
    if (
        actual_tag != state.bookmark_tag_oid
        or actual_target != state.local_commit
        or actual_type != "tag"
        or shown.returncode
        or "\n\n" not in shown.stdout
    ):
        _fail(
            "bookmark_local_intent_mismatch",
            "Local bookmark ref does not match the persisted unpublished intent.",
            EXIT_RECOVERY_REQUIRED,
        )
    try:
        from grim_dawn_sync.bookmarks import parse_bookmark_annotation
        parse_bookmark_annotation(shown.stdout.split("\n\n", 1)[1])
    except (ImportError, SyncError):
        _fail(
            "bookmark_local_intent_mismatch",
            "Local bookmark annotation does not match a managed bookmark.",
            EXIT_RECOVERY_REQUIRED,
        )
    # Delete with the verified tag object as the old value.  A concurrent ref
    # replacement after validation must never be removed.
    deleted = vault.runner.run("update-ref", "-d", ref, state.bookmark_tag_oid or "", check=False)
    remaining = vault.runner.run("rev-parse", "--verify", "--quiet", ref, check=False).stdout.strip()
    if deleted.returncode and remaining != state.bookmark_tag_oid:
        _fail(
            "bookmark_local_intent_mismatch",
            "Local bookmark ref changed before exact cleanup.",
            EXIT_RECOVERY_REQUIRED,
        )
    if deleted.returncode or remaining:
        _fail("release_cleanup_incomplete", "Unpublished local bookmark cleanup is incomplete.", EXIT_RECOVERY_REQUIRED)


def _recover_unpublished_lock_intent(vault: GitVault, state: SyncState, *, state_path: Path) -> str:
    """Abandon an exact local-only lock intent without touching remote refs."""
    if state.phase != "lock_held" or state.local_commit is not None or state.pushed_commit is not None:
        _fail("recovery_lock_missing", "Recovery lock is absent without a local-only lock intent.")
    session = _inspect_local_tag_object(vault, state.local_tag or "", state.lock_oid or "")
    if (session.session_id != state.session_id or session.machine_id != state.machine_id
            or session.base_commit != state.base_commit):
        _fail("recovery_local_tag_mismatch", "Local recovery tag payload does not match state.")
    ref = f"refs/tags/{state.local_tag}"
    resolved = vault.runner.run("rev-parse", "--verify", "--quiet", ref, check=False)
    local_oid = resolved.stdout.strip()
    if resolved.returncode not in {0, 1} or (local_oid and local_oid != state.lock_oid):
        _fail("recovery_local_tag_mismatch", "Local recovery ref changed after lock intent persistence.")
    status = vault.runner.run("status", "--porcelain=v1", "--untracked-files=all", check=False)
    branch = vault.runner.run("symbolic-ref", "--quiet", "HEAD", check=False)
    head = vault.runner.run("rev-parse", "--verify", "--quiet", "HEAD", check=False).stdout.strip()
    if (status.returncode or status.stdout or branch.returncode
            or branch.stdout.strip() != f"refs/heads/{vault.branch}" or head != state.base_commit):
        _fail("recovery_abandon_unproven", "The local vault is not exactly at the unpublished lock base.")
    _recovery_guard(vault, state_path, state)
    if local_oid:
        _delete_exact_local_session_ref(
            vault, state.local_tag, state.lock_oid or "",
            expected_identity=(state.session_id, state.machine_id, state.base_commit),
        )
    _save_recovery_transition(state_path, state, _original_baseline(state))
    return "lock_not_acquired"


def _release_abandoned_lock_held_session(
    vault: GitVault,
    state: SyncState,
    lock: Lock,
    remote_main: str | None,
    *,
    state_path: Path,
) -> str:
    """Release a lock acquired before any snapshot transaction began.

    This is deliberately narrower than ordinary recovery.  The remote base is
    only the lock lease; the exact pre-lock applied baseline is carried in
    state and must be restored without reinterpretation.
    """
    if (
        state.phase != "lock_held"
        or state.local_commit is not None
        or state.pushed_commit is not None
        or remote_main != state.base_commit
    ):
        _fail("recovery_abandon_unproven", "The abandoned lock has commit activity or a changed remote.")
    try:
        vault.validate_commit_snapshot(state.base_commit)
    except SyncError:
        _fail("recovery_abandon_unproven", "The abandoned lock base is not a verified save snapshot.")
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

    release_bookmark_lock(vault, lock, _original_baseline(state), state_path=state_path)
    return "abandoned_lock_released"


def _recover_session(vault: GitVault, state: SyncState, machine_id: str, *, state_path: Path | None = None) -> str:
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
                _delete_exact_unpublished_bookmark(vault, state)
            _recovery_guard(vault, state_path, state)
            _delete_exact_local_session_ref(
                vault, state.local_tag, state.lock_oid or "",
                expected_identity=(state.session_id, state.machine_id, state.base_commit),
            )
            _recovery_guard(vault, state_path, state)
            _save_recovery_transition(state_path, state, _original_baseline(state))
            return "bookmark_complete" if row is not None else "bookmark_not_published"
        if state.phase == "bookmark_release_pending" and main == state.base_commit:
            _recovery_guard(vault, state_path, state)
            _delete_exact_local_session_ref(
                vault, state.local_tag, state.lock_oid or "",
                expected_identity=(state.session_id, state.machine_id, state.base_commit),
            )
            _recovery_guard(vault, state_path, state)
            _save_recovery_transition(state_path, state, _original_baseline(state))
            return "bookmark_complete"
        if state.phase == "unpublished_release_pending" and main == state.base_commit:
            _recovery_guard(vault, state_path, state)
            _delete_exact_local_session_ref(
                vault, state.local_tag, state.lock_oid or "",
                expected_identity=(state.session_id, state.machine_id, state.base_commit),
            )
            _recovery_guard(vault, state_path, state)
            _save_recovery_transition(state_path, state, _original_baseline(state))
            return "unpublished_release_complete"
        if state.pushed_commit and main == state.pushed_commit:
            _recovery_guard(vault, state_path, state)
            _delete_exact_local_session_ref(
                vault, state.local_tag, state.lock_oid or "",
                expected_identity=(state.session_id, state.machine_id, state.base_commit),
            )
            _recovery_guard(vault, state_path, state)
            _save_recovery_transition(
                state_path,
                state,
                SyncState(
                    last_applied_remote_commit=state.pushed_commit,
                    last_applied_manifest_root_hash=state.last_applied_manifest_root_hash,
                    machine_id=machine_id if state.last_applied_manifest_root_hash is not None else None,
                ),
            )
            return "complete"
        if state.phase == "lock_held" and main == state.base_commit:
            return _recover_unpublished_lock_intent(vault, state, state_path=state_path)
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
        try:
            save_state_if_unchanged(state_path, state, confirmed)
        except SyncError as error:
            raise SyncError(
                "bookmark_recovery_state_changed",
                "Bookmark recovery state changed after remote publication; recovery artifacts were retained.",
                EXIT_RECOVERY_REQUIRED,
                {**error.details, "session_id": state.session_id},
            ) from error
        state = confirmed
    if state.phase == "bookmark_release_pending":
        release_bookmark_lock(
            vault,
            Lock(lock.session, remote, state.local_tag),
            _original_baseline(state),
            state_path=state_path,
        )
        return "bookmark_released"
    if state.phase == "unpublished_release_pending":
        release_without_publish(
            vault,
            Lock(lock.session, remote, state.local_tag),
            _original_baseline(state),
            state_path=state_path,
        )
        return "unpublished_release_complete"
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
                previous = replace(state, pushed_commit=None, phase="committed")
                _recovery_guard(vault, state_path, previous)
                _save_recovery_transition(state_path, previous, state)
            main = vault.remote_oid()
        elif main == state.local_commit:
            previous = state
            state = replace(state, pushed_commit=state.local_commit, phase="pushed")
            _recovery_guard(vault, state_path, previous)
            _save_recovery_transition(state_path, previous, state)
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


def recover_session(vault: GitVault, state: SyncState, machine_id: str, *, state_path: Path | None = None) -> str:
    """Recover a session while keeping every persisted bookmark intent recoverable."""
    bookmark_pending = state.phase in {"bookmark_publish_pending", "bookmark_release_pending", "unpublished_release_pending"}
    try:
        return _recover_session(vault, state, machine_id, state_path=state_path)
    except SyncError as error:
        if not bookmark_pending or error.exit_code == EXIT_RECOVERY_REQUIRED:
            raise
        raise SyncError(
            error.code,
            error.message,
            EXIT_RECOVERY_REQUIRED,
            {**error.details, "session_id": state.session_id},
        ) from error
    except Exception as error:
        if not bookmark_pending:
            raise
        raise SyncError(
            "bookmark_recovery_incomplete",
            "Bookmark recovery was interrupted; retry recover with the same persisted intent.",
            EXIT_RECOVERY_REQUIRED,
            {"session_id": state.session_id},
        ) from error
