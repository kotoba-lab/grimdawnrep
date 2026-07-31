from __future__ import annotations

from grim_dawn_sync.errors import SyncError
from threading import Event, get_ident
import inspect

import pytest

from grim_dawn_sync.selection import CancelledSelection, ReconcileCase, SelectionDirective, SelectionRegistry
from grim_dawn_sync.selector_ui import SelectionRequest, TkSelectionPresenter, _candidate_detail_text, build_plan_from_request, load_catalog_in_worker, present_tk, present_tk_from_builder
from grim_dawn_sync.version_catalog import ManifestDiff, SaveCandidate, VersionCatalog


def _catalog() -> VersionCatalog:
    item = SaveCandidate("a" * 32, "remote_head", "Latest", "now", "m", "1" * 64, "b" * 40, 1, 2, 3, ("hero",), ManifestDiff(0, 0, 0))
    return VersionCatalog("t" * 32, "b" * 40, "1" * 64, (item,))


def test_presenter_is_domain_adapter(monkeypatch) -> None:
    sentinel = SelectionRequest("a" * 32, "launch")
    monkeypatch.setattr("grim_dawn_sync.selector_ui.present_tk", lambda *_: sentinel)
    assert TkSelectionPresenter().present(_catalog(), SelectionDirective(True, "remote_head", ("launch", "cancel"), False)) is sentinel


def test_presenter_builder_uses_worker_entry(monkeypatch) -> None:
    sentinel = SelectionRequest("a" * 32, "launch")
    calls = []
    monkeypatch.setattr("grim_dawn_sync.selector_ui.present_tk_from_builder", lambda build, directive: calls.append((build(), directive)) or sentinel)
    directive = SelectionDirective(True, "remote_head", ("launch", "cancel"), False)
    assert TkSelectionPresenter().present_builder(_catalog, directive) is sentinel
    assert calls == [(_catalog(), directive)]


def test_tk_init_failure_is_fail_closed(monkeypatch) -> None:
    import tkinter
    monkeypatch.setattr(tkinter, "Tk", lambda: (_ for _ in ()).throw(RuntimeError("no display")))
    try: present_tk(_catalog(), SelectionDirective(True, "remote_head", ("launch", "cancel"), False))
    except SyncError as error: assert error.code == "selection_ui_unavailable"
    else: raise AssertionError("Tk failure must not select automatically")


def test_equal_policy_never_displays_or_constructs_selector_widgets(monkeypatch) -> None:
    import tkinter
    from tkinter import ttk

    events: list[str] = []

    class Root:
        def withdraw(self): events.append("withdraw")
        def deiconify(self): events.append("deiconify")
        def title(self, _value): pass
        def minsize(self, *_value): pass
        def columnconfigure(self, *_args, **_kwargs): pass
        def rowconfigure(self, *_args, **_kwargs): pass
        def protocol(self, *_args): pass
        def bind(self, *_args): pass
        def mainloop(self): pass
        def destroy(self): events.append("destroy")

    class Handle:
        def close(self): pass

    def immediate(_root, build, on_loaded, on_error):
        try: on_loaded(build())
        except SyncError as error: on_error(error)
        return Handle()

    live = SaveCandidate("c" * 32, "live", "Live", "now", "m", "1" * 64, None,
                         1, 2, 3, (), ManifestDiff(0, 0, 0))
    catalog = VersionCatalog("t" * 32, "b" * 40, "1" * 64, (live,))
    monkeypatch.setattr(tkinter, "Tk", Root)
    monkeypatch.setattr(ttk, "Frame", lambda *_args, **_kwargs: pytest.fail("selector widget constructed"))
    monkeypatch.setattr("grim_dawn_sync.selector_ui.load_catalog_in_worker", immediate)
    result = present_tk_from_builder(
        lambda: catalog,
        SelectionDirective(False, "live", ("launch",), False),
    )
    assert result == SelectionRequest(live.candidate_id, "launch")
    assert events[0] == "withdraw" and "deiconify" not in events


def test_catalog_worker_marshals_callbacks_to_after() -> None:
    scheduled = []; loaded = []
    class Root:
        def after(self, _delay, callback): scheduled.append(callback)
    thread = load_catalog_in_worker(Root(), _catalog, loaded.append, lambda error: (_ for _ in ()).throw(error))
    thread.join(timeout=2)
    while scheduled:
        scheduled.pop(0)()
    assert loaded == [_catalog()]


def test_worker_callback_runs_only_when_main_thread_drains_after() -> None:
    scheduled = []; callback_threads = []
    class Root:
        def after(self, _delay, callback): scheduled.append(callback); return len(scheduled)
        def after_cancel(self, _value): pass
    main = get_ident(); handle = load_catalog_in_worker(Root(), _catalog, lambda _value: callback_threads.append(get_ident()), lambda _error: None)
    handle.join(timeout=2)
    assert callback_threads == []
    while scheduled: scheduled.pop(0)()
    assert callback_threads == [main]


