# セーブ選択・承認付き起動 実装計画

作成日: 2026-08-01
状態: 実装待ち
対象: `grim_dawn_sync` / Windows 2端末運用

## 1. 背景と目的

現行の `launch` は、Vault の remote head を機械的な最新版として fast-forward し、
3-way 判定が `apply` ならユーザー確認なしで live save へ適用する。
Git 上の時刻順と、ユーザーが意図する「ゲーム進行上の正しい最新版」が一致しない場合、
別用途で保存したセーブや意図的なロールバックを区別できない。

本変更の目的は、ゲーム起動前に利用可能なセーブ候補を比較し、必要な場合だけユーザーが
使用版を選択・承認できるようにすることである。過去版を選ぶ操作も正式なワークフローにし、
選ばれなかったデータを名前付き remote 保管版として残す。

成功条件:

- 同一内容なら従来同様、余計な操作なしで起動できる。
- 内容が異なる場合は、live / remote / 履歴 / 名前付き保管版を比較して選べる。
- 選択または承認前に live、remote main、同期 state、lock を変更しない。
- 過去版または local 版を採用するとき、押し出される remote head を取得可能な名前付き版として保管する。
- 選択中に live または remote が変化した場合、古い判断を実行せず再選択を要求する。
- GUI を使わない CLI、テスト、復旧 runbook にも同じ安全規則を適用する。

## 2. 非目標

- Steam Cloud の再有効化や Cloud save の自動削除。
- セーブ内容の編集、アイテム移動、キャラクター改変。
- Git 履歴や名前付き保管版の自動削除。
- 初期版でのクエスト進行解析やアイテム一覧の完全表示。
- 任意 Git ref、任意ファイルパス、任意 commit を GUI 入力欄から直接受け付けること。
- GUI で recovery、bootstrap、enroll の安全境界を迂回すること。

## 3. UX 方針

### 3.1 画面を表示する条件

既定ポリシーは `when_different` とする。

| 状態 | 動作 |
|---|---|
| live と remote head が同一 | 選択画面を省略して通常起動 |
| remote のみ前進 | 選択画面を表示。remote を推奨するが自動適用しない |
| live のみ前進 | 選択画面を表示。live を推奨するが自動公開しない |
| live と remote の両方が前進 | 競合として選択必須。初期選択なし |
| 過去版・名前付き版を明示選択 | 二段階確認を必須化 |
| baseline 不明、recovery 必須、lock 異常 | 選択画面へ進まず既存の安全エラーを表示 |

将来設定として `always` と `when_different` は許可してよいが、`never` や無条件 remote 適用は
通常ショートカットから選べないようにする。

### 3.2 候補一覧

最低限、次を表示する。

- `この端末の現在データ`（live）
- `同期先の最新版`（remote head）
- remote main の直近 10 スナップショット
- managed bookmark として保存された名前付き保管版
- 既存の `archive/*`、`milestone/*` annotated tag（legacy read-only 表示）

各候補の表示項目:

- 表示名と種別
- 公開日時または live の走査日時（ローカルタイムも併記）
- 公開端末 ID
- キャラクター数
- ファイル数と合計サイズ
- live / baseline / remote との関係
- 追加・削除・変更ファイル数
- manifest の安全なパスから導出できるキャラクターディレクトリ名
- 名前付き版のメモ

root hash、commit OID、ローカルパス、remote URL、session ID は通常 UI に表示しない。
診断用の「詳細をコピー」にも既存の allow-list 方針を適用する。

### 3.3 主操作

- `このデータでゲームを起動`
- `このデータを最新版にして終了`（ゲームを起動しない明示的ロールバック）
- `現在のデータを名前を付けて保管`
- `再読み込み`
- `キャンセル`

過去版または remote head 以外を選んだ場合は、確認画面に次を明記する。

1. live がどの候補へ置換されるか。
2. 現在の remote head が自動保管されること。
3. ゲーム起動モードでは、正常終了後のセーブが新しい remote head になること。
4. `最新版にして終了` では、選択版と同内容の新規 commit が remote main になること。

ウィンドウを閉じる、Esc、キャンセルはすべて `cancelled` とし、無変更で終了する。

## 4. アーキテクチャ

GUI を `LaunchWorkflow` に直接埋め込まない。次の4層に分ける。

```text
VersionCatalog（読み取り専用候補作成）
  -> SelectionPolicy / SelectionPresenter（判断と表示）
  -> SelectionPlan（実行前の不変な計画）
  -> LaunchWorkflow / PromoteWorkflow（既存の安全な変更処理）
```

