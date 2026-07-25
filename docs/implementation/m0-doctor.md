# M0: インストール診断とdataset manifest生成

## 目的

所有ゲームの入力境界を読み取り専用で診断し、後続の抽出処理が参照できる版付きmanifestを生成する。

## 対象範囲

- 明示パス、またはWindows上のSteam標準配置からGrim Dawnを検出する。
- Base、GDX1、GDX2、GDX3、英語・日本語localizationの存在を確認する。Base以外の拡張層は optional として manifest に absent/present を記録する。
- 各入力の相対パス、サイズ、UTC更新日時、SHA-256をJSONへ記録する。
- channelは明示指定がない限り `unknown` とする。

ARZ/ARC内容の抽出、セーブ解析、Grim Toolsへのアクセスは対象外とする。

## 入出力契約

```powershell
$env:PYTHONPATH = "src"
python -m grim_dawn_lab doctor --install-path "C:\Program Files (x86)\Steam\steamapps\common\Grim Dawn"
```

出力は `schemas/dataset-manifest.schema.json` に従うJSONである。不足ファイルや未検出は `warnings` に機械可読なcodeとして記録する。`--channel stable` または `--channel public_test` はユーザーが根拠を持って指定するときだけ使用する。

## 受け入れ条件と検証

- 合成fixtureでゲーム非依存テストが通る。
- 診断前後で入力のサイズ、更新日時、SHA-256が一致する。
- 所有ゲームでBase/GDX1/GDX2/GDX3とlocalizationのmanifestを生成できる。GDX3未所持でも診断は成功し、`gdx3: absent` を記録する。

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
```

## 既知リスク

- Steamの追加ライブラリはまだ自動探索しないため、標準配置以外は `--install-path` が必要である。
- file hashは大きな入力を全走査するため、ストレージ速度に応じた時間を要する。
- channelとゲームバージョンはファイル配置から推定しない。

## 次の一手

M0の次チケットで、出力先の衝突回避を含む版付きdatasetディレクトリ初期化と、Steam追加ライブラリ検出を検討する。
