# 実証実験バックログ

静的DBを読めても、実ゲームでの命中数、適用順、同時刻処理までは確定しません。以下を優先順に検証します。

## E01: 公式Armor例の再現

目的: 被ダメージ計算器の最小オラクルを作る。

- 入力: 物理100、Armor 50、吸収70%
- 期待: 65 damage
- 入力: 物理100、Armor 124、吸収70%
- 期待: 30 damage
- 入力: 物理100、Armor 124、吸収84%
- 期待: 16 damage

根拠: 公式 Combat Guide。これはコード実装後の必須回帰試験にします。

## E02: OA/DA境界

目的: DA低下前後で、命中率とクリティカル域がどう変わるかを確認する。

- 公式PTH式の境界値テストを作る。
- 敵OA、プレイヤーDA、DA debuffを別入力にする。
- 平均値だけでなく最大クリティカル倍率を報告する。

## E03: Sunderと後続デバフ

目的: 2025年のコミュニティ報告が、安定版 `1.2.1.6` とPublic Test `1.3.0` のそれぞれで再現するか確認する。

- 同一Sunderの連続適用
- Sunder後の耐性低下
- Sunder後のDA低下
- 単発Sunder攻撃自身と、その後続ヒットの差
- 複数敵からのSunder／デバフ

合格条件: バージョン、難易度、敵、スキル、初期値、各ヒット後の表示値または固定ダメージ結果を記録する。意図の断定は別途開発者情報を要します。

## E04: 多段と「ワンパン」分類

目的: HPが一瞬で消えた事象を、一つのイベントか複数イベントか分ける。

- 60fps以上の録画
- 被弾前後のHP
- 敵数と攻撃モーション
- デバフアイコンの出現フレーム
- 投射物／設置物の数
- 0.25秒、0.5秒、1秒窓でのイベント集約

ゲーム内に十分な戦闘ログがない場合、映像だけで断定できないケースを `unknown` のまま残します。

## E05: Block Recoveryと多段

目的: 一発をBlockできることと、多段全体を防げることを区別する。

- block chance / amount / recoveryを固定
- 既知の多段攻撃を距離別に受ける
- 何発目がBlockされたかを複数試行で記録

## E06: SR環境差

目的: 同一敵・同一ビルドでも、単体戦とSRで危険度が変わる要因を分解する。

- Shardと難易度
- 全Mutator
- 同時出現敵とaggro時刻
- 敵レベル
- Shattered Soul buffなどプレイヤー側の一時効果
- 単体時との被ダメージ差

## 観測記録の最低要件

## 1.3.0.0 / Fangs of Asterkarn backlog

Source: Crate Entertainment, [v1.3.0.0 patch notes](https://forums.crateentertainment.com/t/grim-dawn-version-v1-3-0-0/155979). These are research items, not implementation commitments.

### E07: Berserker identity boundary (high)

- Question: Can GrimTools external mastery IDs, `player.gdc`, and the versioned dataset identify Berserker without guessing?
- Impact: `grimtools.py`, `gdc.py`, dataset mastery records.
- Evidence: a 1.3.0.0 Berserker URL and a human-authorized save, with explicit local record mapping.

### E08: Ascendant difficulty model (high)

- Question: Does Ascendant require a new encounter channel/difficulty enum or invalidate the current normal/elite/ultimate array index?
- Impact: `schemas/encounter.schema.json`, `dataset.py` difficulty indexing.
- Evidence: extracted Ascendant balancing records plus a controlled in-game encounter.

### E09: Awakening and new affix states (medium)

- Question: How do Awakening, Ascendant third affixes, and affix rerolls alter the item model?
- Impact: item normalization and Build equipment provenance.
- Evidence: anonymized item records before/after each operation.

### E10: New encounter candidates (medium)

- Question: What records/phases represent The Dread, Tempest Totems, and Marked by the Void?
- Impact: encounter candidate selection and coverage reports.
- Evidence: DBR record closure and at least one observation per encounter.

### E11: Changed runtime semantics (medium)

- Question: Does converted DoT non-stacking and pet Energy removal affect any current model?
- Impact: combat/timeline unsupported-effect boundaries.
- Evidence: 1.3.0.0 controlled tests; retain `unknown` until then.

各記録は次を必須とします。

- 一意なexperiment ID
- ゲーム版とchannel
- ゲームファイルhashまたはビルド識別情報
- 難易度、敵レベル、SR条件
- BuildDefenseSnapshot
- 操作手順
- 生データ／動画へのローカル参照
- 観測結果
- 予測と差分
- 結論の証拠等級と未確定事項
