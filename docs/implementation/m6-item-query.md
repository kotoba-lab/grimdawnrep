# M6: 全アイテム抽出・二言語item view・照会CLI（実装プラン）

状態: 完了（2026-07-27、T0–T4/T6完了。T5は明示的に範囲外）

## 目的

grimtools/db を目視で階層検索する代わりに、「カオス%が付くレベル90以上のLegendary武器を一覧して」のような照会に、ローカル抽出データから一発で答えられるようにする。人間向けブラウザUIは作らない。照会の主体はCLIとLLMエージェントである。

最終的には既存のBuild import（M4/M5）と組み合わせ、「このビルドに不足する耐性を埋める装備・affix候補」の提案（M7以降）へ接続する。

## 事前決定事項（workerは再検討せず従うこと）

これらは計画時に決定済みである。チケット実行中に代替案を検討しないこと。

1. **二言語方針**: 内部キー・statフィールド名・スロット名・レア度はすべて英語（DBR原名）を正とする。表示名は `name: {"en": ..., "ja": ...}` の形で両言語を常に併記する。検索・フィルタは英語キーで行い、名前のテキスト検索のみEN/JA両方に対して部分一致させる。理由: エージェントの検索とネット照合は英語が有利であり、ユーザーの読解は日本語が必要なため。
2. **選択方式**: 参照閉包のrootを増やすのではなく、`--select-prefix` による前方一致一括選択を新設する。閉包追跡（references）は既存挙動のまま適用する。
3. **rule ID**: item viewは `item-view-v1`、affix viewは `affix-view-v1` とする。ルールを変えたら数字を上げ、旧出力を上書きしない。
4. **fail-closed**: 未知の `Class` 値、未解決タグ、未解決参照は黙って捨てず、必ず `excluded` / `unresolved` として出力に残す。既存リポジトリ方針と同じ。
5. **配布境界**: `data/raw/` と `data/generated/` は今後もgitignore対象。テストfixtureはゲームファイルのコピーではなく、既存 `tests/fixtures` と同様に手書きの合成DBR/ARCのみを使う。`release-audit` が通ることを各チケットのDoDに含める。
6. **出力形式**: viewはJSON Lines（1行=1アイテム）とする。エージェントがgrepしやすく、部分読み込みできるため。

## worker運用ルール（軽量モデル向け）

- 1チケットずつ実行する。チケットのscope外のリファクタリングをしない。
- 各チケットは「実装 → 自動テスト → 実データ検証 → 本文書の該当チケットに結果追記」で完了する。
- テスト実行: リポジトリ直下で `$env:PYTHONPATH = "src"; python -m pytest tests` （PowerShell）。
- 実データ検証コマンドは各チケットに記載のものをそのまま使う。インストールパスは `C:/Program Files (x86)/Steam/steamapps/common/Grim Dawn`。
- 判断に迷ったら、推測で実装せず、チケット末尾に「未解決事項」として記録して停止する。

## チケット

### T0: `--select-prefix` と全アイテム閉包の抽出

**scope**: `src/grim_dawn_lab/dataset.py` と `src/grim_dawn_lab/cli.py`。

- `build_dataset_from_dbr_roots` の呼び出し前に、全layerのファイル一覧から指定prefix（例 `records/items/`）に前方一致するrecord idを列挙して `selected_records` へ加える関数 `enumerate_records_by_prefix(roots, prefix) -> list[str]` を追加する。大文字小文字は既存の正規化（小文字化・`/`区切り）に合わせる。
- `dataset-build` と `dataset-extract` に `--select-prefix`（`action="append"`）を追加する。`--select` との併用可。`--select` の `required=True` は「`--select` と `--select-prefix` の少なくとも一方が必須」に緩和する。
- manifestに選択条件（prefix一覧）を記録し、dataset IDの決定性を保つ。

**DoD**:
- 合成fixture（2層、同一record idの上書きを含む）でprefix選択のテストが通る。
- 実データで下記コマンドが完走し、dataset IDが出力される。2回目の実行で同一IDが再生成される。
- `release-audit` が通る。

**実データ検証**:

