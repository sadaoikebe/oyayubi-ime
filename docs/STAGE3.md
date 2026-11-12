# Stage 3 — 常駐サーバ＋TIP 一括改修

2026-11-11。対象は `docs-and-eval-2026-11-10`、開始 HEAD `ed91f93`。
方針は「薄い TIP・厚いセッション」。辞書ロード約5.4秒・約1.4GBをユーザーごとに
1プロセスへ集約する。サーバ部分はLinuxで検証済み。TIP部分はコードを反映したが、Windowsでのビルド・実機検証は未実施。
コミットは作成しない。辞書・FSM・探索・Mozc/AJIMEEの評価経路は変更しない。

## 読んだ現行実装と差分

- `oyayubi/ime/server.py`: 改行区切りUTF-8 JSON、起動時ready、`dispatch()` は伝送非依存。
  `reset` / `set_direct` / `toggle_direct` と `direct` / `pass_through` は実装済み。
  `context` は冒頭の例だけで分岐がなかったため、今回接続のLM上文を設定する処理を補う。
- `session.py`: FSM、候補ストリーム、文節、修飾キー、学習undo、`_left_surface`、
  `_lm_context` はセッション状態。`reset()` は入力と上文を破棄し、directと学習undoは保持する。
  `set_direct()` の遷移は未確定入力を破棄し、確定・学習しない。
- `learning.py`: ファイルが存在する場合だけ有効。JSONL追記＋signatureによる再読込、
  snapshotによる候補探索中の固定、record ID指定のundo。削除後にファイルを再作成しない。
- `lm_rescore.py`: LMは既定オフ。モデルとscorerのキャッシュは可変で、同時実行には直列化が必要。
- `src/tip/oyayubi_tip.cpp`: `Server::Spawn/Stop/Die` がアプリごとのstdio子プロセスを所有し、
  `Stop/Die` は終了させる。`ActivateEx` はspawnだけで待たない。
  `WantVk/CanEat` にローカルのキー判定があり、`ServerReply/ParseReply` はdirect・文節を読まない。
  `OnSetFocus`、`OnCompositionTerminated` はサーバresetを送らない。
  `HandleKeyImpl` と `DrainStale` が本文を無条件にログへ出す。
  `EditSession` はSetTextまでで文節属性・選択範囲更新がない。
- `register_admin.py` / `unregister_admin.py`: ProfileMgr＋InstallLayoutOrTip、HKLM/HKCUのCOM登録。
  `CMakeLists.txt`: Ninja/MSVC、出力 `dist/NicolaIME3.dll`。既存の登録方式は維持する。

## 伝送方式

| 観点 | Windows名前付きパイプ（Python ctypes） | localhost TCP＋ユーザー用トークンファイル |
| --- | --- | --- |
| Python依存 | 標準ライブラリだがWin32 API/構造体を自前定義 | socket/socketserver/json/secrets等の標準ライブラリのみ |
| アクセス制限 | パイプのDACL、リモート拒否、クライアントの識別を設定 | IPv4 loopbackだけでlisten、ファイルの秘密トークンで認証 |
| TIP | Win32ハンドル、overlapped I/O・キャンセル | Winsock、部分送受信・期限・切断管理、ws2_32追加 |
| Linuxでの検証 | 伝送の本体は実行不可 | 本番と同じ認証・同時接続・終了・再起動を実行可能 |
| 当日の残作業 | サーバのWindows I/Oも初検証になる | Windows差分はロック・detachとTIP側のソケット |

**ドッグフード開始にはTCPを推奨する。** サーバの本体をLinuxで固定しておき、PC当日は
TSFの表示・キー消費・フォーカスに集中できる。待受けは `127.0.0.1:0`（動的ポート）で固定。
公開ネットワークではlistenしない。名前付きパイプへの移行は必要が生じてからでよい。

## サーバの実装とライフサイクル

- `server.py --resident`: `runtime_platform.py` のOSロックを **辞書ロード前** に獲得。
  同時spawnの敗者はロードせず終了する。Linuxはflock、Windowsはmsvcrtの1バイトロック。
  ロックファイルは削除せず、プロセス終了でOSが解放する。古いendpointがあってもロックを優先する。
