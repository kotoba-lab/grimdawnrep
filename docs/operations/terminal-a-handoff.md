# 端末Aハンドオフ

端末Aでは、公開ソースリポジトリを任意の作業場所へ clone してから、同梱の
`ops/terminal-a-save-sync.ps1` を実行する。Vault のURLはこの文書・公開ソース・
設定テンプレートへ記録しない。資格情報をURLに埋め込まず、Git の通常の認証を使う。

```powershell
git clone https://github.com/kotoba-lab/grimdawnrep.git C:\work\grimdawnrep
Set-Location C:\work\grimdawnrep
git pull --ff-only

# まず診断のみ（書込みなし）
.\ops\terminal-a-save-sync.ps1 -VaultRemoteUrl '<PRIVATE_VAULT_URL>' -CloudDisabledConfirmed

# ツール、端末ローカル設定、Vault clone のみ作成する
.\ops\terminal-a-save-sync.ps1 -VaultRemoteUrl '<PRIVATE_VAULT_URL>' -CloudDisabledConfirmed -ApplySetup

# doctor と enroll dry-run を通過した場合だけ、新端末の live save を enroll する
.\ops\terminal-a-save-sync.ps1 -VaultRemoteUrl '<PRIVATE_VAULT_URL>' -CloudDisabledConfirmed -ApplyEnroll
```

`<PRIVATE_VAULT_URL>` は実際のprivate Vault URLへ置換する。URLへトークン・パスワードを
書かない。PowerShell の最後の `TERMINAL_A_HANDOFF` JSON一行だけを端末Bへ返す。
通常出力にはセーブ名、パス、hash、Vault URLを含めない。

既定のmachine IDは `desktop-a` である。同じホスト名を使う端末Bの `melofla` とは別の
同期端末として扱うためであり、`-MachineId melofla` は拒否される。既存の
`config.local.json` は上書きされない。既存Vaultはcleanで、`origin`が渡したURLと一致
するときだけ利用する。

このスクリプトは Grim Dawn / DPYes の起動、bootstrap、push、snapshot、restoreを行わない。
Steam Cloudを無効化済みであることを `-CloudDisabledConfirmed` で明示し、両プロセス停止、
Python 3.11以上、ゲームとDPYesの検出、公開ソースのcleanかつcurrentを確認してから進む。
