# 1.3.0 / Fangs of Asterkarn 移行プラン

作成日: 2026-07-25。本書は長期ループで自律実行するワーカー向けの作業計画である。フェーズは依存順に並んでおり、**必ず上から順に**着手する。各フェーズの「完了条件」をすべて満たしてから次へ進む。

## ゴール（全体の完了定義)

1. `doctor` / `dataset-extract` が gdx3（Fangs of Asterkarn）を含む 4 層構成を正しく扱う。
2. 1.3.0 インストール由来の新データセットが生成され、旧 1.2.1.6 データセットとの差分と再検証キューが記録されている。
3. `player.gdc` パーサーが 1.3.0 で保存されたセーブに対して「正常に読める」か「明示的に未対応と報告する」のどちらかであり、黙って壊れない。
4. コード内のハードコード式定数（DA 式、耐性キャップ、装甲吸収率など）が 1.3.0 データで再検証済み、または「未検証」として明示されている。
5. 追跡対象の台帳（`data/research/`）とドキュメントから 1.2.1.6 を最新版とする記述が消え、検証状態が実態と一致している。
6. 1.3.0 の新規メカニクスが実装対象ではなく調査バックログとして記録されている。
7. 全テストと `release-audit` がパスしている。

## 前提事実（2026-07-25 時点で確認済み）

- 2026-07-23 に安定版 **1.3.0.0** が全プラットフォーム配信。同時に有料拡張 **Fangs of Asterkarn** リリース。
  - 出典: https://forums.crateentertainment.com/t/grim-dawn-version-v1-3-0-0/155979
- この端末の Steam インストールは更新済み。`C:\Program Files (x86)\Steam\steamapps\common\Grim Dawn\gdx3\database\GDX3.arz` が存在する。
- パッチノートで確認済みの戦闘関連変更（敵側はデータ再抽出で反映されるはずのもの）:
  - 敵 Armor -17%（Champion 以上）、敵 % Armor 吸収 -20%（全敵）
  - Spirit 1 あたりの Health 8 → 12
  - ボス Health のレベルスケーリング +32%（Lv100 時点、スーパーボスは約 +15%）
  - Ultimate の敵 % Life Leech 耐性引き下げ
  - Devotion 各種のクリティカルダメージ引き下げ
  - 変換 DoT の単一ソース非スタック不具合修正
  - プレイヤーペットの Energy 消費廃止、ペットの Life Leech 耐性 33% → 60%
- 拡張の新規要素: 第 10 マスタリー Berserker、Ascendant モード（Lv100 以降の難易度、第 3 アフィックス付きドロップ）、Awakening（Epic→Legendary 強化）、アフィックス再抽選、ポーションカスタマイズ、新ボス群（The Dread など）。レベルキャップ・Devotion キャップは据え置き。
- リポジトリ側のバージョン依存箇所の全数調査は完了済み（本書の各フェーズに反映済み）。

## 作業共通ルール

- ゲーム由来データ（`.arz` / `.arc` / `.gdc` / 抽出 DBR）をリポジトリにコミットしない。`data/raw/`・`data/generated/` はローカル専用。フェーズ完了ごとに `python -m grim_dawn_lab release-audit` で確認する。
- テストは `$env:PYTHONPATH = "src"; python -m pytest tests/` で全件実行する。
- 未確定値・未対応効果は近似せず、既存の流儀どおり `unsupported_*` / `unknown` として明示する。
- ゲーム内実測が必要な検証（後述の「要人間」項目）はワーカーが実行できない。該当項目は claims を `stale` / `retest_required` にした上でスキップし、ブロッカーとして最終報告に列挙する。ループを止めない。
- 各フェーズ完了時に、変更内容を 1 コミットにまとめる（コミットメッセージは通常のプロフェッショナルな形式）。

---

## Phase 1: gdx3 入力境界対応

すべての後続フェーズの前提。現状、gdx3 は**警告なしで無視**される。

対象:

