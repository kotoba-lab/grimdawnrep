# 戦闘データ完全収集の可能性と境界

## 結論

「戦闘に関する全データ」を、すべての状況で正しい最終結果まで含む意味で完全収集することはできません。一方、Nemesis以上の敵について、ビルドの弱点診断に必要な情報を高い網羅率で集め、不明点を明示した実用的な解析器を作ることは可能です。

重要なのは、完全性を一つの言葉で扱わず、次の層ごとに測ることです。

## 1. 収集可能性が高い: 宣言的ゲームデータ

ARZ / DBR / template / localizationから、原則として次を収集できます。

- 敵、フェーズ、分類、種族、基礎属性、レベル式
- 基礎耐性、CC耐性、OA、DA、HP、Armor
- スキル参照、スキルレベル式、damage packet、weapon damage
- debuff、RR、OA/DA低下、CC、Sunderなどの定義値
- attack timeout、delay、chance、range、chain
- 出現条件、spawn proxy、loot/equipment参照
- SRや難易度に関する公開DBレコード

この層は「データセットとして全件抽出する」という意味の完全性を目指せます。ただし、参照解決、拡張の上書き順、数式評価、ランダム装備を正しく処理する必要があります。

## 2. DBだけでは不完全: コンパイル済みエンジンの意味論

DBRのtemplateは値の入れ物であり、その挙動を理解して実行するコードはゲームエンジン側にあります。通常のModding Toolsではtemplate自体の基本挙動を変更・新設できず、背後のコードはコンパイル済みだとコミュニティのModding調査で説明されています。

この層には次が含まれます。

- 適用順と同時フレーム近傍の解決順
- 切り捨て、丸め、内部上限、最小値
- 同名／別名effectのstackingと例外
- projectile、AoE、collision、target selection
- AI schedulerで複数候補が競合した時の優先順位
- animationと実際のhit frame
- threat、aggro、pathfindingの細部
- patch固有の不具合やtooltipとの不一致

これらは公式説明、開発者発言、既知の式、制御実験を組み合わせて推定します。ゲームのソースコードがない以上、すべてを静的データだけから証明することはできません。

参考: [Is hardcoding possible in Grim Dawn?](https://forums.crateentertainment.com/t/is-hardcoding-possible-in-grim-dawn/105546)

## 3. 全列挙不能: ランタイムの戦闘状態

次はデータの欠落というより、組合せと時間で決まる状態です。

- 敵とプレイヤーの位置、向き、距離
- projectileごとの軌道と命中数
- 乱数、random equipment、spawn composition
- SR Shard、Mutator、複数Nemesis、aggro時刻
- プレイヤーbuff/debuff、cooldown、barrier、block recovery
- 入力、回避方向、反応時間、FPS、遅延
- 数秒間に重なる通常攻撃、DoT、床、設置物

有限のテストケースとして記録することはできますが、全状態・全攻撃列の列挙は状態爆発により現実的ではありません。出力は単一の断定値ではなく、条件付き範囲、危険な攻撃列、観測済みケースになります。

## なぜ上位プレイヤーにも分からないのか

熟練者が知らないのは不自然ではありません。

1. 標準UIには死因を再構築できる詳細な戦闘ログがありません。
2. Character Sheetは最終結果の一部しか見せず、多段や短時間の重なりを説明しません。
3. DB上の値が分かっても、templateの内部挙動と例外は別問題です。
4. 検証結果がpatchで変わり、古い常識が残ります。
5. 実戦では数百ms内に複数イベントが起き、「単発」と「shotgun」を目視で区別できません。

古くからフォーラムでcombat log／death recapが求められ、第三者ツールによるhookやCustom Gameのconsole利用が話題になってきたこと自体が、この観測不足を裏付けます。

参考: [Is there a combat log?](https://forums.crateentertainment.com/t/is-there-a-combat-log/31186), [Any mod to see what killed me?](https://forums.crateentertainment.com/t/any-mod-to-see-what-killed-me/114339)

## このプロジェクトでの「完成」定義

完全な内部再実装ではなく、対象ごとのcoverageを表示します。

| Coverage | 完了条件 |
|---|---|
| Record | 対象敵の全phase、skill、controller参照を解決済み |
| Numeric | 対象レベル・難易度のdamageとdebuff値を解決済み |
| Semantic | 回避、block、stacking、hit modelを仕様または実験で確認済み |
| Temporal | timeout、delay、chain、multi-hitを攻撃列へ変換済み |
| Empirical | 対象版の実ゲームで予測と観測を照合済み |
| Encounter | SR補正、Mutator、複数敵条件を含む分析が可能 |

各敵・各スキルにこのcoverageを付けます。未達の層がある場合は、弱点判定に与える影響と必要な次の実験を返します。

## 現実的な到達点

- 静的な敵定義カタログ: ほぼ全件を目標にできる。
- 単発被ダメージ: 主要な防御層について高精度を目標にできる。
- ボスの既知コンボ: Nemesis以上を優先して実測で積み上げられる。
- 任意のSR実戦の完全再生: 事前予測では不可能。録画・計測付きの事後分析なら近づける。
- ゲームエンジンと完全同一のシミュレータ: ソースコードなしでは保証不能。

従って、このツールの価値は「すべて知っているふり」ではなく、既知、推定、観測、未知を区別した上で、未知を減らす実験まで案内することにあります。
