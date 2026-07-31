from __future__ import annotations

from types import SimpleNamespace
import json
import subprocess
import sys

import pytest

from grim_dawn_sync.catalog_capability import issue_capability, read_remote_identity, safety_projection, verify_capability
from grim_dawn_sync.errors import SyncError
from grim_dawn_sync.state import SyncState
from grim_dawn_sync.version_catalog import ManifestDiff, SaveCandidate, VersionCatalog


def _projection(note: str | None = None) -> dict:
    config = SimpleNamespace(
        machine_id="a", branch="main", remote="origin", save_root="C:/save", vault_repo="C:/vault",
        public_dict=lambda: {"machine_id": "a", "branch": "main", "remote": "origin"},
    )
    state = SyncState(last_applied_remote_commit="a" * 40, last_applied_manifest_root_hash="1" * 64, machine_id="a")
    item = SaveCandidate("f" * 32, "remote_head", "latest", "2026-08-01T00:00:00Z", "a", "2" * 64,
                         "b" * 40, 1, 2, 3, ("hero",),
                         ManifestDiff(1, 0, 1, ("new",), ("old",), ("hero",)), note)
    catalog = VersionCatalog("ignored", "b" * 40, "1" * 64, (item,))
    return safety_projection(config, state, catalog)


def test_capability_is_deterministic_only_inside_current_utc_bucket() -> None:
    projection = _projection()
    one = issue_capability(projection, clock=lambda: 600.0, monotonic_clock=lambda: 1200.0)
    assert one == issue_capability(projection, clock=lambda: 899.999, monotonic_clock=lambda: 1499.999)
    assert one.startswith("c1_") and len(one) == 67
    verify_capability(one, projection, clock=lambda: 899.999, monotonic_clock=lambda: 1499.999)
    with pytest.raises(SyncError) as error:
        verify_capability(one, projection, clock=lambda: 900.0, monotonic_clock=lambda: 1499.999)
    assert error.value.code == "catalog_expired"
    with pytest.raises(SyncError) as error:
        verify_capability(one, projection, clock=lambda: 600.0, monotonic_clock=lambda: 1500.0)
    assert error.value.code == "catalog_expired"


@pytest.mark.parametrize("changed", [
    lambda value: {**value, "remote_head": "c" * 40},
    lambda value: {**value, "live_root_hash": "3" * 64},
    lambda value: {**value, "state": {**value["state"], "last_applied_manifest_root_hash": "4" * 64}},
    lambda value: {**value, "config": {**value["config"], "branch": "other"}},
    lambda value: {**value, "config": {**value["config"], "remote_url": "test://remotf"}},
    lambda value: {**value, "config": {**value["config"], "save_root_normalized": "C:/savf"}},
    lambda value: {**value, "candidates": [{**value["candidates"][0], "note": "changed"}]},
    lambda value: {**value, "candidates": [{**value["candidates"][0], "diff_from_live": {
        **value["candidates"][0]["diff_from_live"], "character_dirs_changed": ["other"],
    }}]},
    lambda value: {**value, "candidates": [{**value["candidates"][0], "aliases": [{
        "candidate_id": "e" * 32, "kind": "bookmark", "display_name": "alias",
        "created_at": "2026-08-01T00:00:01Z", "commit": "b" * 40, "note": "changed",
    }]}]},
])
def test_every_safety_projection_change_invalidates_token(changed) -> None:
    projection = _projection()
    token = issue_capability(projection, clock=lambda: 600.0, monotonic_clock=lambda: 1200.0)
    with pytest.raises(SyncError) as error:
        verify_capability(token, changed(projection), clock=lambda: 600.0, monotonic_clock=lambda: 1200.0)
    assert error.value.code == "catalog_expired"


@pytest.mark.parametrize("token", ["", "c1_", "c1_" + "g" * 64, "c1_" + "0" * 63, "refs/heads/main"])
def test_malformed_or_ref_injection_token_is_rejected(token: str) -> None:
    with pytest.raises(SyncError) as error:
        verify_capability(token, _projection(), clock=lambda: 600.0, monotonic_clock=lambda: 1200.0)
    assert error.value.code == "invalid_catalog_token"


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.0, True])
def test_wall_and_monotonic_clock_values_fail_closed(bad: float) -> None:
    for kwargs in (
        {"clock": lambda: bad, "monotonic_clock": lambda: 1.0},
        {"clock": lambda: 1.0, "monotonic_clock": lambda: bad},
    ):
        with pytest.raises(SyncError) as error:
            issue_capability(_projection(), **kwargs)
        assert error.value.code == "catalog_clock_invalid"


def test_clock_exceptions_fail_closed() -> None:
    def broken() -> float:
        raise RuntimeError("private")
    with pytest.raises(SyncError) as error:
        issue_capability(_projection(), clock=broken, monotonic_clock=lambda: 1.0)
    assert error.value.code == "catalog_clock_invalid"


def test_capability_verifies_in_a_separate_python_process() -> None:
    projection = _projection()
    token = issue_capability(projection, clock=lambda: 600.0, monotonic_clock=lambda: 1200.0)
    code = (
        "import json,sys;from grim_dawn_sync.catalog_capability import verify_capability;"
        "verify_capability(sys.argv[1],json.loads(sys.argv[2]),clock=lambda:600.0,monotonic_clock=lambda:1200.0);"
        "print('verified')"
    )
    result = subprocess.run([sys.executable, "-c", code, token, json.dumps(projection)],
                            check=False, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0 and result.stdout.strip() == "verified"


def test_remote_identity_binds_one_equal_fetch_and_push_destination() -> None:
    class Runner:
        def run(self, *args, **_kwargs):
            return SimpleNamespace(returncode=0, stdout="ssh://vault\n")
    assert read_remote_identity(SimpleNamespace(runner=Runner()), "origin") == ("ssh://vault", "ssh://vault")


@pytest.mark.parametrize("fetch,push", [
    ("ssh://one\nssh://two\n", "ssh://one\n"),
    ("ssh://one\n", "ssh://two\n"),
    ("", "ssh://one\n"),
])
def test_remote_identity_rejects_multiple_missing_or_different_push_url(fetch: str, push: str) -> None:
    class Runner:
        def run(self, *args, **_kwargs):
            return SimpleNamespace(returncode=0, stdout=push if "--push" in args else fetch)
    with pytest.raises(SyncError) as error:
        read_remote_identity(SimpleNamespace(runner=Runner()), "origin")
    assert error.value.code == "catalog_remote_identity_invalid"
