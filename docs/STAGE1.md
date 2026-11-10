# Stage 1 — 実打鍵ログ

Fable 5.2 判断メモ（2026-11-10）の Stage 1 用。横長 Space 1 本の
ノート PC 標準キーボードで、実際のキーイベントからトークン正解率を測る。
FSM の80 ms / 20 ms、同時打鍵判定、Mozc 評価は変更していない。
記録モードは辞書・変換器・確定学習を使わない。TSF TIP の登録・ビルドも不要。

## Sadao が Windows のノート PC で実行する手順

1. この変更を含むリポジトリを Windows 側に用意する。Python の標準ライブラリだけで動く。
   外付け親指キーやリマッパーを使わず、普段のノート PC の Space 1 本を使う。
   配列は既存ホストと同じ NICOLA-A（US ANSI の VK 対応）。
2. リポジトリ直下の PowerShell で実行する。既存ファイルへの上書きは拒否する。

   ```powershell
   cd C:\Users\marur\oyayubi-ime
   $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
   python -m oyayubi.ime.host_win32 --record "data\recordings\stage1-$stamp.jsonl"
   ```

3. 記録窓をクリックし、お題と「入力する読み」を確認する。40文を順番に表示する。
   **普段の速度で、打ち直しなし**。漢字の見た目を再現するのでなく、表示した読みを
   NICOLA で入力する。助詞も表示どおり「は・へ・を」、数字は数字キーで入力する。
   「、」は P 単独、「。」は Q 単独、「？」は Space + /。
   例: 「次の電車は8時に来る。」なら「つぎのでんしゃは8じにくる。」を入力する。
   Shift キーと親指 Space を取り違えない。
4. かな漢字変換は不要。習慣で押した Space 単独・Enter も記録される。
   入力結果・正誤は表示しない。誤打してもそのまま最後まで打つ。
   Backspace・Esc を押してしまってもイベントは残り、過去の入力は消えず、文も戻らない。
5. 文を打ち終えたら、**全キーを離して F9 を押して離す**と次の文へ進む。
   Enter は通常の確定操作のログ用に残してあり、文を進めない。
   F9 解放時に他のキーが残っている場合は進まない。全キーを離して F9 を押し直す。
   ノート PC の Fn ロック設定によっては **Fn + F9** が必要。
6. フォーカスを別の窓へ移すと、その文は `interrupted` になり採点対象外となる。
   窓へ戻ると次の文へ進む。途中から再開したり、同じ文をやり直したりはしない。
   40文終了の表示が出たら窓を閉じる。途中で閉じた文も完了文とは分けて保存する。
7. 30分を目安に採取する。40文を早く終えたら、新しいファイル名で次のセットを採取できる。
   同じファイルへの追記・セッション再開はしない。最初の動作確認用ファイルと本採取は分ける。
   ログを Linux 側へコピーし、下の再生コマンドを実行する。

ログ・評価レポートにはお題本文と打鍵列を含む。保存先の `data/recordings/` は git 対象外。
記録はこの窓が受けたイベントだけで、グローバルなキーフックではない。
窓の IME コンテキストは切り離すが、実機では既存 IME/TIP が介入しないことも確認する。

## Linux で再生・採点

リポジトリ直下で実行する。Windows・辞書の取得は不要。

```sh
python3 -m tools.replay_stage1 data/recordings/stage1-20261111-120000.jsonl \
  --report data/recordings/stage1-report.json
# 完全な機械可読レポートを標準出力へ出す場合:
python3 -m tools.replay_stage1 tests/fixtures/stage1_synthetic.jsonl --json
# 同梱の合成ログを人が読む場合:
python3 -m tools.replay_stage1 tests/fixtures/stage1_synthetic.jsonl
```

完了文ごとに新しい `ChordFsm` へキーを時系列に渡す。各イベントより前に期限到来した
タイマーを発火し、**期限と同じ時刻のキーよりもタイマーを先に処理する**。
最後の保留出力と数字・編集キー境界は、既存 FSM の timeout 経路で解決する。
これは仮想時計による決定的な再生で、Win32 の16 ms WM_TIMER 配送遅延を再現するものではない。
`ms` はハードウェアの接点時刻ではなく、窓のメッセージ処理時の QPC 測定時刻。

