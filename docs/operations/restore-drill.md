# Historical restore drill (safe target only)

Use this procedure to prove that a historical Vault commit can be restored
without changing Grim Dawn's live save, Vault history, remote, sync state, or
lock. It does not start DPYes or Grim Dawn.

## Boundaries

- A drill requires both `--drill` and `--apply`. Without `--apply`, restore is
  inspection-only and never materializes a directory.
- The CLI creates a new, create-only directory below its own
  `%LOCALAPPDATA%\GrimDawnSaveSync\restore-drills` location. It rejects an
  unsafe/reparse-point parent and never accepts an arbitrary operator path.
- The command validates commit ancestry, committed blobs, manifest, and player
  data, then publishes only to the new target. It performs no push, lock/state
  update, live-save mutation, or process launch.
- JSON intentionally reports no local path. Keep the target path only in the
  operator's local notes.

## Copy-paste execution

Replace the placeholder. `COMMIT` must be visible in the local Vault branch
history.

```powershell
$python = Join-Path $env:LOCALAPPDATA 'GrimDawnSaveSyncTool\.venv\Scripts\python.exe'
$config = Join-Path $env:LOCALAPPDATA 'GrimDawnSaveSync\config.local.json'
$commit = '<COMMIT>'
& $python -m grim_dawn_sync --config $config --json restore --commit $commit --drill --apply
```

Success has `dry_run: false`, `materialized: true`, a safe `drill_id`, and
counts/root hash that match the inspected commit.

## Cleanup

Successful drill evidence is retained under the tool-owned `restore-drills`
directory; the CLI never performs automatic drill cleanup. Do not delete it as
part of this drill run.
