# oyayubi_tip（自前 TSF）

Stage 3 の TIP コード。**Windows ビルド・実機検証は未実施**。
Win11 の `TextInputHost` は共有なので、ActivateEx では接続ワーカーを起動して即 return する。

| 動作 | 実装 |
| --- | --- |
| 常駐接続 | `~/.oyayubi-ime/endpoint.json` の port/token で loopback TCP 認証。既存接続を先に試し、不在なら Python を detach 起動 |
| 起動・復旧 | 初回待機15秒、接続＋認証250ms以内、再接続待ち1秒。起動中はキーを渡す |
| IPC 失敗 | 応答上限はキー・タイマーとも `kKeyBudgetMs` = 250ms。失敗した接続を閉じ、そのキーを渡す。キーの再送・常駐サーバの kill は行わない |
| LM 待ち | 返信の `pending: true` は変換が LM の結果待ち。表示は変えず候補窓も出さない。返信の `timer` で16msタイマーが `timeout` を送って取りに来る。保留中のキーは「変換済み・候補あり」と同じ基準で食べる（サーバが順番に溜めて再生する）。`pending` が無い返信は `false` 扱い |
| 入力除外 | disabled / empty / keyboard closed、`IS_PASSWORD` / `IS_NUMERIC_PASSWORD`。パスワード欄ではトグルも扱わない |
| キー | 文字・親指、編集中のBS/Enter/Esc、文節左右・Shift左右、候補上下・PgUp/PgDn・数字、F6/F7。消費したdownのupだけサーバへ送る |
| モード | Alt＋VK_OEM_3（US配列の Alt＋バッククォート）で direct 切替。repeatで再反転しない。未確定入力は破棄 |
| フォーカス | 両方のOnSetFocus、context push/pop、composition外部終了でreset。候補・タイマー・down状態・確定上文を破棄 |
| 編集 | SYNCが拒否されたらASYNC。世代を無効化して旧編集を破棄。commitと次compositionを順番に適用 |
| 表示 | 未変換は点線、変換済みは実線、注目文節は太い実線。UTF-16オフセット、末尾キャレット、TSF GetTextExtのscreen座標で候補配置。位置取得失敗・不可視の空矩形では非表示にして再試行 |
| 登録カテゴリ | immersive / COMLESS等とdisplay attribute provider。SECUREMODEは登録せず、再登録時に旧登録も削除 |

辞書はユーザーごとに1プロセスで共有し、ImeSessionはTIP接続ごとに独立する。
Deactivateは接続だけを閉じ、最後のTIPが終わってもサーバは維持する。
TIPは周辺本文を取得しない。再接続時はdirectだけを復元し、旧入力を再送しない。

設定は `%USERPROFILE%\.oyayubi-ime\tip.json`（UTF-8 JSON、任意）。変更後はTIPを再Activateする。
省略時の値は以下。`toggle_mods` は `shift` / `ctrl` / `alt` / `win` の完全一致で、
既定のAltトグルではCtrl/Win/Shift併用を除外する。

```json
{"toggle_vk":192,"toggle_mods":["alt"],"log_text":false}
```

ログは `%TEMP%\oyayubi_tip.log`。本文は既定オフで、`log_text:true` の場合だけcomposition/commitを記録する。
認証トークンは常にログに出さない。自動起動サーバのstdioはNULへ接続し、旧`oyayubi_server.err.log`は作らない。
サーバ自身の`--log-text`は独立した明示オプションで、引き続き既定オフ。

通常の Windows 開発ループは、リポジトリのルートから次の1コマンドで行う。

```powershell
powershell -ExecutionPolicy Bypass -File .\src\tip\dev-build.ps1
```

管理者への昇格、未ロードの開発者環境の読み込み、初回の CMake 構成、ビルド、CTest、
登録解除・再登録まで実行する。Visual Studio C++ BuildTools、Windows SDK、CMake、Ninja、
`python` コマンドが必要。ビルドやテストに失敗した場合は登録を更新せず停止する。

