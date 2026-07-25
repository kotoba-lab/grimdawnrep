# Roadmap verification status

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
| 4. Representative enemies, phases, sequences | Verified for the MVP set | 13 real phase records across eight localized enemy names produce 67 scenarios: 61 single hits, four shotgun envelopes, and two DBR-derived DA-debuff/follow-up windows. Hypothetical AI ordering and projectile contacts remain explicit unknowns. |
| 5. One measured example for four encounter classes | Blocked on observations | Deterministic fixtures cover single hit, shotgun, debuff/follow-up combo, and overlap. Observation comparison works, but four real gameplay observations have not been supplied or fabricated. |
| 6. Regression and explicit unsupported behavior | Verified | Regression tests cover import, normalization, combat, timeline, advisor, observations, revalidation, and release boundaries; unsupported effects, versions, records, state, AI ordering, and projectile contact count are explicit. |
| 7. Dataset coexistence, diff, and revalidation | Verified | Content-addressed directories, semantic diff, deterministic claim revalidation queue. |
| 8. No game/save/large Grim Tools replication | Verified | `release-audit` passes against the current tracked and untracked distribution candidates; generated/raw inputs are ignored. |

## External authorization / evidence needed for final closure

1. Four anonymized Observations from real play: one single hit, one multi-projectile/multi-hit event, one debuff then follow-up, and one overlapping source encounter. A timestamped video or manually transcribed HP deltas/status timing is sufficient.

Until those are available, the tool is a tested vertical-slice MVP, not a fully verified completion of every roadmap criterion.
