# DPYes併用・2端末ローカルセーブ同期 最終報告

報告日: 2026-07-30
対応計画: `docs/implementation/save-sync-dpyes-plan.md` §16
状態: 完了

## 結論

計画の T0–T7 と Definition of Done を満たした。2端末の通常運用は、専用
Save Sync 起動経路から同期確認、DPYes 起動、Grim Dawn 終了監視、検証済み
snapshot の private Vault への push までを一連で行う。競合、remote 不通、
lock、検証失敗時は fail-closed であり、DPYes を起動しない。

実環境の Vault remote は private の `kotoba-lab/grim-dawn-save-vault` である。
端末 A（`desktop-a`）の最新 live save を保全し、authoritative snapshot として
push 済みである。端末 A → 端末 B（`melofla`）→ 端末 A の DPYes を伴う実往復を
完了し、A 復帰後の read-only 検証も完了した。過去 commit の restore drill は、
live save を変更しない tool-owned の隔離先への materialization として成功した。

## 完了したチケットと実装

| Ticket | 完了内容 |
| --- | --- |
| T0 | `grim_dawn_sync` CLI、端末ローカル設定・状態、JSON 出力、終了コードを追加。 |
| T1 | live/Cloud/Vault/DPYes の検出、決定的 manifest、`player.gdc` 検証、破壊的変更 guard を実装。 |
| T2 | 検証済み archive、staging、atomic な restore と rollback、dry-run/`--apply` 境界を実装。 |
| T3 | ff-only Vault 同期、snapshot commit/push、履歴読取りを実装。force push と自動 rebase は使用しない。 |
| T4 | remote 固定 tag による排他 lock、比較付き解除、recover を実装。 |
| T5 | DPYes を作業ディレクトリ付きで起動し、新規 Grim Dawn PID を監視する adapter を実装。DPYes 自身の終了はゲーム終了条件にしない。 |
| T6 | `launch` 状態機械、機械可読 log、既存 DPYes shortcut と共存する create-only shortcut を実装。 |
| T7 | private Vault、両端末設定、Cloud からの移行、authoritative snapshot、A→B→A 実往復、隔離 restore drill を実施。 |

主な追加・変更ファイルは次のとおりである。

- 実装: `src/grim_dawn_sync/` 配下の CLI、設定、検出、Git Vault、launcher、manifest、
  process monitor、remote lock、shortcut、snapshot、state、validation、workflow。
- schema: Save Sync の設定・状態・出力に対応する schema。
- テスト: `tests/test_save_sync_*.py` と端末 handoff script のテスト。
- 運用手順: `docs/operations/` 配下の enrollment、live 保全、authoritative snapshot、
  recover、A→B→A 往復、restore drill、shortcut 導入手順。
- shortcut 最終修正: `src/grim_dawn_sync/shortcut.py`、`src/grim_dawn_sync/cli.py`、
  `docs/operations/install-save-sync-shortcut.md`、`docs/operations/terminal-a-agent-enroll.md`
  および対応する shortcut/CLI/workflow テスト。

実セーブ、認証情報、端末ローカル設定、save の hash・内容はソースリポジトリへ追加していない。

## Git lock と競合安全性

排他 lock は remote の `refs/tags/grim-dawn-sync-active` を用いる annotated tag である。
lock 本文には session、machine、base commit を記録し、取得後に再検査する。解除は保持中の
lock object ID と一致する場合だけ比較付きで行う。

Git integration tests は local bare remote と2 clone を使用して、同時取得時に勝者が一方だけに
なること、別 session の解除拒否、push 失敗後の local commit/lock 保持、recover、diverged 時の
自動 rebase/force 不使用、過去 tree の安全な restore を検証した。

## 実環境展開（T7）で変更したもの

この最終段階では、計画 §15 の通常の実装作業禁止を越える操作を T7 として明示承認の上で行った。
従って「実セーブ、実ゲーム、実remote を変更したか」は **Yes** である。

