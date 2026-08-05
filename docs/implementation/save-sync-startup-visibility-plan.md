# 起動時リモート可視化・選択ポリシー・起動時保全・終了時公開分離 実装計画

作成日: 2026-08-05
状態: **完了**（2026-08-05 実装・実機確認済み。§12 の完成判定 1〜7 をすべて確認）
対象: `grim_dawn_sync` / Windows 2端末運用
前提計画: `docs/implementation/save-sync-startup-selection-plan.md`
T-A の判定結果: `docs/implementation/save-sync-visibility-t-a-findings.md`
後続計画: `docs/implementation/save-sync-long-operation-progress-plan.md`（未着手）

以下の本文は着手時点の計画をそのまま残したものである。§11「ワーカーへの指示」等は
当時の指示であり、現在の作業指示ではない。実装後に判明した事実は次のとおり。

- T-A の判定は仮説 (a)（同一 `root_hash` の統合による provenance 不可視化）のみ確定。
  T-A2 は不要と判断し、対処は T-B に統合した。詳細と、実機確認で判明した
  「未解決として残す点」は上記 findings を参照。
- 実機確認で、ユニットテストでは検出できなかった問題を 3 件修正した。
  `--exit-disposition` の対話経路での黙殺、session-start 候補が作成時刻で
  識別できない問題（T-A と同型の再発）、終了時復元の段階が監査ログで
  1 行に潰れる問題。
- `report` モードは catalog 構築をオフライン耐性にしない
  （`managed_bookmarks()` も remote へ問い合わせるため）。
  `offline_policy=deny` の fail-closed は維持されている。

## 0. 背景

2026-08-05 の運用で、選択画面に表示された候補（save 作成 08-05 02:17）より新しい
リモートセーブ（08-05 05:48:36）が存在したが、画面の Reload と再起動のどちらでも
候補として現れなかった。手動の候補再取得と安全な自動選択でリモート版を反映済みであり、
現在は remote/local 一致、lock なし、`status=ready` である。

この計画は、次の4点を実装するためのものである。

1. 起動時にリモート確認結果を表示する。
2. 選択画面の表示条件を設定で切り替える。
3. 起動時のローカル元データを session-start snapshot として自動保全する。
4. ゲーム終了時の公開動作を publish / local-only / restore-startup に分離する。

## 1. 現状のコード事実（着手前に確認済み）

| 項目 | 現在の実装 | 位置 |
|---|---|---|
| 候補構築 | `fetch()` → tracking ref の OID → `remote_history(limit=10)` → bookmark → legacy tag | `version_catalog.py:150` |
| 表示日時 | `manifest["created_at"]` のみ。リモート確認時刻も commit 時刻も保持しない | `version_catalog.py:228` |
| 重複統合 | `root_hash` が同一の候補を1行へ統合し、代表は kind 優先度で決まる | `version_catalog.py:116` |
| 表示条件 | `_POLICY` 固定表。設定項目なし | `selection.py:40` |
| Reload | `SelectionRequest(action="reload")` を返し、CLI ループが catalog を再構築 | `selector_ui.py:210` / `cli.py:581` |
| 起動時保全 | 選択版を適用する場合のみ `archive_before_restore` が走る | `workflow.py:402` |
| 終了時 | `archive_after_game` → `snapshot` → `push` → `release` を無条件実行 | `workflow.py:431-443` |
| config | `CONFIG_KEYS` は閉じた集合、未知キー拒否、`schema_version` は完全一致 | `config.py:16,89` |

## 2. 非目標

- セーブ内容の編集、アイテム移動、キャラクター改変。
- Git 履歴、bookmark、ローカルアーカイブの自動削除。
- `never`（選択画面を出さない設定）の追加。
- 任意パス・任意 ref・任意 commit を UI 入力から受け付けること。
- 既存の lock、fail-closed、ff-only、create-only、archive-before-mutation 契約の緩和。

## 3. T-A: 不可視事象の原因特定（最優先・他チケットの前に完了させる）

「確認結果を表示する」だけでは、05:48 の版が候補に出なかった原因は消えない。
表示機能を先に入れると、原因が残ったまま「表示上は正常」に見える状態を作る危険がある。

### 3.1 検証対象の仮説

- **(a) 重複統合による表示上の消失**: 05:48 commit の `root_hash` が既存候補と同一の場合、
  `_coalesce()` が1行に統合し、代表 kind の `created_at`（例: live の走査時刻や 02:17）だけが
  表示される。取得は成功しているが UI に出ない、という経路。
- **(b) fetch の非 fast-forward 拒否**: `fetch()` は非強制 refspec
  （`refs/heads/<branch>:refs/remotes/<remote>/<branch>`、`git_vault.py:601`）である。
  非 FF 更新では tracking ref が更新されない。ただし `_capture_catalog` は
  `after_remote_snapshot.remote_head != catalog.remote_head` を
  `catalog_context_changed` にするため（`cli.py:368`）、この経路なら
  「古い候補が出る」ではなく「reload が失敗し続ける」になるはず。実挙動と突き合わせる。
