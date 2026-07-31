# Save Sync terminology

- **Sync destination latest**: the commit currently referenced by remote `main`.
  It is a synchronization label, not a claim about the player's intended game
  progress.
- **This device's current data**: the stable manifest scanned from the local
  live save directory.
- **Remote main snapshot**: a historical commit reachable from remote `main`.
- **Named bookmark**: a separately named, managed remote retention version.
  It is never overwritten automatically. The current live candidate can be
  bookmarked without publishing it as remote `main`: save-sync builds an
  isolated commit whose parent is the unchanged remote head, publishes only a
  UUID-named annotated tag, and verifies that tag's object, target, and save
  root. Such a bookmark remains restorable even though it is not in main's
  ancestor history.
  See [Preserve the current live save as a named bookmark](save-sync-live-bookmark.md)
  for the CLI, GUI, verification, and recovery procedure.
- **Selection**: choosing a candidate for launch or promotion.  Until the
  selection has been confirmed, the live save, remote main, terminal state, and
  session lock must remain unchanged.
- **Headless launch**: `--json launch` permits only an equal-content automatic
  launch. If live and remote differ it returns `selection_required`; automation
  must not choose a winner or retry with an arbitrary commit.
- **Catalog capability**: a short-lived binding between a candidate ID and the
  configuration, sync baseline, absent lock, live/remote state, and complete
  verified candidate set that produced it. It is valid only in its current
  five-minute UTC bucket. A consuming process rebuilds and double-checks that
  full context; any change rejects the token instead of rebinding it.
- **Risky explicit launch**: selecting a history, bookmark, or legacy candidate
  from JSON/CLI requires both `--select <candidate-id> --catalog-token <token>`
  and the separate `--confirm` flag. Omitting confirmation is fail-closed.
