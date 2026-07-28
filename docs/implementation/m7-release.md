# M7: Update and distribution operations

Dataset semantic diffs retain added, removed, and field-modified records and now create a deterministic revalidation queue. Changed combat fields enqueue single-hit, ranking, and sensitivity claims; timing/controller fields enqueue timeline and classification claims; skill/template/spawn fields enqueue reference closure.

Datasets remain content-addressed under their dataset ID, so old and new versions coexist and an ID collision with different content is rejected.

`release-audit` checks tracked paths and rejects raw/generated directories and game/save binary extensions. The hand-authored M0 pseudo-archive fixture directory is the only allowlist and each such file must remain at most 4 KiB. The current tracked tree passes the audit. No extracted DBR tree, dataset cache, real save, or Grim Tools page cache is tracked.