def test_worker_close_discards_late_result_and_cancels_poll() -> None:
    scheduled = []; cancelled = []; loaded = []; release = Event()
    class Root:
        def after(self, _delay, callback): scheduled.append(callback); return "poll-id"
        def after_cancel(self, value): cancelled.append(value)
    handle = load_catalog_in_worker(Root(), lambda: (release.wait(), _catalog())[1], loaded.append, lambda _error: None)
    handle.close(); release.set(); handle.join(timeout=2)
    while scheduled: scheduled.pop(0)()
    assert loaded == [] and cancelled == ["poll-id"]


def test_unexpected_worker_error_is_privacy_safe() -> None:
    scheduled = []; errors = []
    class Root:
        def after(self, _delay, callback): scheduled.append(callback)
        def after_cancel(self, _value): pass
    handle = load_catalog_in_worker(Root(), lambda: (_ for _ in ()).throw(RuntimeError("private path")), lambda _value: None, errors.append)
    handle.join(timeout=2)
    while scheduled: scheduled.pop(0)()
    assert errors[0].code == "selection_catalog_failed" and "private" not in errors[0].message


def test_risky_confirmation_is_bound_to_canonical_registry_plan() -> None:
    item = SaveCandidate("a" * 32, "history", "Past", "now", "m", "2" * 64, "b" * 40, 1, 2, 3, (), ManifestDiff(0, 0, 0))
    catalog = VersionCatalog("t" * 32, "a" * 40, "1" * 64, (item,))
    registry = SelectionRegistry(); registry.register(catalog)
    binding = {"expected_context_digest": "c" * 64, "expected_remote_identity": ("test://vault", "test://vault")}
    plain = build_plan_from_request(registry, SelectionRequest(item.candidate_id, "launch"), catalog_token=catalog.token, case=ReconcileCase.REMOTE_AHEAD, **binding)
    with pytest.raises(SyncError) as caught:
        registry.revalidate(plain, live_manifest=lambda: {"root_hash": "1" * 64}, remote_head=lambda: "a" * 40)
    assert caught.value.code == "selection_confirmation_required"
    confirmed = build_plan_from_request(registry, SelectionRequest(item.candidate_id, "launch", confirmation_granted=True), catalog_token=catalog.token, case=ReconcileCase.REMOTE_AHEAD, **binding)
    assert confirmed.confirmed is True


def test_production_plan_builder_rejects_missing_context_binding() -> None:
    item = SaveCandidate("a" * 32, "live", "Live", "now", "m", "2" * 64, None, 1, 2, 3, (), ManifestDiff(0, 0, 0))
    catalog = VersionCatalog("t" * 32, "b" * 40, "2" * 64, (item,))
    registry = SelectionRegistry(); registry.register(catalog)
    with pytest.raises(SyncError) as caught:
        build_plan_from_request(registry, SelectionRequest(item.candidate_id, "launch"),
                                catalog_token=catalog.token, case=ReconcileCase.EQUAL)
    assert caught.value.code == "selection_context_required"


def test_plain_text_request_preserves_special_characters_as_data() -> None:
    request = SelectionRequest("a" * 32, None, "<b>&;$(x)", "line-like <tag> & text", action="bookmark")
    assert request.display_name == "<b>&;$(x)" and request.note == "line-like <tag> & text"


def test_detail_text_has_required_safe_comparison_fields_without_internal_ids() -> None:
    item = SaveCandidate("a" * 32, "history", "Named", "2026-08-01T00:00:00Z", "machine-a",
                         "1" * 64, "b" * 40, 2, 4, 512, ("Hero",),
                         ManifestDiff(1, 2, 3, ("New",), ("Old",), ("Hero",)), "plain <note>")
    remote = SaveCandidate("c" * 32, "remote_head", "Latest", "2026-08-01T00:00:00Z", "machine-b",
                           "2" * 64, "d" * 40, 2, 4, 512, (), ManifestDiff(0, 0, 0))
    catalog = VersionCatalog("t" * 32, remote.commit, "3" * 64, (item, remote), baseline_root_hash=item.root_hash)
    text = _candidate_detail_text(item, catalog, SelectionDirective(True, "remote_head", ("launch",), False), [item, remote])
    for expected in ("UTC /", "machine-a", "Characters: 2", "Files: 4", "512 bytes", "live: different",
                     "baseline: same", "remote main: different", "1 added, 2 deleted, 3 changed",
                     "Character directories", "plain <note>", "Not the policy default", "Warning:"):
        assert expected in text
    assert item.root_hash not in text and item.commit not in text and catalog.token not in text


def test_close_escape_cancel_and_reload_requests_are_domain_noops() -> None:
    writes: list[object] = []
    cancelled = CancelledSelection()
    reload_request = SelectionRequest(None, None, action="reload")
    assert cancelled.status == "cancelled" and reload_request.action == "reload" and writes == []
    source = inspect.getsource(__import__("grim_dawn_sync.selector_ui", fromlist=["present_tk_from_builder"]).present_tk_from_builder)
    assert 'protocol("WM_DELETE_WINDOW", close)' in source
    assert 'bind("<Escape>", lambda _event: close())' in source
    assert "_candidate_detail_text(item, catalog, resolved, selectable)" in source
    assert "_candidate_detail_text(item, catalog, directive, selectable)" not in source
    assert source.index("root.withdraw()") < source.index("load_catalog_in_worker")
    assert source.index("if not resolved.show_selector:") < source.index("root.deiconify()")