- **(c) 履歴上限**: `build(history_limit=10)` 固定。bookmark / legacy を含む候補数と
  統合結果によって、目的の commit が範囲外になっていないか。
- **(d) reload 経路の catalog 再利用**: `cli.py:581` は `selected` と `catalog_token` を
  `None` に戻すため理屈上は再構築だが、`present_builder` の worker が
  初回 catalog を保持していないか実測する。

### 3.2 作業

- `<local_root>/logs/launch.jsonl` の当該時刻帯を読む。
- Vault で読み取りのみのコマンドを実行し、05:48 commit の OID、`.sync/manifest.json` の
  `root_hash` と `created_at`、committer date を取得する。02:17 候補の同項目と突き合わせる。
- `grim-dawn-sync versions --json` を当時と同条件（可能な範囲）で再現し、候補集合を記録する。
- 実 remote へは一切書き込まない。`fetch` / `ls-remote` / `log` / `cat-file` のみ。

### 3.3 DoD

- 原因コードパスが1つに特定されているか、特定不能なら「特定できなかった事実」と
  除外できた仮説が記録されている。
- 原因が (a) なら T-B に「統合行でも全 provenance の日時を必ず表示する」を追加する。
- 原因が (b)(c)(d) なら、修正を独立チケット T-A2 として切り、T-B より先に入れる。

## 4. T-B: リモート確認結果の表示

### 4.1 データモデル

3つの時刻を**別フィールド**として扱う。混同したまま1つに丸めない。

```python
@dataclass(frozen=True)
class RemoteCheckReport:
    status: Literal["ok", "failed", "skipped"]
    checked_at: str | None            # 実際に remote へ問い合わせた UTC 時刻
    error_code: str | None            # status != "ok" のときのみ
    head_committed_at: str | None     # remote head commit の committer date
```

- `checked_at` は `VersionCatalogBuilder.build()` 内で `fetch()` / `remote_oid()` が
  成功した直後に採取する。呼び出しを実行していない場合は `skipped` とし、
  過去の値を再利用しない。
- `head_committed_at` は検証済み OID に対してのみ `git log -1 --format=%cI` を読む。
  外部入力から ref を組み立てない。
- 候補の作成日時は従来どおり `manifest["created_at"]`（= save 作成時刻）。
- `VersionCatalog` に `remote_check: RemoteCheckReport` を追加する。

### 4.2 UI

- 候補一覧の上部に固定3行を出す。
  - `リモート確認: <checked_at のローカル時刻>`
  - `採用候補のセーブ作成: <created_at のローカル時刻>`
  - `リモート最新のコミット: <head_committed_at のローカル時刻>`
- 各候補行にも `セーブ作成` と `コミット` の2列を出す。統合された候補は、
  統合された全 provenance の kind と日時を詳細ペインに列挙する（T-A の (a) 対策）。
- `status != "ok"` のときは一覧上部に文字の警告を出す。色だけに意味を持たせない。
  文言例: 「リモート未確認です。表示されている候補は古い可能性があります。」
- 未確認状態から `launch` する場合は追加の確認を1段挟む。
  ただし `offline_policy=deny` の既存 fail-closed 挙動は弱めない。
  「表示だけ成功して確認は失敗している」状態を無警告で通さないことが目的である。

### 4.3 JSON / ログ

- `versions --json` と `launch --json` の `selection_required` ペイロードに
  `remote_check` ブロックを追加する。
- 監査ログには時刻と status のみを追加する。commit OID、root hash、パス、
  キャラクター名、remote URL は従来どおり追加しない。

### 4.4 DoD

- 3つの時刻が別フィールドとして出力され、UI で同時に読める。
- 起動時刻とセーブ作成時刻の乖離だけで「古い可能性」を利用者が判断できる。
- リモート確認に失敗した起動が、警告なしに正常起動と同じ見た目にならない。

## 5. T-C: 選択画面の表示ポリシー設定

### 5.1 設定

- `config.local.json` に `selection_policy` を追加する。値は `"on_diff"`（既定、現行動作）
  または `"always"`。`CONFIG_KEYS` に追加し、未知キー拒否は維持する。
- `schema_version` は `1.1.0` へ上げる。読み込みは `{1.0.0, 1.1.0}` を受理し、
  `1.0.0` かつ未指定なら `on_diff` とする。書き出しは `1.1.0`。
  （現在は完全一致要求のため、この緩和は明示的な変更点として扱う。）
- CLI: `grim-dawn-sync launch --selector always|on-diff`。指定時は config を上書きする。

### 5.2 決定表への影響