- private Vault remote に初期・以後の検証済み snapshot を commit/push した。
- 端末 A の最新 live save を、事前保全 archive を伴って authoritative snapshot として Vault に反映した。
- 両端末で local save 運用を展開し、DPYes 経由で Grim Dawn を起動・正常終了した。
- A→B→A の各 leg で protected `launch` が必要な restore、lock、archive、検証、push、lock 解放を実行した。
- historical restore は `restore --commit … --drill --apply` に限り、隔離先へ materialize した。live save、Vault 履歴、remote、lock、同期 state は変更していない。

両端末の Steam Cloud は無効化済みである。ただし端末 B に残る旧 Cloud save は、明示的に削除する判断と操作が行われるまで保持する。同期ツールは Cloud データの削除を自動化しない。

## T7 の確定設定と通常手順

| 項目 | 確定値 |
| --- | --- |
| Vault remote | private の `https://github.com/kotoba-lab/grim-dawn-save-vault.git` |
| 端末 A | machine ID `desktop-a`、Steam Cloud 無効 |
| 端末 B | machine ID `melofla`、Steam Cloud 無効 |
| authoritative data | 端末 A の先行していた最新 live save を Vault へ反映済み |
| 通常起動 | 両端末とも新設の Save Sync shortcut を使用 |
| legacy shortcut | 既存 DPYes shortcut は変更せず保持 |
| 旧 Cloud data | 端末 B で明示削除の指示があるまで保持 |

各端末ではゲームと DPYes が終了していることを確認してから Save Sync shortcut を起動する。
shortcut は端末ローカル設定を明示して `launch` を呼び、remote の ff-only 同期、整合確認、
排他 lock、必要な restore、DPYes 起動、Grim Dawn 終了監視、archive、snapshot の検証と
push、lock 解放を順に行う。失敗時は迂回起動せず、対応する `status`、`doctor`、`recover`
手順を使う。過去版の確認には live restore ではなく、隔離先だけを使用する
`restore --commit … --drill --apply` を使う。

## 検証結果

- 全 Python tests（shortcut の COM staging 修正後）: **430 passed, 5 skipped**。
- release audit: **safe=true / 126 checked paths / 0 violations**。
- git diff check: **pass**。
- 実端末受入: A authoritative snapshot push、A→B→A DPYes 往復、A 復帰の read-only 検証、隔離 historical restore drill を完了。
- shortcut 修正 commit: `25b3f35`（self-contained shortcut）と `7533f02`
  （Windows COM staging 修正）。source HEAD `7533f02` は `origin/master` へ push 済み。

## shortcut の端末受入

新しい Save Sync shortcut は create-only で導入し、PATH、`PYTHONPATH`、未導入の console
script に依存しない。`TargetPath` は導入に使用した Python runtime、`Arguments` は端末固有の
source と設定を安全に復元して `grim_dawn_sync.cli.main(... launch ...)` を呼ぶ自己完結した
embedded 経路、`WorkingDirectory` は検証済みの source directory とした。

端末 A（`desktop-a`）と端末 B（`melofla`）の双方で導入後の `.lnk` を Windows COM から
再読込し、`TargetPath`、`Arguments`、`WorkingDirectory` が計画値と一致することを確認した。
両端末の受入 sentinel は `shortcut_installed_verified` で完了した。既存 legacy shortcut は
導入前後で hash、length、mtime がすべて不変である。

shortcut 自体のクリックによる新しいゲーム session は、最終導入確認では開始していない。
ただし shortcut が呼ぶものと同じ embedded `launch` 経路は、端末 A → B → A の実 DPYes
roundtrip で Grim Dawn の起動・終了、同期、snapshot push、復帰後検証まで完了している。
従って、`.lnk` の COM 再読込と実 launch 経路の検証を分離しており、shortcut をクリックした
との誤った記録はしていない。

## 通常運用上の留意

通常起動は Save Sync shortcut のみを使う。起動前の remote 接続、整合、lock のいずれかが失敗した
場合は、手動で DPYes や Grim Dawn を起動して同期を迂回しない。異常終了、push 失敗、lock 解放失敗は
`status` と `recover` の運用手順に従う。live save を更新せず過去版を確認したい場合は、live restore
ではなく隔離 restore drill を使用する。
