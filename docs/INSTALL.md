# インストール・更新・アンインストール（setup / uninstall）

設計の正本は [REDESIGN.md](REDESIGN.md) の §4（置き場所）と §8（手順）。この文書は、利用者と
開発者がそれを使うための説明。

## 約束

1. **アプリを開いたまま、入れる・上げる・消すが終わる。** サインアウトや再起動を求めない。
2. **どこで失敗しても、前の版か新しい版のどちらかが動く。** 途中の状態で止まらない。
   やり直せば続きから進む（手で何かを消す必要は無い）。
3. **利用者のデータは消さない。** 学習した内容・登録した単語・設定は `%USERPROFILE%\.yuragi-ime\`
   にあり、更新でもアンインストールでも触らない。消すのは `-RemoveData` を付けたときだけ。
4. **管理者権限で動くのは小さな PowerShell だけ（UAC は 1 回）。** Python は管理者権限で動かさない。
5. **取得は公式の場所からだけ、版は固定し、ハッシュを照合する。** 照合は、置き場所に置いたあとでも行う。

## 置き場所

```
%ProgramFiles%\yuragi-ime\<tag>\           管理者だけが書ける。全アプリに読み込まれるものはここ
    yuragi_tip.dll  yuragi-broker.exe  elevated.ps1

%LOCALAPPDATA%\yuragi-ime\                 利用者の権限で動くものだけ
    versions\<tag>\app\                     Python のソース
    versions\<tag>\engine.json              ブローカーがエンジンを起動するための道順
    shared\runtime\<版とハッシュ>\           同梱 Python 3.13 + tkinter + torch CPU + 依存
    shared\dict\<上流コミット>\              Mozc 辞書（マニフェストの SHA-256 で照合済み）
    shared\cache\<マニフェストのハッシュ>.pkl  辞書キャッシュ（エンジンが作る）
    shared\model\<リビジョン>\               LM モデル
    staging\                                更新の作業場。成功したら versions へ移す

%USERPROFILE%\.yuragi-ime\                 利用者のデータ。更新でもアンインストールでも触らない
    config.json  tip.json  learning.jsonl  personal_lexicon.jsonl
    logs\broker.log  logs\engine.log  logs\notices.log

