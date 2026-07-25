# M2: DBR抽出と版付きdataset

## 1.3.0.0 migration verification (2026-07-25)

Four-layer extraction produced manifest-backed dataset `03b4dc2b5f596072dcde1d6cd422e69915f3a79a64466661809064be31131917` (29,367 records). Local diffs and queues: `plan130-diff-1b4876.json` (15,908 queue entries) and `plan130-diff-aea4d9.json` (13,461). Moosilauke `nemesis_undead_02a/b` HP fields are unchanged (for example `defensiveLife=50` and `characterLife=0` in both datasets), so the reported boss-health change is a scaling record/runtime effect outside this select closure. Armor and absorption fields are likewise absent from this closure and from the current normalized view; this is documented as coverage, not an extraction omission. Broader balancing-record selection is required before those patch-note values can be asserted from a diff.

## 現在地

M2のRaw DBR縦切り、ARC v3 localization、敵skill正規化view、一コマンド再生成を実装した。公式同梱 `ArchiveTool.exe -database` が読み取り専用で生成するDBRを入力とし、次を行う。

- CSV形式のフィールドを文字列のまま保持し、重複キーを配列として失わない。
- Base → GDX1 → GDX2の指定順で同一record idを上書きする。
- 各正規化recordにsource layer、上書き元、原record id、normalization rule IDを残す。
- 選択recordから `records/**/*.dbr` 参照を追跡する。セミコロン区切り参照も個別に扱う。
- canonical JSONのSHA-256をdataset IDとし、ID別ディレクトリへ保存する。
- 同じ入力から同じIDを再生成し、異なるIDの旧datasetを上書きしない。
- ARC v3のpart/file tableとraw LZ4 blockを読み、英語・日本語tagを解決する。
- monsterの全skill、skill level式、level別damage配列、controller、難易度補正recordを正規化viewへ変換する。
- dataset間でrecord追加・削除とfield追加・削除・変更を区別する。
- `dataset-extract` で診断、入力ハッシュ、ARZ抽出、dataset生成、前後入力同一性確認を一巡する。
- `data/raw/` と `data/generated/` は `.gitignore` で配布対象外にする。

## 実データ検証

The representative root set was expanded on 2026-07-13 from the two Moosilauke records to 13 Nemesis phase records. The normalized view rule is now `monster-skill-view-v2`: unnamed phases receive a stable `phase:<record-stem>` label, direct aether/chaos packets are retained, direct `offensivePoison*` is typed as acid, and defensive-ability reduction plus duration are emitted under each skill's `applies` evidence.

2026-07-13の所有ゲームからBase 33,306、GDX1 18,454、GDX2 16,350 DBRを読み取り専用抽出した。Moosilauke両phaseの次のrecordをrootに参照閉包を生成した。

```text
records/creatures/enemies/nemesis/nemesis_undead_02a.dbr
records/creatures/enemies/nemesis/nemesis_undead_02b.dbr
```

修正前のセミコロン参照誤認を実データで検出し、修正した。統合datasetではMoosilauke閉包とgameengine補助recordを含む20,014 recordsをdataset化し、3,032件以上の拡張上書きを来歴付きで処理した。導入済み全層に実体のない参照は `missing_references` に残し、黙って補完しない。

両phaseについて、英語名 `Moosilauke, the Chillwind`、日本語名 `冷風のムージラウク`、全skill候補、level 100でのskill level、damage packet、Base/拡張の原recordとfieldをJSONへ出力した。初回の全抽出は約8分、入力SHA-256キャッシュ後の再生成は約44秒だった。

## コマンド

```powershell
$env:PYTHONPATH = "src"
python -m grim_dawn_lab dataset-extract `
  --install-path "C:/Program Files (x86)/Steam/steamapps/common/Grim Dawn" `
  --channel stable `
  --enemy-level 100 --difficulty ultimate --player-count 1 `
  --select records/creatures/enemies/nemesis/nemesis_undead_02a.dbr `
  --select records/creatures/enemies/nemesis/nemesis_undead_02b.dbr
```

## 既知制約

- 難易度pakの配列順は `difficulty major × player count minor` として解決している。最終OA/DAにはengine由来のlevel/attribute寄与が残るため、現在のattributesは式成分と補正値のtraceとして扱い、実測または表示値照合前に最終値と断定しない。
- 二つの未解決component skill参照は導入済みBase/GDX1/GDX2に実体がない。将来拡張層検出の対象である。
- 全item索引はRaw閉包に含まれるが、用途別item viewはM4のBuild import前に拡張する。