- `selection_policy(case)` を `selection_policy(case, *, policy)` にし、純関数のまま保つ。
- `always` は `show_selector` のみを `True` に変えてよい。
  `initial_selection`、`allowed_operations`、`bookmark_displaced_remote` は変更しない。
- `BASELINE_MISSING` は `always` でも `show_selector=False` のままとし、既存の安全エラーを出す。
- `--json` は `always` でも自動選択しない。`selection_required` を返す既存契約を維持する。
- `never` および無条件 remote 適用は追加しない。

### 5.3 DoD

- `ReconcileCase` × `policy` の全組合せがテストされている。
- `always` 設定で EQUAL 起動しても、選択なしに live が書き換わらない。
- 旧 schema の config が壊れずに読める。

## 6. T-D: session-start snapshot

### 6.1 動作

- 起動確定後、ロック取得後、**live への最初の変更より前**に、選択内容にかかわらず
  現行 live を検証済みローカルアーカイブとして作成する。
- 新 workflow state `SESSION_START_SNAPSHOT` を `ACQUIRE_LOCK` の直後に追加する。
- 作成手順は既存 `preserve` の二段公開（`cli.py:888` 付近の
  `.preserve-incomplete-*` → 検証 → 公開）をそのまま使う。中途半端なディレクトリを
  候補として見せない。
- ID 規約: `save-session-start-<root_hash[:16]>-<uuid32>`。
  既存の置換前アーカイブ（`save-before-*`）とプレフィックスで区別する。
- メタデータ `<archive>/.session-start.json`:

```json
{
  "schema_version": "1.0.0",
  "kind": "grim_dawn_session_start_snapshot",
  "created_at": "2026-08-05T00:00:00Z",
  "root_hash": "...",
  "machine_id": "...",
  "session_id": "...",
  "launched_from_candidate_kind": "remote_head"
}
```

### 6.2 候補としての公開（設計上の要注意点）

要求どおり、選択画面の候補に `session-start` を表示できるようにする。ここが最大の変更点である。

- 現在 `selection.py:167` は `kind != "live"` の候補に検証済み commit を要求する。
  `session_start` は commit を持たないローカル候補なので、**ローカル検証済みアーカイブ経路**を
  明示的に追加する必要がある。
- 許可条件は次の3点をすべて満たす場合のみとする。いずれか欠ければ候補にしない。
  1. アーカイブがツール所有の `archives/` 直下にあり、リンク・リパースを経由しない。
  2. `.session-start.json` がスキーマ検証に合格する。
  3. 再走査した manifest の `root_hash` が記録値と一致する。
- 任意パス指定は受け付けない。既存どおり opaque `candidate_id` 経由のみで選択する。
- 復元は既存の `plan_restore` / `archive_before_restore` / `apply_restore` を使い、
  置換前アーカイブは従来どおり別に作る（session-start アーカイブで代替しない）。
- 候補一覧では「今回の起動前データ」として別セクションに置き、
  `live` / `remote_head` / `history` / `bookmark` と並べて選択できるようにする。

### 6.3 失敗時の扱い

- session-start アーカイブの作成失敗は、live 変更前なので fail-closed で中止する。
  live は不変のまま、recovery required にはしない。
- `status` に session-start アーカイブの件数と合計容量を出す。自動削除はしない。
  ディスク使用量が起動ごとに増えることを運用ドキュメントに明記する。

### 6.4 単独リリース時の必須注記

**session-start を残すだけでは、終了時の自動 push によるリモート更新は止まらない。**
T-D を T-E より先に出す場合、リリースノートと runbook に
「終了時 push は依然発生するため、デュープ用途にはまだ使えない」と明記すること。

## 7. T-E: 終了時公開動作の分離

### 7.1 3つの disposition

| 値 | 動作 |
|---|---|
| `publish`（既定） | 現行どおり `archive_after_game` → `snapshot` → `push` → `release` |
| `local-only` | `archive_after_game` まで実行し、snapshot と push を行わずロックを解放する |
| `restore-startup` | `archive_after_game` 実行後、session-start アーカイブから live を復元し、push しない |

### 7.2 決定タイミング

- `SelectionPlan` に `exit_disposition` を追加し、起動前に固定する。
- 終了後、`push` を実行する**前**に確認画面を出す。変更を許すのは安全方向のみ、
  すなわち `publish → local-only` と `publish → restore-startup` だけとする。
  push 後には戻せないため、逆方向の変更は受け付けない。
- `--json` / headless は `launch --exit-disposition publish|local-only|restore-startup` で
  明示指定する。未指定時は `publish`。暗黙の変更はしない。

### 7.3 local-only の実装要件

- `release()` は現在 `(lock, pushed_commit, manifest)` を要求する。
  公開なし解放 API（`release_without_publish` 相当）を `session_lock` に追加する。