ビルド成果物の `dist\NicolaIME3.dll` を `dist\NicolaIME3-a.dll` と
`dist\NicolaIME3-b.dll` に交互にコピーして登録するため、通常の更新に再ログインは不要。
新しいアプリから新版が読み込まれ、起動中の古いアプリは旧版のまま動く。
すべてに新版を行き渡らせるにはアプリを再起動するかサインアウトする。
次のコピー先スロットも古いアプリが使用中の場合は、使用中であることを表示して停止するので、
該当アプリを終了するかサインアウトしてから再実行する。
スロットの DLL は2つ溜まるだけで、残っていても害はない。
登録処理は常駐サーバを起動しない。
従来の `NicolaIME3.dll` をアプリが読み込んでいる場合は、初回の移行時だけ、
それらのアプリの終了やサインアウトでビルド出力へのロックを解放してから実行する。

手動で登録する場合は、管理者 PowerShell で
`python src\tip\unregister_admin.py` → `python src\tip\register_admin.py`。
両スクリプトとも末尾に任意の DLL パスを指定できる。
登録の既定は `dist\NicolaIME3.dll`、解除の既定は HKLM の InprocServer32 に現在登録された
DLL（未登録なら `dist\NicolaIME3.dll`）。使用中の DLL を直接置換する場合は、
その DLL を使うアプリを終了するかサインアウトする必要がある。
通常ユーザーで`python -m oyayubi.ime.client`を実行すると常駐の起動・認証を先に確認できる。

NicolaIMEを選ぶのはタスクバーの **Japanese → NicolaIME** または Win+Space。
Alt+Shiftは言語巡回。実機スモークの全項目は[`docs/STAGE3.md`](../../docs/STAGE3.md)参照。

```powershell
cd src\tip
cmake -B build -G Ninja
cmake --build build
ctest --test-dir build --output-on-failure
```

JSONテストは既定でビルドする（不要なら`-DTIP_BUILD_JSON_TESTS=OFF`）。
DLLはMSVC＋Windows 10/11 SDKでビルドする。C++17、`/MT`、UTF-8ソースを使用し、
`ole32 / oleaut32 / uuid / advapi32 / user32 / gdi32 / ws2_32`をリンクする。
LinuxではDLLを対象から外し、同じCMakeからJSONテストだけをビルドできる。
以下はLinuxでReleaseビルド・CTestの成功を確認済み。Windows DLLの検証とは別である。

```sh
cmake -S src/tip -B /tmp/oyayubi-tip-tests -DCMAKE_BUILD_TYPE=Release
cmake --build /tmp/oyayubi-tip-tests
ctest --test-dir /tmp/oyayubi-tip-tests --output-on-failure
```

API照合に使用した公式資料:
[入力スコープ取得](https://learn.microsoft.com/en-us/windows/win32/api/inputscope/nn-inputscope-itfinputscope)、
[表示属性provider](https://learn.microsoft.com/en-gb/windows/win32/tsf/providing-display-attributes)、
[InsertTextAtSelection](https://learn.microsoft.com/en-us/windows/win32/api/msctf/nf-msctf-itfinsertatselection-inserttextatselection)、
[StartComposition](https://learn.microsoft.com/en-us/windows/win32/api/msctf/nf-msctf-itfcontextcomposition-startcomposition)、
[GetTextExt](https://learn.microsoft.com/en-us/windows/win32/api/msctf/nf-msctf-itfcontextview-gettextext)。
未確定文は`TF_IAS_QUERYONLY`で範囲だけ照会し、composition開始が受理されてから挿入する。
非同期編集の失敗も状態を無効化し、次のタイマー／実キーでresetする。
CRTを使う接続ワーカーは`_beginthreadex`で起動し、`/MT`に必要なDLL thread通知を維持する。
参照: [CRTのスレッド作成](https://learn.microsoft.com/en-us/cpp/c-runtime-library/reference/beginthread-beginthreadex)、
[DisableThreadLibraryCallsの制約](https://learn.microsoft.com/en-us/windows/win32/api/libloaderapi/nf-libloaderapi-disablethreadlibrarycalls)。
署名・リンク成功、TSFのロック／ASYNC順序、入力スコープ、候補位置、WindowsのdetachとACLはPCで確認する。