HKLM  CLSID・TIP 登録 → %ProgramFiles%\yuragi-ime\<tag>\yuragi_tip.dll
HKCU\Software\yuragi-ime   Tag = <tag>    「この利用者には、この版が入っている」の印
HKCU\...\CurrentVersion\Run  yuragi-ime  = サインイン時にブローカーを起動
```

大きいもの（ランタイム・辞書・モデル）は中身で名前を付けて共有するので、版を上げても中身が
同じなら取り直さない。

## インストールと更新（`setup.ps1`）

README の 1 行で `setup.ps1` を取得して実行する。インストールも更新も同じ。**管理者ではない普段の
PowerShell から**実行する（管理者権限が要る所では確認が 1 回出る）。

| 段 | すること | ここで失敗したら |
|---|---|---|
| 1 | 保守中の印（`Local\yuragi-ime-maintenance`）を立てる。この間はブローカーもエンジンも起動しない | — |
| 2 | `staging\<tag>\` に取得・照合・展開。足りない共有物だけ作る（作りかけの名前で作って、完成したら改名） | 何も変わっていない。前の版が動き続ける。やり直せば続きから |
| 3 | 本物のブローカーで試運転（`--selftest`）。エンジンを実際に起動して 1 回変換し、ウィンドウが出ないことを確かめる。利用者のデータは使わない | 同上 |
| 4 | `staging\<tag>` を `versions\<tag>` へ改名 | 同上 |
| 5 | 管理者権限（`elevated.ps1`）: Program Files に配置（置いた先でハッシュ照合）、`regsvr32`、レジストリを読み返して確認、他の版の削除（使用中は次の起動時） | 前の版の登録のまま。前の版が動き続ける |
| 6 | 旧ブローカーを終わらせ、HKCU の Tag と Run キーを新しい版に、言語一覧に追加。旧版（v0.1.0）からは学習履歴・個人辞書を移す。旧名（oyayubi-ime）の版からは、その製品のキー（`HKCU\Software\oyayubi-ime`）を消して旧ブローカー（`oyayubi-broker`）を止め、`%USERPROFILE%\.oyayubi-ime` の中身を `.yuragi-ime` へ移す（移すだけで消さない。両方にある名前は旧い方を残す） | — |
| 7 | 印を下ろし、新しいブローカーを起動して `ready` を確認（最大 60 秒） | 理由を表示。次のサインインでも起動を試みる |
| 8 | 使われていない版・共有物・キャッシュの片付け。旧名の `%LOCALAPPDATA%\oyayubi-ime` も消す | 片付けの失敗はインストールの失敗にしない。次回の setup がまた試す |

更新のあと、開いたままのアプリには古い DLL が残る。古い DLL は自分のタグが HKCU の Tag と違うので
何も起動せず、新しいブローカーにつながる（プロトコルが同じならそのまま使え、違えば
「このアプリを開き直すと使えます」と表示する）。

ARM 版の Windows には、まだ対応していない（setup が最初に止まる）。

## アンインストール（`uninstall.ps1`）

README の 1 行で、リリースの `uninstall.ps1` を取得して実行する。インストールと同じ形で、
入れた版の名前を知らなくてよい。

```powershell
iwr -UseBasicParsing https://github.com/sadaoikebe/yuragi-ime/releases/latest/download/uninstall.ps1 -OutFile $env:TEMP\yuragi-uninstall.ps1; powershell -ExecutionPolicy Bypass -File $env:TEMP\yuragi-uninstall.ps1
```

リリースの `uninstall.ps1` は入口（`packaging/uninstall-entry.ps1`）で、入っている版を見つけて、
その版の本体（`versions\<tag>\app\packaging\uninstall.ps1`）を呼ぶ。引数（`-RemoveData` `-Yes`）は
そのまま本体に渡す。

- 入っている版は、HKCU の Tag で見つける。Tag が無ければ、Program Files に登録が残っている版
  （本体は Tag を最初に消すので、途中で止まったアンインストールのやり直しはこちらで見つかる）。
- 入っていなければ、そう表示して何もせずに終わる。
- 開発用の登録（タグ `dev`）は、リポジトリの `packaging\uninstall.ps1` で外す（[DEVENV.md](DEVENV.md)）。

本体がすること:

1. 保守中の印。
2. HKCU の Tag と Run キーを消す（最初に。残った DLL はこれで何も起動しなくなる）。言語一覧から外す。
3. ブローカーに `quit`（エンジンも終わる）。
4. 管理者権限: 登録解除、読み返して確認、`Program Files\yuragi-ime\` を削除（使用中の DLL は次の
   Windows 起動時に消える予約）。
5. HKCU の `Software\yuragi-ime`（Tag と揮発キー Session）と、言語一覧から外したときに Windows が書く
   TSF の利用者設定（`SOFTWARE\Microsoft\CTF\TIP\{CLSID}`）を削除。`%LOCALAPPDATA%\yuragi-ime\` を削除。
6. 結果を表示する。アプリを開いたままでも問題ない。

`%USERPROFILE%\.yuragi-ime\` は残る。消すときは `-RemoveData`（場所と大きさを表示して確かめてから
消す。`-Yes` で確認を省く）。

## 開発機

- `src\tip\dev-build.ps1`: TIP とブローカーを Release でビルドして試験する（管理者権限・登録なし）。
  CI もこれを使う。
- `src\tip\dev-register.ps1 [-Python <python.exe>]`: タグ `dev` として登録する。エンジンは
  リポジトリから指定の Python で動き、DLL とブローカーは `dist\` から読み込む（Program Files には
  置かない）。DLL は `yuragi_tip-a.dll`、`-b.dll`、… のうち、登録中でも使用中でもない最初のものに
  コピーして登録するので、古い DLL を読み込んだアプリを閉じなくても更新や入れ直しができる。

## 既知の限界

- 署名なしの DLL とブローカーは、Smart App Control やウイルス対策に止められ得る。setup は検出も回避も
  しない。止められた場合は、試運転（段 3）か起動の確認（段 7）が理由を表示して止まる。
- 取得先（GitHub / python.org / PyTorch / PyPI / HuggingFace）に届かないネットワークではインストール
  できない。届かなかった取得の段で止まる（事前の接続確認はしない）。