期待列はログ内の `reading` と `data/nicola_a.json` から逆引きする。
単独面は `Plain(key)`、親指面は左右を区別しない `AmbShift(key)`。
複数面にある文字は単独面を優先する。句読点も物理キーのトークンで比較し、
P の `，` は入力表示に合わせ `、` に対応させる。ASCII 数字は `literal`。
未対応文字は黙って除外せずエラーにする。お題は自作の40文で、
構成/校正、機械/機会、橋/箸、感想/乾燥、期間/機関などの同音語、句読点、数字を含む。

正解率はトークン単位の Levenshtein 距離で定義する。

```text
N = 期待トークン数
S, D, I = 置換、欠落、余分なトークン数（各コスト1）
トークン正解率 = max(0, 1 - (S + D + I) / N)
```

全完了文の N と誤り数を合算して計算する。文ごとの率の平均ではない。
一致数も別途出力するため、挿入があれば「一致数 / N」と率が異なる。
期待列との対応が同点の場合は一致/置換、欠落、挿入の順に選ぶ。
`ThumbTap`（Space 単独）は変換操作で、読みだけから正解位置を指定できないため採点から除外する。
`raw_tokens` と `thumb_taps` には残す。Backspace・Enter・Esc は回数を別集計し、
過去のトークンを削除・修正しない。キーの自動リピートも生ログに残すが、
FSM の既存のリピート抑止に従い重複トークンにはしない。

失敗はアラインメントの編集1件ごとに、以下の排他的な型へ分類する。

| レポートのキー | 分類条件 |
| --- | --- |
| `thumb_overlap_shift_loss` | 期待 `AmbShift(k)` → 実出力 `Plain(k)`。その文字の押下時に Space が押下継続中で、その押下区間が既に先行 `AmbShift` に使われている |
| `unintended_chord` | 期待 `Plain(k)` → 実出力 `AmbShift(k)` |
| `missed_chord` | 期待 `AmbShift(k)` → 実出力 `Plain(k)` で、上の継続 Space 条件を満たさない |
| `other` | 異なるキーへの置換、挿入、欠落、その他 |

**型は推定であり、物理的な原因の断定ではない。** 特に単一 Space では、
片方の親指を離す前にもう片方が着地した事実はイベントにならない。
長い押しっぱなしや単なる誤打と完全には区別できない。
「取りこぼし」には Space 自体の押し忘れも含み、対応が曖昧な列では分類も揺れる。
`errors` に期待/実出力、文内トークン位置、実出力元のイベント番号（文内0始まり）、
出力時刻を保存するので、生イベントと併せて読む。
候補選択・変換の良さを測る評価ではない。

`interrupted` / `incomplete` 文は一覧を出して採点から除外する。
`session_end` がない場合も警告を出し、既に `complete` で閉じた文だけ採点できる。
壊れた JSON、不正な型・キーコード・時刻逆転・文ID不整合は行番号つきエラー。
最終行が途中で切れたファイルもエラーにするため、必要なら原本を保管したうえで
切れた最終行だけを除いたコピーを評価する。
採点対象が0文なら率は未定義（JSON は `null`）、終了コード1。不正ログは終了コード2。

## JSONL 仕様 (`oyayubi.stage1.v1`)

UTF-8、1行1 JSON オブジェクト。全行に `type` と `ms` がある。
`ms` は QPC 起点からの非負の整数ミリ秒で、単調非減少（同一時刻可）。
ホストで `floor((QPC_now - qpc_origin) * 1000 / qpc_frequency)` を計算する。
生 QPC tick の絶対値を `ms` と取り違えない。

| type | 追加フィールド |
| --- | --- |
| `session` | `schema`, `clock="QPC"`, `time_unit="ms"`, `qpc_frequency`（tick/秒）, `qpc_origin`（起点tick）, `layout="NICOLA-A"`, `keyboard="single-space-laptop"`, `next_prompt_vk=120`, `prompt_count` |
| `prompt_start` | `id`, `text`（表示文）, `reading`（入力する読み。句読点と数字を含む） |
| `key` | `id`（対象文）, `vk`（Win32 VK、整数0〜255）, `action="down"` または `"up"` |
| `prompt_end` | `id`, `status="complete" / "interrupted" / "incomplete"`, `event_count`, `held_vks`（終了時の押下キー一覧） |
| `focus` | `focused`（真偽値） |
| `session_end` | `reason="complete" / "window_closed"` |