```powershell
$env:PYTHONPATH = "src"
python -m grim_dawn_lab dataset-extract `
  --install-path "C:/Program Files (x86)/Steam/steamapps/common/Grim Dawn" `
  --channel stable `
  --select-prefix records/items/
```

**注意**: 閉包が数万recordsになり初回抽出は10分超になり得る。時間は記録するだけでよく、この段階で最適化しない。

**結果（2026-07-27）**:

- 実装: `enumerate_records_by_prefix(roots, prefix)` を追加し、全layerの `.dbr` を正規化済み（小文字、`/`）record idとして重複除去・ソートして列挙するようにした。`dataset-build` / `dataset-extract` に繰り返し指定可能な `--select-prefix` を追加し、`--select` と併用可能にした。両方未指定時だけエラーにし、manifest相当のdataset coreへ `selected_prefixes` を保存してdataset IDへ反映する。
- 基盤修復: 実ARC v3（record table / TOC / LZ4 file part）を読む `arc.py` を追加し、F: の `Text_EN.arc` で11,047タグ（例 `tagWeaponAxeB003=Pit Master's Axe`）を実読取確認した。既存CLIのtop-level importは、存在しない非T0モジュールがdatasetコマンドまで妨げないようcommand別lazy importへ変更した。`release.py` はgitの追跡・未追跡候補を対象に `data/raw/`、`data/generated/`、captures、save拡張子をfail-closedで検出する実装を追加した。
- 合成fixture: 実ARC v3構造の手書きfixtureと、2層・同一record idの上書き・prefixの大文字/`\\` 指定を含む `test_select_prefix_enumerates_layers_and_preserves_override_provenance` を追加した。`--select` / `--select-prefix` の少なくとも一方が必須となるCLIテスト、同じselected recordsでもprefix条件の有無でdataset IDが変わるテストも追加した。
- 実データ検証: `F:/SteamLibrary/steamapps/common/Grim Dawn` で指定の `dataset-extract --select-prefix records/items/` を2回完走。両回とも dataset ID `cb799fd1c0bbfc738abf53c0af17a7606b19826331f6b9a8a32a3c3d708109fa` を再生成した（records 42,685、selected records 25,864、prefix `records/items/`）。1回目でbase/gdx1/gdx2/gdx3の抽出を生成し、2回目は4 layerの抽出キャッシュを再利用した。
- 全tests / release-audit: `$env:PYTHONPATH = "src"; python -m pytest tests` は25 passed。`python -m grim_dawn_lab release-audit` は `safe: true`、violation 0。`git diff --check` も通過。
- 未解決事項: T0のDoDに関する未解決事項なし。T1以降は未着手。

### T1: item-view-v1（本体statの正規化view）

**scope**: 新規 `src/grim_dawn_lab/item_view.py` と `cli.py` のサブコマンド `items-view`。

手順:

1. まず実datasetから `Class` フィールドの distinct 値一覧を出す（使い捨てスクリプトでよい。結果をこの文書に追記する）。
2. その一覧をもとに `Class` → スロット（英語: `weapon_1h`, `weapon_2h`, `ranged_1h`, `ranged_2h`, `shield`, `offhand`, `head`, `chest`, `shoulders`, `hands`, `legs`, `feet`, `waist`, `amulet`, `ring`, `medal`, `relic` など）の明示的なマッピング表を実装する。表にない `Class` のrecordは変換せず、`excluded`（record idとClass値）として出力する。
3. 各アイテムrecordを次の形の1行JSONへ正規化する。

```json
{
  "record": "records/items/gearweapons/axe1h/b003c_axe.dbr",
  "rule": "item-view-v1",
  "slot": "weapon_1h",
  "classification": "Rare",
  "item_level": 52,
  "level_requirement": 52,
  "name": {"en": "...", "ja": "...", "tags": ["tagWeaponAxeB003"]},
  "stats": {"offensiveChaosModifier": 24.0},
  "references": {"item_skill": null, "augment_skills": [], "modified_skills": [], "item_set": null},
  "provenance": {"source_layer": "gdx3", "overrides": ["base"]}
}
```

