from __future__ import annotations

from pathlib import Path
import time as time_module
import pytest

from grim_dawn_sync.git_vault import GitVault
from grim_dawn_sync.errors import SyncError
from grim_dawn_sync.manifest import build_manifest
from grim_dawn_sync.session_start import create_session_start_archive
from grim_dawn_sync.version_catalog import RemoteCheckReport, VersionCatalogBuilder, _diff
from test_save_sync_git_vault import checkout_remote_main, clone_pair, git, save, valid


def test_remote_history_is_bounded_newest_first(tmp_path: Path) -> None:
    _, one, _ = clone_pair(tmp_path); vault = GitVault(one); source = save(tmp_path / "source", b"one")
    first = vault.snapshot(source, machine_id="a", session_id="one", validator=valid); vault.push(first)
    save(source, b"two"); second = vault.snapshot(source, machine_id="a", session_id="two", validator=valid); vault.push(second)
    vault.fetch()
    assert vault.remote_history(limit=1) == (second,)


def test_catalog_is_read_only_deduplicates_roots_and_has_live_diffs(tmp_path: Path) -> None:
    _, one, _ = clone_pair(tmp_path); source = save(tmp_path / "source", b"one"); vault = GitVault(one)
    first = vault.snapshot(source, machine_id="a", session_id="one", validator=valid); vault.push(first)
    # A second snapshot with identical content must not make a duplicate choice.
    second = vault.snapshot(source, machine_id="a", session_id="two", validator=valid); vault.push(second)
    before = {path.relative_to(one).as_posix() for path in one.rglob("*") if ".git" not in path.relative_to(one).parts}
    catalog = VersionCatalogBuilder(vault, source, machine_id="a").build(history_limit=10)
    after = {path.relative_to(one).as_posix() for path in one.rglob("*") if ".git" not in path.relative_to(one).parts}
    assert before == after and len(catalog.candidates) == 1
    assert catalog.candidates[0].kind == "live" and catalog.candidates[0].diff_from_live.added == 0


def test_catalog_lists_remote_head_and_history_in_order(tmp_path: Path) -> None:
    _, one, _ = clone_pair(tmp_path); source = save(tmp_path / "source", b"one"); vault = GitVault(one)
    first = vault.snapshot(source, machine_id="a", session_id="one", validator=valid); vault.push(first)
    save(source, b"two"); second = vault.snapshot(source, machine_id="a", session_id="two", validator=valid); vault.push(second)
    catalog = VersionCatalogBuilder(vault, source, machine_id="a").build(history_limit=10)
    # The locally live root is integrated with the identical remote head, so
    # only the distinct historical root remains selectable.
    assert [(item.kind, item.commit) for item in catalog.candidates[1:]] == [("history", first)]


def test_catalog_lists_distinct_legacy_annotated_tag_read_only(tmp_path: Path) -> None:
    _, one, _ = clone_pair(tmp_path); source = save(tmp_path / "source", b"old"); vault = GitVault(one)
    old = vault.snapshot(source, machine_id="a", session_id="old", validator=valid); vault.push(old)
    save(source, b"new"); new = vault.snapshot(source, machine_id="a", session_id="new", validator=valid); vault.push(new)
    git(one, "tag", "-a", "archive/old-save", old, "-m", "legacy")
    catalog = VersionCatalogBuilder(vault, source, machine_id="a").build(history_limit=1)
    assert ("legacy", old) in [(item.kind, item.commit) for item in catalog.candidates]


def test_same_root_keeps_remote_and_named_bookmark_as_selectable_aliases(tmp_path: Path) -> None:
    _, one, _ = clone_pair(tmp_path); source = save(tmp_path / "source", b"same"); vault = GitVault(one)
    commit = vault.snapshot(source, machine_id="a", session_id="same", validator=valid); vault.push(commit)
    from grim_dawn_sync.bookmarks import _create_bookmark_test_only
    made = _create_bookmark_test_only(vault, commit, display_name="Named immediately", note="keep this", created_by="a")
    catalog = VersionCatalogBuilder(vault, source, machine_id="a").build()
    assert len(catalog.candidates) == 1 and catalog.candidates[0].kind == "live"
    aliases = catalog.candidates[0].aliases
    assert {(item.kind, item.display_name, item.note, item.commit) for item in aliases} >= {
        ("remote_head", "Sync destination latest", None, commit),
        ("bookmark", "Named immediately", "keep this", commit),
    }
    bookmark_alias = next(item for item in aliases if item.kind == "bookmark")
    selected = catalog.candidate(bookmark_alias.candidate_id)
    assert selected.kind == "bookmark" and selected.commit == commit and selected.note == "keep this"