- `resident.py`: 辞書・配列表・学習ストアを1回ロードし、接続ごとに `ImeSession` を作る。
  同時に接続を維持でき、各接続内は要求順を保持する。要求処理だけを共通ロックで直列化し、
  LearningStoreの追記／snapshotと共有LM scorerを保護する。読み待ち中の接続は他接続を塞がない。
  重い変換中のCPU並列化は今回の対象外。
- 標準runtimeディレクトリは `~/.oyayubi-ime`、`--runtime-dir` で変更可。
  `endpoint.json` は `version/port/token`。256-bitランダムトークンを毎起動生成し、一時ファイルから
  atomic replaceでready後だけ公開する。トークンを引数・stdout・ログへ出さない。
  Unixのディレクトリは0700、endpointは0600。Windowsはユーザープロファイルの継承ACLを使う。
  PC当日に別ユーザーから読めないことを確認する。共有・同期フォルダをruntimeに指定しない。
- wire: 最初に `{"op":"hello","version":1,"token":"…"}`、認証成功で
  `{"ok":true,"msg":"ready","version":1}`。以後は既存dispatchのJSONを1行1要求・1応答で使う。
  認証前にはセッションを作らない。`quit` は当該接続だけを閉じ、サーバ全体を止めない。
  切断・不正要求ではそのセッションを破棄し、他の接続は維持する。
- `client.py` の `connect_or_start()` は既存endpointへの接続・認証を先に試し、失敗時に
  `python -m oyayubi.ime.server --resident` をdetachしてreadyを期限つきで待つ。
  `runtime_platform.py` にWindowsの起動フラグだけを分離する。
  子のstdioはDEVNULLで、終了ハンドルをTIPが所有してkillする形にしない。
  実機ではTIPのワーカーからこの手順を移植する。ActivateExやTSFキー処理で辞書ロードを待たない。
- 初回待機は既定15秒、接続・認証は1回最大250ms。通常キー応答はTIPの `kKeyBudgetMs`（250ms）を上限にする。
  起動中はキーをアプリへ渡す。失敗キーは再送しない。
  返信待ちが失敗した接続は閉じ、遅れた応答を次のキーの応答に流用しない。
  サーバのkillは行わず、ワーカーで再接続・必要なら再spawnする。
  再接続は新セッションなので入力・候補・タイマーを破棄し、TIPのdirect設定だけ再送する。
  再接続待ちはTIP側で1秒間隔を既定とし、入力経路を塞がない。
- 最後のTIPが切断しても常駐を維持する（idle終了なし）。手動終了はサーバプロセスを終了する。
  正常終了はendpointを削除し、異常終了で残ったendpointは次回起動時に置換する。
  PythonクライアントのCLI `python -m oyayubi.ime.client` は起動・認証確認後に切断する。

### 学習とLM上文

学習ストアのオブジェクトとパスはサーバで共有する。`--no-learning` と既存の「ファイルなしは無効」
を維持し、起動のために学習ファイルを自動生成しない。他接続での確定は次のsnapshotから見える。
学習undoのID列は接続単位なので、F7が他アプリの確定を取り消すことはない。
既存の候補選択・コスト・減衰・外部ファイル変更の扱いは変更しない。

LMは既定オフ。オンの場合はモデル/scorerだけ共有し、`LmRescorer.last` は接続単位とする。
scorerのキャッシュはcontextをキーにして共有するが、各変換に渡す上文は当該接続の
`_lm_context` だけ。composition・学習の左隣 `_left_surface` も接続を跨がない。
`context` は渡された確定済み本文の末尾200文字をその接続に設定する。
他アプリの本文を取得・注入しない。TIPは既定で周辺本文を取得せず、接続内の確定蓄積だけを使う。
フォーカス移動・composition外部終了では `reset`、切断ではセッション破棄により上文を消す。
resetは既存どおりdirectと学習undoを保持する。

## TIP側変更リスト（コード反映済み、Windows未検証）

