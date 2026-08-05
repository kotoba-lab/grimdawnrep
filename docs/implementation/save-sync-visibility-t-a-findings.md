# T-A 不可視事象 原因特定 結果

対象: 2026-08-05 運用における「05:48 のリモートセーブが候補に現れなかった」事象。

## 結論（先出し）

- **(a) 重複統合による表示上の消失: 確定。**
- (b) fetch の非 fast-forward 拒否: **除外**
- (c) `history_limit=10` による範囲外: **除外**
- (d) reload 経路での catalog 再利用: **除外**

原因コードパス: `src/grim_dawn_sync/version_catalog.py:116` の `_coalesce()`
（および代表選出ロジック `_REPRESENTATIVE_PRIORITY` / `values.index(item)` タイブレーク、同ファイル
111-132行目）。

## 検証根拠

### (a) 確定 — 実データでの検証

Vault（読み取り専用コマンドのみ使用: `git log`, `git show`, `git reflog`, `git ls-remote`,
`git fetch --dry-run`）を確認したところ、`refs/remotes/origin/main` 直近2コミットのうち
以下2件が **同一 `root_hash`** を持つことを確認した。

| commit (先頭12桁) | committer date (JST) | manifest created_at (UTC) | root_hash (先頭16桁) | machine_id |
|---|---|---|---|---|
| `fc61d8acb759` | 2026-08-05T11:39:47+09:00 | 2026-08-05T02:39:24Z | `4cf894dff2736dbf` | desktop-a |
| `f0b32b72c8cb` | 2026-08-05T05:48:36+09:00 | 2026-08-04T20:47:51Z | `4cf894dff2736dbf` | melofla |

`f0b32b72c8cb` の committer date（JST 05:48:36）は、事象報告にある「05:48:36」と一致する。
この2コミットは `remote_history()` の列挙順で共に `kind="history"`（`remote_head` はさらに新しい
別 root の `5baef38fccb7` が占めている）となり、`_coalesce()` の
`root_hash` グルーピングで同一グループに入る。

`_coalesce()` の代表選出は `(_REPRESENTATIVE_PRIORITY[item.kind], values.index(item))` で
決まるため、`kind` が同一（history）の場合は **列挙順で先に candidates リストに追加された方**
（`remote_history()` が返す新しい順で先に来る `fc61d8acb759`）が代表になり、
`f0b32b72c8cb` の provenance（display_name・created_at・commit OID）は
`CandidateAlias` として `aliases` タプルに格納される。

ここで重要な補正がある。`selector_ui.py:175-180` は
`selectable = [representative, *aliases...]` の形で **alias も 1 行ずつ Treeview に展開している**。
したがって「行そのものが消えていた」わけではない。実際に起きていたのは次である。

- 統合された 2 行はどちらも `display_name` が `"Remote main snapshot"`（`version_catalog.py:188`）。
- `file_count` 列・diff 列は同一 root なので一致する。
- **commit の committer date（05:48:36）を表示する列は、一覧にも詳細ペインにも存在しない。**
  当時読めたのは `manifest["created_at"]`（セーブ走査時刻）だけであり、
  利用者が探していた「05:48 のコミット」という識別子は UI のどこにも出ていなかった。

なお `root_hash` はファイルのパス・サイズ・sha256 のみから計算され `created_at` を含まない。
したがって同一 `root_hash` の 2 コミットでも `created_at` は一致するとは限らない。
実際 2026-08-05 の実 Vault では、統合された 2 件の `created_at` は
02:39:24Z と 20:47:51Z（前日）で**異なっていた**。
「統合により created_at が同一値に潰れる」という説明は誤りなので採らない。

→ 仮説(a)の機構（同一 `root_hash` の複数 provenance が 1 グループへ統合され、
代表以外が `aliases` に落ちる）は実データで確認できた。
ただし失敗様態は「行の消失」ではなく
**「provenance を識別する情報、とりわけ commit 日時と commit OID が一切描画されていない」**である。
T-B の DoD は「行が出ること」ではなく
**「統合された各 provenance が互いに識別できること」**を判定基準とする。

### (b) 除外 — fetch 非FF拒否

`git_vault.py:600-601` の `fetch()` は
`fetch --no-tags <remote> refs/heads/<branch>:refs/remotes/<remote>/<branch>` で、
`+` プレフィックスも `--force` も付かない非強制 refspec である（理論上は非FF更新を拒否しうる）。

しかし `refs/remotes/origin/main` の reflog を確認したところ、直近20件すべてが
`update by push` または `fetch ...: fast-forward` であり、拒否や巻き戻りの記録は無い。
また今回の対話的 `git fetch --dry-run -v origin` 実行結果も `[up to date]` で、
現在のローカル追跡refはリモート実体（`git ls-remote origin refs/heads/main` の結果と同一OID）
と完全一致していた。

→ 今回の事象発生時間帯（実際に不可視になった候補が生じた時間帯）に非FF拒否が起きた形跡はない。
このリポジトリの運用（単一ライン上の直列コミット）では非FF事象自体が起きていないため、
仮説(b)は本件の原因としては除外する。ただし別リポジトリ運用で強制 push や巻き戻りが
発生した場合の潜在リスクとしては残る（別途注記に値するが、今回の事象の説明にはならない）。