- `src/grim_dawn_lab/doctor.py:14-20` — `REQUIRED_INPUTS` に gdx3 の ARZ（`gdx3/database/GDX3.arz`）とローカライズを追加する。ただし拡張未所持のインストールも正当な入力なので、gdx3 は「必須」ではなく「存在すれば manifest に含め、無ければ absent と記録する」optional 層として設計すること。既存の Base/GDX1/GDX2 の扱いと一貫させる（GDX1/GDX2 も本来 DLC である点を踏まえ、required/optional の区分を manifest 上で明示する）。
- `src/grim_dawn_lab/dataset.py:462-466` — アーカイブ層リスト `base/gdx1/gdx2` に `gdx3` を追加（オーバーライド順は base < gdx1 < gdx2 < gdx3）。
- `src/grim_dawn_lab/dataset.py:483-491` — ローカライズ ARC パスに gdx3 分を追加（gdx3 に localization リソースが存在するか実インストールを確認して決める）。
- `tests/fixtures/game_install/` — 疑似インストールに 4 KiB 以下の合成 `gdx3/database/GDX3.arz` を追加し、doctor / dataset のテストを 4 層で更新。gdx3 欠如ケース（拡張未所持）のテストも追加。
- `docs/implementation/m0-doctor.md` — 層構成の記述を更新。

完了条件:

- [ ] 実インストールに対する `python -m grim_dawn_lab doctor --install-path "C:\Program Files (x86)\Steam\steamapps\common\Grim Dawn"` の manifest に GDX3.arz の sha256 が含まれる。
- [ ] gdx3 なし疑似インストールで doctor がエラーではなく「gdx3: absent」相当を報告する。
- [ ] 全テストパス、`release-audit` パス。

## Phase 2: 実セーブによる gdc 検証

対象:

- `src/grim_dawn_lab/gdc.py:258-260` — `data_version` 許容値 `(6, 7, 8)`。1.3.0 でプレイ・保存されたセーブの `data_version` を実測する。
  - 手順: `python -m grim_dawn_lab save-import --path <save> --redact-name` を既存セーブに対して実行。セーブの mtime が 2026-07-23 以降のものが 1.3.0 で保存された候補。**mtime が全て 2026-07-23 より古い場合、1.3.0 保存セーブは存在しない**。その場合はバージョンバンプの有無を確定できないので、「未確認」として記録し、`UnsupportedGdcVersion` の報告メッセージに 1.3.0 の可能性を明記する改善のみ行う。ゲームを起動してセーブを作る作業は「要人間」。
- バンプが確認された場合: 新 `data_version` のブロック構造を既存の読み取り専用方針で解析し、確定できたフィールドのみ対応、不明部分は `unknown_*` として保持。`gdc.py:192-196` の装備 12 スロット固定・`build.py:11-14` の `EQUIPMENT_SLOTS` にスロット追加がないかを実データで確認。
- `tests/test_gdc.py:114-116` — 「version 9 拒否」テストを実測結果に合わせて更新（9 が正当になった場合は新しい未対応版番号で拒否テストを維持）。
- `docs/implementation/m4-player-gdc.md` — 実測結果（file version / data version / スロット数 / スキル数）を追記。

完了条件:

- [ ] 1.3.0 保存セーブの有無と、有る場合はその `data_version` が文書に記録されている。
- [ ] パーサーが 1.3.0 セーブを「読める」または「明示的に未対応と報告する」ことをテストで固定している。
- [ ] 全テストパス。

## Phase 3: 再抽出とデータセット差分

対象:

