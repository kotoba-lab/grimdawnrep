from __future__ import annotations

from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace

import pytest

from grim_dawn_sync import cli
from grim_dawn_sync.errors import SyncError
from grim_dawn_sync.selection import CancelledSelection
from grim_dawn_sync.selector_ui import SelectionRequest
from grim_dawn_sync.state import SyncState
from grim_dawn_sync.version_catalog import CandidateAlias, ManifestDiff, RemoteCheckReport, SaveCandidate, VersionCatalog


def _candidate(candidate_id: str, kind: str, root: str, commit: str | None) -> SaveCandidate:
    return SaveCandidate(candidate_id, kind, kind, "2026-08-01T00:00:00Z", "machine", root, commit,
                         1, 1, 1, (), ManifestDiff(0, 0, 0))


def _install_context(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, live: str, remote: str,
                     baseline: str, token: str = "t" * 32,
                     selection_policy: str = "on_diff",
                     remote_check: object | None = None) -> tuple[Path, VersionCatalog, list[object]]:
    config_path = tmp_path / "config.local.json"
    config = SimpleNamespace(save_root=tmp_path / "live", vault_repo=tmp_path / "vault", remote="origin",
                             branch="main", machine_id="machine", stable_scan_retries=1,
                             stable_window_seconds=0, selection_policy=selection_policy)
    config.public_dict = lambda: {"machine_id": "machine", "save_root": str(tmp_path / "live"),
                                  "vault_repo": str(tmp_path / "vault"), "remote": "origin", "branch": "main"}
    commit = "a" * 40
    items = [_candidate("live-id", "live", live, None)]
    if remote != live:
        items.append(_candidate("remote-id", "remote_head", remote, commit))
    catalog_kwargs = {} if remote_check is None else {"remote_check": remote_check}
    catalog = VersionCatalog(token, commit, live, tuple(items), **catalog_kwargs)
    calls: list[object] = []

    class Vault:
        remote = "origin"
        remote_head = "refs/heads/main"
        class Runner:
            def run(self, *args, **_kwargs):
                if args[0] == "remote":
                    return SimpleNamespace(returncode=0, stdout="test://remote\n")
                if args[0] == "ls-remote":
                    if "--refs" in args:
                        return SimpleNamespace(returncode=0, stdout="")
                    return SimpleNamespace(returncode=0, stdout=f"{commit}\trefs/heads/main\n")
                raise AssertionError(args)
        runner = Runner()
        def remote_oid(self) -> str:
            return commit
        def validate_commit_snapshot(self, oid: str) -> dict[str, str]:
            assert oid == commit
            return {"root_hash": remote}
        def bind_remote_identity(self, expected: tuple[str, str]) -> None:
            assert expected == ("test://remote", "test://remote")

    class Builder:
        def __init__(self, *_args: object, **_kwargs: object) -> None: pass
        def build(self) -> VersionCatalog: return catalog

    class Workflow:
        def __init__(self, got_config: object, root: Path) -> None:
            assert got_config is config and root == tmp_path
        def execute_selection_plan(self, plan: object, registry: object, *, context_revalidate=None,
                                   exit_disposition_gate=None) -> dict[str, str]:
            registry.revalidate(plan, live_manifest=lambda: {"root_hash": live}, remote_head=lambda: commit)
            assert context_revalidate is not None
            context_revalidate(getattr(plan, "expected_context_digest"))
            calls.append(plan)
            return {"state": "COMPLETE", "commit": "b" * 40}

    monkeypatch.setattr(cli, "load_config", lambda _path: config)
    monkeypatch.setattr(cli, "_process_preflight", lambda _config: calls.append("preflight"))
    monkeypatch.setattr(cli, "_vault", lambda _config: Vault())
    monkeypatch.setattr(cli, "inspect_remote_lock_readonly", lambda _vault: None)
    monkeypatch.setattr(cli, "VersionCatalogBuilder", Builder)
    state = SyncState(last_applied_remote_commit=commit, last_applied_manifest_root_hash=baseline, machine_id="machine")
    monkeypatch.setattr(cli, "load_state", lambda _path: state)
    monkeypatch.setattr(cli, "stable_manifest", lambda *_args, **_kwargs: {"root_hash": live})
    monkeypatch.setattr(cli, "LaunchWorkflow", Workflow)
    return config_path, catalog, calls


def test_equal_json_launch_uses_canonical_selection_path_not_legacy_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = "1" * 64
    config_path, _, calls = _install_context(monkeypatch, tmp_path, live=root, remote=root, baseline=root)
    result = cli.launch(config_path, as_json=True)
    assert result["result"]["state"] == "COMPLETE"
    plan = calls[-1]
    assert getattr(plan, "candidate_kind") == "live" and getattr(plan, "mode") == "launch"


def test_always_policy_json_launch_on_equal_requires_explicit_selection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """T-C 5.2/5.3: `always` widens show_selector even for EQUAL, so headless
    --json must not silently auto-select and rewrite live."""
    root = "1" * 64
    config_path, _, calls = _install_context(
        monkeypatch, tmp_path, live=root, remote=root, baseline=root, selection_policy="always",
    )
    with pytest.raises(SyncError) as error:
        cli.launch(config_path, as_json=True)
    assert error.value.code == "selection_required"
    assert calls == ["preflight"] * 3 and "COMPLETE" not in str(calls)


