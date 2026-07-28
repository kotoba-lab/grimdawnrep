# M4: player.gdc read-only import

## Implemented slice

- Detects local saves and Steam Cloud saves for app `219990`.
- Decrypts supported GDC data versions 6, 7, and 8 in memory.
- Verifies the header, every top-level block checksum, parsed block lengths, and nested inventory/stash block checksums.
- Reads character metadata, base attributes, equipped item identity (base, affixes, transmute, component, augment), and character/devotion skills.
- Records SHA-256, size, and modification time and verifies the source has not changed after import.
- Maps the save to the input-independent Build schema. Character names are omitted by default in the Python model and can be redacted in CLI output.

## Safety and rejection behavior

The importer never opens a save for writing. Unsupported data versions, invalid magic, invalid lengths, truncated data, invalid booleans, and checksum mismatches are rejected. A changed source during the read is rejected as `input_changed_during_read`.

## Real-data verification

On 2026-07-13, the importer discovered Steam Cloud saves and successfully imported a current real save with file version 2 and data version 8. It resolved all 12 equipment slots and 96 character/devotion skill entries. SHA-256, byte length, and nanosecond modification time were unchanged before and after the import. Character name and path were not recorded in this document or fixtures.

With explicit user authorization, the same save was submitted to the official Grim Tools `upload_save.php` parser. `same-save-compare` independently normalized both results and matched 6/6 scalar fields, 12/12 locally represented equipment slots, and 96/96 ordered skill allocations with zero mismatches. The local save hash remained unchanged after the comparison. Personal paths, save bytes, response payloads, and character names remain outside the repository.

## Known boundary

`player.gdc` stores record identities and allocations, not the final temporary buff state. Static equipment and passive-skill defenses can be resolved against owned extracted DBRs, but the result remains an explicitly approximate combat snapshot until compared with the live character sheet. Same-save upload comparison proves decoder agreement for persisted identity and allocation fields; it does not convert scoped Grim Tools ID pairs into a universal mapping.

No personal save or game-derived binary is included in the repository.
# 1.3.0 save verification (2026-07-25)

The newest locally available `player.gdc` files were modified on 2026-07-24, so they are 1.3.0 candidates. The newest candidate accepted `data_version: 8` at the header, but failed while parsing its inventory payload with an unrecognized item structure. The importer now fails closed with `UnsupportedGdcVersion` and an explicit `possible 1.3.0+ save format change` diagnostic; it emits no partial Build model. A human-created, known-1.3.0 save is still required to characterize the block layout and verify the 12 equipment slots and skill count.

Block 3 parsing explicitly supports versions 4 through 11 and rejects every other version before field parsing with `unsupported_block_version:3:<version>`. Item layouts are versioned: v8 adds `ascendant_record`, `ascendant_rerolls`, and `seed_rerolls`; v11 adds `affix_rerolls`. The same version is passed to every bag and equipment item reader. One-byte record strings retain ordinary printable ASCII, while all non-printable, backslash, and non-ASCII bytes are emitted as lowercase `\\xNN` escapes. This makes the output deterministic and reversible while consuming the declared byte length exactly; length bounds and block checksums remain strict. The three weapon-set selector fields are preserved as raw bytes rather than coerced to booleans.

Within Block 3, the bag `unused` and equipment `attached` fields are likewise retained as raw bytes because their documented layout does not constrain them to `0` or `1`. Boolean validation remains strict for fields whose format is actually boolean, including the header and skill structures.

Block 4 explicitly supports versions 6 through 11 and rejects every other version before field parsing. For its stash tabs, versions 9 and later add four integer appearance indexes and a UTF-16 button name after the item array. Versions 6 through 8 retain the earlier tab layout. The fields are consumed only to preserve strict block boundaries and checksum validation; the existing public stash summary remains unchanged.

Block 8 explicitly supports versions 5 through 8. Version 8 inserts one raw `unknown_v8` byte after each character skill's `sublevel`; versions 5 through 7 retain the earlier layout. The following `active` and `transition` fields remain strictly validated booleans. Static author bytecode and anonymous structural validation identify the v6+ trailing integer as a bounded `sub_skill_count`, followed by that many `sub_skills` records (`name`, `autocast_skill`, `autocast_controller`, `parent_skill`); v5 has no such tail. Other Block 8 versions fail closed before layout parsing.