- Phase 1 完了後の 4 層構成で `doctor` → `dataset-extract` を実行し、1.3.0 データセットを新規生成する（既存の Moosilauke 系 select から開始。抽出は 1 回あたり数分〜10 分程度かかる想定）。
- `dataset.py:377-426` の `diff_datasets` + `build_revalidation_queue` で、`input_manifest` 付きの旧 1.2.1.6 データセット（`data/generated/datasets/` 配下の 2 件: `1b4876f7…`, `aea4d937…`）との差分と再検証キューを生成し、`data/generated/` に保存する。
- パッチノート既知の変更（敵 Armor -17%、% 吸収 -20%、ボス HP スケーリング +32%）が差分に現れるかを突き合わせ、結果を記録する。**現れない場合は抽出パイプラインの取りこぼしを疑って原因を調査する**（これ自体が重要な検証）。
- `input_manifest: null` の旧データセット 8 件は版が証明できないため、以後の比較基準に使わない旨を記録する（削除は不要。content-addressed なので共存できる）。
- 再発防止: `cli.py:118` の `dataset-build` 経路が manifest なしのデータセットを作る問題に対し、manifest 未指定時は dataset に `input_manifest: null` ではなく警告を出す、または manifest を必須化する小改修を行う。

完了条件:

- [ ] 1.3.0 の `input_manifest` 付きデータセットが少なくとも 1 件生成されている。
- [ ] 旧安定版データセットとの diff と再検証キューがローカルに保存され、要約が docs に記録されている。
- [ ] パッチノート既知変更と差分の突き合わせ結果が記録されている。
- [ ] 全テストパス、`release-audit` パス。

## Phase 4: 式定数の再検証

対象（確認は 1.3.0 の抽出済み DBR に対して行う）:

- `src/grim_dawn_lab/build.py:218` — DA 式定数（level×12、physique×0.5、+53）を `records/game/combatformulas.dbr` の 1.3.0 実値と照合。可能ならハードコードをやめて DBR から読む実装に変更し、読めない場合のみ従来値へフォールバックして `trace` に出所を記録する。
- `src/grim_dawn_lab/build.py:244,246` — 耐性キャップ 80、基礎装甲吸収 70% を `records/game/gameengine.dbr` 等の実値と照合。同様に DBR 読み取り化を検討。
- Spirit→Health が 8→12 に変更された点: `characterAttributeEquations` 系レコード（`dataset.py:256-277` が参照）を 1.3.0 データで確認し、リポジトリ内に Spirit→Health の旧値を前提とした計算・文書があれば更新（現状コードでは Health 導出は限定的だが、docs/domain の記述を含めて掃く）。
- `src/grim_dawn_lab/combat.py:13-20`（部位重み）、`combat.py:44-73`（PTH 式・55% フロア・クリ帯域 90/105/120/135/150）: 今回のパッチノートに変更記載はないが、根拠が 1.2.1.6 検証のため失効している。公式 Combat Guide / ゲームデータから 1.3.0 での値を確認できたものは「1.3.0 で再確認」と記録。**ゲーム内実測が必要なもの（E01 装甲オラクル 65/30/16、E02 PTH 境界）は「要人間」**。実測できない項目は `docs/implementation/verification-status.md` で unverified に落とすだけにする。
- `tests/test_combat.py` / `tests/test_build.py` の期待値: 式が変わっていた場合のみ更新。変わっていなければ「fixture としての回帰値」と「1.3.0 実値」の関係をコメントではなく docs 側に記録。

完了条件:

- [ ] DA 式・耐性キャップ・装甲吸収率について「1.3.0 DBR 実値と一致 / 不一致（修正済み）/ DBR から確認不能」のいずれかが文書化されている。
- [ ] ゲーム内実測が必要な未検証項目が「要人間」リストとして明示されている。
- [ ] 全テストパス。

## Phase 5: 台帳・ドキュメント更新

対象:

