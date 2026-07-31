# Preserve the current live save as a named bookmark

Use this procedure to retain the currently verified live save without changing
remote `main`. The operation publishes only a UUID-named managed annotated tag.
It does not launch the game or replace the live save.

## CLI procedure

Run the commands from the installed save-sync environment with the game and
DPYes stopped. Keep the catalog token and candidate ID as opaque values; do not
construct or edit either value.

1. Obtain a fresh verified catalog:

   ```powershell
   $versions = & $python -m grim_dawn_sync --config $config --json versions |
       ConvertFrom-Json -ErrorAction Stop
   $live = @($versions.candidates | Where-Object kind -eq 'live')
   if ($live.Count -ne 1) { throw 'live_candidate_unavailable' }
   $token = [string]$versions.catalog_token
   $candidate = [string]$live[0].candidate_id
   ```

2. Validate the exact request without publishing:

   ```powershell
   & $python -m grim_dawn_sync --config $config --json bookmark `
       --candidate $candidate --catalog-token $token `
       --name 'Before experiment' --note 'Optional plain-text note'
   if ($LASTEXITCODE -ne 0) { throw 'bookmark_dry_run_failed' }
   ```

3. Apply once with the same unmodified token and candidate:

   ```powershell
   & $python -m grim_dawn_sync --config $config --json bookmark `
       --candidate $candidate --catalog-token $token `
       --name 'Before experiment' --note 'Optional plain-text note' --apply
   if ($LASTEXITCODE -ne 0) { throw 'bookmark_apply_failed' }
   ```

The token is short-lived and bound to the complete configuration, remote
identity, baseline, lock absence, live data, remote head, candidates, aliases,
and their diffs. If it expires or any input changes, rerun `versions` and the
dry run. Never retry `--apply` with a stale token or a copied candidate from an
older catalog.

## GUI procedure

Open the normal save-selection window from the installed shortcut or
non-JSON `launch`/`promote` command. Select **This device's current data**, use
the **bookmark** action, enter a display name and optional note, then confirm.
Closing the window, pressing Escape, or cancelling publishes nothing.

## Success invariants

After success:

- remote `main` is exactly the same commit as before;
- live save bytes and their verified root hash are unchanged;
- Vault HEAD, branch, index, and worktree are unchanged;
- terminal baseline state is restored, the session lock is absent, and no
  recovery phase remains;
- one create-only `grim-dawn-save-<UUID>` annotated tag exists remotely;
- its remote tag object, peeled commit, and committed manifest root were all
  verified, and the bookmark can be selected or restored later.

The normal JSON result deliberately omits commit IDs, root hashes, paths,
remote URLs, character names, notes, and session IDs. Do not add those values
to tickets, chat, screenshots, or ordinary audit logs.

## Interrupted publication and recovery

If the command exits with recovery required, do not delete local tags, the
remote lock, or `state.json`, and do not rerun bookmark `--apply`. Stop the game
and DPYes, verify the configured checkout and remote, then run:

```powershell
& $python -m grim_dawn_sync --config $config --json recover
```

Recovery accepts only the same machine, remote identity, exact on-disk state,
remote main, lock object/session, bookmark ref, annotated-tag object, peeled
commit, and save root.

- `bookmark_publish_pending`: the local tag exists and the publication intent
  was saved before push. Recovery either confirms/replays that exact tag and
  returns `bookmark_released`, confirms it after an already-finished lock
  deletion and returns `bookmark_complete`, or proves it was never published,
  removes only the exact local generated tag, and returns
  `bookmark_not_published`.
- `bookmark_release_pending`: remote publication was confirmed and only lock
  release or local session-tag cleanup remains. Recovery returns
  `bookmark_released` or `bookmark_complete`.

Any identity, lock, state, tag-object, target, main, or root mismatch fails
closed. Correct the external cause and rerun `recover`; do not manually force,
move, or recreate refs.
