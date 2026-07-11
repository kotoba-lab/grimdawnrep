# Roadmap

## Phase 0: 調査基盤（現在）

- [x] 問題設定の裏付けと反証
- [x] 証拠レベルと版管理方針
- [x] データ取得戦略
- [x] 防御・遭遇スキーマの初版
- [ ] 安定版 `1.2.1.6` の実ARZで抽出を検証
- [ ] Public Test `1.3.0` を別datasetとして比較

完了条件: 一つの主張を出典、ゲーム版、検証状態まで追跡できる。

## Phase 1: 一撃計算の縦切り

- 手入力BuildDefenseSnapshot
- 手入力EnemySkill
- OA/DA、耐性、部位別装甲、吸収の計算
- 最小／平均／最大と部位別結果
- 中間値を表示する説明トレース
- 公式Combat Guideの例を回帰テスト化

完了条件: 公式のArmor例を再現し、1敵1攻撃に対する被ダメージ内訳を説明できる。

## Phase 2: ローカルゲームDB抽出

- ARZ読取PoC
- Base / GDX1 / GDX2 / 将来拡張の上書き順
- localization tag解決
- Monster -> Skill -> Controller参照の追跡
- レベル・難易度式の解決
- パッチ差分レポート

完了条件: 選定したNemesis一体の全フェーズと攻撃候補を正規化JSONへ出せる。

## Phase 3: 攻撃列と実測

- attack timeout / delay / chance / range / chain
- 多段・projectile・ground effect表現
- 動画フレームを用いた観測フォーマット
- 静的予測対実測の誤差レポート
- Sunder、RR、DA低下の版別試験

完了条件: 「真の単発」か「短時間の複合被弾」かを一つの遭遇で分類できる。

## Phase 4: Build import

- `player.gdc` 読み取り専用import
- 装備、スキル、Devotionから防御スナップショット生成
- 一時バフのON/OFFシナリオ
- GrimTools URL連携の可否調査

完了条件: 実キャラクターから、平常時と主要バフ時の防御状態を再現できる。

## Phase 5: Encounter advisor

- Nemesis以上を検索・比較
- 「このビルドが弱い攻撃」ランキング
- 改修候補の感度分析
- SRの敵組合せとMutatorシナリオ
- 根拠、信頼度、未確定項目のUI

完了条件: 弱点と対策を、数値の中間過程と証拠レベル付きで提示できる。

## やらないこと（当面）

- GrimToolsを置き換えるフル装備／スキルエディタ
- 全敵を最初から手作業で攻略記事化
- 出典やゲーム版のないTier表
- 実測なしの単一「生存スコア」
- ゲームファイルやセーブの無断再配布
