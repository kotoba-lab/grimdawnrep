# Grim Dawn Encounter Lab

Grim Dawn のビルドを「キャラクターシート上の強さ」だけでなく、敵の攻撃、デバフ、行動条件、戦闘環境まで含めて評価するための調査・解析リポジトリです。

## 目標

最終的には、GrimTools のビルドまたは `player.gdc` を入力すると、次のような問いに根拠付きで答えるツールを目指します。

- このビルドは、どの敵のどの攻撃に弱いか。
- 単発、連続ヒット、デバフ後の追撃のどれが致死要因か。
- 耐性超過、DA、装甲、装甲吸収率、CC耐性などのどこを直すと効果が大きいか。
- キャンペーン、Shattered Realm、単体スーパーボス戦で評価が変わる理由は何か。
- 計算結果のうち、ゲームデータ由来、公式仕様、コミュニティ実測、未検証仮説はどれか。

## 現在地

初期リサーチとM0–M3の縦切りを完了し、M4の読み取り専用 `player.gdc` パーサーまで実装しています。現時点の結論は次の通りです。

1. 問題設定は妥当です。GrimTools にはビルド計算機と敵DBがありますが、特定ビルドへ敵の攻撃列を適用する encounter-aware な解析層はありません。
2. 敵行動のすべてがブラックボックスというわけではありません。ゲームDBにはスキル、初回使用遅延、再使用遅延、使用確率、距離条件、連鎖行動などが含まれます。
3. 一方で、ランダム装備、位置関係、複数ヒット、複数ボス、SR Mutator、エンジン挙動、バージョン固有バグは静的DBだけでは確定できません。実働テストが必要です。
4. 「ワンパン」は一つの分類ではありません。真の単発、同フレーム近傍の多段、耐性低下やSunder後の追撃、床ダメージとの重なりを分けて扱います。
5. データは GrimTools の画面スクレイピングへ依存せず、ユーザーが所有するゲームの ARZ をローカル解析して生成する方針が有力です。

2026-07-13に、この端末のSteam版インストールからBase Game、GDX1、GDX2、英語・日本語localizationを抽出し、Moosilaukeの全フェーズを版付きdatasetへ正規化しました。既存のSteam Cloudセーブも、入力不変を確認しながら装備・スキルを共通Buildモデルへ読み込めています。

詳しくは [初期調査](docs/research/initial-findings.md)、[戦闘データ完全収集の境界](docs/research/coverage-boundary.md)、[データ戦略](docs/architecture/data-strategy.md)、[実証実験バックログ](docs/research/experiment-backlog.md) を参照してください。

## リポジトリ構成

```text
docs/
  architecture/    データ取得・更新設計
  domain/          戦闘モデルと用語
  research/        調査結果、反証、未確定事項
  roadmap.md       段階的な実装計画
data/
  research/        出典と主張の機械可読台帳
schemas/           将来の抽出器・解析器が出力するJSON Schema
```

`data/raw/` と `data/generated/` はローカル生成物用で、ゲーム由来データをそのままリポジトリへ配布しない方針です。

## 重要な前提

- 数値と挙動は必ずゲームバージョン、難易度、敵レベル、コンテンツ条件に紐付けます。
- 現在の安定版は `1.3.0.0`（2026-07-23配信、Fangs of Asterkarn同時リリース）です。
- 公式ガイドも将来拡張の内容を含む場合があるため、公式ページであってもバージョン無指定の仕様は検証対象です。
- フォーラム投稿は、開発者発言、再現手順付き実測、一般的説明、体感談を同列に扱いません。

## 実装済みCLI

```powershell
$env:PYTHONPATH = "src"
python -m grim_dawn_lab doctor --install-path "C:\Program Files (x86)\Steam\steamapps\common\Grim Dawn"
python -m grim_dawn_lab single-hit --build tests/fixtures/combat/build.json --skill tests/fixtures/combat/skill.json
python -m grim_dawn_lab dataset-extract --install-path "C:\Program Files (x86)\Steam\steamapps\common\Grim Dawn" --select "records/creatures/enemies/nemesis/nemesis_undead_02a.dbr"
python -m grim_dawn_lab sequence --build tests/fixtures/combat/build.json --attacks tests/fixtures/combat/attacks.json
python -m grim_dawn_lab save-import --path "C:\path\to\player.gdc" --redact-name
python -m grim_dawn_lab grimtools-import https://www.grimtools.com/calc/DV9G4mQN
python -m grim_dawn_lab same-save-compare --save "C:\path\to\player.gdc" --grimtools-response upload-response.json
python -m grim_dawn_lab advise --build build.json --dataset dataset.json --context context.json --format markdown
python -m grim_dawn_lab release-audit
```

`doctor` はM0のゲーム入力manifestを生成する。`single-hit` はM1の説明可能な一撃計算、`dataset-extract` はM2のARZ/ARC抽出、`sequence` はM3の攻撃列・状態遷移、`save-import` はM4の読み取り専用セーブ取込、`grimtools-import` はM5の共有URL取込、`same-save-compare` は明示許可済みの公式解析応答との同一セーブ照合、`advise` はM6のランキングと感度分析、`release-audit` はM7の配布境界監査である。未対応効果や未解決値は近似せず明示する。

## 次の実装単位

M0–M7の縦切りはfixtureで回帰試験され、所有ゲーム・実在セーブ・公開共有URLによる統合スモークテストまで進んでいます。セーブ由来の最終防御値はローカルDBRから装備と常時Passiveを解決しますが、属性丸め、seed変動、一時バフは未確定として残します。

残る検証境界と各マイルストーンの完了条件、標準実装ループは [実装ロードマップ](docs/roadmap.md) を参照してください。
