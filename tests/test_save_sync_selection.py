from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

from grim_dawn_sync.errors import SyncError
from grim_dawn_sync.selection import (
    ReconcileCase,
    SelectionRegistry,
    cancel_selection,
    classify_reconciliation,
    selection_policy,
)
from grim_dawn_sync.version_catalog import ManifestDiff, SaveCandidate, VersionCatalog


def candidate(kind: str, root: str, commit: str | None) -> SaveCandidate:
    return SaveCandidate("candidate-" + kind, kind, kind, "2026-08-01T00:00:00+00:00", "machine", root, commit, 0, 0, 0, (), ManifestDiff(0, 0, 0))


def catalog(*items: SaveCandidate, token: str = "t" * 32, remote: str | None = "a" * 40, live: str = "1" * 64) -> VersionCatalog:
    return VersionCatalog(token, remote, live, tuple(items))


@pytest.mark.parametrize(("live", "remote", "baseline", "expected"), [
    ("a", "a", "a", ReconcileCase.EQUAL),
    ("a", "b", "a", ReconcileCase.REMOTE_AHEAD),
    ("b", "a", "a", ReconcileCase.LIVE_AHEAD),
    ("b", "c", "a", ReconcileCase.DIVERGED),
    ("a", "b", None, ReconcileCase.BASELINE_MISSING),
])
def test_classification_is_pure_and_complete(live, remote, baseline, expected) -> None:
    assert classify_reconciliation(live_root_hash=live, remote_root_hash=remote, baseline_root_hash=baseline) is expected
    directive = selection_policy(expected)
    if expected is ReconcileCase.DIVERGED:
        assert directive.initial_selection is None
    if expected is ReconcileCase.BASELINE_MISSING:
        assert directive.allowed_operations == ("cancel",)


@pytest.mark.parametrize("case", list(ReconcileCase))
@pytest.mark.parametrize("policy", ["on_diff", "always"])
def test_selection_policy_full_case_by_policy_matrix(case: ReconcileCase, policy: str) -> None:
    """All 5 ReconcileCase values x both policy values (T-C 5.3 requirement)."""
    on_diff = selection_policy(case, policy="on_diff")
    directive = selection_policy(case, policy=policy)
    # `always` may only ever widen show_selector to True; every other field,
    # and BASELINE_MISSING's show_selector in particular, stays untouched.
    assert directive.initial_selection == on_diff.initial_selection
    assert directive.allowed_operations == on_diff.allowed_operations
    assert directive.bookmark_displaced_remote == on_diff.bookmark_displaced_remote
    if policy == "on_diff":
        assert directive.show_selector == on_diff.show_selector
    elif case is ReconcileCase.BASELINE_MISSING:
        assert directive.show_selector is False
    else:
        assert directive.show_selector is True


def test_selection_policy_defaults_to_on_diff_when_unspecified() -> None:
    assert selection_policy(ReconcileCase.EQUAL) == selection_policy(ReconcileCase.EQUAL, policy="on_diff")


def test_selection_policy_rejects_unknown_policy_name() -> None:
    with pytest.raises(SyncError) as caught:
        selection_policy(ReconcileCase.EQUAL, policy="never")
    assert caught.value.code == "invalid_selection_policy"


def test_equal_live_representative_never_bookmarks_displaced_remote() -> None:
    live = candidate("live", "1" * 64, None)
    value = catalog(live, live="1" * 64)
    registry = SelectionRegistry(); registry.register(value)
    plan = registry.build_plan(catalog_token=value.token, candidate_id=live.candidate_id,
                               mode="launch", case=ReconcileCase.EQUAL)
    assert plan.reconcile_case is ReconcileCase.EQUAL
    assert plan.bookmark_displaced_remote is False


@pytest.mark.parametrize("row", json.loads((Path(__file__).parent / "fixtures" / "save_sync_selection_decisions.json").read_text(encoding="utf-8")), ids=lambda row: row["case"])
def test_policy_matches_the_fixed_decision_table(row: dict) -> None:
    directive = selection_policy(ReconcileCase(row["case"]))
    assert directive.show_selector is row["show_selector"]
    assert directive.initial_selection == row["initial_selection"]
    assert directive.allowed_operations == tuple(row["allowed_operations"])
    assert directive.bookmark_displaced_remote is row["bookmark_displaced_remote"]
    if row["case"] == "equal":
        assert not directive.show_selector and directive.initial_selection == "remote_head"
    if row["case"] == "remote_ahead":
        assert directive.initial_selection == "remote_head" and not directive.bookmark_displaced_remote
    if row["case"] == "live_ahead":
        assert directive.initial_selection == "live" and directive.bookmark_displaced_remote
    if row["case"] == "diverged":
        assert directive.initial_selection is None and directive.bookmark_displaced_remote