対象の中心は `src/tip/oyayubi_tip.cpp`。

1. `kCats` から `GUID_TFCAT_TIPCAP_SECUREMODE` を削除する。
   `RegisterCategories(TRUE)` でも過去のSECUREMODEを明示Unregisterして残留を除く。
   secure desktop対応を宣言しない。登録済みDLLの置換だけでは旧カテゴリは消えない。
2. `Server` を常駐接続の所有に替える。Spawn/Alive/Stop/Die、PumpReady、ReadLine、Sendを変更。
   Winsockを初期化しendpointを読む。ワーカーで接続・認証・必要時spawnし、状態通知だけTSFへ戻す。
   `ActivateEx` はワーカー起動で即return、`Deactivate` は接続だけ閉じる。
   タイマーとキーが同じ接続へ要求を重ねないよう一列にする。
   タイムアウトは接続を捨てて再接続する。古いcompositionへ遅延編集を適用しないよう世代を持たせる。
   `CMakeLists.txt` に `ws2_32` を追加する。
3. `ServerReply/ParseReply/NoteReply` でdirect/pass_through、segments/focus/pageを読む。
   現在のGrabJsonString系はescaped quote・改行・Unicode escapeを正しく戻せないので、
   JSON文字列を正しくdecodeするパーサに交換する。認証や本文をsubstring判定しない。
4. `WantVk/CanEat/OnTestKeyDown/OnKeyDown` でAlt＋VK_OEM_3を通常のModHeld除外より先に検出。
   Ctrl/Win/Shift併用は除外し、初回downだけ `toggle_direct`。repeatと対応upは同じトグルとして
   消費し再反転させない。direct中もこのキーは検出し、他のキーは素通し。
   設定ファイルの `toggle_vk=192`, `toggle_mods=["alt"]` を既定とし差し替え可能にする。
   切替は未確定文を破棄する既存セッション仕様に合わせ、候補窓・タイマー・TSF compositionも消す。
5. `OnTestKey*` は副作用のない候補判定だけ、実送信は `OnKey*` に一度だけ行う。
   direct/pass_throughを最優先し、`pass_through=false` だけでは全キーを消費しない。
   Stage 3の返信マスクは `direct / pass_through / n_tokens / converted / candidates / timer` と
   送信前状態を組み合わせたキーの役割判定とする（現行プロトコルに専用consumedフィールドはない）。
   文字・親指、編集中のBS/Enter/Esc、変換中の左右・上下・PgUp/PgDn・数字・Shift左右、F6/F7を
   セッションと揃え、Shift状態を `mods` に載せる。空状態のEnter/BS、Ctrl/Alt/Winショートカットは渡す。
   down_eaten_は実際に消費したdownだけ記録し、upとの組を保つ。IPC失敗時はそのキーを渡す。
6. `EditSession::DoEditSessionImpl` で未変換・変換済み・注目文節のdisplay attributeを設定する。
   `ITfDisplayAttributeProvider` と属性GUID/atomを登録し、`GUID_PROP_ATTRIBUTE` を文節範囲へ設定。
   Pythonのdisplay_start/endはコードポイントなのでUTF-16へ変換する（サロゲートペアを確認）。
   composition末尾へ `SetSelection` してキャレットを進め、候補窓位置はTSF context viewの
   `GetTextExt` からscreen座標で取る。現行の固定座標/GUITHREADINFO依存を置換する。
   commitと次compositionが同じ応答にある場合は両方を順に適用する（現行Applyはcommitだけを選ぶ）。
7. `KeyboardDisabled/CanEat` に入力スコープ判定を追加する。TSFの `GUID_PROP_INPUTSCOPE` から
   `ITfInputScope::GetInputScopes` を読み、`IS_PASSWORD` / `IS_NUMERIC_PASSWORD` では
   トグルを含めキーを扱わず、本文・周辺文も送信しない。COM配列はCoTaskMemFreeする。
   フォーカス入り口でも検査して旧compositionをresetする。disabled/empty/closedの既存除外も維持する。
