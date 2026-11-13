oyayubi-ime のインストール (設計: docs/INSTALL.md)

持ち込むのは setup.ps1 だけです。本体・辞書・LM・モデルは、
setup.ps1 が対象 PC の側で公式の場所から取得して構築します。

インストール:
  1. GitHub Releases から setup.ps1 をダウンロードする
  2. PowerShell で実行する:
       powershell -ExecutionPolicy Bypass -File setup.ps1
  環境チェック (Python の有無とバージョン、空き容量、取得先への接続) を
  先に行い、足りないものは「入れますか？」と聞いてから入れます。
  完了まで: 登録 → スモークテスト (実辞書で変換を1回確認) を自動で行います。

アンインストール:
  インストール先 (%LOCALAPPDATA%\oyayubi-ime\app\packaging) の
  uninstall.ps1 を実行すると、登録解除からファイル削除まで行い、
  完全に元に戻ります。学習履歴・個人辞書・設定を残したいときだけ
  -KeepData を付けます (退避先が表示されます)。

前提:
  - Windows 10 以降 64bit
  - インストール時に管理者権限 (IME 登録のため。全 IME 共通)
  - 取得先 (GitHub / PyPI / PyTorch / HuggingFace / python.org) への接続
  - Python は無くても構いません (公式の同梱ランタイムを承認制で入れます)
