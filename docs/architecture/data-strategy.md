# データ取得・更新戦略

## 方針

主要データ源は、ユーザーが所有する Grim Dawn のローカルゲームファイルです。GrimTools は照合・人間向け参照に使いますが、非公開APIやDOM構造へ本体機能を依存させません。

```text
Game install
  database.arz / GDX*.arz / localization ARC
              |
              v
      local extractor/importer
              |
              v
 versioned normalized JSON  <--- manual evidence/observations
              |
              +----> encounter simulator
              +----> search/index
              +----> provenance and confidence report
```

## なぜローカル抽出か

- 公式 Modding Guide は、Database Records がモンスター、装備、スキルを定義すると説明しています。
- ARZを読み取るMITライセンスのGoライブラリが公開されています。
- GrimTools Monster Database 自身もゲーム資源から自動生成されると説明されています。
- パッチ追随時に「どのDBRフィールドが変わったか」を差分化できます。
- ゲーム由来データを本リポジトリへ再配布せずに済みます。

候補ライブラリ: [codeberg.org/alex-ilchukov/arz/v2](https://pkg.go.dev/codeberg.org/alex-ilchukov/arz/v2)（MIT）

## 配布境界

リポジトリへ含めるもの:

- 抽出・正規化コード
- JSON Schema
- フィールドマッピング
- テスト用の小さな手製fixture
- 出典URL、主張、検証手順、期待値
- ユーザー自身の観測データを匿名化・明示許諾したもの

デフォルトで含めないもの:

- `database.arz`、`GDX*.arz`、ARC
- 大量の抽出済みDBRまたはゲーム文言
- `player.gdc` や共有スタッシュ
- GrimToolsから収集した大量のページ／データ複製

この境界の法的妥当性は正式な法務判断ではありません。公開前に Crate Entertainment の利用条件とファンコンテンツ方針を確認します。

## データ層

### 1. Raw record layer

ARZレコードを可能な限り損失なく表現します。原フィールド名、値、由来アーカイブ、レコードパス、ゲーム版ハッシュを保持します。

### 2. Normalized domain layer

解析に必要な概念へ変換します。

- Monster / phase / classification / race
- Monster scaling formula and resolved level
- Skill / controller / autocast / chain
- Damage packet / DoT / weapon damage
- Debuff / Sunder / CC / dispel
- AI timing / chance / range condition
- Difficulty / SR scaling / Mutator

原値を捨てず、変換規則のIDを保持します。

### 3. Observation layer

録画、フレーム時刻、被ダメージ前後のHP、デバフアイコン、敵数、距離などの実測です。静的DBと矛盾しても上書きせず、両方を残します。

### 4. Claim layer

「Sunderは後続デバフを増幅する」のような人間が読む主張です。ゲーム版、証拠、状態を持ちます。

状態の例:

- `confirmed`: 対象版で十分な証拠がある
- `provisional`: 再現したが範囲・原因が未確定
- `disputed`: 証拠が衝突している
- `stale`: 旧版では確認したが対象版で未再検証
- `rejected`: 反証された

## Build入力

優先順位は次の通りです。

1. 手入力の防御スナップショットJSON: 最小MVPを先に検証できる。
2. `player.gdc` 読み取り専用インポート: 実キャラクターを正確に扱う。
3. GrimTools共有URL: 利便性は高いが、安定した公式APIが確認できるまで補助機能。
4. 独自フルビルドエディタ: 当面作らない。GrimToolsと重複するため。

`player.gdc` は既存の公開パーサがある一方、版更新で壊れた報告もあります。必ず読み取り専用、ファイルハッシュ記録、fixture回帰試験で扱います。

参考: [Save File Decryption Tools](https://forums.crateentertainment.com/t/tool-save-file-decryption-tools/110765)

## 更新検知

各データセットは次を持ちます。

- `game_version`
- `channel`: `stable` / `public_test`
- 各ARZのSHA-256
- 抽出器バージョン
- 正規化ルールバージョン
- 生成日時

新パッチでは全件を無言で置換せず、次の差分を出します。

- 追加・削除された敵／フェーズ／スキル
- damage、RR、DA reduction、Sunder、CCの変更
- timing、chance、range、chainの変更
- SR scaling / Mutatorの変更
- 既存claimの再検証が必要になった範囲

## 現時点の制約

2026-07-11の作業環境では Steam は存在しますが、App ID `219990` の Grim Dawn 本体はインストールされていません。そのため、ARZパーサの実データ検証と敵レコードのフィールドマッピングは未実施です。