### (c) 除外 — history_limit 範囲外

`f0b32b72c8cb` は `remote_history(limit=10)` の列挙で新しい方から2番目
（`5baef38fccb7` が1番目 = remote_head）であり、10件の上限に対して十分に範囲内。
bookmark・legacy タグを加えても対象コミットが除外される状況ではない。

→ 仮説(c)は除外。

### (d) 除外 — reload 経路の catalog 再利用（静的確認）

`cli.py:513` `_execute_selection_command()` の `while True:` ループを追跡した。

- `request.action == "reload"` のとき `selected = None; catalog_token = None; continue`
  （`cli.py:581` 付近）でループ先頭に戻る。
- ループ先頭では `interactive = not as_json and selected is None` が真になるため、
  再度 `ui.present_builder(build_in_worker, directive_after_scan)` が呼ばれる
  （`cli.py:528-548`）。
- `build_in_worker()` は毎回 `_fresh_catalog(config_path, config)` を新規呼び出しする
  （`cli.py:534`）。
- `selector_ui.py:210-221` の `reload_only()` は現在の選択ウィンドウを
  `action="reload"` の `SelectionRequest` で閉じるだけで、catalog を保持しない。
- `present_tk_from_builder`（`selector_ui.py:131` 以降）は毎回新規ウィンドウを開き、
  `load_catalog_in_worker`（`selector_ui.py:101`）が渡された `build` 引数を
  新規スレッドで呼び直す。過去の `VersionCatalog` を再利用するキャッシュ経路は無い。

→ 静的読解の範囲では、Reload は毎回 `vault.fetch()` を含む完全な catalog 再構築を行っており、
古い catalog を使い回す経路は見つからなかった。fake を使った pytest 再現は今回不要と判断した
（コード上の分岐が単純で、モック無しでも到達経路が一意に追えたため）。仮説(d)は除外。

## launch.jsonl から分かったこと（補助情報）

- `C:\Users\melof\AppData\Local\GrimDawnSaveSync\logs\launch.jsonl` には
  08-05 02:32〜03:11 の2セッション分の起動ログしかなく、これ以降 08-05 中の
  追加エントリは存在しない。
- 起動ワークフロー（`launch` コマンドの state machine）のログには "reload" という
  event 種別は存在せず、選択画面内の Reload ボタン操作は launch.jsonl に記録されない
  設計になっている。そのため「Reload 操作の記録」を launch.jsonl から直接確認することは
  できなかった（**特定できなかった事実**）。Reload の実挙動確認は上記(d)のとおり
  ソースコードの静的追跡で行った。
- 事象報告にあった「候補のセーブ作成 08-05 02:17」という時刻は、上記コミット群の
  committer date / manifest created_at のいずれとも厳密には一致しなかった
  （最も近いのは `fc61d8acb759` の manifest created_at 2026-08-05T02:39:24Z UTC だが、
  20分強のずれがある）。UI 上の表示丸めか、報告時の記憶誤差の可能性があるが、
  これも**特定できなかった事実**として明記する。ただし (a) の機構自体は
  「同一 root_hash の異なる provenance が1行に統合される」という形で実データにより
  確認済みであり、事象の技術的原因説明としてはこれで十分と判断した。

## 再現条件

同一 `root_hash`（＝ライブセーブ内容が変化していない状態）で、異なるタイミング・異なる
`machine_id` から2回以上コミットが積まれた場合、`kind` が同じ（例: どちらも history）である
限り、2 行は同一の `display_name` で並び、**どちらがどのコミットかを UI 上で同定する手段が無い**。
Treeview には両方の行が出るため「候補が消えた」ようには見えないが、
利用者が探している「特定のリモートコミット」を指し示せない点で実害は同じである。

## 未解決として残す点（実機確認 2026-08-05 時点）

事象報告の「候補のセーブ作成 08-05 02:17」は、実 Vault のどの commit の
committer date とも manifest `created_at` とも一致しない。T-B 実装後に実 Vault へ対して
カタログを再構築して確認した結果、当該グループの 2 件は
`save created 11:39 / commit dated 11:39` と `save created 05:47 / commit dated 05:48`
として**個別に読める**状態になっており、§12.6 の「再現条件が既知で対処済み」は満たす。

一方で「02:17」という報告値そのものの出所は特定できていない。
事象当時の Vault 状態（その後 `fc61d8a`・`5baef38` が追加されている）を復元できないため、
当時の画面表示を厳密に再構成することはできなかった。
本ドキュメントは「統合による provenance 不可視化という機構が実在し、対処済みである」ことまでを
確定事実として記録し、報告値との厳密な突き合わせは未解決のまま残す。

## 推奨する修正方針

**T-B に統合する。独立の T-A2 は不要。**

計画書 §3.3 の DoD に明記されている通り、原因が (a) の場合は T-B の
「統合された候補は、統合された全 provenance の kind と日時を詳細ペインに列挙する」
（§4.2）で対応する設計になっており、これをそのまま実装すればよい。(b)(c)(d) はいずれも
今回の事象の原因として確定しなかったため、追加の修正チケットを起こす必要はない。
