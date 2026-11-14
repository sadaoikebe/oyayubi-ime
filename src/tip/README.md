# oyayubi_tip（TSF テキストサービス、REDESIGN §7）

他人のアプリの中で動く DLL（`NicolaIME3.dll`）。**できることを減らしてある**: 打鍵をブローカーへ送り、
返事を画面に書く。それだけ。Python を起動せず、再試行せず、ファイルを持たない。
起動してよいのはブローカーだけで、条件は §7.3 の 4 つ（タグが一致・保守中でない・整合性レベル「中」・
このサインインで 3 回未満）。

| ファイル | 中身 |
|---|---|
| `oyayubi_tip.cpp` | TSF とのやりとり（キー、変換中の文字の描画、表示属性、候補ウィンドウ、言語バー、トグル）と、ブローカーとの接続（`BrokerLink`） |
| `tip_logic.h` | TSF に依存しない判断: 返事の読み取り（知らない項目は無視、無い項目は既定値）、食べるかどうか、返事の順序、状態と表示 |
| `tip_json.h` | 小さな JSON 解析器（ブローカーと共有） |
| `tests/tip_logic_test.cpp` `tests/tip_json_test.cpp` | 上の 2 つの単体試験（どの OS でも動く） |
| `tests/mingw/ctffunc.h` | Linux で llvm-mingw を使って型検査するときだけの補い（製品のビルドでは使わない） |

## 動き

| 場面 | 動作 |
|---|---|
| 接続 | `\\.\pipe\oyayubi-ime-<SID>-<セッション>`。試すのは ActivateEx・かな入力の打鍵・「もう一度試す」のときだけ、2 秒に 1 回まで |
| 打鍵 | 要求に `seq` を付けて送り、返事を最大 200ms 待つ。間に合えば同期の編集で反映。間に合わなければ食べてすぐ戻り、返事が来たら非同期で反映（接続は切らない、画面も触らない） |
| 遅い返事 | 1 秒以上待っている間は「変換の準備をしています…」 |
| エンジンの入れ替わり・エラー・停止・切断 | 見えている文字をそのまま確定する（消さない） |
| タイマー | 返事の `timer`（同時打鍵の判定期限、LM の次の問い合わせ）のときだけ 1 回鳴る |
| パスワード欄 | フォーカスが移ったときに 1 回調べる。調べ中は素通し |
| タスクバー | 「お」「N」、準備中は薄い「お」、停止は「!」、対象外は「N」 |
| メニュー | 通常: 切り替え・設定を開く / 準備中: （準備中）・設定を開く / 停止: もう一度試す・原因を表示・設定を開く |

設定は `%USERPROFILE%\.oyayubi-ime\tip.json`（任意）:

```json
{"toggle_vk":192,"toggle_mods":["alt"],"log":false,"log_text":false}
```

ログ（`%TEMP%\oyayubi_tip.log`）は `log:true` のときだけ書く。入力した文字は `log_text:true` のときだけ。

## ビルド

```
cmake -S src/tip -B build/tip -DCMAKE_BUILD_TYPE=Release -DOYAYUBI_TAG=<tag>
cmake --build build/tip && ctest --test-dir build/tip
```

Windows では `dist\NicolaIME3.dll` ができる。Linux では単体試験だけがビルドされる。