8. 両方の `OnSetFocus`、`OnPushContext/OnPopContext`、`OnCompositionTerminated` で
   reset・候補非表示・タイマーとdown状態の解除を接続する。旧コンテキストの確定を新アプリへ挿入しない。
9. `HandleKeyImpl` のcomp/commitと `DrainStale` の生JSONログを既定オフにする。
   本文デバッグは明示設定時だけ。トークンは常に非表示。
   `server.py` の `--log-text` は引き続き既定オフ。登録スクリプト・READMEのsecure表記も更新する。

## PC当日の順番とスモーク

1. 通常ユーザーのPowerShellで `python -m unittest discover -s tests -v`。
   `python -m oyayubi.ime.client` で常駐起動・認証を確認する。
   2回目で同じPID・endpointを使用し、辞書が増えないことをタスクマネージャーで見る。
   runtime ACLとWindowsのロック・detachもこの段階で確認する。サーバは管理者として起動しない。
2. 上記TIP差分をレビューし、VS開発者環境でビルドする。

   ```powershell
   cd src\tip
   cmake -B build -G Ninja
   cmake --build build
   ```

3. 旧TIPを使うアプリを閉じる。管理者PowerShellでリポジトリから
   `python src\tip\unregister_admin.py` → `python src\tip\register_admin.py`。
   DLL使用中ならサインアウト後に置換・再登録する。登録処理は常駐を起動しない。
4. 通常ユーザーでサーバを起動し、Win+SpaceまたはトレイのJapanese → NicolaIMEを選ぶ。
   Alt+Shiftは言語巡回で、日本語内のNicolaIME選択には使わない。

| スモーク | 合格条件 |
| --- | --- |
| 通常入力 | NotepadでJ→「と」。`かえる` とStage 2の文を入力・変換・選択・確定。文節下線・注目・キャレット・候補位置も正しい |
| Alt+` | 通常→directで未確定文が破棄され、英字が素通し。repeatで反転せず、再度Alt+`でかなへ戻る。設定変更も確認 |
| パスワード欄 | ブラウザ等のpassword欄・数値password欄で通常入力とトグルが無効、本文がサーバやログへ送られない |
| 複数アプリ | Notepadとブラウザを同時接続。候補・direct・FSM・上文が混ざらず、辞書プロセスは1つ |
| アプリ／欄切替 | Space保留中・変換中に切替。旧入力が破棄され、新欄にtimeout/commitが漏れない。元へ戻っても空から入力 |
| サーバ不在 | 起動待ち中にアプリが止まらず、キーが素通し。ready後に入力できる |
| クラッシュ・再起動 | 変換中にサーバを終了。キーが素通しになり、1回だけ再起動・新規セッションで復帰。direct設定はTIPから復元、旧commitは再送しない |
| 解除・再接続 | 一方のアプリ終了で他方が継続。最後のアプリ終了でも常駐維持。次の起動で辞書再ロードなし |
| ログ | 通常操作後のTIP/serverログに本文・認証トークンがない |

実機確認前にTIP改修を完了扱いにしない。問題時は `unregister_admin.py` で外してMicrosoft IMEへ戻す。

## 未決事項と推奨デフォルト

| 項目 | 推奨デフォルト／残る確認 |
| --- | --- |
| 常駐の起動主体 | 最初のTIPのワーカー。ログオン自動起動は後日、初日は手動client起動も使用可 |
| 寿命 | idle終了なし、ユーザーのログオフ／明示終了まで維持 |
| モードの範囲 | 接続ごと。focus resetでは保持、再接続時にTIPの記憶をset_directで復元 |
| 設定ファイル | `%USERPROFILE%\\.oyayubi-ime\\tip.json`の読込を実装済み。toggleキーはAlt+VK_OEM_3、サーバと同じruntime既定を共有。変更後はTIPを再Activate |
| Windows ACL | ユーザープロファイルの継承ACLを使用。別ユーザーの読取り不可を実機で確認、共有ディレクトリは使わない |
| LM／周辺本文 | LMオフ。オンでも接続内の確定上文のみ。TSFでの周辺本文取得は当面しない |
| 遅い変換 | 常駐サーバは LM をワーカーで走らせ、間に合わなければ `pending` を返して16msタイマーで取りに来る。TIP予算はキー・タイマーとも250ms、超過時はfail-open（設計 `~/workspace/nicola-projects/lm-conversion-design-2026-11-12.md`） |
| 専用キー消費マスク | 今回wire互換を維持。既存返信状態でTIPの判定を揃える。専用consumeフィールドの追加は必要なら別変更としてテストする |

