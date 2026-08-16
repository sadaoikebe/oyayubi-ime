# oyayubi_tip（自前 TSF）

Win11 の `TextInputHost` は全 IME 共有なので、ここで待つと全部止まる。

作法は SampleIME / CorvusSKK / Mozc の TIP から移植した。

| 知見 | 出典 | こちら |
| --- | --- | --- |
| 無効コンテキスト・キーボード閉は食べない | 三者 | `GUID_COMPARTMENT_KEYBOARD_DISABLED` / `OPENCLOSE` |
| 空なら Back/Enter/Esc はアプリへ | SampleIME | `WantVk` |
| `ActivateEx` で I/O しない | SampleIME / CorvusSKK | spawn して即 return |
| IPC 失敗はそのキーだけ素通し。サーバは殺さない | Mozc | timeout は soft fail。8 回連続だけ Die |
| キー処理を SEH で囲む | CorvusSKK | `HandleKey` / `DoEditSession` / `OnTimer` |
| SYNC が拒否されたら ASYNC | CorvusSKK + SampleIME | `TF_E_SYNCHRONOUS` で再試行 |
| 登録カテゴリ | SampleIME / CorvusSKK | immersive / COMLESS / secure |

まだやらない（Mozc 形の次の段）: 常駐サーバ + 接続ごとの `ImeSession`。今はアプリごとに Python が立つ。

まだ登録しなくてよい。試験は先に `python -m oyayubi.ime.host_win32`。

DLL は `dist\NicolaIME3.dll`。入れるときだけ管理者で `python src\tip\register_admin.py`。外すのは `unregister_admin.py`（HKCU の残骸も消す）。

Alt+Shift は言語（US / 日本語 / 中国語）を回す。日本語の既定は Microsoft IME のままなので、NicolaIME は巡回から飛ばされる。これは登録が日本語の第 2 IME として成功しているときの動き。切り替えるのはタスクバーから **Japanese → NicolaIME**、または Win+Space。`j` で「と」が出れば TIP は動いている。

ログ: `%TEMP%\oyayubi_tip.log` と `%TEMP%\oyayubi_server.err.log`

```bat
cd C:\Users\marur\oyayubi-ime\src\tip
cmake -B build -G Ninja
cmake --build build
```