- `data/research/sources.json` — `patch-1.3.0` ソース（Crate 公式フォーラムのパッチノート URL、`version_scope: "1.3.0.0"`）を追加。`as_of` を更新。
- `data/research/claims.json` — `applies_to: ["1.2.1.6"]` の SR Nemesis 主張と Sunder 主張を `stale`（要再検証）へ。`as_of` を更新。パッチノートで確認済みの変更（敵 Armor/吸収、Spirit Health 等)を新規 claim として evidence 付きで追加。
- `README.md` — 47 行目の版記述を「安定版 1.3.0.0（2026-07-23 配信、Fangs of Asterkarn 同時リリース）」に更新。25 行目・70 行目の Base/GDX1/GDX2 記述を 4 層に更新。58 行目の存在しないパス `tests/fixtures/timeline/combo.json` を実在の fixture パスに修正（パッチと無関係の既存バグ、ついでに直す）。
- `docs/implementation/verification-status.md` — 全「Verified」行を再判定。1.3.0 で再確認できたものだけ Verified に戻し、残りは版スコープ付きで unverified 化。日付更新。
- `docs/architecture/data-strategy.md:122-130` — インストール棚卸しを 4 層構成で更新。
- `docs/research/initial-findings.md:134-138` — 版状況の節を更新（1.3.0 リリース済み、Fangs of Asterkarn は予告どおり配信）。
- `docs/research/experiment-backlog.md` — E03（Sunder）の「1.2.1.6 vs 1.3.0 公開テスト」というスコープを「1.3.0.0 安定版で再実施」に書き換え。E01/E02 も対象版を 1.3.0.0 に更新。
- `docs/implementation/m2-dataset.md` — Phase 3 の実測に基づきレコード数・時間を更新（gdx3 で増える）。49 行目の未解決コンポーネントスキル 2 件が gdx3 で解決するか確認して追記。
- `docs/implementation/m5-grimtools.md` — grimtools.com は 1.3.0.0 対応済み。公開共有 URL を 1 件取り込んで実地再検証し（タイトル正規表現 `grimtools.py:20` と `buildInfo` 形状が維持されているか）、結果を追記。Berserker を含むビルド URL があれば優先。壊れていた場合の修正は最小限（新フィールドは従来方針どおり明示的に unknown 扱い）。
- `tests/fixtures/advisor/context.json` の `game_version: "1.2.1.6"` は、Phase 3 の新データセットに合わせて更新するか、他 fixture と同様 `"fixture"` に正規化する。

完了条件:

- [ ] リポジトリ内 grep で「1.2.1.6 が現行安定版である」と読める記述が残っていない（履歴・実測記録としての言及は可）。
- [ ] claims/sources の `as_of` が更新され、stale 化と新規 claim 追加が済んでいる。
- [ ] grimtools 実地再検証の結果が m5 文書に記録されている。
- [ ] 全テストパス、`release-audit` パス。

## Phase 6: 新規メカニクスのバックログ化（実装しない）

1.3.0 / 拡張の新概念を調査バックログとして記録するのみ。実装はスコープ外。

- `docs/research/experiment-backlog.md` または新規文書に以下を追加:
  - Berserker マスタリー: grimtools 取込・セーブ解析・データセットへの影響調査。
  - Ascendant モード: 難易度体系への影響（`schemas/encounter.schema.json:11` の channel/難易度 enum、`dataset.py:122-123` の難易度配列インデックス前提が Ascendant で崩れないか）。
  - Awakening / Ascendant 第 3 アフィックス / アフィックス再抽選: アイテムモデルへの影響。
  - 新ボス群（The Dread、Tempest Totems、Marked by the Void）: encounter 候補リスト（`docs/research/initial-findings.md:153`)への追加。
  - 変換 DoT スタック修正・ペット Energy 廃止: 現行モデルへの影響有無のメモ。

完了条件:

- [ ] 上記が版・出典付きでバックログに記録されている。
- [ ] 最終報告として、全フェーズの結果・「要人間」ブロッカー一覧・未解決事項をまとめる。

---

## 要人間（ワーカーが実行できない作業の集約）

- ゲームを起動して 1.3.0 でセーブを作成・実測すること全般（Phase 2 のセーブ生成、Phase 4 の E01/E02 ゲーム内オラクル、E03 Sunder 実測）。
- Fangs of Asterkarn の購入状態の確認（gdx3 データは存在するが、所持状況はゲーム内でのみ確定する）。

これらに依存するステップは記録・スキップし、他の作業を進めること。