- `stats` は数値化できるフィールドのうち非ゼロのもののみ。フィールド名はDBR原名のまま変えない。
- 名前は `itemNameTag` に加え、存在すれば `itemQualityTag` / `itemStyleTag` も解決し、解決済み部品を空白結合した表示名をEN/JA各々で作る。未解決タグは `null` にせず、`name.unresolved_tags` として残す。
- 付与スキル等の参照はこの段階では**解決しない**。record idを `references` に転記するだけ（解決はT3）。

**結果（2026-07-27）**:

- Class調査: 実dataset全体のdistinct `Class` を取得した。item配下で装備として明示的に対応した値は `WeaponMelee_Axe/Dagger/Mace/Scepter/Sword`、各`2h`、`WeaponHunting_Ranged1h/Ranged2h`、`WeaponArmor_Shield/Offhand`、`ArmorProtective_*`、`ArmorJewelry_*`、`ItemArtifact/ItemRelic/ItemEnchantment/ItemUsableSkill/ItemNote/QuestItem` とリセット・faction consumable群である。loottable、formula、proxy、`None` 等の残りのClassは推測せずunknown_classとして除外した。
- Class調査（全値）: `records/items/` の25,864 recordsについて、欠損を`<missing>`として集計したdistinct Class全65値は次の通り。mapping対象は上記実装の`CLASS_TO_SLOT`にある値のみであり、表のそれ以外（及び`<missing>`）はすべて`unknown_class`としてexcludedへ出力する。

| Class | 件数 |
| --- | ---: |
| `<missing>` | 1271 |
| `AreaOfInterest` | 1 |
| `ArmorJewelry_Amulet` | 426 |
| `ArmorJewelry_Medal` | 419 |
| `ArmorJewelry_Ring` | 395 |
| `ArmorProtective_Chest` | 670 |
| `ArmorProtective_Feet` | 176 |
| `ArmorProtective_Hands` | 211 |
| `ArmorProtective_Head` | 737 |
| `ArmorProtective_Legs` | 259 |
| `ArmorProtective_Shoulders` | 695 |
| `ArmorProtective_Waist` | 309 |
| `Decoration` | 1 |
| `Destructible` | 177 |
| `FixedItemBlastContainer` | 2 |
| `FixedItemContainer` | 595 |
| `ItemArtifact` | 91 |
| `ItemArtifactFormula` | 924 |
| `ItemAscensionFormula` | 8 |
| `ItemAttributeReset` | 1 |
| `ItemDevotionReset` | 1 |
| `ItemDifficultyUnlock` | 4 |
| `ItemEnchantment` | 384 |
| `ItemFactionBooster` | 30 |
| `ItemFactionWarrant` | 12 |
| `ItemNote` | 313 |
| `ItemRandomSetFormula` | 2 |
| `ItemRelic` | 107 |
| `ItemRerollFormula` | 3 |
| `ItemSetFormula` | 1 |
| `ItemTransmuter` | 58 |
| `ItemTransmuterSet` | 24 |
| `ItemUsableSkill` | 17 |
| `LevelTable` | 1118 |
| `LootItemTable_DynWeight` | 2532 |
| `LootMasterTable` | 913 |
| `LootRandomizer` | 7984 |
| `LootRandomizerTable` | 1081 |
| `OneShot_Food` | 3 |
| `OneShot_Gold` | 1 |
| `OneShot_PotionHealth` | 4 |
| `OneShot_PotionMana` | 1 |
| `OneShot_Sack` | 1 |
| `OneShot_Scroll` | 47 |
| `OneShot_SkillUnlock` | 32 |
| `Prop` | 1 |
| `Proxy` | 143 |
| `ProxyAccessoryPool` | 110 |
| `QuestItem` | 150 |
| `SetPiece` | 54 |
| `SetPiecePart` | 20 |
| `SetPiecePool` | 52 |
| `WeaponArmor_Offhand` | 516 |
| `WeaponArmor_Shield` | 355 |
| `WeaponHunting_Ranged1h` | 239 |
| `WeaponHunting_Ranged2h` | 309 |
| `WeaponMelee_Axe` | 256 |
| `WeaponMelee_Axe2h` | 113 |
| `WeaponMelee_Dagger` | 309 |
| `WeaponMelee_Mace` | 280 |
| `WeaponMelee_Mace2h` | 129 |
| `WeaponMelee_Scepter` | 272 |
| `WeaponMelee_Spear2h` | 114 |
| `WeaponMelee_Sword` | 269 |
| `WeaponMelee_Sword2h` | 132 |
- 実装: `item_view.py` に固定Class→slot mappingと `item-view-v1` JSONL生成を追加し、`items-view --dataset <dataset.json>` を追加した。名前はquality/style/name tagを順にEN/JAで空白結合し、未解決タグは `name.unresolved_tags` に保持する。数値化可能な非ゼロfieldは原名のまま`stats`に、skill/set参照は未解決のままrecord idとして転記する。未知Classは `items-v1.excluded.jsonl` にrecord/Class/reasonを出力する。
- 合成fixture: 2層上書き、unknown Class、未解決tag、EN/JA名前合成、非ゼロstat、参照転記を検証するテストを追加した。
- 実データ: `data/generated/views/cb799fd1c0bbfc738abf53c0af17a7606b19826331f6b9a8a32a3c3d708109fa/items-v1.jsonl` を生成。行数8,700、excluded 17,164。既知Legendary武器 `records/items/awakened/gearweapons/axe1h/c011_axe.dbr` はEN `Notched Bone of a Thousand Deaths`、JA `ノッチト ボーン オブ ア サウザンド デス` で、実ゲームtag資材と目視一致した。
- 検証: `$env:PYTHONPATH = "src"; python -m pytest tests` は26 passed、`release-audit` はsafe。未解決事項なし。T2以降は未着手。