### 4.1 VersionCatalog

新規候補: `src/grim_dawn_sync/version_catalog.py`

責務:

- live manifest を安定走査する。
- fetch 後の remote main 履歴を上限付きで列挙する。
- 各 commit の `.sync/manifest.json` と `.sync/vault.json` を検証する。
- managed bookmark / legacy annotated tag を安全に列挙する。
- 候補間の manifest 差分サマリーを計算する。
- UI が commit/ref/path を直接扱わない opaque `candidate_id` を発行する。

候補モデル例:

```python
@dataclass(frozen=True)
class SaveCandidate:
    candidate_id: str
    kind: Literal["live", "remote_head", "history", "bookmark"]
    display_name: str
    created_at: str
    machine_id: str
    root_hash: str
    commit: str | None
    character_count: int
    file_count: int
    total_bytes: int
    character_labels: tuple[str, ...]
    note: str | None
```

`candidate_id` はその1回の catalog 内だけで有効とし、外部入力から Git ref を組み立てない。

### 4.2 SelectionPlan と競合再検証

新規候補: `src/grim_dawn_sync/selection.py`

`SelectionPlan` は最低限次を固定する。

- 選択候補の commit / root hash
- catalog 作成時の remote head OID
- catalog 作成時の live root hash
- 選択モード（launch / promote-only）
- remote head の保管要否
- ユーザー指定の表示名とメモ

ユーザーが承認してから最初の変更を行う直前に、remote head と live manifest を再取得する。
どちらかが plan と異なれば `selection_stale` で中止し、画面を再読み込みする。

選択後の変更順序:

```text
再検証
  -> remote session lock取得
  -> 押し出されるremote headをmanaged bookmarkとして保管・remote検証
  -> 現在liveを検証済みローカルarchiveへ退避
  -> 選択版をliveへ原子的適用
  -> launch または promote-only snapshot
  -> push
  -> lock解放
```

途中失敗は既存の recovery handoff 契約へ統合し、lock取得後の失敗を単なる UI エラーとして扱わない。

### 4.3 Managed bookmark

新規候補: `src/grim_dawn_sync/bookmarks.py`

名前付き保管版は annotated tag を使う。ただしユーザー文字列を ref 名へ直接入れない。

```text
refs/tags/grim-dawn-save-<UUID>
```

annotation は versioned JSON とする。

```json
{
  "schema_version": "1.0.0",
  "kind": "grim_dawn_save_bookmark",
  "display_name": "影の心臓 素材保管用",
  "note": "必要時にこの版から取り出す",
  "created_at": "2026-08-01T00:00:00Z",
  "created_by": "melofla"
}
```

規則:

- display name は必須、1～80文字。
- note は任意、最大500文字。
- 制御文字、NUL、改行の異常形、無効 Unicode を拒否する。
- UI ではplain textとして扱い、markupやコマンドとして解釈しない。
- tag はcreate-only。上書き、移動、自動削除をしない。
- push 後に peeled target が期待 commit と一致することをremoteで確認する。
- 通常 fetch の tag 挙動に依存せず、列挙は制限付き `ls-remote` と検証済み取得を使う。
- 現在存在する `archive/*` と `milestone/*` はlegacy bookmarkとして読めるが、移動・改名しない。

### 4.4 GUI adapter

新規候補: `src/grim_dawn_sync/selector_ui.py`

- Python 標準の `tkinter` / `ttk` を第一候補とし、追加依存を増やさない。
- GUIは `SelectionPresenter` protocol のadapterとし、ドメインテストではfake presenterを使う。
- 候補走査はworker thread、UI更新はTk main threadで行い、画面を固めない。
- Tk初期化失敗時は自動選択せず `selection_ui_unavailable` でfail closed。
- 選択肢の色だけに意味を持たせず、推奨理由と警告文を文字で表示する。
- 破壊的操作ボタンは通常起動ボタンと離し、二段階確認する。

## 5. CLI とショートカット契約

既存ショートカットは引数なしの `launch` を呼ぶため、移行時にショートカットを書き換えなくても
対話UIへ入れる構成を優先する。

提案CLI:

```text
grim-dawn-sync launch
grim-dawn-sync --json launch --select auto-safe
grim-dawn-sync --json launch --select <candidate-id> --catalog-token <token>
grim-dawn-sync versions --json
grim-dawn-sync bookmark --candidate <candidate-id> --name <name> --note <note> --apply
grim-dawn-sync promote --candidate <candidate-id> --catalog-token <token> --apply
```

