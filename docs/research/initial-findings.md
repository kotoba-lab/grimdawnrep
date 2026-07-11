# 初期調査: 問題設定の裏付けと反証

調査日: 2026-07-11

## 要約

「ビルド計算機では完成して見えるのに、特定の敵や高層SRで死因が分からない」という問題は実在します。ただし原因は、敵情報が完全に存在しないことよりも、次の情報が結合されていないことです。

- ビルドの平常時／バフ時／デバフ時の防御状態
- 敵スキルのダメージ、命中判定、使用条件、再使用間隔
- 攻撃に付随する耐性低下、DA低下、CC、Sunder、バフ解除
- 複数ヒットと複数敵が作る短時間の攻撃列
- SR深度、Mutator、難易度、敵レベルなどの環境補正
- プレイヤーの距離、位置、回避、クールダウン状態

したがって作るべきものは、別のビルド計算機ではなく、**ビルドと遭遇を結合する説明可能なシミュレータ**です。

## 裏付けられた点

### 1. GrimTools の敵DBは重要だが、最終回答ではない

GrimTools Monster Database の公開説明によると、敵の属性、耐性、スキル、出現地点に加え、スキルの初回遅延、距離などの特殊挙動も表示します。一方で、敵装備は Loot Table による組合せが多いため考慮されず、表示されるスキル値はパーセントボーナスでスケール済みではありません。また多くの敵スキルには表示名がありません。

これは次の二点を意味します。

- 敵分析の原材料はすでにかなり存在する。
- 画面上の一つの数値を、そのまま最終被ダメージと見なすことはできない。

