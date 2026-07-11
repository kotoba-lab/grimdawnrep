# 戦闘・危険度モデル v0

## 出力したい答え

単一の「強さスコア」ではなく、次を返します。

- `incoming_damage_range`: 装甲部位、乱数、クリティカルを含む範囲
- `lethal_probability_bounds`: 仮定を明示した致死確率の上下限
- `burst_windows`: 0.25秒、0.5秒、1秒、3秒などの累積被ダメージ
- `failure_modes`: 単発、多段、デバフ連鎖、CC、回復停止など
- `countermeasures`: 何をどれだけ変えれば閾値を越えるか
- `confidence`: データと挙動の確からしさ
- `unknowns`: 結論を変えうる未観測項目

## 入力状態

### BuildDefenseSnapshot

- HP、DA
- 現在耐性、最大耐性、超過分
- 物理耐性
- 部位別ArmorとArmor Absorption
- Shield block chance / amount / recovery
- Dodge / Deflect
- 種族別ダメージ減少
- % / flat damage absorption
- CC耐性
- 回復、再生、ADCTH、回路遮断スキル
- 一時バフと稼働条件
- Evade / movement skillのクールダウン

### EncounterState

- ゲーム版、難易度、敵レベル
- 敵とフェーズ、ランダム装備
- 敵スキル、使用条件、タイミング、命中形状
- プレイヤーへ現在付与されているデバフ
- SR Shard、補正、Mutator
- 同時に戦う敵
- 距離、位置、経過時間

## 一撃の処理

公式ガイドの順序を基準に、概念的には次の処理を行います。

```text
raw skill/weapon packet
  -> avoidance eligibility and roll
  -> OA/DA hit and critical outcome
  -> shield block state
  -> percent reduced damage from monster type
  -> resistance after debuffs and overcap
  -> location-based armor for physical component
  -> flat reduced damage from monster type
  -> percent damage absorption
  -> flat damage absorption
  -> HP / barriers / recovery timeline
```

物理成分 `P` が装甲段階へ到達し、抽選部位のArmorを `A`、Armor Absorptionを `q` としたとき、公式例と整合する基本形は次です。

```text
P_after_armor = P - min(P, A) * q
```

ただし、物理耐性、Shield、種族減少などはこの前段で解決されます。Armor Ratingの単一表示値だけで計算せず、部位分布を保持します。

## 攻撃列

敵AIの `attackTimeout`、`attackDelay`、`attackChance`、距離条件、chainを使い、確定タイムラインではなく可能な攻撃列を生成します。

```text
t=0.00  combat starts
t=1.20  debuff candidate (range condition satisfied)
t=1.55  multi-hit attack, hit 1..N
t=2.10  ground tick overlaps another enemy attack
t=2.30  circuit breaker becomes available/unavailable
```

ランダム性やAI競合があるため、次の三種類を分けます。

- `guaranteed`: DB条件上、必ず成立する部分
- `possible`: 条件と確率次第で成立する部分
- `observed`: 実ゲームで確認した具体的な列

## 弱点判定

「この敵に弱い」は、以下のルールIDを組み合わせて説明します。

- `resistance_overcap_shortfall`: RR後に耐性が目標未満
- `critical_exposure`: DA低下後にクリティカル域へ入る
- `armor_tail_risk`: 特定部位で物理致死閾値を割る
- `cc_recovery_lockout`: CC中に回復・移動・攻撃回復が止まる
- `dispel_dependency`: 解除されうるバフへの防御依存が高い
- `multi_hit_block_gap`: Block Recovery中に後続ヒットが通る
- `shotgun_exposure`: 距離／位置により複数判定が重なる
- `ground_overlap`: 設置ダメージと追撃の離脱猶予が不足
- `sustain_mismatch`: 受けるDPSが持続回復を超える
- `burst_cooldown_gap`: 防御クールダウン外のburstが致死的
- `unknown_interaction`: 版固有・未検証挙動が結論を左右する

## 断定を避ける条件

以下が欠ける場合、ツールは数値を一つに固定しません。

- 敵スキルの実際のレベルまたは%補正
- ランダム装備の有無
- 多段数／投射物の同時命中可能数
- SR補正またはMutator
- 一時バフの稼働状態
- 敵の攻撃が回避・Deflect・Block対象か
- Sunderやデバフの対象版での相互作用

結果には必ず「前提を変えると結論が変わる項目」を添えます。
