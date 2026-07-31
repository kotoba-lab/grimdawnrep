# Roadmap verification status

## Save Sync startup selection (2026-08-01)

Status: implementation complete and accepted against isolated automated fixtures;
real-terminal T7 rollout remains pending.

### Implemented and reviewed

- Added a bounded, verified version catalog for live, remote main, history,
  managed bookmarks, and legacy annotated tags, with coalesced selectable aliases.
- Added short-lived catalog capabilities bound to configuration, state, remote
  fetch/push identity, lock absence, live/remote roots, and the complete candidate
  projection. Stale or changed context fails before the first workflow mutation.
- Added explicit selection policy and canonical plans for equal, remote-ahead,
  live-ahead, diverged, and missing-baseline cases. History/bookmark/legacy launch
  requires a second confirmation.
- Added create-only managed bookmarks, including a detached live-save bookmark
  path protected by the remote session lock and recoverable publication intent.
- Integrated displaced-remote preservation, verified archive/restore, local Vault
  fast-forward alignment, promote-only publication, remote URL rechecks, and
  recovery handoff into the launch workflow.
- Added the Tk worker/main-thread adapter, privacy-safe `versions` output,
  explicit CLI selection/token/confirmation contracts, and create-only shortcut
  migration with read-only inspection of the previous `.lnk`.
- Updated terminology and Terminal A roundtrip/shortcut runbooks so differing
  saves are never silently approved by headless automation.

Independent review found no remaining High-severity issue. Follow-up review
findings were resolved and covered by focused tests. The review gate approves
the code for isolated acceptance and staged manual T7; it is not evidence that
two-terminal deployment has already occurred. A visible isolated Windows Tk
window was exercised automatically, but native messagebox appearance and real
terminal integration remain manual boundaries.

### Verification evidence

- Main T6 targeted acceptance checkpoint: `200 passed, 2 skipped`.
- Focused suite results recorded during final verification: selection `32 passed`;
  catalog capability `24 passed`; selector UI `11 passed`; CLI selection
  `14 passed`; workflow `50 passed, 1 skipped`; shortcut migration `8 passed`;
  selection contract `1 passed`; version catalog `7 passed`; bookmarks `4 passed`;
  CLI regression `42 passed`; Terminal A handoff docs `21 passed`.
- Previously failing remote-only and diverged Vault-base integration paths were
  repaired and individually re-run: remote-only `1 passed`, both diverged winners
  `2 passed`. Live-only bookmark and stale-before-mutation integration checks also
  passed individually.
- Final low-risk contract additions for coalesced alias privacy and managed-tag
  count/output bounds: `4 passed`.
- Visible isolated Windows Tk smoke exercised window close, Esc, F5 reload,
  bookmark, launch, and promote. Promote verified all four danger statements and
  returned `promote-only` with confirmation bound. The native messagebox OS chrome
  was replaced by a deterministic test answer, so its human-visible appearance is
  still unverified. The selector UI regression after this smoke was `12 passed`.
- Workspace-temporary Windows COM shortcut migration E2E: `1 passed`. A fake Git
  source/config/`python.exe` and fake Desktop proved old-shortcut read-only
  inspection, stale checkout detection, create-only Save Selection migration,
  exact target/arguments/working directory, unchanged source revision, old `.lnk`
  byte/timestamp invariance, and second-create rejection. The real Desktop and
  real shortcuts were not accessed.
- Explicit history/bookmark/legacy promotion end-to-end coverage: `4 passed`.
- Real Git recovery coverage for managed bookmark publication/release faults:
  `4 passed`.
- Interactive stale refresh, headless stale rejection, and explicit-token stale
  rejection were exercised fail-closed.
- Latest broad regression checkpoint across all 25 save-sync test files:
  `507 passed, 5 skipped in 701.14s`.
- These entries are overlapping targeted runs and must not be added together as
  a unique full-suite total. All five skips were existing Windows-environment
  limitations where symbolic links could not be created: the Git Vault hook,
  state parent-directory and destination-file checks, workflow ancestor check,
  and existing-shortcut check. The COM shortcut test passed; no skip was an
  accepted failure.

No real save, private Vault, or real remote main was changed. Git mutation in
integration tests was confined to disposable temporary repositories and fake
saves. No source commit or push was made for this implementation session.

### Observed real-terminal rollout boundary

Read-only inspection reported save sync `ready`, Vault relation `equal`, and game
process state `clear`. That does not authorize rollout. The following blockers
remain:

- It is not established whether the currently inspected machine is Terminal A or
  Terminal B for the staged rollout order.
- The source checkout is dirty, so an unattended pull/deploy step must not be
  inferred safe.
- The new Save Selection shortcut and the expected project virtual environment
  are absent.
- The previous Save Sync shortcut does not match the current source/arguments
  shape and must be treated as stale, not silently reused.

These were read-only observations. No shortcut, environment, real save, private
Vault, lock, state, or remote ref was changed.