出典: [GrimTools Monster Database 公開スレッド](https://forums.crateentertainment.com/t/grimtools-monster-database/42861)

### 2. 敵の攻撃パターンはDBから部分的に復元できる

公式 Modding Guide は Monster DBR の `Skill Configuration` として、少なくとも以下を説明しています。

- `attackTimeout`: 戦闘開始から最初に使うまでの時間
- `attackDelay`: 再使用判定までの時間
- `attackChance`: Delay後に使用する確率
- `attackRange`: 使用を検討する距離帯
- `chainInitialSkill` / `chainNextSkill`: スキル列
- 低ヘルス時、死亡時、出現時、回復、自己／味方バフの各スキル

よって「敵の挙動は完全なブラックボックス」という強い主張には反証があります。ただしDBは、アニメーション所要時間、実際の命中位置、プレイヤー移動、AIの優先順位競合など、ランタイムの全結果を保証しません。

出典: [公式 Grim Dawn Modding Guide](https://www.grimdawn.com/downloads/Grim%20Dawn%20Modding%20Guide.pdf) pp.35-38

### 3. 被ダメージは層構造であり、ビルドの単一スコアへ潰せない

公式 Combat Guide は防御適用順を次のように示します。

1. Fumble / Dodge / Projectile Deflection
2. OA対DAの命中判定
3. Shield
4. 種族に対する%ダメージ減少
5. Resistances
6. Armor
7. 種族に対するフラットダメージ減少
8. % Damage Absorption
9. Flat Damage Absorption

さらに装甲は部位抽選で、頭15%、肩15%、胴26%、腕12%、脚20%、足12%です。同じ「Armor Rating」でも、弱い部位へ物理攻撃が当たる尾部リスクがあります。OA/DAも平均被ダメージだけでなく、命中率とクリティカル倍率を変えます。

出典: [公式 Combat Guide](https://www.grimdawn.com/guide/gameplay/combat/)

### 4. SRは単体ボス試験とは別の実験条件である

公式 Shattered Realm Guide は、深くなるほど敵が強くなり、Mutator は1個から最大8個まで増え、Shardごとにランダム化されると説明しています。従って「スーパーボスをタンクできる」と「高層SRのランダムな複合条件を安定して通る」は同じ評価軸ではありません。

さらに安定版 `1.2.1.6` では Shard 25+ に最低1体のNemesisが出現し、追加Nemesisの確率も上がりました。敵組合せリスクは現行版で無視できません。

出典: [公式 Shattered Realm Guide](https://www.grimdawn.com/guide/game-settings/shattered-realm/), [v1.2.1.6 Patch Notes](https://forums.crateentertainment.com/t/grim-dawn-version-v1-2-1-6-hotfix/146187)

### 5. 静的仕様だけでは拾えない相互作用が実際にある

Sunderについて、2025年のコミュニティ実験では「後続のSunderや他デバフの強度まで増幅する」挙動が報告され、複数ヒットや複数ボスで耐性低下・DA低下が想定以上になる例が示されました。同スレッド内でも意図した仕様かバグかは確定していません。

これは公式仕様として採用してよい証拠ではありません。しかし、次の設計要件を強く支持します。

- 主張をゲーム版に紐付ける。
- 計算式とは別に再現テストを持つ。
- 「既知の仕様」「観測された挙動」「未確定」をUIで分ける。

出典: [Can Sunder buff/enhance Sunder and other debuffs?](https://forums.crateentertainment.com/t/can-sunder-buff-enhance-sunder-and-other-debuffs/144682)

## 反証・修正すべき仮説

### 「カルキュレーターで分からない部分は主に敵の挙動」

概ね正しいですが、少し狭いです。実際には次の四層があります。

1. **敵定義**: ステータス、スキル、AI使用条件
2. **戦闘環境**: SR補正、Mutator、難易度、敵レベル、複数敵
3. **プレイヤー状態**: 一時バフ、回復、回避、位置、デバフ残時間
4. **エンジン挙動**: 多段判定、投射物重なり、フレーム近傍の順序、版固有バグ

敵DBだけを整備しても、2-4を欠けば「なぜ死んだか」には届きません。

### 「ワンパンされた」

解析上は最低でも次に分ける必要があります。

- `single_hit`: 一つのダメージイベントが現在HPを超えた。
- `shotgun`: 同一スキルの複数投射物／複数判定が短時間に当たった。
- `combo`: デバフ、CC、追撃が所定時間内に連鎖した。
- `overlap`: 複数敵、床、DoT、通常攻撃が重なった。
- `recovery_failure`: 被弾量より、回復不能・ライフスティール停止・CCが主因だった。
- `unknown`: 観測情報が足りず分類不能。

ツールは断定より先に、この分類と必要な追加観測を返すべきです。

### 「完璧そうなビルド」

遭遇条件なしに完璧さは定義できません。少なくとも次のプロファイルを分けます。

- 単体・固定パターンのCelestial
- SR 30-31の周回速度と安定性
- SR深層の生存限界
- Nemesis複数体の最悪組合せ
- Hardcore向けの尾部リスク抑制

## 証拠レベル

| 等級 | 意味 | 例 |
|---|---|---|
| A | 公式仕様または版が一致するゲームDB | Combat Guide、ARZ/DBR |
| B | パッチノート、開発者の明示発言 | v1.2.1.6 Patch Notes |
| C | 再現条件と数値を伴うコミュニティ実測 | 固定ダメージを用いたSunder試験 |
| D | 攻略経験、体感、再現条件の弱い報告 | 「この敵はワンパンが多い」 |
| U | 仮説、未確認 | 投射物が特定距離で全弾命中する等 |

同じ主張に複数の証拠を紐付け、版が変わったら `stale` にします。

## バージョン上の注意

- 2026-07-11時点で公式 Patch Notes の最新安定版は `1.2.1.6 + Hotfix` です。
- 同時点で `1.3.0` の公開テストが進行中です。
- 現行公式ガイドは Fangs of Asterkarn / Berserker / Onslaught の記述を含みます。安定版データと混ぜず、仕様ページにも `applies_to` を持たせます。

出典: [Patch Notes 一覧](https://forums.crateentertainment.com/c/grimdawn/patch-notes/28), [v1.3.0 Public Test](https://forums.crateentertainment.com/c/grimdawn/public-testing-discussion/38)

## 最初の検証対象

最初から全Hero/Bossを対象にしません。静的解析と実測の差が見えやすい次の類型を、一体ずつ選びます。

1. 高物理単発とDAの影響
2. 多段／投射物の重なり
3. 耐性低下後の属性追撃
4. Sunder後の追撃
5. 強いCCが回復を阻害する戦闘
6. バフ解除／Nullificationが防御層を崩す戦闘
7. 床・設置物を含む持続的な位置取り試験
8. SRで危険なNemesis二体の組合せ

敵名を先に固定するより、上記の失敗モードを一つずつ再現できる敵をゲームDB抽出後に選定します。候補として Aleksander、Kaisan、Grava'Thul、Iron Maiden、Moosilauke、Kubacabra と、Ravager / Callagadra / Mogdrogen / Crate を比較対象にしますが、攻撃特性は未検証のまま断定しません。