def test_manifest_diff_includes_file_and_character_directory_changes() -> None:
    def manifest(*rows): return {"files": [{"path": path, "size": size, "sha256": digest} for path, size, digest in rows]}
    before = manifest(("main/Old/player.gdc", 1, "a"), ("main/Same/player.gdc", 1, "a"), ("world/a", 1, "a"))
    after = manifest(("main/New/player.gdc", 1, "b"), ("main/Same/player.gdc", 2, "b"), ("world/b", 1, "c"))
    result = _diff(before, after)
    assert (result.added, result.removed, result.changed) == (2, 2, 1)
    assert result.character_dirs_added == ("New",) and result.character_dirs_removed == ("Old",)
    assert result.character_dirs_changed == ("Same",)


def test_malformed_history_or_tag_failure_is_never_silently_skipped(tmp_path: Path) -> None:
    source = save(tmp_path / "source", b"one")
    class Vault:
        remote_ref = "refs/remotes/origin/main"
        def preflight(self): pass
        def fetch(self): pass
        def _oid(self, _ref): return "a" * 40
        def remote_history(self, *, limit): raise SyncError("malformed_remote_history", "malformed history", 4)
    with pytest.raises(SyncError) as caught:
        VersionCatalogBuilder(Vault(), source, machine_id="a").build()  # type: ignore[arg-type]
    assert caught.value.code == "malformed_remote_history"

    class TagVault(Vault):
        def _oid(self, _ref): return None
        def remote_history(self, *, limit): return ()
        def managed_bookmarks(self): return ()
        def legacy_annotated_tags(self): raise SyncError("malformed_legacy_tag", "malformed tag", 4)
    with pytest.raises(SyncError) as caught:
        VersionCatalogBuilder(TagVault(), source, machine_id="a").build()  # type: ignore[arg-type]
    assert caught.value.code == "malformed_legacy_tag"


def test_remote_check_report_fields_are_independent_and_frozen() -> None:
    """RemoteCheckReport's three timestamps are distinct axes, never coalesced."""
    ok = RemoteCheckReport(status="ok", checked_at="2026-08-05T02:00:00+00:00", error_code=None,
                           head_committed_at="2026-08-05T02:39:47+00:00")
    assert ok.checked_at != ok.head_committed_at
    assert ok.status == "ok" and ok.error_code is None

    failed = RemoteCheckReport(status="failed", checked_at=None, error_code="git_command_failed", head_committed_at=None)
    assert failed.status == "failed" and failed.checked_at is None and failed.error_code == "git_command_failed"
    assert failed.head_committed_at is None

    skipped = RemoteCheckReport(status="skipped", checked_at=None, error_code=None, head_committed_at=None)
    assert skipped.status == "skipped" and skipped.checked_at is None and skipped.error_code is None


def test_build_records_ok_remote_check_with_checked_at_and_head_committed_at(tmp_path: Path) -> None:
    _, one, _ = clone_pair(tmp_path); source = save(tmp_path / "source", b"one"); vault = GitVault(one)
    commit = vault.snapshot(source, machine_id="a", session_id="one", validator=valid); vault.push(commit)
    catalog = VersionCatalogBuilder(vault, source, machine_id="a").build()
    report = catalog.remote_check
    assert report.status == "ok" and report.error_code is None
    assert report.checked_at is not None and report.head_committed_at is not None
    # The three timestamps are independent: the candidate's own save-creation
    # time must not be silently substituted for either remote-check time.
    remote_head_candidate = next(item for item in catalog.candidates if item.kind == "live")
    assert remote_head_candidate.created_at != report.checked_at


def test_fetch_failure_marks_remote_check_failed_without_aborting_catalog_build(tmp_path: Path) -> None:
    """T-B 4.1: a failed remote reach must not silently disappear or hard-fail.

    The catalog still builds from whatever was locally cached by a prior
    successful fetch, but is explicitly marked unconfirmed.
    """
    _, one, _ = clone_pair(tmp_path); source = save(tmp_path / "source", b"one"); vault = GitVault(one)
    commit = vault.snapshot(source, machine_id="a", session_id="one", validator=valid); vault.push(commit)
    vault.fetch()  # populate the local remote-tracking ref once, successfully

    class FetchFailsVault(GitVault):
        def fetch(self) -> None:
            raise SyncError("git_command_failed", "simulated network failure", 4)

    failing_vault = FetchFailsVault(one)
    catalog = VersionCatalogBuilder(failing_vault, source, machine_id="a",
                                    remote_check_failure="report").build()
    report = catalog.remote_check
    assert report.status == "failed"
    assert report.checked_at is None
    assert report.error_code == "git_command_failed"
    # The stale, locally cached remote head is still readable and reported.
    assert report.head_committed_at is not None
    assert catalog.remote_head == commit
    all_entries = [entry for item in catalog.candidates for entry in (item, *item.aliases)]
    assert any(entry.commit == commit for entry in all_entries)