例（中間のイベントは省略）:

```jsonl
{"type":"session","ms":0,"schema":"oyayubi.stage1.v1","clock":"QPC","time_unit":"ms","qpc_frequency":10000000,"qpc_origin":123456789,"layout":"NICOLA-A","keyboard":"single-space-laptop","next_prompt_vk":120,"prompt_count":40}
{"type":"prompt_start","ms":0,"id":"daily-01","text":"おはよう、今日は少し早く起きた。","reading":"おはよう、きょうはすこしはやくおきた。"}
{"type":"key","ms":500,"id":"daily-01","vk":32,"action":"down"}
{"type":"key","ms":520,"id":"daily-01","vk":74,"action":"down"}
```

F9 自身の down/up も当該文の `key` として残り、解放後に `prompt_end` →
次の `prompt_start` が同じ時刻で並ぶ。最後の文の後は `session_end`。
フォーカス喪失では `focus(false)` → `prompt_end(interrupted)`、復帰すると
`focus(true)` → 次の `prompt_start`。同じ文は再開しない。
各行を追記・flush する。通常終了は finally でも閉じ、クラッシュ時も既出行は残る
（電源断に対する fsync 保証はない）。1ファイル1セッションで、時刻順は行順を優先する。

記録中核は `oyayubi/ime/recording.py`、表示と QPC/Win32 メッセージ処理は
`oyayubi/ime/host_win32.py`、再生と採点は `tools/replay_stage1.py` に分離した。
標準出力の JSON レポートは `oyayubi.stage1.report.v1` で、`summary`、
文ごとの期待列・実列・編集詳細、除外文、正常終了印の有無を含む。

## 検証と Windows で残る確認

Linux / Python 3.12 で次を実行する。

```sh
python3 -m unittest discover -s tests -v
python3 -m tools.wp0.run_regression
python3 -m tools.replay_stage1 tests/fixtures/stage1_synthetic.jsonl
python3 -m py_compile oyayubi/ime/host_win32.py
```

固定した合成 JSONL は8文。期待16、実出力16、一致11、S=4 / D=1 / I=1、
正解率62.50%。内訳は重なりでシフト消失1、意図しない同時打鍵1、
取りこぼし1、その他3。**これは道具の検証値であり、実打鍵の成績ではない。**
追加テストでは79/80/81 ms と19/20 ms の境界、リピート、Backspace の保持、
全キー解放前の F9、フォーカス中断、途中終了、不正ログ、CLI の JSON 出力を確認する。

2026-11-11、Linux / Python 3.12.3 での実行結果:
全94テスト合格（既存79件＋Stage 1 追加15件、34.849秒）、回帰ゲート5/5合格。
合成ログの CLI 出力は上記の数値と一致。Win32 ホストの `py_compile` と
`git diff --check` も成功。FSM 本体と `run_mozc_eval` に差分なし。

**Win32 依存部分は Windows でのスモーク確認が別途必要。Linux のテストでは未確認。**
Sadao の本採取前に、別名の短いログで以下を確認する。

- 64 bit Python で記録窓が開き、日本語のお題と読みが折り返される。辞書なしで起動する。
- 内蔵キーボードの文字、Space、P/Q、/、数字、Backspace、Enter、F9 の down/up が
  Windows の IME に奪われず記録される。F9 がマルチメディアキーなら Fn を併用する。
  OEM キーの VK 対応（特に `;` と `/`）も実機のキーボード配列で確認する。
- Space を押したまま J→K を打つ列、Space を離して押し直す列の両方が欠けずに記録される。
- F9 のキーリピートで文が飛ばない。他キー押下中には進まない。
- Alt+Tab で文が `interrupted` になり、戻ると次の文へ進む。
  窓の×・Alt+F4・40文完了の各経路でファイルが閉じる。
- QPC `ms` が非減少で、保存したログが Linux の再生ツールで受理される。
  保存失敗時にエラーが表示され、記録し続けているように見えない。

実打鍵の結果が出るまでは FSM を凍結する。ログの推定分類だけでタイミング定数を変えない。