- `state.last_applied_remote_commit` と `last_applied_manifest_root_hash` は変更しない。
- 結果として次回起動は `LIVE_AHEAD` になる。これは仕様であり、選択画面で
  「この端末のデータは未公開」と明示する。デュープ用途の要点はここにある。
- `state.phase` に未公開解放用の遷移を追加し、`recover` を対応させる。
  ここを省くと中断時に fail-closed が壊れるため、必須作業とする。

### 7.4 restore-startup の実装要件

- 終了後データも必ず `archive_after_game` で保全してから復元する。復元で消さない。
- 復元は `archive_before_restore` を経由し、復元後の `root_hash` が
  `.session-start.json` の記録値と一致することを検証する。
- 不一致・復元失敗は recovery required とする。
- push は行わず、`local-only` と同じ未公開解放パスでロックを解放する。

### 7.5 DoD

- 3つの disposition それぞれで、終了後の remote head、state、lock、アーカイブが
  期待どおりであることが統合テストで確認されている。
- `local-only` と `restore-startup` のどちらでも remote main が変化しない。
- 各 disposition の post-lock フォールト注入から recover できる。

## 8. 実装順序

1. T-A（診断）— 必要なら T-A2（修正）
2. T-B（表示）
3. T-C（ポリシー設定）
4. T-D（session-start）
5. T-E（公開分離）

- 小さい commit に分ける。T-D と T-E を同一 commit にしない。
- 実端末への展開は T-E まで揃ってから行う。T-D 単独展開時は 6.4 の注記を必ず付ける。

## 9. テスト

### Unit

- `RemoteCheckReport` の ok / failed / skipped と、3時刻フィールドの独立性。
- 統合候補（同一 root_hash）で全 provenance の日時が保持されること。
- `ReconcileCase` × `selection_policy` の全組合せ。
- config schema 1.0.0 / 1.1.0 の読み込みと未知キー拒否。
- `.session-start.json` の正常・境界・不正・パス偽装。
- `exit_disposition` の許可される遷移と拒否される遷移。

### Integration

- リモート確認失敗時に警告付きで選択画面が出て、無警告起動にならない。
- `always` 設定で EQUAL 起動しても live が無承認で書き換わらない。
- session-start アーカイブ作成 → 候補表示 → 選択 → 復元の一巡。
- `local-only` 終了後、remote head 不変・state 不変・lock なし・次回 `LIVE_AHEAD`。
- `restore-startup` 終了後、live が起動時 root_hash に戻り、終了後データもアーカイブに残る。
- session-start 作成失敗、未公開解放失敗、復元失敗の各フォールト注入からの recover。

### Regression

- `doctor` / `status` / `recover` / `restore --drill` / `snapshot` / `preserve`。
- bootstrap / enroll / shortcut create-only。
- DPYes 起動、プロセス監視、終了後スナップショット（`publish` 経路）。
- 既存 `archive/*` / `milestone/*` タグの表示と restore inspection。

## 10. 維持する安全境界

- 選択・承認前に live、remote main、state、lock を変更しない。
- 三者比較（live / remote / baseline）と fail-closed 判定を維持する。
- ff-only、create-only、archive-before-mutation を維持する。
- opaque `candidate_id` と短命 catalog token を維持し、外部入力から ref を組み立てない。
- 監査ログに追加してよいのは時刻と status のみ。hash、パス、キャラクター名、
  remote URL は追加しない。
- ゲーム実行中の catalog / restore / promote は従来どおり拒否する。

## 11. ワーカーへの指示

- T-A を飛ばして T-B から始めないこと。原因未特定のまま表示機能を入れると、
  同じ不可視事象が「表示上は正常」に化ける。
- 既存の未追跡・未コミット変更を削除、reset、checkout しないこと。
- 実セーブと private remote を使う検証は、unit / integration 合格後に、
  明示承認を得てから行うこと。
- 実 remote main を変更する前に、対象 head の managed bookmark を作成して remote 検証すること。
- UI や設定の都合で既存の fail-closed 契約を弱めないこと。
- 完了報告には、変更ファイル、T-A の判定結果、テスト結果、実機確認の有無、
  remote 変更の有無、残課題を含めること。

## 12. 完成判定

1. 起動時に「リモート確認日時」「セーブ作成日時」「コミット日時」が別々に表示される。
2. リモート未確認・取得失敗が明示的に警告される。
3. 選択画面を `always` で毎回表示できる。
4. 起動時のローカル元データが session-start snapshot として自動保全され、
   選択画面から明示的に選べる。
5. 終了時に publish / local-only / restore-startup を選べ、
   後者2つで remote main が変化しない。
6. T-A で特定した不可視事象が再現しない、または再現条件が既知で対処済みである。
7. 最終状態に lock、recovery phase、Vault 差分が残らない。