**DoD**:
- 合成fixtureで、上書き来歴・未知Class・未解決タグの3ケースを含むテストが通る。
- 実datasetから `data/generated/views/<dataset-id>/items-v1.jsonl` が生成され、行数・excluded件数がこの文書に記録される。
- 既知アイテム1件（任意のLegendary武器）についてEN/JA名が実ゲーム表記と一致することを目視確認し、record idと両言語名をこの文書に記録する。

### T2: `items-query`（二言語照会CLI）

**scope**: 新規 `src/grim_dawn_lab/item_query.py` と `cli.py` のサブコマンド `items-query`。

引数:

- `--view <path>`: T1のJSONLパス（必須）
- `--slot`, `--classification`: 完全一致、繰り返し可（OR）
- `--min-level`, `--max-level`: `level_requirement` に適用
- `--stat FIELD` / `--stat "FIELD>=N"`: 繰り返し可（AND）。演算子なしは「非ゼロで存在」
- `--name TEXT`: EN/JA両方の表示名への部分一致（大文字小文字無視）
- `--format table|json`（既定 `table`）、`--limit N`（既定50）
- 並び順: 最初に指定した `--stat` の値の降順。stat指定がなければ `level_requirement` 降順。

table出力の列: record / name.en / name.ja / slot / classification / levelReq / 指定statの値。

**DoD**:
- フィルタ組み合わせ・並び順・limit・日本語部分一致のテストが通る（合成viewで）。
- 実データで次のコマンドが動き、出力サンプル（先頭5行）をこの文書に記録する。

```powershell
$env:PYTHONPATH = "src"
python -m grim_dawn_lab items-query --view <T1の出力> `
  --stat "offensiveChaosModifier>=1" --classification Legendary --min-level 90