## Linuxでの受け入れ

既存stdio・静的トランスクリプトを含む全144件（うち実LM2件skip）を基準とする。
新規テストでは実TCP認証、2接続の同時利用、状態分離、切断破棄、共有学習とundoの分離、
LM上文、起動競合、既存接続の再利用、起動失敗の期限、サーバ再起動を固定する。
WindowsのTSF・入力スコープ・DLL登録・見た目は上記のPCスモークで確認する。

### 今回の変更と検証結果

| Task | 変更ファイル | 検証 |
| --- | --- | --- |
| A | `docs/STAGE3.md` | 上記現行コードを実読し、TIP未実装箇所とPC手順を記載 |
| B | `oyayubi/ime/server.py`、追加 `resident.py` / `client.py` / `runtime_platform.py`、`tests/test_server.py`、追加 `tests/test_resident.py` / `tests/resident_fixture.py` | 既存144件を維持し14件追加、全158件OK（従来の実LM2件skip） |
| C | `oyayubi/ime/host_win32.py` | 記録カウンタを500msごとの表示snapshotへ変更。文進行・focus変化は即時、その他は表示変化時だけInvalidateRect |

```sh
python3 -m unittest discover -s tests -v
python3 -m tools.wp0.run_regression
python3 -m compileall -q oyayubi/ime tests/resident_fixture.py tests/test_resident.py
git diff --check
```

全テストは158件OK（skip2）、変換回帰ゲートは5/5 PASS、コンパイル・差分検査も成功。
この実行環境ではサンドボックスがsocket作成を拒否するため、TCPを使うテストは制限外で実行した。
テスト用の小辞書を使った別プロセス起動試験では、同時3起動でもロード1回、既存接続再利用、
クラッシュ後の古いendpointからの復帰を確認した。
実辞書を使う本番CLIのスモークも成功（6.57秒）。辞書プロセス1つへの2接続、独立した入力、
「と」の確定、SIGINTでの正常終了とendpoint削除を確認した。

記録ホストはLinux上でWin32 APIをモックして実行し、毎キーで再描画しないこと、変化のないtimer・
focus通知で再描画しないこと、文進行・focus変化で即時再描画することを確認した。
同じイベント列を直接Recorderに渡した参照と、出力JSONL（全タイムスタンプ含む）がバイト単位で一致した。
Recorder本体・イベント引渡し・QPCによる時刻取得は変更していない。
Windowsの実画面での点滅確認はPC当日に行う。

Mozc/AJIMEEの全件評価は再実行していない。評価ローダー・辞書・学習コスト・FSM・探索と
既存トランスクリプトの期待値は変更せず、評価経路の単体テストと変換回帰ゲートを維持した。


### TIPコード反映（2026-11-11、Windows未検証）

開始HEAD `00ece0c`、同じブランチ上の未コミット差分。上の変更リスト1〜9をコードへ反映した。
Windowsビルド・リンク・TSF実機スモークの完了を意味しない。Pythonの辞書・FSM・セッション・常駐実装は変更していない。