### Remaining T7 manual checklist

1. On real Windows, perform the remaining human UI check: native messagebox
   appearance, keyboard traversal/focus, readable four-statement warning, and
   responsiveness during catalog loading. Automated visible Tk already covers
   close, Esc, reload, bookmark, launch, promote, and confirmation binding.
2. On Terminal B, COM-inspect the existing shortcut without editing it, run the
   create-only migration, and verify exact TargetPath, Arguments, and
   WorkingDirectory. Recheck the old and legacy shortcut fingerprints unchanged.
3. With explicit authorization and a verified pre-operation managed bookmark,
   test equal-content launch, remote-ahead selection, cancellation, and reload on
   Terminal B. Confirm no unintended save or remote-main mutation.
4. Create a named bookmark and pass an isolated restore inspection; then promote
   an explicitly chosen past version and confirm the displaced head remains
   retrievable.
5. Repeat the shortcut inspection and staged rollout on Terminal A, then perform
   the documented A → B → A roundtrip with user-visible save evidence.
6. Finish only after both terminals report `ready`, `vault_relation=equal`, no
   active lock, no recovery phase, and no unexpected Vault worktree difference.
   Record the actual manual evidence without logging paths, remote URLs, hashes,
   character names, or raw save data.

## M6 item query (2026-07-27)

- T0–T4完了: `records/items/` datasetからEN/JA item v1/v2、affix v1、JSONL照会CLIを生成し、未知Class・未解決参照はfail-closedで出力する。
- 最終検証: 30 pytest、CLI smoke、`release-audit`、`git diff --check`を通過。ゲーム由来raw/generatedとsaveはgit配布候補外。

## 1.3.0 constant verification (2026-07-25)

- Verified from `base/records/game/combatformulas.dbr`: DA coefficients `level*12`, `physique*0.5`, and `+53`; PTH minimum `55`; PTH thresholds/modifiers; and armor-region weights (head 15, shoulders 15, torso 26, arms 12, legs 20, feet 12).
- Verified from `base/records/game/gameengine.dbr`: `armorDefensiveAbsorption=70`.
- DBR-unverified: resistance cap 80. The current selected dataset does not contain a safe authoritative cap field, so the existing model constant remains a documented fallback rather than a DBR-derived value.
- Spirit-to-Health: 1.3.0 patch notes report 12, but the `characterAttributeEquations` records traversed by `dataset.py` are enemy-bio references, not player Spirit equations. The extracted DBR selection therefore cannot directly verify the value; no repository calculation depends on the prior value 8.
- Human verification required: E01 armor oracle, E02 PTH boundaries, and E03 Sunder behavior. No hard-coded constant was changed to a dataset lookup because the selected dataset does not safely cover all player-runtime sources.

Updated: 2026-07-25

| Definition of done | Status | Evidence / remaining boundary |
| --- | --- | --- |
| 1. Regenerate from a clean owned install | Verified | `doctor` and `dataset-extract`; real Base/GDX1/GDX2/GDX3/EN/JA inputs; input hashes unchanged. |
| 2. Ordered expansion integration | Verified | Deterministic Base → GDX1 → GDX2 → GDX3 overlay. |
| 3. Real `player.gdc` and shared URL into one Build model | Partially verified | GrimTools 1.3.0.0 import is verified; a 1.3 candidate GDC reports data_version 8 then explicitly fails closed for unknown inventory structure. |
| 4. Representative enemies, phases, sequences | Partially verified for 1.3.0 | Final 1.3 dataset produces 11 scenarios from 13 attacks for one selected enemy. The prior 67 scenarios across eight names are an older representative-set measurement, not a 1.3 revalidation. Hypothetical AI ordering and projectile contacts remain explicit unknowns. |
| 5. One measured example for four encounter classes | Blocked on observations | Deterministic fixtures cover single hit, shotgun, debuff/follow-up combo, and overlap. Observation comparison works, but four real gameplay observations have not been supplied or fabricated. |
| 6. Regression and explicit unsupported behavior | Verified | Regression tests cover import, normalization, combat, timeline, advisor, observations, revalidation, and release boundaries; unsupported effects, versions, records, state, AI ordering, and projectile contact count are explicit. |
| 7. Dataset coexistence, diff, and revalidation | Verified | Content-addressed directories, semantic diff, deterministic claim revalidation queue. |
| 8. No game/save/large Grim Tools replication | Verified | `release-audit` passes against the current tracked and untracked distribution candidates; generated/raw inputs are ignored. |

## External authorization / evidence needed for final closure

1. Four anonymized Observations from real play: one single hit, one multi-projectile/multi-hit event, one debuff then follow-up, and one overlapping source encounter. A timestamped video or manually transcribed HP deltas/status timing is sufficient.

Until those are available, the tool is a tested vertical-slice MVP, not a fully verified completion of every roadmap criterion.