def test_token_candidate_and_cross_catalog_tampering_fail_closed() -> None:
    registry = SelectionRegistry(); one = catalog(candidate("remote_head", "2" * 64, "b" * 40)); registry.register(one)
    with pytest.raises(SyncError, match="unknown"):
        registry.build_plan(catalog_token="x" * 32, candidate_id="candidate-remote_head", mode="launch", case=ReconcileCase.REMOTE_AHEAD)
    plan = registry.build_plan(catalog_token=one.token, candidate_id="candidate-remote_head", mode="launch", case=ReconcileCase.REMOTE_AHEAD)
    with pytest.raises(SyncError) as caught:
        registry.confirm(replace(plan, catalog_token="y" * 32))
    assert caught.value.code == "selection_plan_tampered"
    with pytest.raises(SyncError) as caught:
        registry.confirm(replace(plan, selected_root_hash="f" * 64))
    assert caught.value.code == "selection_plan_tampered"


def test_catalog_expiry_and_history_second_confirmation() -> None:
    now = [100.0]; registry = SelectionRegistry(ttl_seconds=5, clock=lambda: now[0])
    item = candidate("history", "2" * 64, "b" * 40); value = catalog(item); registry.register(value)
    plan = registry.build_plan(catalog_token=value.token, candidate_id=item.candidate_id, mode="launch", case=ReconcileCase.REMOTE_AHEAD)
    with pytest.raises(SyncError) as caught:
        registry.revalidate(plan, live_manifest=lambda: {"root_hash": "1" * 64}, remote_head=lambda: "a" * 40)
    assert caught.value.code == "selection_confirmation_required"
    registry.revalidate(registry.confirm(plan), live_manifest=lambda: {"root_hash": "1" * 64}, remote_head=lambda: "a" * 40)
    now[0] = 105.0
    with pytest.raises(SyncError) as caught:
        registry.build_plan(catalog_token=value.token, candidate_id=item.candidate_id, mode="launch", case=ReconcileCase.REMOTE_AHEAD)
    assert caught.value.code == "catalog_expired"


def test_revalidation_detects_live_or_remote_change_before_mutation() -> None:
    registry = SelectionRegistry(); item = candidate("remote_head", "2" * 64, "b" * 40); value = catalog(item); registry.register(value)
    plan = registry.build_plan(catalog_token=value.token, candidate_id=item.candidate_id, mode="launch", case=ReconcileCase.REMOTE_AHEAD)
    for live, remote in (("9" * 64, "a" * 40), ("1" * 64, "c" * 40)):
        with pytest.raises(SyncError) as caught:
            registry.revalidate(plan, live_manifest=lambda root=live: {"root_hash": root}, remote_head=lambda oid=remote: oid)
        assert caught.value.code == "selection_stale"


def test_legacy_or_bookmark_needs_verified_target_and_never_accepts_an_arbitrary_oid() -> None:
    legacy = candidate("legacy", "2" * 64, "b" * 40); value = catalog(legacy)
    registry = SelectionRegistry(); registry.register(value)
    with pytest.raises(SyncError) as caught:
        registry.build_plan(catalog_token=value.token, candidate_id=legacy.candidate_id, mode="launch", case=ReconcileCase.REMOTE_AHEAD)
    assert caught.value.code == "candidate_not_authorized"
    approved = SelectionRegistry(verified_bookmark_target=lambda item: item.commit == "b" * 40); approved.register(value)
    assert approved.build_plan(catalog_token=value.token, candidate_id=legacy.candidate_id, mode="launch", case=ReconcileCase.REMOTE_AHEAD).selected_commit == "b" * 40
    arbitrary = candidate("legacy", "3" * 64, "f" * 40); bad_catalog = catalog(arbitrary, token="u" * 32); approved.register(bad_catalog)
    with pytest.raises(SyncError) as caught:
        approved.build_plan(catalog_token=bad_catalog.token, candidate_id=arbitrary.candidate_id, mode="launch", case=ReconcileCase.REMOTE_AHEAD)
    assert caught.value.code == "candidate_not_authorized"


def test_cancel_has_no_callbacks_or_mutation() -> None:
    calls: list[str] = []
    assert cancel_selection().status == "cancelled"
    assert calls == []


