# Terminal A setup failure sentinels

`-ApplySetup` emits one `TERMINAL_A_HANDOFF` JSON line. Failure codes contain no
paths, remote URLs, save names, hashes, command output, or credential material.
Use the code when requesting support.

Python lookup tries `py`, then `python`, then `python3`. It resolves the actual
interpreter, requires Python 3.11 or later, and uses that exact executable to
create the virtual environment.

The setup-specific codes are `python_launcher_not_found`,
`python_3_11_or_later_required`, `source_fetch_failed`, `venv_create_failed`,
`package_install_failed`, `vault_clone_failed`, `doctor_command_failed`,
`enroll_dry_run_failed`, and `enroll_apply_failed`. Other unexpected errors are
reported as `<stage>_failed`, with the stage limited to validation, Python
discovery, source-current checking, venv creation, package installation, vault
clone, config write, doctor, or enrollment.
