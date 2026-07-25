# M5: Grim Tools shared URL adapter

## 1.3.0.0 live verification (2026-07-25)

`https://www.grimtools.com/calc/2mgMmMlZ` imported successfully. The title/buildInfo JSON shape expected by the importer was retained, yielding game version `1.3.0.0`, level 100, 16 equipment slots, and a populated skill list. The payload reports mastery IDs `04` and `10` with display class `Reaver`. A public post described it as Nightblade/Berserker, but local external-ID-to-DBR mapping is intentionally not implemented; Berserker is therefore explicit `unknown`, not inferred.

The adapter validates exact `grimtools.com/calc/<8-character-id>` URLs, canonicalizes HTTPS/www, limits responses to 2 MB, applies a timeout, and caches one HTML response per shared ID. Network, HTTP, malformed page, and schema failures are explicit errors.

Shared pages expose a server-rendered `window['buildInfo']` JSON object. The adapter reads that self-described payload (not DOM state or minified application JavaScript) and maps level, attributes, masteries, equipment, affixes, components, augments, skills, and autocast links into the common Build shape. A saved HTML fixture keeps contract tests network-independent.

Grim Tools IDs (`it…`, `pre…`, `suf…`, `sk…`) are not local DBR record paths. Until a versioned ID mapping is available, this is reported as `grimtools_external_ids_require_local_record_mapping`. Field-level Build diff remains usable and explains this identity mismatch rather than claiming false equivalence.

Live verification on 2026-07-13 imported an existing shared build for game version 1.2.1.6 with 14 equipped slots, 79 skills, and two masteries.

The official save-upload response has a separate offline normalizer. It never performs an upload itself: the caller supplies previously saved JSON from `upload_save.php`, and `same-save-compare` checks it against a read-only local GDC import. A user-authorized real comparison matched all 6 scalar fields, all 12 locally represented equipment slots, and all 96 ordered skills. The resulting local-record/external-ID pairs are evidence scoped to that exact save, not a claimed global mapping.