def test_always_policy_still_lets_an_explicit_equal_selection_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    root = "1" * 64
    config_path, _, calls = _install_context(
        monkeypatch, tmp_path, live=root, remote=root, baseline=root, selection_policy="always",
    )
    issued = cli.versions(config_path)
    result = cli.launch(config_path, as_json=True, selected="live-id", catalog_token=issued["catalog_token"])
    assert result["result"]["state"] == "COMPLETE"


def test_selector_cli_flag_overrides_config_policy_for_equal_json_launch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    root = "1" * 64
    config_path, _, calls = _install_context(
        monkeypatch, tmp_path, live=root, remote=root, baseline=root, selection_policy="on_diff",
    )
    # Config says on_diff (auto-select on EQUAL); the CLI override forces always.
    with pytest.raises(SyncError) as error:
        cli.launch(config_path, as_json=True, selector="always")
    assert error.value.code == "selection_required"
    # Without the override the same EQUAL state still auto-selects as before.
    result = cli.launch(config_path, as_json=True)
    assert result["result"]["state"] == "COMPLETE"


def test_selector_cli_flag_can_relax_an_always_config_back_to_on_diff(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    root = "1" * 64
    config_path, _, _ = _install_context(
        monkeypatch, tmp_path, live=root, remote=root, baseline=root, selection_policy="always",
    )
    result = cli.launch(config_path, as_json=True, selector="on-diff")
    assert result["result"]["state"] == "COMPLETE"


def test_invalid_selector_override_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = "1" * 64
    config_path, _, _ = _install_context(monkeypatch, tmp_path, live=root, remote=root, baseline=root)
    with pytest.raises(SyncError) as error:
        cli.launch(config_path, as_json=True, selector="never")
    assert error.value.code == "invalid_selection_policy"


def test_interactive_always_policy_shows_selector_on_equal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    root = "1" * 64
    config_path, _, _ = _install_context(
        monkeypatch, tmp_path, live=root, remote=root, baseline=root, selection_policy="always",
    )

    class Presenter:
        def present_builder(self, build, directive):
            catalog = build()
            policy = directive(catalog)
            assert policy.show_selector is True
            return SelectionRequest("live-id", "launch")

    assert cli.launch(config_path, presenter=Presenter())["result"]["state"] == "COMPLETE"


def test_json_and_auto_safe_refuse_different_saves_without_mutation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    live, remote = "1" * 64, "2" * 64
    config_path, _, calls = _install_context(monkeypatch, tmp_path, live=live, remote=remote, baseline=live)
    for kwargs in ({"as_json": True}, {"selected": "auto-safe"}):
        with pytest.raises(SyncError) as error:
            cli.launch(config_path, **kwargs)
        assert error.value.code == "selection_required"
    assert calls == ["preflight"] * 6


def test_interactive_selection_and_cancel_are_explicit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    live, remote = "1" * 64, "2" * 64
    config_path, _, calls = _install_context(monkeypatch, tmp_path, live=live, remote=remote, baseline=live)

    class Presenter:
        def present_builder(self, build, directive) -> SelectionRequest:
            catalog = build(); directive(catalog)
            assert catalog.candidate("remote-id").kind == "remote_head"
            return SelectionRequest("remote-id", "launch")

    assert cli.launch(config_path, presenter=Presenter())["result"]["state"] == "COMPLETE"  # type: ignore[arg-type]
    assert getattr(calls[-1], "candidate_kind") == "remote_head"

    class Cancel:
        def present_builder(self, _build, _directive) -> CancelledSelection:
            return CancelledSelection()

    before = len(calls)
    assert cli.launch(config_path, presenter=Cancel())["result"] == {"state": "CANCELLED"}  # type: ignore[arg-type]
    assert calls[before:] == ["preflight"]


def test_real_cli_interactive_path_resolves_callable_directive_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    live, remote = "1" * 64, "2" * 64
    config_path, _, _ = _install_context(monkeypatch, tmp_path, live=live, remote=remote, baseline=live)
    resolved: list[object] = []
    class Presenter:
        def present_builder(self, build, directive):
            value = build(); resolved.append(directive(value))
            return SelectionRequest("remote-id", "launch")
    assert cli.launch(config_path, presenter=Presenter())["result"]["state"] == "COMPLETE"  # type: ignore[arg-type]
    assert len(resolved) == 1 and getattr(resolved[0], "initial_selection") == "remote_head"


def test_versions_token_is_consumed_by_a_fresh_process_equivalent_scan(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    live, remote = "1" * 64, "2" * 64
    config_path, _, calls = _install_context(monkeypatch, tmp_path, live=live, remote=remote, baseline=live)
    issued = cli.versions(config_path)
    result = cli.launch(config_path, as_json=True, selected="remote-id", catalog_token=issued["catalog_token"])
    assert result["result"]["state"] == "COMPLETE"
    assert getattr(calls[-1], "candidate_id") == "remote-id"


def test_changed_or_tampered_catalog_token_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    live, remote = "1" * 64, "2" * 64
    config_path, _, calls = _install_context(monkeypatch, tmp_path, live=live, remote=remote, baseline=live)
    token = cli.versions(config_path)["catalog_token"]
    tampered = token[:-1] + ("0" if token[-1] != "0" else "1")
    with pytest.raises(SyncError) as error:
        cli.launch(config_path, as_json=True, selected="remote-id", catalog_token=tampered)
    assert error.value.code == "catalog_expired" and calls == ["preflight"] * 5


def test_unknown_candidate_cannot_be_used_as_a_ref(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    live, remote = "1" * 64, "2" * 64
    config_path, _, _ = _install_context(monkeypatch, tmp_path, live=live, remote=remote, baseline=live)
    token = cli.versions(config_path)["catalog_token"]
    with pytest.raises(SyncError) as error:
        cli.launch(config_path, as_json=True, selected="refs/heads/main", catalog_token=token)
    assert error.value.code == "unknown_candidate"


def test_versions_json_projection_does_not_expose_roots_commits_or_labels(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    live, remote = "1" * 64, "2" * 64
    config_path, _, _ = _install_context(monkeypatch, tmp_path, live=live, remote=remote, baseline=live)
    result = cli.versions(config_path)
    rendered = cli._render(result, as_json=True)
    assert live not in rendered and remote not in rendered and "a" * 40 not in rendered
    assert "machine" not in rendered and "remote_head" in rendered


def test_versions_json_includes_remote_check_times_and_status_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    live, remote = "1" * 64, "2" * 64
    report = RemoteCheckReport(status="ok", checked_at="2026-08-05T02:00:00+00:00", error_code=None,
                               head_committed_at="2026-08-05T02:39:47+00:00")
    config_path, _, _ = _install_context(monkeypatch, tmp_path, live=live, remote=remote, baseline=live,
                                         remote_check=report)
    result = cli.versions(config_path)
    assert result["remote_check"] == {
        "status": "ok", "checked_at": "2026-08-05T02:00:00+00:00",
        "error_code": None, "head_committed_at": "2026-08-05T02:39:47+00:00",
    }
    rendered = cli._render(result, as_json=True)
    assert live not in rendered and remote not in rendered and "a" * 40 not in rendered


def test_headless_launch_fails_closed_when_remote_check_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """T-B 4.2/4.4: a failed remote check must never look like a normal,
    unwarned launch to a headless (no-UI) caller."""
    root = "1" * 64
    report = RemoteCheckReport(status="failed", checked_at=None, error_code="git_command_failed",
                               head_committed_at=None)
    config_path, _, calls = _install_context(
        monkeypatch, tmp_path, live=root, remote=root, baseline=root, remote_check=report,
    )
    with pytest.raises(SyncError) as error:
        cli.launch(config_path, as_json=True)
    assert error.value.code == "remote_check_failed"
    assert error.value.details["remote_check"]["status"] == "failed"
    assert "COMPLETE" not in str(calls)


def test_confirm_flag_is_not_an_unverified_remote_bypass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """--confirm authorizes a risky *candidate*; it must never double as an
    override for an unconfirmed remote (plan section 10)."""
    root = "1" * 64
    report = RemoteCheckReport(status="failed", checked_at=None, error_code="git_command_failed",
                               head_committed_at=None)
    config_path, _, calls = _install_context(
        monkeypatch, tmp_path, live=root, remote=root, baseline=root, remote_check=report,
    )
    with pytest.raises(SyncError) as error:
        cli.launch(config_path, as_json=True, confirmed=True)
    assert error.value.code == "remote_check_failed"
    assert "COMPLETE" not in str(calls)


def test_skipped_remote_check_does_not_block_headless_launch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A catalog that never attempted a remote check (e.g. not yet wired into
    a caller's flow) stays non-blocking; only an attempted-and-failed check
    is fail-closed."""
    root = "1" * 64
    config_path, _, _ = _install_context(monkeypatch, tmp_path, live=root, remote=root, baseline=root)
    result = cli.launch(config_path, as_json=True)
    assert result["result"]["state"] == "COMPLETE"


def test_single_build_rejects_live_change_during_lightweight_close(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    live, remote = "1" * 64, "2" * 64
    config_path, _, _ = _install_context(monkeypatch, tmp_path, live=live, remote=remote, baseline=live)

    monkeypatch.setattr(cli, "stable_manifest", lambda *_args, **_kwargs: {"root_hash": "3" * 64})
    with pytest.raises(SyncError) as error:
        cli.versions(config_path)
    assert error.value.code == "catalog_context_changed"


@pytest.mark.parametrize("equal", [True, False], ids=["equal", "divergent"])
def test_interactive_builds_catalog_once_and_prelock_revalidation_does_not_rebuild(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, equal: bool,
) -> None:
    live = "1" * 64
    remote = live if equal else "2" * 64
    config_path, _, _ = _install_context(monkeypatch, tmp_path, live=live, remote=remote, baseline=live)
    original_builder = cli.VersionCatalogBuilder
    builds: list[str] = []

    class CountingBuilder:
        def __init__(self, *args, **kwargs):
            self.inner = original_builder(*args, **kwargs)
        def build(self):
            builds.append("build")
            return self.inner.build()

    monkeypatch.setattr(cli, "VersionCatalogBuilder", CountingBuilder)

    class Presenter:
        def present_builder(self, build, directive):
            catalog = build()
            policy = directive(catalog)
            if equal:
                assert policy.show_selector is False
                return SelectionRequest("live-id", "launch")
            assert policy.show_selector is True
            return SelectionRequest("remote-id", "launch")

    assert cli.launch(config_path, presenter=Presenter())["result"]["state"] == "COMPLETE"
    assert builds == ["build"]


@pytest.mark.parametrize(
    "component",
    ["process", "config", "state", "lock", "identity", "tag", "live", "main"],
)
@pytest.mark.parametrize("phase", ["pre-lock", "post-lock"])
def test_final_lightweight_snapshot_rejects_late_context_change(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, component: str, phase: str,
) -> None:
    live, remote = "1" * 64, "2" * 64
    config_path, _, _ = _install_context(monkeypatch, tmp_path, live=live, remote=remote, baseline=live)
    expected_config = cli.load_config(config_path)
    expected_state = cli.load_state(cli._state_path(config_path))
    vault = cli._vault(expected_config)
    lock = SimpleNamespace(
        session=SimpleNamespace(session_id="11111111-1111-4111-8111-111111111111", base_commit="a" * 40),
        oid="b" * 40,
        local_tag="grim-dawn-sync-11111111-1111-4111-8111-111111111111",
    )
    acquired_lock = lock if phase == "post-lock" else None
    required_state = cli._locked_state(expected_config, lock, expected_state) if acquired_lock else expected_state
    baseline_snapshot = cli._execution_ref_snapshot(vault)
    if acquired_lock:
        baseline_snapshot = replace(baseline_snapshot, lock_oid=lock.oid)
        monkeypatch.setattr(cli, "load_state", lambda _path: required_state)
        monkeypatch.setattr(cli, "_execution_ref_snapshot", lambda _vault: baseline_snapshot)

    if component == "process":
        calls = 0
        def process(_config):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise SyncError("game_already_running", "running", 5)
        monkeypatch.setattr(cli, "_process_preflight", process)
    elif component == "config":
        changed = SimpleNamespace(**vars(expected_config))
        changed.public_dict = lambda: {**expected_config.public_dict(), "machine_id": "changed"}
        values = iter((expected_config, changed))
        monkeypatch.setattr(cli, "load_config", lambda _path: next(values))
    elif component == "state":
        changed = replace(required_state, last_applied_manifest_root_hash="3" * 64)
        values = iter((required_state, changed))
        monkeypatch.setattr(cli, "load_state", lambda _path: next(values))
    elif component in {"lock", "tag", "main"}:
        if component == "lock":
            changed = replace(baseline_snapshot, lock_oid=("c" * 40 if acquired_lock else "b" * 40))
        elif component == "tag":
            changed = replace(baseline_snapshot, remote_ref_snapshot="2" * 64)
        else:
            changed = replace(baseline_snapshot, remote_head="c" * 40)
        values = iter((baseline_snapshot, changed))
        monkeypatch.setattr(cli, "_execution_ref_snapshot", lambda _vault: next(values))
    elif component == "identity":
        values = iter((("test://remote", "test://remote"), ("test://changed", "test://changed")))
        monkeypatch.setattr(cli, "read_remote_identity", lambda *_args: next(values))
    elif component == "live":
        values = iter(({"root_hash": live}, {"root_hash": "3" * 64}))
        monkeypatch.setattr(cli, "stable_manifest", lambda *_args, **_kwargs: next(values))

    with pytest.raises(SyncError) as caught:
        cli._capture_execution_context(
            config_path, expected_config, expected_state, vault, acquired_lock=acquired_lock,
        )
    assert caught.value.code in {
        "catalog_context_changed", "game_already_running", "lock_held",
    }


def test_post_lock_snapshot_normalizes_only_the_owned_lock_transition(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    live, remote = "1" * 64, "2" * 64
    config_path, _, _ = _install_context(monkeypatch, tmp_path, live=live, remote=remote, baseline=live)
    expected_config = cli.load_config(config_path)
    expected_state = cli.load_state(cli._state_path(config_path))
    vault = cli._vault(expected_config)
    lock = SimpleNamespace(
        session=SimpleNamespace(session_id="11111111-1111-4111-8111-111111111111", base_commit="a" * 40),
        oid="b" * 40,
        local_tag="grim-dawn-sync-11111111-1111-4111-8111-111111111111",
    )
    monkeypatch.setattr(cli, "load_state", lambda _path: cli._locked_state(expected_config, lock, expected_state))
    snapshot = replace(cli._execution_ref_snapshot(vault), lock_oid=lock.oid)
    monkeypatch.setattr(cli, "_execution_ref_snapshot", lambda _vault: snapshot)
    context = cli._capture_execution_context(
        config_path, expected_config, expected_state, vault, acquired_lock=lock,
    )
    assert context["state"] == expected_state.as_dict()
    assert context["lock"] is None


def _composite_snapshot(stdout: str, *, returncode: int = 0):
    commands: list[tuple[str, ...]] = []
    class Runner:
        def run(self, *args, **_kwargs):
            commands.append(args)
            return SimpleNamespace(returncode=returncode, stdout=stdout)
    vault = SimpleNamespace(remote="origin", remote_head="refs/heads/main", runner=Runner())
    return cli._execution_ref_snapshot(vault), commands


def test_composite_snapshot_uses_one_exact_query_and_separates_owned_lock() -> None:
    main, lock, tag, target = "a" * 40, "b" * 40, "c" * 40, "d" * 40
    managed = "refs/tags/grim-dawn-save-" + "1" * 32
    rows = (
        f"{main}\trefs/heads/main\n"
        f"{lock}\t{cli.LOCK_REF}\n"
        f"{main}\t{cli.LOCK_REF}^{{}}\n"
        f"{tag}\t{managed}\n"
        f"{target}\t{managed}^{{}}\n"
        f"{tag}\trefs/tags/archive/legacy\n"
        f"{target}\trefs/tags/archive/legacy^{{}}\n"
    )
    snapshot, commands = _composite_snapshot(rows)
    assert commands == [(
        "ls-remote", "origin", "refs/heads/main", cli.LOCK_REF,
        "refs/tags/grim-dawn-save-*", "refs/tags/archive/*", "refs/tags/milestone/*",
    )]
    assert snapshot.remote_head == main and snapshot.lock_oid == lock

    changed_lock, _ = _composite_snapshot(rows.replace(lock, "e" * 40, 1))
    changed_main, _ = _composite_snapshot(rows.replace(main, "f" * 40, 1))
    changed_tag, _ = _composite_snapshot(rows.replace(tag, "9" * 40, 1))
    assert changed_lock.remote_ref_snapshot == snapshot.remote_ref_snapshot
    assert changed_main.remote_head != snapshot.remote_head
    assert changed_main.remote_ref_snapshot != snapshot.remote_ref_snapshot
    assert changed_tag.remote_ref_snapshot != snapshot.remote_ref_snapshot


@pytest.mark.parametrize(
    ("case", "expected_code"),
    [
        ("command", "catalog_ref_snapshot_failed"),
        ("bytes", "catalog_ref_snapshot_failed"),
        ("rows", "catalog_ref_snapshot_failed"),
        ("malformed", "catalog_ref_snapshot_invalid"),
        ("oid", "catalog_ref_snapshot_invalid"),
        ("ref", "catalog_ref_snapshot_invalid"),
        ("duplicate", "catalog_ref_snapshot_invalid"),
        ("orphan-peeled", "catalog_ref_snapshot_invalid"),
        ("long-ref", "catalog_ref_snapshot_invalid"),
    ],
)
def test_composite_snapshot_fails_closed_on_every_parser_bound(case: str, expected_code: str) -> None:
    oid = "a" * 40
    returncode = 1 if case == "command" else 0
    if case == "bytes":
        output = "x" * (cli._REMOTE_REF_SNAPSHOT_MAX_BYTES + 1)
    elif case == "rows":
        output = "".join(
            f"{oid}\trefs/tags/archive/{index:04d}\n"
            for index in range(cli._REMOTE_REF_SNAPSHOT_MAX_ROWS + 1)
        )
    elif case == "malformed":
        output = f"{oid} refs/heads/main\n"
    elif case == "oid":
        output = "not-an-oid\trefs/heads/main\n"
    elif case == "ref":
        output = f"{oid}\trefs/heads/unrequested\n"
    elif case == "duplicate":
        output = f"{oid}\trefs/heads/main\n{oid}\trefs/heads/main\n"
    elif case == "orphan-peeled":
        output = f"{oid}\trefs/tags/archive/orphan^{{}}\n"
    elif case == "long-ref":
        output = f"{oid}\trefs/tags/archive/{'x' * 600}\n"
    else:
        output = ""
    with pytest.raises(SyncError) as caught:
        _composite_snapshot(output, returncode=returncode)
    assert caught.value.code == expected_code


def test_post_lock_double_snapshot_stays_within_remote_and_hash_budgets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    live, remote = "1" * 64, "2" * 64
    config_path, _, _ = _install_context(monkeypatch, tmp_path, live=live, remote=remote, baseline=live)
    expected_config = cli.load_config(config_path)
    expected_state = cli.load_state(cli._state_path(config_path))
    lock = SimpleNamespace(
        session=SimpleNamespace(session_id="11111111-1111-4111-8111-111111111111", base_commit="a" * 40),
        oid="b" * 40,
        local_tag="grim-dawn-sync-11111111-1111-4111-8111-111111111111",
    )
    commands: list[tuple[str, ...]] = []
    class Runner:
        def run(self, *args, **_kwargs):
            commands.append(args)
            if args[0] == "remote":
                return SimpleNamespace(returncode=0, stdout="test://remote\n")
            if args[0] == "ls-remote":
                return SimpleNamespace(
                    returncode=0,
                    stdout=f"{'a' * 40}\trefs/heads/main\n{lock.oid}\t{cli.LOCK_REF}\n",
                )
            raise AssertionError(args)
    vault = SimpleNamespace(remote="origin", remote_head="refs/heads/main", runner=Runner())
    stable_calls: list[str] = []
    monkeypatch.setattr(cli, "load_state", lambda _path: cli._locked_state(expected_config, lock, expected_state))
    monkeypatch.setattr(cli, "stable_manifest", lambda *_args, **_kwargs: stable_calls.append("hash") or {"root_hash": live})

    context = cli._capture_execution_context(
        config_path, expected_config, expected_state, vault, acquired_lock=lock,
    )

    ls_remote = [command for command in commands if command[0] == "ls-remote"]
    assert context["live_root_hash"] == live
    assert len(ls_remote) <= 2
    assert len(stable_calls) <= 2 and len(stable_calls) * 2 <= 4


def test_single_build_rejects_fetch_or_push_url_change(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    live, remote = "1" * 64, "2" * 64
    config_path, _, _ = _install_context(monkeypatch, tmp_path, live=live, remote=remote, baseline=live)
    vault = cli._vault(None)
    class ChangingRunner:
        calls = 0
        def run(self, *args, **_kwargs):
            if args[0] == "ls-remote":
                return SimpleNamespace(returncode=0, stdout=f"{'a' * 40}\trefs/heads/main\n")
            pair = self.calls // 2; self.calls += 1
            url = "test://remote" if pair == 0 else "test://changed"
            return SimpleNamespace(returncode=0, stdout=url + "\n")
    vault.runner = ChangingRunner()
    monkeypatch.setattr(cli, "_vault", lambda _config: vault)
    with pytest.raises(SyncError) as error:
        cli.versions(config_path)
    assert error.value.code == "catalog_context_changed"


def test_single_build_rejects_state_config_lock_and_recovery_changes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    live, remote = "1" * 64, "2" * 64
    config_path, _, _ = _install_context(monkeypatch, tmp_path, live=live, remote=remote, baseline=live)
    state_one = SyncState(last_applied_remote_commit="a" * 40, last_applied_manifest_root_hash=live, machine_id="machine")
    state_two = SyncState(last_applied_remote_commit="a" * 40, last_applied_manifest_root_hash="3" * 64, machine_id="machine")
    states = iter((state_one, state_two))
    monkeypatch.setattr(cli, "load_state", lambda _path: next(states))
    with pytest.raises(SyncError) as error:
        cli.versions(config_path)
    assert error.value.code == "catalog_context_changed"

    config_path, _, _ = _install_context(monkeypatch, tmp_path, live=live, remote=remote, baseline=live)
    original = cli.load_config(config_path)
    changed_config = SimpleNamespace(**vars(original))
    changed_config.public_dict = lambda: {"machine_id": "other", "save_root": str(tmp_path / "live")}
    configs = iter((original, changed_config))
    monkeypatch.setattr(cli, "load_config", lambda _path: next(configs))
    with pytest.raises(SyncError) as error:
        cli.versions(config_path)
    assert error.value.code == "catalog_context_changed"

    config_path, _, _ = _install_context(monkeypatch, tmp_path, live=live, remote=remote, baseline=live)
    snapshot = cli._execution_ref_snapshot(cli._vault(None))
    monkeypatch.setattr(cli, "_execution_ref_snapshot", lambda _vault: replace(snapshot, lock_oid="b" * 40))
    with pytest.raises(SyncError) as error:
        cli.versions(config_path)
    assert error.value.code == "lock_held"

    config_path, _, _ = _install_context(monkeypatch, tmp_path, live=live, remote=remote, baseline=live)
    monkeypatch.setattr(cli, "load_state", lambda _path: SimpleNamespace(phase="lock_held"))
    with pytest.raises(SyncError) as error:
        cli.versions(config_path)
    assert error.value.code == "recovery_required"


def test_versions_rejects_running_game_before_catalog_io(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli, "load_config", lambda _path: object())
    monkeypatch.setattr(cli, "_process_preflight", lambda _config: (_ for _ in ()).throw(
        SyncError("game_already_running", "running", 5)))
    monkeypatch.setattr(cli, "VersionCatalogBuilder", lambda *_a, **_k: pytest.fail("catalog must not be built"))
    with pytest.raises(SyncError) as error:
        cli.versions(tmp_path / "config.local.json")
    assert error.value.code == "game_already_running"


def test_promote_is_dry_run_without_apply(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli, "load_config", lambda _path: pytest.fail("dry run must not inspect or mutate"))
    assert cli.promote(tmp_path / "config.local.json", apply=False) == {
        "schema_version": "1.0.0", "command": "promote", "dry_run": True,
    }


def test_cross_process_capability_connects_promote_and_bookmark_apply(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    live, remote = "1" * 64, "2" * 64
    config_path, _, calls = _install_context(monkeypatch, tmp_path, live=live, remote=remote, baseline=live)
    issued = cli.versions(config_path)
    promoted = cli.promote(config_path, apply=True, as_json=True, selected="remote-id",
                           catalog_token=issued["catalog_token"])
    assert promoted["result"]["state"] == "COMPLETE"
    assert getattr(calls[-1], "mode") == "promote-only"

    issued = cli.versions(config_path)
    made: list[tuple[str, str, str | None]] = []
    monkeypatch.setattr(
        cli,
        "create_commit_bookmark_locked",
        lambda _vault, commit, _root, **kwargs: made.append((commit, kwargs["display_name"], kwargs["note"])),
    )
    dry = cli.bookmark(config_path, "remote-id", "Named save", "note", apply=False,
                       catalog_token=issued["catalog_token"])
    assert dry == {"schema_version": "1.0.0", "command": "bookmark", "dry_run": True, "verified": True}
    assert made == []
    issued = cli.versions(config_path)
    result = cli.bookmark(config_path, "remote-id", "Named save", "note", apply=True,
                          catalog_token=issued["catalog_token"])
    assert result["created"] is True and made == [("a" * 40, "Named save", "note")]


def test_cross_process_capability_allows_live_bookmark_dry_run_and_apply(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    live, remote = "1" * 64, "2" * 64
    config_path, _, _ = _install_context(monkeypatch, tmp_path, live=live, remote=remote, baseline=remote)
    issued = cli.versions(config_path)
    assert cli.bookmark(
        config_path, "live-id", "Live save", None, apply=False,
        catalog_token=issued["catalog_token"],
    )["verified"] is True

    manifest = {"root_hash": live}
    monkeypatch.setattr(cli, "stable_manifest", lambda *_args, **_kwargs: manifest)
    made: list[dict[str, object]] = []

    def create_live(_vault, _root, got_manifest, **kwargs):
        assert got_manifest is manifest
        kwargs["before_lock"]()
        kwargs["after_lock"]()
        made.append(kwargs)

    monkeypatch.setattr(cli, "create_live_bookmark_locked", create_live)
    issued = cli.versions(config_path)
    result = cli.bookmark(
        config_path, "live-id", "Live save", "note", apply=True,
        catalog_token=issued["catalog_token"],
    )
    assert result["created"] is True
    assert made[0]["expected_remote_head"] == "a" * 40
    assert made[0]["display_name"] == "Live save"


def test_gui_bookmark_action_can_name_live_candidate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    live, remote = "1" * 64, "2" * 64
    config_path, _, _ = _install_context(monkeypatch, tmp_path, live=live, remote=remote, baseline=live)
    monkeypatch.setattr(cli, "stable_manifest", lambda *_args, **_kwargs: {"root_hash": live})
    made: list[str] = []

    def create_live(_vault, _root, _manifest, **kwargs):
        kwargs["before_lock"]()
        kwargs["after_lock"]()
        made.append(str(kwargs["display_name"]))

    monkeypatch.setattr(cli, "create_live_bookmark_locked", create_live)

    class Presenter:
        def present_builder(self, build, directive):
            catalog = build()
            assert "bookmark" in directive(catalog).allowed_operations
            return SelectionRequest("live-id", None, "GUI live", "note", action="bookmark")

    result = cli.promote(config_path, apply=True, presenter=Presenter())
    assert result["result"]["state"] == "BOOKMARKED" and made == ["GUI live"]


def test_risky_explicit_launch_requires_separate_confirm_flag(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    live, remote = "1" * 64, "2" * 64
    config_path, base, _ = _install_context(monkeypatch, tmp_path, live=live, remote=remote, baseline=live)
    history = _candidate("history-id", "history", "3" * 64, "b" * 40)
    risky = VersionCatalog(base.token, base.remote_head, base.live_root_hash, base.candidates + (history,))
    class Builder:
        def __init__(self, *_args: object, **_kwargs: object) -> None: pass
        def build(self) -> VersionCatalog: return risky
    monkeypatch.setattr(cli, "VersionCatalogBuilder", Builder)
    token = cli.versions(config_path)["catalog_token"]
    with pytest.raises(SyncError) as error:
        cli.launch(config_path, as_json=True, selected="history-id", catalog_token=token)
    assert error.value.code == "selection_confirmation_required"
    assert cli.launch(config_path, as_json=True, selected="history-id", catalog_token=token,
                      confirmed=True)["result"]["state"] == "COMPLETE"


@pytest.mark.parametrize(("kind", "command"), [("bookmark", "launch"), ("legacy", "promote")])
def test_coalesced_named_alias_is_authorized_by_canonical_catalog_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str, command: str) -> None:
    live, remote = "1" * 64, "2" * 64
    config_path, base, _ = _install_context(monkeypatch, tmp_path, live=live, remote=remote, baseline=live)
    representative = base.candidate("live-id")
    alias = CandidateAlias(f"{kind}-alias", kind, "Named alias", "2026-08-01T00:00:00Z", "b" * 40, "note")
    aliased = VersionCatalog(base.token, base.remote_head, base.live_root_hash,
                             (replace(representative, aliases=(alias,)), *base.candidates[1:]))
    class Builder:
        def __init__(self, *_args: object, **_kwargs: object) -> None: pass
        def build(self) -> VersionCatalog: return aliased
    monkeypatch.setattr(cli, "VersionCatalogBuilder", Builder)
    listed = cli.versions(config_path)
    token = listed["catalog_token"]
    rows = {item["candidate_id"]: item for item in listed["candidates"]}
    assert representative.candidate_id in rows
    assert rows[alias.candidate_id]["kind"] == kind
    assert set(rows[alias.candidate_id]) == {
        "candidate_id", "kind", "file_count", "character_count", "total_bytes", "diff",
    }
    rendered = cli._render(listed, as_json=True)
    assert alias.display_name not in rendered and str(alias.note) not in rendered
    assert str(alias.commit) not in rendered and representative.root_hash not in rendered
    if command == "launch":
        result = cli.launch(config_path, as_json=True, selected=alias.candidate_id, catalog_token=token, confirmed=True)
    else:
        result = cli.promote(config_path, apply=True, as_json=True, selected=alias.candidate_id, catalog_token=token)
    assert result["result"]["state"] == "COMPLETE"
    with pytest.raises(SyncError) as caught:
        cli.launch(config_path, as_json=True, selected="unknown-alias", catalog_token=token, confirmed=True)
    assert caught.value.code == "unknown_candidate"


def test_interactive_stale_then_reload_rebuilds_fresh_catalog_before_reselection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    config_path, _, _ = _install_context(
        monkeypatch, tmp_path, live="1" * 64, remote="2" * 64, baseline="2" * 64,
    )
    state = SyncState(last_applied_remote_commit="a" * 40,
                      last_applied_manifest_root_hash="2" * 64, machine_id="machine")
    generation = 0
    seen: list[tuple[str, str]] = []
    mutations: list[str] = []

    class Vault:
        def bind_remote_identity(self, expected):
            assert expected == ("test://remote", "test://remote")

    def fresh_catalog(_config_path, _config, **_kwargs):
        nonlocal generation
        generation += 1
        digit = f"{generation:x}"
        candidate = _candidate(f"candidate-{generation:02d}", "live", digit * 64, None)
        catalog = VersionCatalog(f"token-{generation:02d}".ljust(32, "t"), "a" * 40,
                                 digit * 64, (candidate,), baseline_root_hash="2" * 64)
        projection = {
            "generation": generation,
            "config": {"remote_fetch_url": "test://remote", "remote_push_url": "test://remote"},
            "execution_context": {"generation": generation},
        }
        return Vault(), catalog, state, projection

    monkeypatch.setattr(cli, "_fresh_catalog", fresh_catalog)
    monkeypatch.setattr(cli, "_selection_case", lambda *_args: cli.ReconcileCase.LIVE_AHEAD)
    monkeypatch.setattr(cli, "_assert_capability_boundary", lambda *_args: None)

    class Workflow:
        attempts = 0
        def __init__(self, *_args, **_kwargs): pass
        def execute_selection_plan(self, _plan, _registry, *, context_revalidate=None, exit_disposition_gate=None):
            Workflow.attempts += 1
            if Workflow.attempts == 1:
                assert mutations == []
                raise SyncError("selection_stale", "changed", 4)
            mutations.append("promoted")
            return {"state": "COMPLETE"}

    monkeypatch.setattr(cli, "LaunchWorkflow", Workflow)

    class Presenter:
        calls = 0
        def present_builder(self, build, directive):
            Presenter.calls += 1
            catalog = build(); directive(catalog)
            candidate = catalog.candidates[0]
            seen.append((catalog.token, candidate.candidate_id))
            assert mutations == []
            if Presenter.calls == 2:
                return SelectionRequest(None, None, action="reload")
            return SelectionRequest(candidate.candidate_id, "promote-only")

    result = cli.promote(config_path, apply=True, presenter=Presenter())
    assert result["result"]["state"] == "COMPLETE"
    assert Presenter.calls == 3 and Workflow.attempts == 2 and mutations == ["promoted"]
    assert len({token for token, _ in seen}) == 3
    assert len({candidate_id for _, candidate_id in seen}) == 3


@pytest.mark.parametrize(
    ("exit_code", "details"),
    [
        (6, {}),
        (4, {"mutated": True}),
        (4, {"recovery_required": True}),
        (4, {"next_command": "grim-dawn-sync recover"}),
    ],
    ids=["post-lock-exit", "mutated", "recovery-required", "next-command"],
)
def test_interactive_post_mutation_stale_fails_without_ui_or_build_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, exit_code: int, details: dict[str, object],
) -> None:
    live, remote = "1" * 64, "2" * 64
    config_path, _, _ = _install_context(monkeypatch, tmp_path, live=live, remote=remote, baseline=live)
    builds: list[str] = []
    original_fresh = cli._fresh_catalog

    def counted_fresh(*args, **kwargs):
        builds.append("build")
        return original_fresh(*args, **kwargs)

    monkeypatch.setattr(cli, "_fresh_catalog", counted_fresh)

    class Workflow:
        attempts = 0
        def __init__(self, *_args, **_kwargs): pass
        def execute_selection_plan(self, *_args, **_kwargs):
            Workflow.attempts += 1
            raise SyncError("selection_stale", "changed after mutation", exit_code, details)

    class Presenter:
        calls = 0
        def present_builder(self, build, directive):
            Presenter.calls += 1
            catalog = build(); directive(catalog)
            return SelectionRequest("remote-id", "launch")

    monkeypatch.setattr(cli, "LaunchWorkflow", Workflow)
    with pytest.raises(SyncError) as caught:
        cli.launch(config_path, presenter=Presenter())
    assert caught.value.code == "selection_stale"
    assert Presenter.calls == 1 and Workflow.attempts == 1 and builds == ["build"]


@pytest.mark.parametrize("as_json", [True, False], ids=["headless-json", "explicit-select"])
def test_noninteractive_stale_selection_fails_once_without_retry_or_ui(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, as_json: bool,
) -> None:
    live, remote = "1" * 64, "2" * 64
    config_path, _, _ = _install_context(monkeypatch, tmp_path, live=live, remote=remote, baseline=live)
    issued = cli.versions(config_path)
    original_fresh = cli._fresh_catalog; scans: list[str] = []; attempts: list[str] = []; mutations: list[str] = []
    def counted_fresh(*args, **kwargs):
        scans.append("scan")
        return original_fresh(*args, **kwargs)
    monkeypatch.setattr(cli, "_fresh_catalog", counted_fresh)
    class Workflow:
        def __init__(self, *_args, **_kwargs): pass
        def execute_selection_plan(self, *_args, **_kwargs):
            attempts.append("attempt")
            raise SyncError("selection_stale", "changed", 4)
    class NoUI:
        def present_builder(self, *_args, **_kwargs):
            pytest.fail("noninteractive stale selection must not enter the UI loop")
    monkeypatch.setattr(cli, "LaunchWorkflow", Workflow)
    with pytest.raises(SyncError) as caught:
        cli.launch(config_path, as_json=as_json, selected="remote-id",
                   catalog_token=issued["catalog_token"], presenter=NoUI())
    assert caught.value.code == "selection_stale"
    assert attempts == ["attempt"] and scans == ["scan"] and mutations == []
