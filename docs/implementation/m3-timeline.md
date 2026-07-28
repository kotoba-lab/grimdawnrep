# M3: 攻撃列・状態変化・実測照合

## 実装済み縦切り

`grim-dawn-lab sequence` はBuildDefenseSnapshotと時刻付きattack列を入力し、一撃計算を各hitへ適用する。

- resistance reductionとDA reductionを有効期限付き状態として保持する。
- デバフ前後の追撃を異なる防御状態で再計算する。
- 同一attackのmulti-hit、projectile、ground、異なるsourceの重なりを時間軸へ展開する。
- `single_hit`、`shotgun`、`combo`、`overlap`、`recovery_failure`、`unknown`へ根拠とconfidence付きで分類する。
- 未対応DoTや状態効果をゼロ扱いせず `unknowns` へ残す。
- Observation JSONの実測hitと予測平均を対応させ、絶対・相対誤差と未対応hitを返す。
- 実DBRからspecial attackのtimeout、delay、chance、range、skill recordをattack candidateとして正規化する。

## 回帰fixture

- 80% fire resistanceで100 damageを受けると20 damage。
- 30ポイントのfire resistance reduction後は同じ追撃が50 damage。
- 同時3 projectileを `shotgun` と分類する。
- groundと別enemy hitの0.05秒差を `overlap` と分類する。
- 未対応DoTを `unknown` とし、計算不能を明示する。

## Observation

`schemas/observation.schema.json` に、ゲーム版、scenario、時刻付きevent、観測方法、source hashを保持する。動画または手動観測の生データそのものは配布せず、許諾された数値・hashだけをfixture化する。

## 既知制約

DBRのtimeout、delay、chance、rangeはattack候補の境界を作るが、AI競合、位置、animation frame、同時敵、projectile実命中数を一意には決めない。静的候補を観測済み列として扱わず、Observationがある場合だけ `observed` とする。
