# Preserve the current live save

Use this command when the current terminal's live save must be protected before any sync or recovery work:

```powershell
grim-dawn-sync --json preserve
grim-dawn-sync --json preserve --apply
```

`preserve` is a local-only operation. It does not read or modify the vault, Git remote, sync lock, or terminal state. The default command only scans and validates the configured live save; it does not create directories or files.

With `--apply`, Grim Dawn and DPYes must both be stopped. The command copies the stable, validated save to an incomplete local staging directory under the config directory's `archives` folder, checks process status again, then atomically publishes a unique `save-preserved-...` archive. Only a published archive is reported as `verified: true` with an `archive_id`.

If copying, validation, or the final process check fails, the command fails without advertising an archive. Its clearly named incomplete staging directory is retained for inspection and must not be treated as a verified backup.
