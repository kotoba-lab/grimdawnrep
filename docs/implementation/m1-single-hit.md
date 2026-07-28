# M1: 一撃計算の説明可能な縦切り

## 完了状態

M1の最初の説明可能な縦切りは完了した。fixture由来のBuildDefenseSnapshotとEnemySkillを使い、入力から最終HP差分までをCLIで再現できる。

## 実装範囲

`grim-dawn-lab single-hit --build BUILD.json --skill SKILL.json` は、直接ダメージpacketをShield、monster type別割合軽減、耐性、部位別装甲、monster type別固定軽減、割合吸収、固定値吸収の順で処理し、部位別の最小・平均・最大、Shield分岐、全体範囲、HP差分、警告をJSONで返す。

OA対DAは公式Combat GuideのPTH式と55%下限を使い、公式例の整数ロール境界に従って通常命中、ミス、1.1–1.5倍criticalへ分配する。

## 検証済み契約

- Armor 50、吸収率70%、物理100で被ダメージ65。
- Armor 124、吸収率70%、物理100で被ダメージ30。
- Armor 124、吸収率84%、物理100で被ダメージ16。
- PTH 97で通常命中89%、1.1倍critical 8%、ミス3%。
- 耐性低下はuncapped resistanceを減らしてから最大耐性で制限する。
- 防御順は耐性、装甲、割合吸収、固定値吸収。
- 未対応damage typeとDoTは近似せず `unsupported_effect` warningにする。
- Shieldの成功・失敗を独立した分岐として残し、block chanceで期待値を重み付けする。
- 各部位・Shield分岐・最小最大ごとに、全防御段階の中間値をtraceへ残す。
- OA/DA結果を最小・最大・一攻撃当たり期待値と最終HP差分へ反映する。

## 既知制約

DoT、fumble/dodge/deflect、block recoveryを含む攻撃列、複数hit、debuffの時間遷移はM3で扱う。PTH 100超のcritical帯は公式例の整数帯域を全帯域幅で正規化しており、実測照合まではモデル由来の計算として区別する。

## 検証コマンド

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
python -m grim_dawn_lab single-hit --build tests/fixtures/combat/build.json --skill tests/fixtures/combat/skill.json
```

公式根拠: [Grim Dawn Combat Guide](https://www.grimdawn.com/guide/gameplay/combat/)
