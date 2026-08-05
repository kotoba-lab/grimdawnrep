# Exit disposition: publish / local-only / restore-startup

Every `launch` now fixes, before the game ever starts, what happens after it exits. This is the "exit disposition" and it has exactly three values.

## The three values

| Disposition | What happens after the game exits |
|---|---|
| `publish` (default) | Unchanged from before this feature: `archive_after_game -> snapshot -> push -> release`. The new save is archived locally, committed, pushed to remote main, and the lock is released. |
| `local-only` | `archive_after_game` runs (the new save is archived locally), but nothing is committed or pushed. The lock is released without publishing. |
| `restore-startup` | `archive_after_game` runs first (the new save is archived locally, exactly like `local-only`), then live is restored back to this launch's own session-start snapshot (what live looked like right before this launch began). Nothing is committed or pushed. |

In every case, this launch's session-start snapshot (see `docs/operations/session-start-snapshot.md`) is still created right after the lock is acquired, before anything else touches live. `local-only` and `restore-startup` never change remote main, never change this terminal's recorded baseline (`last_applied_remote_commit` / `last_applied_manifest_root_hash`), and never touch any other terminal's data.

## Choosing a disposition

- **Interactive launch:** the save-selection window has three radio buttons ("Publish after exit" / "Keep this terminal's data local only" / "Restore this launch's start-of-session save") next to the Launch button. The choice is fixed for that launch as soon as you press Launch. After the game exits, and only if you chose `publish`, a second, small confirmation window lets you downgrade to `local-only` or `restore-startup` at the last moment (right before anything would be pushed). You can never go the other way: once you have chosen `local-only` or `restore-startup`, or once the save has already been pushed, there is no further prompt and no way back toward `publish`.
- **Headless / `--json`:** pass `--exit-disposition publish|local-only|restore-startup` to `grim-dawn-sync launch`. If you omit the flag, the disposition is `publish` — there is no implicit change and no config-level default for this per-run choice.

## Why the safety direction only goes one way

A push cannot be undone once remote main has moved. So the only downgrade the tool will ever perform automatically is *away* from publishing (`publish -> local-only` or `publish -> restore-startup`), and only before the push happens. It will never move a plan from `local-only` or `restore-startup` back toward `publish`, and it will never switch between `local-only` and `restore-startup` after the fact — there is no safety reason to prefer one over the other once the game has already exited under the other disposition.

## What this is for

- **`local-only`** is for a session you want to keep on this terminal without syncing it anywhere yet — for instance, an experimental play session, or when you are offline and do not want to be blocked by `offline_policy=deny` deciding for you.
- **`restore-startup`** is for a session you want to *try and discard*: play, see what happens, and have the terminal end up exactly where it started, with the played session preserved in a local archive (never silently lost) in case you change your mind later. This is the first release where a session-start snapshot restore actually suppresses the publish that would otherwise have already happened for that same session (see the "Important limitation" history in `docs/operations/session-start-snapshot.md`).

## Recovery

If a launch is interrupted after the lock was acquired but before the chosen disposition finished (a crash, a forced shutdown, etc.), `grim-dawn-sync recover` handles all three dispositions the same way it already handles `publish`: it inspects the exact terminal-local state and either finishes the interrupted operation or safely abandons it, without ever reinterpreting the disposition that was already fixed for that launch. `local-only`/`restore-startup` releases use a distinct recovery phase (`unpublished_release_pending`) from the ordinary bookmark and publish paths, but the guarantee is the same: the lock is never left silently orphaned, and remote main is never touched by a recovery of either disposition.

## Disk usage

Every disposition still creates this launch's session-start archive and still runs `archive_after_game` on exit. Neither is pruned automatically; see `docs/operations/session-start-snapshot.md` and `grim-dawn-sync status`'s `session_start_usage`/`archive_usage` fields for how to monitor growth.
