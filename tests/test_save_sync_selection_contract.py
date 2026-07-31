"""T0 contract fixture: every reconciliation state has one safe policy."""
from __future__ import annotations

import json
from pathlib import Path


def test_selection_decision_table_is_complete_and_unambiguous() -> None:
    rows = json.loads((Path(__file__).parent / "fixtures" / "save_sync_selection_decisions.json").read_text(encoding="utf-8"))
    assert {row["case"] for row in rows} == {"equal", "remote_ahead", "live_ahead", "diverged", "baseline_missing"}
    for row in rows:
        assert isinstance(row["show_selector"], bool)
        assert row["initial_selection"] in {None, "live", "remote_head"}
        assert row["allowed_operations"]
        if row["case"] == "diverged":
            assert row["initial_selection"] is None
        if row["case"] == "baseline_missing":
            assert row["allowed_operations"] == ["cancel"]