| ファイル | 反映内容 |
| --- | --- |
| `src/tip/oyayubi_tip.cpp` | Winsock認証接続、起動ワーカー、detach/NUL、250ms接続・250msキー期限、再接続とdirect復元。トグル／返信状態による消費判定、modsとdown/up対応、focus reset、入力スコープ除外、display attribute provider、文節のUTF-16範囲、キャレットと候補位置、本文ログの明示設定 |
| `src/tip/tip_json.h` | endpoint・設定・返信の構造化JSONパーサ。quote、改行等のescapeとUnicode／surrogate pairの復元。外部依存なし |
| `src/tip/CMakeLists.txt` | `ws2_32`等のリンク、新規ヘッダとテストを登録。`TIP_BUILD_JSON_TESTS`は既定ON、Linuxではテストだけビルド。MSVC runtime設定をターゲット生成前へ移し、`/utf-8`とWindows 10/11対象のAPI宣言マクロを指定 |
| `src/tip/tests/tip_json_test.cpp` | endpoint／返信、偽の入れ子フィールド、escape／サロゲート、壊れたJSON、64-bit整数境界・範囲超過を検査。LinuxのCMake Releaseビルド・CTest成功 |
| `src/tip/register_admin.py` / `README.md` | 旧SECUREMODE削除がDLL登録で行われる旨、設定場所と既定値、常駐の寿命・ログ・実機検証手順 |

ワーカーは接続の状態・世代だけを公開し、COM／TSFには触れない。タイマーとキーの送受信は
同じ接続上で一列にする。失敗接続の応答は流用せず、キーを再送しない。
編集はフォーカスと接続の世代を確認し、旧文脈の編集を無効化する。
同期編集が非同期の旧composition削除を追い越す場合も、新しい入力の前に同じcontextの旧compositionを削除する。
SetText直後のGetTextExtが失敗した場合は、後続の読み取り専用編集で位置取得を再試行する。
GetTextExtが不可視を示す空矩形を返す場合と、active viewを取得できない場合も候補を隠す。
未確定文の新規挿入は範囲照会→StartComposition受理→SetTextの順とし、
アプリ側のcomposition拒否（S_OKかつnull）で本文が残ることを防ぐ。
非同期編集の失敗は状態を無効化し、次のタイマー／実キーでresetする。
設定の詳細と任意のJSON検査ビルド手順は`src/tip/README.md`参照。

### TIP監査と仕上げ（2026-11-11）

変更リストとの照合結果（いずれもコード上の充足。Windowsの動作合格は未判定）:

| 項目 | 照合箇所・結果 |
| --- | --- |
| 1 SECUREMODE | `kCats`から除去済み。`RegisterCategories`の旧カテゴリUnregisterを維持 |
| 2 常駐接続 | `Server::Start/Run/Connect/SpawnResident/Send/Stop`。ワーカー起動、250ms接続、250msキー、直列化、切断再接続、世代、direct復元、killなし。`ws2_32`リンク済み |
| 3 JSON／返信 | `tip_json.h`、`ParseReply/NoteReply`。direct・pass_through・文節・focus・page・timerを構造で読取り、escape／Unicodeを復元。今回整数範囲超過も修正 |
| 4 トグル | `TipConfig/ToggleKey/OnKeyDown/OnKeyUp/ToggleImpl`。既定Altのみ、設定変更、初回downのみ反転、repeat／up対応、未確定破棄 |
| 5 キー消費 | `WantVk/CanEat/HandleKeyImpl/OnTestKey*/OnKey*`。OnTestは照会のみ、実キーで一度送信、返信状態と送信前の役割で判定。Shiftをmodsへ載せ、消費downのみupを対応。IPC失敗は素通し |
| 6 編集／表示 | `AttributeInfo/AttributeEnum/EditSession/Utf16Offset/PositionCandidate`。3属性、コードポイント→UTF-16、末尾選択、GetTextExt、commit＋次composition。今回composition拒否・非同期編集失敗・不可視候補を修正 |
| 7 入力スコープ | `PasswordScopeWithCookie/ScopeSession/KeyboardDisabled/Eligible`。read cookieでGetAppProperty→GetValue→GetInputScopes、password 2種でトグルも除外。CoTaskMemFree、周辺本文取得なし |
| 8 フォーカス | 両OnSetFocus、push/pop、外部composition終了→`ResetFocus/ClearLocal`。サーバreset、候補・timer・down解除、旧編集無効化と同じcontextの旧composition削除 |
| 9 ログ／登録説明 | `TipConfig::log_text`は既定false、本文ログは設定時のみ。生JSONログなし、tokenログなし、登録スクリプトとREADMEも更新済み |

