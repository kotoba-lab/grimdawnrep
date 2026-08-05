"""Tk adapter for explicit, fail-closed save selection."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from queue import Queue, Empty
import re
from threading import Thread
from typing import Callable, Literal, Protocol

from grim_dawn_sync.errors import EXIT_CONFLICT, SyncError
from grim_dawn_sync.selection import (
    CancelledSelection,
    ExitDisposition,
    ReconcileCase,
    SelectionDirective,
    SelectionMode,
    SelectionPlan,
    SelectionRegistry,
    allowed_exit_disposition_transition,
)
from grim_dawn_sync.version_catalog import SaveCandidate, VersionCatalog


SelectionAction = Literal["select", "bookmark", "reload"]


@dataclass(frozen=True)
class SelectionRequest:
    candidate_id: str | None
    mode: SelectionMode | None
    display_name: str | None = None
    note: str | None = None
    action: SelectionAction = "select"
    confirmation_granted: bool = False
    exit_disposition: ExitDisposition = "publish"


class SelectionPresenter(Protocol):
    def present(self, catalog: VersionCatalog, directive: SelectionDirective) -> SelectionRequest | CancelledSelection: ...
    def present_builder(self, build: Callable[[], VersionCatalog], directive: SelectionDirective | Callable[[VersionCatalog], SelectionDirective]) -> SelectionRequest | CancelledSelection: ...


def _display_time(value: str | None, *, absent: str = "Not applicable (no commit)") -> str:
    if value is None:
        return absent
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")); local = parsed.astimezone()
        return f"{parsed.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} / {local.strftime('%Y-%m-%d %H:%M %Z')}"
    except (ValueError, TypeError):
        return "Verified time unavailable"


def _remote_check_summary_lines(catalog: VersionCatalog, item: SaveCandidate | None = None) -> str:
    """The three plan-mandated times, kept as three separate lines.

    They are distinct axes and must never be collapsed into one value: when
    the remote check ran, when the shown save was created, and when remote
    main's head was committed.
    """
    report = catalog.remote_check
    return (f"Remote check: {_display_time(report.checked_at, absent='not performed')} (status: {report.status})\n"
            f"Selected candidate's save creation: "
            f"{_display_time(item.created_at, absent='no candidate selected') if item is not None else 'no candidate selected'}\n"
            f"Remote main's latest commit: {_display_time(report.head_committed_at, absent='unavailable')}")


def _provenance_lines(item: SaveCandidate, catalog: VersionCatalog) -> str:
    """List every coalesced provenance's kind, save-creation, and commit time.

    Several distinct commits can share one root hash (T-A: two save pushes
    with identical content, different commit times).  Without this, they are
    indistinguishable in the UI even though only one row is shown per root.
    """
    representative = next((entry for entry in catalog.candidates if entry.root_hash == item.root_hash), None)
    if representative is None:
        return ""
    entries = [representative, *representative.aliases]
    if len(entries) <= 1:
        return ""
    lines = "\n".join(
        f"  - {entry.kind}: save created {_display_time(entry.created_at)}; commit dated {_display_time(entry.committed_at)}"
        for entry in entries
    )
    return f"Combined provenance for this save content ({len(entries)} entries share it):\n{lines}\n"


def _candidate_detail_text(item: SaveCandidate, catalog: VersionCatalog, directive: SelectionDirective,
                           selectable: list[SaveCandidate]) -> str:
    diff = item.diff_from_live; labels = ", ".join(item.character_labels) or "No character directories detected"
    live_relation = "same" if item.root_hash == catalog.live_root_hash else "different"
    remote_candidate = next((entry for entry in selectable if entry.kind == "remote_head"), None)
    remote_relation = "unavailable" if remote_candidate is None else ("same" if item.root_hash == remote_candidate.root_hash else "different")
    baseline_relation = "unavailable" if catalog.baseline_root_hash is None else ("same" if item.root_hash == catalog.baseline_root_hash else "different")
    reason = ("Recommended: this matches the policy for the current synchronization state."
              if item.kind == directive.initial_selection else "Not the policy default: review the relations and warning before continuing.")
    warning = ("Warning: selecting a history, bookmark, or session-start version requires explicit confirmation."
               if item.kind in {"history", "bookmark", "legacy", "session_start"} else "Warning: verify that this is the intended game progress before launch.")
    char_diff = (f"Character directories: +{', '.join(diff.character_dirs_added) or 'none'}; "
                 f"-{', '.join(diff.character_dirs_removed) or 'none'}; changed {', '.join(diff.character_dirs_changed) or 'none'}.")
    provenance = _provenance_lines(item, catalog)
    return (f"Save created: {_display_time(item.created_at)}\nCommit dated: {_display_time(item.committed_at)}\n"
            f"Published by: {item.machine_id}\n"
            f"Characters: {item.character_count} ({labels})\nFiles: {item.file_count}; Size: {item.total_bytes} bytes\n"
            f"Relations — live: {live_relation}; baseline: {baseline_relation}; remote main: {remote_relation}\n"
            f"Diff from live: {diff.added} added, {diff.removed} deleted, {diff.changed} changed files.\n"
            f"{char_diff}\nNote: {item.note or 'none'}\n{provenance}{reason}\n{warning}")


def build_plan_from_request(registry: SelectionRegistry, request: SelectionRequest, *, catalog_token: str, case: ReconcileCase,
                            expected_context_digest: str | None = None, expected_remote_identity: tuple[str, str] | None = None) -> SelectionPlan:
    """Bind presenter confirmation to the canonical registry plan.

    A fake presenter cannot bypass second confirmation merely by returning a
    candidate: only an explicit confirmation flag on a risky request causes
    the registry's canonical plan to be confirmed.
    """
    if request.action != "select" or request.candidate_id is None or request.mode is None:
        raise SyncError("invalid_selection_request", "Selection request cannot create a launch plan.", EXIT_CONFLICT)
    if (
        not isinstance(expected_context_digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", expected_context_digest)
        or not isinstance(expected_remote_identity, tuple)
        or len(expected_remote_identity) != 2
        or any(not isinstance(value, str) or not value or "\0" in value for value in expected_remote_identity)
    ):
        raise SyncError("selection_context_required", "Production selection plans require bound context and remote identity.", EXIT_CONFLICT)
    plan = registry.build_plan(catalog_token=catalog_token, candidate_id=request.candidate_id, mode=request.mode,
                               case=case, display_name=request.display_name, note=request.note,
                               expected_context_digest=expected_context_digest, expected_remote_identity=expected_remote_identity,
                               exit_disposition=request.exit_disposition)
    if plan.confirmation_required and request.confirmation_granted:
        return registry.confirm(plan)
    return plan


class CatalogWorker:
    def __init__(self, thread: Thread, root: object, state: dict[str, object]) -> None:
        self.thread, self.root, self.state = thread, root, state
    def join(self, timeout: float | None = None) -> None: self.thread.join(timeout)
    def close(self) -> None:
        self.state["closed"] = True
        after_id = self.state.get("after_id")
        if after_id is not None:
            try: self.root.after_cancel(after_id)  # type: ignore[attr-defined]
            except Exception: pass
            self.state["after_id"] = None


def load_catalog_in_worker(root: object, build: Callable[[], VersionCatalog], on_loaded: Callable[[VersionCatalog], None], on_error: Callable[[SyncError], None]) -> CatalogWorker:
    """Run catalog I/O off-thread and marshal exactly one result via Tk.after."""
    results: Queue[VersionCatalog | SyncError] = Queue(maxsize=1)
    state: dict[str, object] = {"closed": False, "after_id": None}
    def worker() -> None:
        try: value: VersionCatalog | SyncError = build()
        except SyncError as error: value = error
        except Exception: value = SyncError("selection_catalog_failed", "Save versions could not be loaded.", EXIT_CONFLICT)
        if not state["closed"]:
            try: results.put_nowait(value)
            except Exception: pass
    def poll() -> None:
        state["after_id"] = None
        if state["closed"]: return
        try: value = results.get_nowait()
        except Empty:
            try: state["after_id"] = root.after(25, poll)  # type: ignore[attr-defined]
            except Exception: state["closed"] = True
            return
        if state["closed"]: return
        if isinstance(value, SyncError): on_error(value)
        else: on_loaded(value)
    thread = Thread(target=worker, daemon=True); thread.start()
    try: state["after_id"] = root.after(0, poll)  # type: ignore[attr-defined]
    except Exception:
        state["closed"] = True
        raise SyncError("selection_ui_unavailable", "Save selection UI is unavailable; no selection was made.", EXIT_CONFLICT) from None
    return CatalogWorker(thread, root, state)


def present_tk_from_builder(build: Callable[[], VersionCatalog], directive: SelectionDirective | Callable[[VersionCatalog], SelectionDirective]) -> SelectionRequest | CancelledSelection:
    """Resolve policy off-screen and show a window only when selection is needed."""
    root = None; worker: CatalogWorker | None = None
    result: SelectionRequest | CancelledSelection = CancelledSelection()
    callback_error: BaseException | None = None
    try:
        import tkinter as tk
        from tkinter import messagebox, ttk
        root = tk.Tk(); root.withdraw(); root.title("Grim Dawn Save Selection"); root.minsize(720, 420)
        root.columnconfigure(0, weight=1); root.rowconfigure(0, weight=1)

        def fail_callback(_exc, value, _traceback) -> None:
            nonlocal callback_error
            callback_error = value; close()
        root.report_callback_exception = fail_callback

        def close() -> None:
            if worker is not None: worker.close()
            try: root.destroy()
            except Exception: pass

        def on_error(error: SyncError) -> None:
            nonlocal callback_error
            callback_error = error; close()

        def populate(catalog: VersionCatalog) -> None:
            nonlocal result
            resolved = directive(catalog) if callable(directive) else directive
            if not resolved.show_selector:
                item = next((candidate for candidate in catalog.candidates if candidate.kind == "live"), None)
                if item is None:
                    on_error(SyncError("selection_required", "No verified automatic selection is available.", EXIT_CONFLICT)); return
                result = SelectionRequest(item.candidate_id, "launch"); close(); return
            frame = ttk.Frame(root, padding=12); frame.grid(sticky="nsew"); frame.columnconfigure(0, weight=1)
            ttk.Label(frame, text="Choose the save data to use", font=("TkDefaultFont", 12, "bold")).grid(row=0, column=0, sticky="w")
            remote_ok = catalog.remote_check.status == "ok"
            summary = ttk.Label(frame, justify="left", text=_remote_check_summary_lines(catalog)); summary.grid(row=1, column=0, sticky="w", pady=(4, 0))
            next_row = 2
            if not remote_ok:
                warning_text = ("Warning: the remote version could not be freshly confirmed "
                                f"(status: {catalog.remote_check.status}). The candidates shown below may be stale.")
                ttk.Label(frame, justify="left", foreground="#a00", text=warning_text).grid(row=next_row, column=0, sticky="w", pady=(4, 0))
                next_row += 1
            tree_row, detail_row, fields_row, buttons_row, destructive_row = next_row, next_row + 1, next_row + 2, next_row + 3, next_row + 4
            frame.rowconfigure(tree_row, weight=1)
            tree = ttk.Treeview(frame, columns=("name", "kind", "created", "committed", "files", "changes"), show="headings", selectmode="browse", height=9)
            for key, text, width in (("name", "Save version", 200), ("kind", "Type", 90), ("created", "Save created", 150),
                                     ("committed", "Commit", 150), ("files", "Files", 70), ("changes", "Changes", 120)):
                tree.heading(key, text=text); tree.column(key, width=width, anchor="w")
            tree.grid(row=tree_row, column=0, sticky="nsew", pady=(8, 8))
            detail = ttk.Label(frame, justify="left", wraplength=680); detail.grid(row=detail_row, column=0, sticky="ew")
            name_var, note_var = tk.StringVar(), tk.StringVar()
            disposition_var = tk.StringVar(value="publish")
            fields = ttk.Frame(frame); fields.grid(row=fields_row, column=0, sticky="ew", pady=(8, 0)); fields.columnconfigure(1, weight=1)
            ttk.Label(fields, text="Bookmark name").grid(row=0, column=0, sticky="w"); ttk.Entry(fields, textvariable=name_var).grid(row=0, column=1, sticky="ew")
            ttk.Label(fields, text="Note").grid(row=1, column=0, sticky="w"); ttk.Entry(fields, textvariable=note_var).grid(row=1, column=1, sticky="ew")
            if "launch" in resolved.allowed_operations:
                disposition_frame = ttk.LabelFrame(fields, text="After the game exits")
                disposition_frame.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
                ttk.Radiobutton(disposition_frame, text="Publish the new save to remote main (default)",
                                variable=disposition_var, value="publish").grid(row=0, column=0, sticky="w")
                ttk.Radiobutton(disposition_frame, text="Keep this terminal's data local only (do not publish)",
                                variable=disposition_var, value="local-only").grid(row=1, column=0, sticky="w")
                ttk.Radiobutton(disposition_frame, text="Restore this launch's start-of-session save and do not publish",
                                variable=disposition_var, value="restore-startup").grid(row=2, column=0, sticky="w")
            selectable = [item for representative in catalog.candidates
                          for item in (representative, *(catalog.candidate(alias.candidate_id) for alias in representative.aliases))]
            by_id = {item.candidate_id: item for item in selectable}
            for item in selectable:
                diff = item.diff_from_live
                tree.insert("", "end", iid=item.candidate_id, values=(item.display_name, item.kind, _display_time(item.created_at),
                                                                      _display_time(item.committed_at), item.file_count,
                                                                      f"+{diff.added} / -{diff.removed} / ~{diff.changed}"))
            def selected() -> SaveCandidate | None:
                ids = tree.selection(); return by_id.get(ids[0]) if ids else None
            def update_detail(_event=None) -> None:
                item = selected()
                # The summary's save-creation line tracks the selection so the
                # three mandated times stay readable together at all times.
                summary.configure(text=_remote_check_summary_lines(catalog, item))
                if item is None: detail.configure(text="Select a version to see its comparison."); return
                detail.configure(text=_candidate_detail_text(item, catalog, resolved, selectable))
            tree.bind("<<TreeviewSelect>>", update_detail)
            initial = next((item.candidate_id for item in selectable if item.kind == resolved.initial_selection), None)
            if initial: tree.selection_set(initial); tree.focus(initial); update_detail()
            def choose(mode: SelectionMode) -> None:
                nonlocal result
                item = selected()
                if item is None: return
                risky = item.kind in {"history", "bookmark", "legacy", "session_start"} or mode == "promote-only"
                confirmed = False
                if not remote_ok:
                    unconfirmed_text = ("The remote version could not be freshly confirmed this session "
                                        f"(status: {catalog.remote_check.status}).\n"
                                        "The candidates shown may be out of date. Continue anyway?")
                    # Acknowledging staleness is a separate question from the
                    # risky-candidate second confirmation, so it deliberately
                    # does not set ``confirmed``: answering yes here must never
                    # authorize a history/bookmark restore on its own.
                    if not messagebox.askyesno("Remote check unconfirmed", unconfirmed_text, parent=root): return
                if risky:
                    text = ("1. The current live save may be replaced by the selected version.\n"
                            "2. The current remote main version will be preserved automatically when displaced.\n"
                            "3. Launch mode publishes the save produced after the game exits normally.\n"
                            "4. Make-latest-and-exit publishes a new commit with the selected contents.\n\nContinue?")
                    if not messagebox.askyesno("Confirm save selection", text, parent=root): return
                    confirmed = True
                disposition: ExitDisposition = disposition_var.get() if mode == "launch" else "publish"  # type: ignore[assignment]
                result = SelectionRequest(item.candidate_id, mode, name_var.get() or None, note_var.get() or None,
                                          confirmation_granted=confirmed, exit_disposition=disposition); close()
            def bookmark_only() -> None:
                nonlocal result
                item = selected()
                if item is None or not name_var.get(): return
                result = SelectionRequest(item.candidate_id, None, name_var.get(), note_var.get() or None, action="bookmark"); close()
            def reload_only() -> None:
                nonlocal result
                result = SelectionRequest(None, None, action="reload"); close()
            normal = ttk.Frame(frame); normal.grid(row=buttons_row, column=0, sticky="w", pady=(12, 0))
            launch_button = None
            if "launch" in resolved.allowed_operations:
                launch_button = ttk.Button(normal, text="Launch with this data", command=lambda: choose("launch"), underline=0); launch_button.grid(row=0, column=0, padx=4)
                root.bind("<Alt-l>", lambda _event: choose("launch"))
            if "bookmark" in resolved.allowed_operations:
                ttk.Button(normal, text="Save named bookmark", command=bookmark_only, underline=11).grid(row=0, column=1, padx=4)
            if "reload" in resolved.allowed_operations:
                ttk.Button(normal, text="Reload", command=reload_only, underline=0).grid(row=0, column=2, padx=4); root.bind("<F5>", lambda _event: reload_only())
            ttk.Button(normal, text="Cancel", command=close, underline=0).grid(row=0, column=3, padx=4); root.bind("<Escape>", lambda _event: close())
            if "promote" in resolved.allowed_operations:
                destructive = ttk.Frame(frame, padding=(0, 18, 0, 0)); destructive.grid(row=destructive_row, column=0, sticky="e")
                ttk.Button(destructive, text="Make latest and exit", command=lambda: choose("promote-only"), underline=0).grid()
            root.protocol("WM_DELETE_WINDOW", close)
            tree.focus_set()
            root.deiconify()
        worker = load_catalog_in_worker(root, build, populate, on_error)
        root.protocol("WM_DELETE_WINDOW", close); root.bind("<Escape>", lambda _event: close())
        root.mainloop()
        if callback_error is not None:
            if isinstance(callback_error, SyncError): raise callback_error
            raise SyncError("selection_ui_unavailable", "Save selection UI is unavailable; no selection was made.", EXIT_CONFLICT) from callback_error
        return result
    except SyncError:
        raise
    except Exception as error:
        raise SyncError("selection_ui_unavailable", "Save selection UI is unavailable; no selection was made.", EXIT_CONFLICT) from error
    finally:
        if worker is not None: worker.close()
        if root is not None:
            try: root.destroy()
            except Exception: pass


def present_exit_disposition_confirmation(current: str) -> str:
    """Offer a post-game, push-only-safe downgrade of the fixed exit disposition.

    Only ``publish`` has anything to downgrade from (see
    ``allowed_exit_disposition_transition``); every other current value is
    returned unchanged without showing a dialog, since there is no safe
    direction left to move.  Any Tk failure fails closed to ``current``
    unchanged, which the caller/workflow will simply apply as originally
    fixed before launch.
    """
    if current != "publish":
        return current
    try:
        import tkinter as tk
        from tkinter import ttk
    except Exception:
        return current
    choice = current
    try:
        root = tk.Tk(); root.withdraw(); root.title("Confirm save publication")
        frame = ttk.Frame(root, padding=12); frame.grid(sticky="nsew")
        ttk.Label(frame, justify="left", wraplength=420,
                  text=("The game has exited. This session's new save is ready to publish "
                        "to remote main.\n\nYou may still choose to keep it local-only, or "
                        "restore this launch's start-of-session save instead. Publishing "
                        "cannot be undone once you continue.")).grid(row=0, column=0, columnspan=3, sticky="w")

        def pick(value: str) -> None:
            nonlocal choice
            choice = value
            root.destroy()

        ttk.Button(frame, text="Publish", command=lambda: pick("publish")).grid(row=1, column=0, padx=4, pady=(12, 0))
        ttk.Button(frame, text="Keep local only", command=lambda: pick("local-only")).grid(row=1, column=1, padx=4, pady=(12, 0))
        ttk.Button(frame, text="Restore start-of-session save", command=lambda: pick("restore-startup")).grid(row=1, column=2, padx=4, pady=(12, 0))
        root.protocol("WM_DELETE_WINDOW", lambda: pick("publish"))
        root.deiconify()
        root.mainloop()
    except Exception:
        return current
    return choice if allowed_exit_disposition_transition(current, choice) else current


def present_tk(catalog: VersionCatalog, directive: SelectionDirective) -> SelectionRequest | CancelledSelection:
    return present_tk_from_builder(lambda: catalog, directive)


class TkSelectionPresenter:
    def present(self, catalog: VersionCatalog, directive: SelectionDirective) -> SelectionRequest | CancelledSelection:
        return present_tk(catalog, directive)
    def present_builder(self, build: Callable[[], VersionCatalog], directive: SelectionDirective | Callable[[VersionCatalog], SelectionDirective]) -> SelectionRequest | CancelledSelection:
        return present_tk_from_builder(build, directive)
