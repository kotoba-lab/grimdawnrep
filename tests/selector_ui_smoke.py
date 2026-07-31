"""Manual Windows smoke driver for the Tk selector; it changes no Vault data.

Run with ``PYTHONPATH=src python tests/selector_ui_smoke.py``.  The catalog is
in-memory but is delivered through the real worker/main-thread entry.  Verify
Tab/tree arrows, Alt+L, F5, Esc/window-close cancellation, the separated
promote button, bookmark-only action, and all four confirmation statements.
"""
import argparse

from grim_dawn_sync.selection import SelectionDirective
from grim_dawn_sync.selector_ui import present_tk_from_builder
from grim_dawn_sync.version_catalog import ManifestDiff, SaveCandidate, VersionCatalog


_CONFIRMATION_LINES = (
    "1. The current live save may be replaced by the selected version.",
    "2. The current remote main version will be preserved automatically when displaced.",
    "3. Launch mode publishes the save produced after the game exits normally.",
    "4. Make-latest-and-exit publishes a new commit with the selected contents.",
)


def _install_auto_action(action: str) -> None:
    """Drive only this in-memory smoke window through its real Tk widgets."""
    import tkinter as tk
    from tkinter import messagebox, ttk

    original_tk = tk.Tk

    class AutoTk(original_tk):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.after(50, self._attempt_action)

        def _widgets(self):
            pending = list(self.winfo_children())
            while pending:
                widget = pending.pop(0)
                yield widget
                pending.extend(widget.winfo_children())

        def _attempt_action(self) -> None:
            buttons = {
                str(widget.cget("text")): widget
                for widget in self._widgets()
                if isinstance(widget, ttk.Button)
            }
            if "Cancel" not in buttons:
                self.after(50, self._attempt_action)
                return
            if action == "escape":
                self.event_generate("<Escape>", when="tail")
            elif action == "bookmark":
                entries = [widget for widget in self._widgets() if isinstance(widget, ttk.Entry)]
                if len(entries) != 2 or "Save named bookmark" not in buttons:
                    raise AssertionError("bookmark widgets missing")
                entries[0].insert(0, "SmokeBookmark")
                entries[1].insert(0, "in-memory only")
                buttons["Save named bookmark"].invoke()
            elif action == "promote":
                if "Make latest and exit" not in buttons:
                    raise AssertionError("promote widget missing")
                buttons["Make latest and exit"].invoke()
            else:
                raise AssertionError("unknown smoke action")

    def confirm_yes(title: str, text: str, **kwargs) -> bool:
        if title != "Confirm save selection" or not all(line in text for line in _CONFIRMATION_LINES):
            raise AssertionError("confirmation contract changed")
        if kwargs.get("parent") is None:
            raise AssertionError("confirmation has no parent")
        print("SELECTOR_UI_SMOKE_CONFIRM answer=yes statements=4", flush=True)
        return True

    tk.Tk = AutoTk
    if action == "promote":
        messagebox.askyesno = confirm_yes


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--auto-action", choices=("escape", "bookmark", "promote"))
    args = parser.parse_args(argv)
    if args.auto_action:
        _install_auto_action(args.auto_action)
    live = SaveCandidate("1" * 32, "live", "This device's current data", "2026-08-01T00:00:00Z", "smoke", "1" * 64, None, 1, 2, 3, ("SmokeHero",), ManifestDiff(0, 0, 0))
    history = SaveCandidate("2" * 32, "history", "Remote main snapshot", "2026-07-31T00:00:00Z", "smoke", "2" * 64, "a" * 40, 1, 2, 3, ("SmokeHero",), ManifestDiff(1, 0, 1))
    catalog = VersionCatalog("f" * 32, "a" * 40, live.root_hash, (live, history))
    result = present_tk_from_builder(lambda: catalog, SelectionDirective(True, "live", ("launch", "promote", "bookmark", "reload", "cancel"), True))
    print(f"SELECTOR_UI_SMOKE_SUCCESS action={args.auto_action or 'manual'} result={result!r}")


if __name__ == "__main__": main()