def test_plan_nonce_binds_every_safety_field_and_confirmation_lives_in_registry() -> None:
    registry = SelectionRegistry(); item = candidate("history", "2" * 64, "b" * 40); value = catalog(item); registry.register(value)
    plan = registry.build_plan(catalog_token=value.token, candidate_id=item.candidate_id, mode="launch", case=ReconcileCase.REMOTE_AHEAD, display_name="name", note="note")
    for field, forged in {
        "mode": "promote-only", "bookmark_displaced_remote": False, "confirmation_required": False,
        "confirmed": True, "display_name": "forged", "note": "forged",
    }.items():
        with pytest.raises(SyncError) as caught:
            registry.resolve(replace(plan, **{field: forged}))
        assert caught.value.code == "selection_plan_tampered"
    with pytest.raises(SyncError):
        registry.revalidate(replace(plan, confirmed=True), live_manifest=lambda: {"root_hash": "1" * 64}, remote_head=lambda: "a" * 40)
    approved = registry.confirm(plan)
    assert registry.resolve(approved) == approved


@pytest.mark.parametrize("case,mode", [(ReconcileCase.EQUAL, "promote-only"), (ReconcileCase.BASELINE_MISSING, "launch")])
def test_policy_forbidden_operations_cannot_make_a_plan(case: ReconcileCase, mode: str) -> None:
    registry = SelectionRegistry(); item = candidate("remote_head", "2" * 64, "b" * 40); value = catalog(item); registry.register(value)
    with pytest.raises(SyncError) as caught:
        registry.build_plan(catalog_token=value.token, candidate_id=item.candidate_id, mode=mode, case=case)
    assert caught.value.code == "selection_operation_forbidden"


@pytest.mark.parametrize("kind,root,commit", [("other", "2" * 64, "b" * 40), ("remote_head", "bad", "b" * 40), ("remote_head", "2" * 64, "HEAD")])
def test_unknown_kind_and_invalid_candidate_identifiers_are_rejected(kind: str, root: str, commit: str) -> None:
    registry = SelectionRegistry(); item = candidate(kind, root, commit); value = catalog(item); registry.register(value)
    with pytest.raises(SyncError) as caught:
        registry.build_plan(catalog_token=value.token, candidate_id=item.candidate_id, mode="launch", case=ReconcileCase.REMOTE_AHEAD)
    assert caught.value.code == "candidate_not_authorized"


@pytest.mark.parametrize("ttl", [float("inf"), float("nan"), -1])
def test_ttl_must_be_finite_and_positive(ttl: float) -> None:
    with pytest.raises(ValueError):
        SelectionRegistry(ttl_seconds=ttl)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), "100", None, True])
def test_injected_clock_invalid_values_fail_closed(value: object) -> None:
    item = candidate("remote_head", "2" * 64, "b" * 40); value_catalog = catalog(item)
    registry = SelectionRegistry(clock=lambda: value)
    with pytest.raises(SyncError) as caught:
        registry.register(value_catalog)
    assert caught.value.code == "catalog_clock_invalid"


def test_nonce_tampering_and_expiry_before_or_after_confirmation_are_rejected() -> None:
    now = [10.0]; item = candidate("history", "2" * 64, "b" * 40); value = catalog(item)
    registry = SelectionRegistry(ttl_seconds=1, clock=lambda: now[0]); registry.register(value)
    plan = registry.build_plan(catalog_token=value.token, candidate_id=item.candidate_id, mode="launch", case=ReconcileCase.REMOTE_AHEAD)
    with pytest.raises(SyncError) as caught:
        registry.resolve(replace(plan, plan_nonce="f" * 32))
    assert caught.value.code == "selection_plan_tampered"
    now[0] = 11.0
    with pytest.raises(SyncError) as caught:
        registry.confirm(plan)
    assert caught.value.code == "catalog_expired"

    now = [20.0]; fresh = SelectionRegistry(ttl_seconds=1, clock=lambda: now[0]); fresh.register(value)
    fresh_plan = fresh.build_plan(catalog_token=value.token, candidate_id=item.candidate_id, mode="launch", case=ReconcileCase.REMOTE_AHEAD)
    approved = fresh.confirm(fresh_plan); now[0] = 21.0
    with pytest.raises(SyncError) as caught:
        fresh.revalidate(approved, live_manifest=lambda: {"root_hash": "1" * 64}, remote_head=lambda: "a" * 40)
    assert caught.value.code == "catalog_expired"
