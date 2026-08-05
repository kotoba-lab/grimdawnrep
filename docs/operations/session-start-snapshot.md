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

## Important limitation: this does not stop the end-of-session publish

**Keeping a session-start archive around does not, by itself, stop the existing unconditional publish behavior at the end of a game session.** Today, `launch` still always runs `archive_after_game -> snapshot -> push -> release` when the game exits normally, exactly as before this change. The three-way disposition split (`publish` / `local-only` / `restore-startup`) described for a later change is **not implemented yet**.

Concretely: if you launch, play, and exit normally, your session-start snapshot preserves what the live save looked like *before* that session, but the *new* save produced by that session is still committed and pushed to remote main when the game exits, the same as it always was. There is currently no way to "try a session and discard it" using only this feature — restoring the session-start archive on a later launch (or a future `restore-startup` disposition) has not yet been extended to suppress the intervening publish that already happened.

Do not treat this feature as a duplication/rollback tool on its own until the end-of-session disposition split described above ships.
