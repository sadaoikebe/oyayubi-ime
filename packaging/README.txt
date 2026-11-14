yuragi-ime のインストール (詳しくは docs/INSTALL.md)

持ち込むのは setup.ps1 だけです。本体・辞書・LM・モデルは、setup.ps1 が
対象 PC の側で公式の場所から取得して構築します。

インストールと更新 (同じ手順。アプリを開いたままでよい):
  管理者ではない普段の PowerShell で
    powershell -ExecutionPolicy Bypass -File setup.ps1
  取得するものを表示して確認してから進みます。管理者の確認は 1 回だけ
  (IME の登録)。途中で失敗しても、それまでの版がそのまま動きます。

アンインストール (リリースの uninstall.ps1。入っている版を見つけて、その版の本体を呼ぶ):
    powershell -ExecutionPolicy Bypass -File uninstall.ps1
  入っていなければ、そう表示して何もせずに終わります。
  学習した内容・登録した単語・設定 (%USERPROFILE%\.yuragi-ime) は残ります。
  消すときは -RemoveData を付けます。

このフォルダ:
  setup.ps1      インストールと更新 (リリース時にタグとハッシュが刻まれる)
  uninstall.ps1  アンインストールの本体 (各版に入る)
  uninstall-entry.ps1  アンインストールの入口 (リリースには uninstall.ps1 の名前で置く)
  elevated.ps1   管理者権限で動く部分 (配置・登録・削除の 3 つだけ)
  fetch_model.py LM モデルを固定リビジョンで取得する
  requirements.txt  依存の固定
  smoke.py       開発用: 実辞書で変換を 1 回確かめる
