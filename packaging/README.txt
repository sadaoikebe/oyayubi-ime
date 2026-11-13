oyayubi-ime リリースパッケージ
==============================

あいまい親指シフト IME（oyayubi-ime）を、Python も開発ツールも無い PC に
入れるための一式です。DLL・同梱 Python・辞書が全部入っています。

動作環境
--------
- Windows 10 / 11（64bit）
- 登録のときだけ管理者権限が要ります（初回インストールと更新時）

インストール
------------
1. zip をどこかに展開する
2. 展開したフォルダで PowerShell を開いて実行:
     powershell -ExecutionPolicy Bypass -File .\install.ps1
   管理者への昇格ダイアログが出たら承認してください
3. 本体は %LOCALAPPDATA%\oyayubi-ime\app にコピーされ、IME が登録されます
4. Win+Space で「NicolaIME」に切り替えて使います
   サーバは初回使用時に自動で起動します

「DLL が使用中」と言われたら、サインアウト→サインインしてから
install.ps1 をもう一度実行してください。

管理ウィンドウ（単語登録・学習・設定）
------------------------------------
インストール先のフォルダで:
  .\runtime\python.exe -m oyayubi.ime.admin_ui

入っていないもの
----------------
- LM（言語モデル）による再スコアリング。変換は LM なしの経路で動き、
  個人 PC 版との違いはそこだけです

更新
----
新しい zip を持ってきて、install.ps1 を再実行するだけです。

アンインストール
----------------
インストール先のフォルダで:
  powershell -ExecutionPolicy Bypass -File .\uninstall.ps1
学習履歴と個人辞書は %USERPROFILE%\.oyayubi-ime\userdata に残ります。
一緒に消すときは .\uninstall.ps1 -Purge を使ってください。

動かないとき
------------
このパッケージは署名されていないため、Smart App Control や
ウイルス対策ソフトが DLL の読み込みを止めることがあります。
「IME の一覧には出るが入力できない」場合はその可能性が高く、
その PC では使えません。

データの場所
------------
- アプリ本体: %LOCALAPPDATA%\oyayubi-ime\app
  （学習は app\data\learning.jsonl、個人辞書は app\data\personal_lexicon.jsonl）
- 実行時ファイル・設定: %USERPROFILE%\.oyayubi-ime