def test_fetch_failure_aborts_by_default_so_offline_policy_deny_is_not_relaxed(tmp_path: Path) -> None:
    """Plan section 10 / 4.2: adding remote-check reporting must not weaken the
    existing fail-closed behavior.  ``offline_policy=deny`` is implemented by
    the builder aborting, so reporting has to stay opt-in.
    """
    _, one, _ = clone_pair(tmp_path); source = save(tmp_path / "source", b"one"); vault = GitVault(one)
    commit = vault.snapshot(source, machine_id="a", session_id="one", validator=valid); vault.push(commit)
    vault.fetch()

    class FetchFailsVault(GitVault):
        def fetch(self) -> None:
            raise SyncError("git_command_failed", "simulated network failure", 4)

    failing_vault = FetchFailsVault(one)
    with pytest.raises(SyncError) as error:
        VersionCatalogBuilder(failing_vault, source, machine_id="a").build()
    assert error.value.code == "git_command_failed"


def test_invalid_remote_check_failure_policy_is_rejected(tmp_path: Path) -> None:
    _, one, _ = clone_pair(tmp_path); source = save(tmp_path / "source", b"one")
    with pytest.raises(SyncError) as error:
        VersionCatalogBuilder(GitVault(one), source, machine_id="a", remote_check_failure="ignore")
    assert error.value.code == "invalid_remote_check_policy"


def test_same_root_hash_different_commits_keep_distinct_committed_at_provenance(tmp_path: Path) -> None:
    """T-A repro: identical save content pushed twice must stay identifiable.

    Both commits share one root_hash and coalesce into one row, but their
    committer dates (unlike ``created_at``, which merely reflects each
    snapshot's own scan time) must remain readable per provenance entry so a
    user can tell which commit is which.
    """
    _, one, _ = clone_pair(tmp_path); source = save(tmp_path / "source", b"same"); vault = GitVault(one)
    first = vault.snapshot(source, machine_id="a", session_id="first", validator=valid); vault.push(first)
    time_module.sleep(1.1)  # commit timestamps have one-second resolution
    second = vault.snapshot(source, machine_id="b", session_id="second", validator=valid); vault.push(second)

    catalog = VersionCatalogBuilder(vault, source, machine_id="a").build()
    assert len(catalog.candidates) == 1
    representative = catalog.candidates[0]
    all_entries = [representative, *representative.aliases]
    by_commit = {entry.commit: entry.committed_at for entry in all_entries if entry.commit is not None}
    assert set(by_commit) == {first, second}
    assert by_commit[first] is not None and by_commit[second] is not None
    assert by_commit[first] != by_commit[second]


def test_catalog_lists_session_start_archive_with_no_commit_and_no_committed_at(tmp_path: Path) -> None:
    """T-D 6.2: session_start candidates are commit-less, local-only archives."""
    _, one, _ = clone_pair(tmp_path); source = save(tmp_path / "source", b"remote-content"); vault = GitVault(one)
    remote = vault.snapshot(source, machine_id="a", session_id="one", validator=valid); vault.push(remote)
    save(source, b"live-changed")  # live now differs from the archived pre-launch data
    live_manifest_before_change = build_manifest(save(tmp_path / "pre-launch", b"pre-launch-data"), machine_id="a")
    archives = tmp_path / "archives"
    archive_id = create_session_start_archive(
        archives, tmp_path / "pre-launch", manifest=live_manifest_before_change, machine_id="a",
        session_id="11111111-1111-1111-1111-111111111111", launched_from_candidate_kind="remote_head",
    )
    catalog = VersionCatalogBuilder(vault, source, machine_id="a", archives_root=archives).build()
    session_start_items = [item for item in catalog.candidates if item.kind == "session_start"]
    assert len(session_start_items) == 1
    item = session_start_items[0]
    assert item.commit is None
    assert item.committed_at is None
    assert item.root_hash == live_manifest_before_change["root_hash"]
    assert item.source_archive_id == archive_id


def test_catalog_without_archives_root_never_scans_local_archives(tmp_path: Path) -> None:
    _, one, _ = clone_pair(tmp_path); source = save(tmp_path / "source", b"one"); vault = GitVault(one)
    vault.push(vault.snapshot(source, machine_id="a", session_id="one", validator=valid))
    catalog = VersionCatalogBuilder(vault, source, machine_id="a").build()
    assert not any(item.kind == "session_start" for item in catalog.candidates)


def test_catalog_skips_tampered_session_start_archive_but_still_builds(tmp_path: Path) -> None:
    _, one, _ = clone_pair(tmp_path); source = save(tmp_path / "source", b"one"); vault = GitVault(one)
    vault.push(vault.snapshot(source, machine_id="a", session_id="one", validator=valid))
    archives = tmp_path / "archives"
    pre_launch = save(tmp_path / "pre-launch", b"pre-launch-data")
    manifest = build_manifest(pre_launch, machine_id="a")
    archive_id = create_session_start_archive(
        archives, pre_launch, manifest=manifest, machine_id="a",
        session_id="22222222-2222-2222-2222-222222222222", launched_from_candidate_kind="live",
    )
    (archives / archive_id / "main" / "a" / "player.gdc").write_bytes(b"tampered")
    catalog = VersionCatalogBuilder(vault, source, machine_id="a", archives_root=archives).build()
    assert not any(item.kind == "session_start" for item in catalog.candidates)
    assert catalog.candidates  # the rest of the catalog still built fine
