# ゆらぎ（yuragi-ime）

ゆらぎは、親指シフト（NICOLA）で日本語を打つための Windows の IME です。

専用の親指シフトキーボードがなくても、ふつうのノート PC で親指シフトが使えます。

## 対象

- キーボード: US （ANSI）配列
- Windows 10 / 11 の 64bit 版

## 既知の制限

- **US 配列（ANSI）専用です。** JIS配列のキーボードでは、通常の親指シフトエミュレータをお使いください。
- インストールでは約 1GB を取得します。空きが 3GB 以上必要です。

## インストール・更新

管理者ではない普段の PowerShell で、次の1行を実行します。

```powershell
iwr -UseBasicParsing https://github.com/sadaoikebe/yuragi-ime/releases/latest/download/setup.ps1 -OutFile $env:TEMP\yuragi-setup.ps1; powershell -ExecutionPolicy Bypass -File $env:TEMP\yuragi-setup.ps1
```

途中で、管理者の確認が1回出ます。アプリは開いたままで構いません。

## アンインストール

同じく PowerShell で、次の1行を実行します。

```powershell
iwr -UseBasicParsing https://github.com/sadaoikebe/yuragi-ime/releases/latest/download/uninstall.ps1 -OutFile $env:TEMP\yuragi-uninstall.ps1; powershell -ExecutionPolicy Bypass -File $env:TEMP\yuragi-uninstall.ps1
```

学習した内容・登録した単語・設定は残ります。

全部消す場合は、次の1行を実行します。

```powershell
iwr -UseBasicParsing https://github.com/sadaoikebe/yuragi-ime/releases/latest/download/uninstall.ps1 -OutFile $env:TEMP\yuragi-uninstall.ps1; powershell -ExecutionPolicy Bypass -File $env:TEMP\yuragi-uninstall.ps1 -RemoveData
```

## 使い始め

1. **Win+Space** で「ゆらぎ」を選びます。
2. タスクバーの右下に、入力モードが出ます。

| 表示 | 意味 |
|---|---|
| ゆ | かな入力 |
| Y | 直接入力（英字がそのまま入る） |
| 薄い ゆ | 準備中。打った文字は、準備ができてから順に入ります |
| ! | 日本語変換が止まっています。右クリックで「もう一度試す」 |

「ゆ」と「Y」は、クリックで切り替わります。キーでは `` Alt+` `` です。

## もっと詳しく

- [使い方](docs/USAGE.md) — キー操作、候補窓、設定ウィンドウ、困ったとき、更新とアンインストール
- [開発と技術文書](docs/DEVELOPMENT.md) — 変換のしくみ、設計書、ビルドと試験

## ライセンス

自前のコードは [MIT](LICENSE) です。インストール時に取得する Mozc の辞書・Python と PyTorch などの部品・言語モデルには、それぞれの条件が付きます。
詳しくは [NOTICE.md](NOTICE.md) を見てください。