今回追加・修正したのは、上記の整数境界検査、composition開始拒否と非同期編集失敗の扱い、
候補位置がない場合の非表示、`cstdarg/cwchar/process.h`の明示include、CMakeの登録・リンク・runtime・文字コード・対象OS設定、
既定ONのC++テストとLinux用ビルド経路、設定読込の古い記述と検証記録。
CRTを使うワーカーを`_beginthreadex`で起動し、`/MT`と両立しない`DisableThreadLibraryCalls`も除去した。
参照: [CRTのスレッド作成](https://learn.microsoft.com/en-us/cpp/c-runtime-library/reference/beginthread-beginthreadex)、
[DLL thread通知の制約](https://learn.microsoft.com/en-us/windows/win32/api/libloaderapi/nf-libloaderapi-disablethreadlibrarycalls)。
JSONテストは変更前にも直接g++で成功を確認し、変更後はCMake経由のReleaseビルド・CTestで1/1 PASS。
登録スクリプトはAST構文検査、C++は括弧・include・シンボルと公式API宣言を点検し、`git diff --check`も成功。
Python全158件OK（従来の実LM2件skip）、変換回帰5/5 PASSを再確認した。
socket制限下の初回実行ではTCPテスト12件がPermissionErrorになったため、全158件の成功は制限外での再実行結果。
`oyayubi/`配下は変更していない。コミットも作成していない。

Windows DLLのコンパイル・リンクとTSF実機試験はこの環境では実行できず、未検証。
LinuxのC++実行はJSON部分だけであり、TIP本体のコンパイル成功を示すものではない。
APIはMicrosoft LearnおよびMicrosoft公開SDKヘッダと照合した。
参照: [入力スコープのSDK宣言](https://raw.githubusercontent.com/microsoft/win32metadata/main/generation/WinSDK/RecompiledIdlHeaders/um/InputScope.h)、
[GetAppProperty](https://learn.microsoft.com/en-us/windows/win32/api/msctf/nf-msctf-itfcontext-getappproperty)、
[RequestEditSession](https://learn.microsoft.com/en-us/windows/win32/api/msctf/nf-msctf-itfcontext-requesteditsession)、
[StartComposition](https://learn.microsoft.com/en-us/windows/win32/api/msctf/nf-msctf-itfcontextcomposition-startcomposition)、
[GetTextExt](https://learn.microsoft.com/en-us/windows/win32/api/msctf/nf-msctf-itfcontextview-gettextext)、
[Winsock select](https://learn.microsoft.com/en-us/windows/win32/api/winsock2/nf-winsock2-select)、
[継承ハンドル指定](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-updateprocthreadattribute)。

PCで特に確認する箇所:

- MSVC＋Windows 10/11 SDK。`inputscope.h`の`IS_NUMERIC_PASSWORD`、`ITfDisplayAttributeProvider`一式とGUID、`ws2_32`のリンク。SEHのラッパーへデストラクタ付きローカル変数を入れない（C2712回避）。
- `WSAStartup`、非ブロッキング`connect`／`select`／部分送受信、切断時と250ms超過時のfail-open。
- `STARTUPINFOEXW`、`PROC_THREAD_ATTRIBUTE_HANDLE_LIST`によるNULのみの継承、
  `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`、アプリ終了後の常駐維持。
- `GetAppProperty(GUID_PROP_INPUTSCOPE)`の読み取りcookie、パスワード／数値パスワード欄、
  OnTestが副作用を持たず実キーだけを一度送ること。
- `RegisterGUID`と`GUID_PROP_ATTRIBUTE`、`ShiftStart/ShiftEnd`のUTF-16範囲、サロゲートペア、
  `SetSelection`と`GetTextExt`、layout更新後の候補位置、文節／ページ移動。
- SYNC拒否時のASYNC順序と世代無効化、未確定／変換中の欄・アプリ切替、
  commit＋次composition、トグルrepeat／修飾キー順序、サーバ再起動時のdirect復元。