```

**結果（2026-07-27）**:

- 実装: `item_query.py` と `items-query` を追加。slot/classificationは各々OR、各filter種はAND、level範囲・repeatable stat条件・EN/JAのcase-insensitive部分一致・table/json・limitを実装した。欠損slot/classification/name/stats/levelは安全に非一致または空列として扱う。指定statがあれば最初のstat値降順、なければlevel requirement降順にした。
- 合成view: filter組合せ、stat値順/level順、limit、日本語部分一致、欠損fieldを含むテストを追加した。
- 実データ照会: 指定コマンド（viewはT1出力）での先頭5行は次の通り。`offensiveChaosModifier` 降順である。

| record | name.en | name.ja | slot | classification | levelReq | offensiveChaosModifier |
| --- | --- | --- | --- | --- | ---: | ---: |
| `records/items/gearweapons/melee2h/d206_axe2h.dbr` | Mythical Wrath of Tenebris | 神話級 テネブリスの憤怒 | weapon_2h | Legendary | 94 | 233 |
| `records/items/gearweapons/caster/d314_scepter.dbr` | Beronath's Voidlost Horn | ベロナスの虚無に失われし角 | weapon_1h | Legendary | 94 | 220 |
| `records/items/faction/weapons/melee2h/f304a_axe2h.dbr` | Umbra'dol, Legacy of Blood | 《血の遺産》アンブラ・ドル | weapon_2h | Legendary | 94 | 210 |
| `records/items/gearweapons/swords1h/d104_sword.dbr` | Mythical Pagar's Betrayal | 神話級 パガールの裏切り | weapon_1h | Legendary | 94 | 208 |
| `records/items/gearweapons/caster/d210_scepter.dbr` | Judgment of the Three | 三神の審判 | weapon_1h | Legendary | 94 | 205 |
- 検証: `$env:PYTHONPATH = "src"; python -m pytest tests` は28 passed、`release-audit` はsafe。未解決事項なし。T3以降は未着手。

### T3: 付与スキル・modifier・セット参照の解決

**scope**: `item_view.py` の拡張（rule IDを `item-view-v2` に上げる）。

- `references` の各record（itemSkillName、augmentSkillName系、modifiedSkillName系、itemSetName）をdataset内で解決し、参照先の非ゼロstatフィールドを `granted` セクションへ要約する。skill名タグもEN/JAで解決する。
- 変換系フィールド（conversion In/Out/Percentage）を `granted.conversions` として明示する。
- dataset内に実体がない参照は `unresolved` に残す（既存 `missing_references` と同じ思想）。
- セットは `lootsets` のrecordからセット名（EN/JA）と段階ボーナスのstatを要約し、所属アイテム側には `item_set` のrecord idとセット名のみ持たせる（ボーナス本体はセット側の行として出力する）。

**DoD**: 合成fixture（付与スキルあり・変換あり・未解決参照あり・セット2段ボーナス）のテストが通り、実データの既知アイテム1件で付与スキル名EN/JAが実ゲーム表記と一致することを記録する。

**結果（2026-07-27）**:

- 実装: `item-view-v2` をv1とは別の `items-v2.jsonl` / `items-v2.excluded.jsonl` / `item-sets-v2.jsonl` として生成可能にした。item/augment/modified/modifier skill参照をdataset内で解決して非ゼロstatsとEN/JA skill名を `granted.references` に、suffixなし・番号付きのconversion In/Out/Percentageをsource record/indexつきで `granted.conversions` に出力する。未解決recordはkind/recordを `granted.unresolved` に残す。セットはitem側にrecord+nameだけを置き、set record idごとに一意な独立行へ、scalar metadataを除外した数値配列のstage bonus statを出力する。
- 合成fixture: 付与skill、suffix 2のconversion、未解決augment参照、同一setを参照する2 itemでもset行1件、itemLevel除外、setの2段bonus内容を個別に検証するテストを追加した。
- 実データ: v2 item行8,700、excluded17,164、未解決参照0、独立set行199（record idも199件で全行一意）を生成。既知item `records/items/awakened/gearweapons/axe1h/c011_axe.dbr` の付与skill `records/skills/itemskillsgdx3/legendary/addnotch.dbr` はEN `Add a Notch`、JA `刻み目の追加` で、実ゲームtag資材と目視一致した。
- 検証: pytest 29 passed、release-audit safe、diff check通過。未解決事項なし。T4以降は未着手。

### T4: affix-view-v1（接頭辞・接尾辞）

**scope**: 新規または `item_view.py` 内の別rule。出力は `affixes-v1.jsonl`。

手順:

1. まず実datasetで `records/items/lootaffixes/` 配下の構造（stat付きrecordとテーブルrecordの区別）を調査し、結果をこの文書に追記してから正規化ルールを書く。
2. stat付きaffix recordを、名前EN/JA・非ゼロstat・levelRequirement付きで1行JSONへ正規化する。
3. どのスロット/アイテム種に付き得るかはテーブルrecordから機械的に確定できる範囲のみ `applicable` として出力し、確定できない場合は `applicable: "unknown"` とする。推測で埋めない。

**DoD**: 合成fixtureテスト、実データでの生成行数記録、既知affix1件（例: 耐性系接尾辞）のEN/JA名一致確認。`items-query` に `--view` としてaffix viewを渡した場合も動作すること（スロット等の欠損フィールドは空扱い）。

**調査結果（2026-07-27、実装着手前）**:

- `records/items/lootaffixes/` は9,760 records。Classは`LootRandomizer` 7,984件、`LootRandomizerTable` 1,081件、Class欠損695件。`LootRandomizer`は`lootRandomizerName`、stat、levelRequirementを持つaffix本体であり、`LootRandomizerTable`が`randomizerName*`で本体を参照するtableである。Class欠損recordにはcompletion/ascended等の未知構造が混在する。
- このdatasetのtable参照だけから個々のstat recordの適用slotを確定できないものは、推測せず`applicable: "unknown"`として扱う。

**結果（2026-07-27）**:

- `affix-view-v1` / `affixes-view` を追加。`LootRandomizer`本体を名前EN/JA・未解決tag・非ゼロ数値stat・level/provenanceとして出力し、`LootRandomizerTable`は逆参照用tableとしてrowにせず、Class欠損/未知構造はexcludedに残す。tableから明示fieldでslot/item種を確定できない適用先は`unknown`とした。
- 合成fixtureでstat affix、未解決tag、nonzero stat、unknown適用先、`items-query`へのaffix row入力を検証した。
- 実生成: `affixes-v1.jsonl` 7,984行、excluded 695行。known resistance affix `records/items/lootaffixes/prefix/ad017b_res_chaos_05.dbr` は`defensiveChaos=54`、level 92、EN `Ordered`、JA `オーダード`で実tag資材と一致した。
- `items-query --view affixes-v1.jsonl --stat defensiveChaosModifier` は欠損slot等を安全に扱い、0件を正常出力した。pytest 30 passed、release-audit safe、diff check通過。T5以降は未着手。

### T5: 耐性ギャップ提案（M7スコープ、ここでは着手しない）

Build import（M4/M5）の耐性値とT1–T4のviewを突き合わせ、不足耐性を埋める候補を出す。本プランでは範囲外。T4完了後に別途プランを起こす。

### T6: 文書更新と監査

- 本文書へ各チケットの結果を追記済みであることを確認する。
- `README.md` の「現在地」、`docs/roadmap.md`、`docs/implementation/verification-status.md` へM6の到達点を1〜3行で追記する。
- `release-audit` を最終実行し、追跡対象にゲーム由来データが含まれないことを確認する。

**結果（2026-07-27）**: T0–T4のdataset ID、出力件数、test数、未解決事項を再照合し、本書・README・roadmap・verification statusを更新した。T5は未着手かつ別プラン対象。最終smoke（items-view v1/v2、items-query item/affix、affixes-view）、pytest、release-audit、diff checkを通過し、gitの追跡/未追跡候補に`data/raw/`、`data/generated/`、save拡張子は無い。fixtureは手書き合成DBR/ARCのみ。

## 既知のリスク・複雑さ

- **閉包の肥大**: `records/items/` からの参照はloottable・スキル・FXへ広がる。抽出時間とdatasetサイズは記録し、問題になった場合のみ「参照追跡の深さ/種類の制限」を別チケットとして起こす。先回りで制限しない。
- **表示名の合成規則**: 実ゲームの表示名は複数タグの合成であり、順序・空白規則が言語で異なる可能性がある。EN/JA各1件の目視照合をDoDに入れているのはこのため。完全一致しない場合は差異を記録し、タグ部品を保持したまま先へ進む。
- **affixの適用可能性**: テーブル構造が複雑な場合、`applicable` の網羅は諦めて `unknown` を許す。耐性パズル用途では「statで検索できること」が最優先である。
- **1.3系の未知フィールド**: Fangs of Asterkarn由来の新フィールドが出たら、無視せず `stats` にそのまま含める（数値なら）か `excluded` に記録する。