契約:

- 非JSON `launch`: `when_different` ポリシーでGUIを表示する。
- `--json launch` は差分があるとき暗黙選択をせず、`selection_required` を返す。
- `auto-safe` は内容同一のnoop起動だけを許可する。
- candidate ID と catalog token は短命で、remote/live再検証なしには実行できない。
- CIやrunbookで任意 commit を渡して承認を迂回できないようにする。
- `restore --commit` は管理者向け既存操作として残すが、通常ショートカットからは呼ばない。

JSON出力には表示用メモやキャラクター名を既定で含めない。必要な場合は明示フラグとし、
監査ログには引き続き記録しない。

## 6. Workflow の変更方針

現在の `LaunchWorkflow._reconcile_three_way()` は判定と適用方針を同時に決めている。
これを次へ分解する。

1. `classify_reconciliation(...) -> ReconcileCase`
2. `build_selection_plan(case, catalog, choice) -> SelectionPlan`
3. `execute_selection_plan(plan)`
4. 既存のゲーム起動・終了後snapshot処理

新しい `WorkflowState` 候補:

```text
BUILD_CATALOG
WAIT_SELECTION
REVALIDATE_SELECTION
BOOKMARK_DISPLACED_REMOTE
PREPARE_SELECTED_RESTORE
PROMOTE_SELECTED_SAVE
```

`WAIT_SELECTION` までは `self.mutated == False` を不変条件にする。
GUIキャンセルや `selection_stale` は recovery required にしない。
lock取得後の bookmark / restore / push 失敗は既存同様 recovery required とする。

## 7. 実装チケット

### T0: 契約固定とfixture

作業:

- 本計画のdecision tableをテストfixture化する。
- live / remote / baseline の同一、片側前進、両側前進、baseline欠落を列挙する。
- managed bookmark schema と入力制限を固定する。
- GUI文言と「最新版」の意味（remote main）を用語集へ追加する。

DoD:

- すべての状態に、画面表示要否、初期選択、許可操作、保管要否が定義されている。
- 曖昧な自動選択ケースが残っていない。

### T1: 読み取り専用 VersionCatalog

作業:

- `GitVault` に上限付き履歴列挙APIを追加する。
- commitのmanifest / vault metadataを検証して候補へ変換する。
- live候補と候補間diff summaryを実装する。
- legacy annotated tagを安全に列挙する。

DoD:

- catalog作成中にcheckout、restore、commit、push、tag作成、state更新を行わない。
- malformed commit/tagはfail closedまたは明示的unavailable候補になる。
- 10件上限、重複root hashの表示統合、時刻順がテストされている。

### T2: Managed bookmark API

作業:

- create-only annotated tag生成、push、remote target検証を実装する。
- JSON annotation validationを実装する。
- `versions` と `bookmark` CLIを追加する。

DoD:

- ref injection、既存tag上書き、長大/不正メモを拒否する。
- push結果不明時は成功扱いせず、再照合可能なエラーを返す。
- bookmarkから `restore` inspectionが成功する統合テストがある。

### T3: SelectionPolicy / SelectionPlan

作業:

- decision tableを純粋関数として実装する。
- opaque candidate ID / catalog tokenを実装する。
- live root と remote head の再検証を実装する。

DoD:

- stale planは最初の変更前に必ず拒否される。
- cancelはログ以外の書込みゼロ。
- history/bookmark選択は二段階承認なしに実行できない。

### T4: Workflow統合

作業:

- `LaunchWorkflow` を選択前/選択後へ分割する。
- remote head自動bookmark、live archive、選択版restoreをlock配下へ統合する。
- `promote-only` workflowを追加する。
- recovery stateとaudit eventを追加する。

DoD:

- 押し出されるremote headのbookmark検証前にmainを更新しない。
- live archive完成前にliveを変更しない。
- 選択版と公開後remote manifestのroot hashが一致する。
- 全post-lock fault injectionがrecover可能、または安全にrollbackされる。

### T5: Tk選択UI

作業:

- 一覧、比較詳細、メモ入力、確認、キャンセルを実装する。
- fake presenterでUI非依存テストを作る。
- Windows実機向けsmoke test driverを追加する。

DoD:

- キーボードのみで操作可能。
- window close/Escが無変更キャンセルになる。
- 過去版のpromoteボタンは初期focusにならない。
- UI threadを長時間I/Oで停止させない。

### T6: CLI・既存ショートカット・runbook移行

作業:

