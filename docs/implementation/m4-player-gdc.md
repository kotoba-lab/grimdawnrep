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
