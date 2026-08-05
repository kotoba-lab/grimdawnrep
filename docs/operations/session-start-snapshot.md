# Session-start local snapshot

Every `launch` now creates one additional local archive automatically, right after the sync lock is acquired and before any restore, launch, or promotion touches the live save. This is separate from `preserve`, from the pre-restore `save-before-restore-...` archive, and from the post-game `archive_after_game` archive.

## What it does

- After lock acquisition and before the first live-save mutation of a launch, the exact current live save is copied to a new, verified local archive named `save-session-start-<root_hash prefix>-<uuid>` under the config directory's `archives/` folder.
- The archive's directory also contains a `.session-start.json` sidecar recording schema version, creation time, root hash, machine ID, the workflow session ID, and which candidate kind the launch was started from.
- The archive is published the same way `preserve` publishes: it is written to an incomplete-named staging directory first, verified, and only then atomically renamed to its final name. A crash or interruption during creation never leaves a half-written directory visible as a candidate.
- If creating this archive fails for any reason (disk full, permission error, a stale live save, etc.), the launch stops immediately. The live save has not been touched yet, so it is left exactly as it was, and the sync lock is released automatically — running `recover` is not required for this specific failure.
- The archive then appears as a normal candidate ("This launch's pre-session data") in `versions`/`launch`'s save selection, alongside `live`, `remote_head`, `history`, and `bookmark`. Selecting it requires the same second confirmation as history/bookmark, and restores it through the existing, verified restore path (never an arbitrary path).

## Disk usage

- **This archive is created on every launch and is never deleted automatically.** Disk usage under `archives/` grows by one save's worth of data per launch, indefinitely.
- `grim-dawn-sync status` reports the current session-start archive count and total bytes (`session_start_usage`) so this growth is visible, but nothing is pruned on the terminal's behalf. Deleting old `save-session-start-...` directories is a manual, operator-initiated action.

## Exit disposition now controls whether that session ever publishes

**As of the exit-disposition split (plan section 7), the end-of-session publish is no longer unconditional.** Every `launch` decides, before the game ever starts, what happens after it exits: `publish` (the historical, and still default, behavior), `local-only`, or `restore-startup`. See `docs/operations/exit-disposition.md` for the full description of all three and how to choose one.

In short: choosing `local-only` or `restore-startup` for a launch means that launch's session-start snapshot is now actually useful for "try a session and discard it" — the game's own new save is still archived locally (never silently lost), but it is never committed or pushed to remote main. `restore-startup` goes one step further and restores this launch's session-start archive back into the live save afterward, so the terminal ends the session looking exactly as it did before the launch began, aside from the archives it leaves behind.

Choosing the default `publish` disposition (or not specifying `--exit-disposition` at all) keeps the exact old behavior: `archive_after_game -> snapshot -> push -> release` runs unconditionally, same as before this feature existed.