- 非JSON `launch` を対話入口へ接続する。
- JSON automationを`auto-safe`既定へ変更する。
- shortcut payloadとcreate-only契約の互換性を確認する。
- 端末A/B、roundtrip、diagnose、restore runbookを更新する。

DoD:

- 既存ショートカットから新UIが起動するか、検証済み原子的更新手順がある。
- 古いソースcheckoutを参照するショートカットを検出し、黙って旧workflowを実行しない。
- headless runbookが選択必須状態を自動承認しない。

### T7: 2端末受入試験と段階展開

順序:

1. fake Vaultによる全decision table試験。
2. 隔離した一時Vaultと偽saveでWindows UI smoke test。
3. 端末Bだけへ導入し、同一内容の通常起動を確認。
4. 意図的にremoteのみ前進させ、選択・キャンセル・再読込を確認。
5. 名前付き保管版を作り、隔離restore drillで取得可能性を確認。
6. 過去版をpromoteし、旧headの自動bookmarkと新mainを確認。
7. 端末Aへ導入。
8. A -> B -> A roundtripを実施。

DoD:

- どの試験でも未選択データが失われない。
- 旧remote headは名前付き版またはGit祖先として取得可能。
- 最終状態が `ready`、`vault_relation=equal`、lockなし、recoveryなし。
- 両端末のショートカットが同一機能版を参照している。

## 8. 必須テスト一覧

### Unit

- decision table全分岐。
- candidate dedup、並び順、履歴上限。
- manifest差分（追加、削除、変更、character dir差分）。
- bookmark JSONの正常・境界・不正Unicode・ref injection。
- catalog token改ざん、candidate ID不一致、期限切れ。
- GUI cancel / close / stale refresh。

### Integration

- remoteのみ前進を選択してapply。
- liveのみ前進を選択し、旧remoteをbookmark後にpublish。
- 両側前進でどちらを選んでも敗者が保管される。
- history/bookmarkを選び、live archive後にrestore。
- promote-onlyでゲームを起動せずremote main更新。
- 選択中のremote更新、live更新、lock出現を検出して中止。
- bookmark push失敗、restore失敗、snapshot push失敗、lock解放失敗からrecover。
- 既存 `archive/*` / `milestone/*` タグの表示とrestore inspection。

### Regression

- `doctor`、`status`、`recover`、`restore --drill`、`snapshot`、`preserve`。
- bootstrap / enroll。
- shortcut create-only、安全なパス検証、引数エスケープ。
- DPYes起動、ゲームプロセス監視、終了後snapshot。
- save容量のみ減少する正常rewrite。

## 9. セキュリティ・安全性チェック

- remote由来のtag名、注釈、manifest pathをUI markupやコマンドとして評価しない。
- Git呼出しは固定subcommandと検証済み引数のみ。shell文字列連結をしない。
- commitはremote mainの祖先または検証済みbookmark targetに限定する。
- bookmark作成以外の任意ref pushをUIから許可しない。
- 選択前にlockを残さない。
- game実行中はcatalog/restore/promoteを拒否する。
- current live、選択元、archive、公開後remoteのroot hashを各境界で照合する。
- キャラクター名・パス・remote URL・hashを監査ログへ追加しない。

## 10. ワーカーへの実装指示

- T0から順に小さいcommitへ分けること。T4とT5を同一commitにしない。
- 各チケット完了時に対象テストと既存save-sync全テストを実行すること。
- 実セーブやprivate Vaultを使う試験は、unit/integration合格後のT7だけに限定すること。
- 実remote mainを変更する前に、対象headのmanaged bookmarkを作成してremote検証すること。
- 既存の未追跡・未コミット変更を削除、reset、checkoutしないこと。
- UIの都合で既存のfail-closed、ff-only、create-only、archive-before-mutation契約を弱めないこと。
- 完了報告には、変更ファイル、decision table結果、テスト結果、実機確認、remote変更有無、残課題を含めること。

## 11. 完成判定

次をすべて満たしたとき完成とする。

1. 通常ショートカット起動時、差分がある場合は選択・承認なしにliveを上書きしない。
2. 差分がない場合は従来の1操作起動を維持する。
3. live、remote head、履歴、名前付き版を比較して選べる。
4. 過去版を選び、旧liveと旧remoteを保管した上で最新版へpromoteできる。
5. 選択中の競合更新を検知し、古いplanを実行しない。
6. キャンセルが完全に無変更である。
7. remote保管版が実際のrestore inspectionに合格する。
8. A -> B -> Aの実機roundtripが成功する。
9. 最終状態にlock、recovery phase、Vault差分が残らない。
