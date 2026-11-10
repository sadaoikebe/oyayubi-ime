# Stage 2 前半 — セッションの入力・確定・リセット

判断元: `../fable-decisions-2026-11-10.md` の Stage 2。今回の対象はトランスクリプト基盤、
実装順1・2・6。文節保持・移動・文節候補・直接入力切替は次段。

## 入力と確定

リテラルは変換しない1文字のトークンとして composition に入る。かな列をリテラルで分割し、
各列を独立に BOS/EOS つきでデコードする。辞書の語もPOS連接も境界を跨がない。
候補列挙にも同じ分割を使う。デコーダの RID 枝刈り（1-best）と読みprefix枝刈り（nbest）は維持。

- 変換中に次の文字の down: 選択中の表示を commit し、新 composition を開始。親指Spaceの押下は保持。
- 数字・記号・Shift+英字も文字として扱う。数字の旧全文候補ショートカットは文字入力に統一。
  全文候補の Space/↑/↓ は従来どおり。文節候補の数字選択は次段。
- Enter: 未解決の打鍵も含め全確定。Esc: 変換中は変換取消、入力中は破棄。
- BS: 変換中は変換取消だけ（トークン保持）、入力中は末尾1トークン削除。
- 編集キーの up は操作しない。未解決の打鍵の後にリテラルや編集キーが来る場合、
  既存FSMのtimeout経路で未解決打鍵を先に解決してから処理する。
- Ctrl/Alt/Win 併用は素通し。ShiftはUS ANSIの大文字・記号として取り込み、
  Shiftが先に離れても文字のupをかなイベントとして再解釈しない。

FSMの80ms/20ms定数・同時打鍵規則は変更していない。

## 句読点の出典と Sadao 確認 TODO

`data/nicola_a.json` の空だった単独面は、同じリポジトリの
`docs/PLAN.md`「上段/下段（抜粋）」の表から Q=「。」、P=「，」、Z=「．」、/=「・」を転記した。
表のかな/親指面にはJSONとの差異があるので、親指面の空欄は上書きしない。
Pの正本値「，」を保ち、今回指定された合格文のためセッションの日本語表示で「、」へ正規化する。
このPの表示方針も Sadao 確認対象。全角/半角の追加設定や未定のNICOLA割り当ては作らない。

| 未確定の割り当て | 現状 / 確認事項 |
| --- | --- |
| Q/右親指 | JSONは空、PLAN抜粋は「゜」。矛盾するため空欄を維持 |
| Z/右親指 | JSONは空、PLAN抜粋は「び」。矛盾するため空欄を維持 |
| ;/左親指 | JSONは空、PLAN抜粋は「ぱ」。矛盾するため空欄を維持 |
| //左親指 | JSONは空、PLAN抜粋は「゛」。矛盾するため空欄を維持 |
| 数字段および他のANSI記号のNICOLA親指面 | 正本に無し。追加せず、数字・記号自体をリテラル入力 |
| NICOLAのShift専用記号面 | 正本に無し。追加せず、Shift時は通常のUS ANSI文字を入力 |
| Pの読点表示 | 正本「，」→表示「、」の方針を要確認 |

US ANSIの通常文字はNICOLA割り当てとは別の物理配列として `chord/keys.py` に定義。
0–9 と Shift時の `)!@#$%^&*(`、英字のShift時A–Z、OEM記号の通常/Shift文字を扱う。
かなのあるOEMキーの単独面は従来どおりNICOLAを優先（例: `;` は「ん」）。

## サーバの追加プロトコル

従来のJSON 1行1要求。`{"op":"reset"}` はフォーカス喪失・アプリ側のcomposition打ち切りを通知する。
commitは返さず、トークン、composition、候補、FSM、タイマー、押下キー、修飾追跡を捨てる。
既にアプリへ返したcommitを取り消す操作ではない。FSM通常処理で押下キーを保持する仕様は変えず、
セッションのリセットでのみ押下状態を完全に捨てる。

キー要求の任意フィールド `mods` は小文字名の配列（例 `{"op":"down","vk":65,"t":100,"mods":["shift"]}`）。
省略した従来クライアントでは修飾キー自体のdown/upで追跡する。TIPからの修飾・reset通知の接続はStage 3。
今回のLinux側検証は本番の `server.dispatch()` をそのまま通す。

## 検証

トランスクリプトの形式とケースは [tests/transcripts/README.md](../tests/transcripts/README.md)。

```sh
python3 -m unittest tests.test_fsm tests.test_session tests.test_evaluation tests.test_server tests.test_transcripts -v
python3 -m tools.wp0.run_regression
python3 -m tools.wp0.run_mozc_eval --details /tmp/oyayubi-stage2-mozc.jsonl
```

固定辞書の候補選択なしケースと、実辞書＋既存の全文候補操作ケースの両方で
「今日は、機関の校正をした。」を確定する。実辞書の1-bestは「構成」であることを隠さず、
辞書コストの変更で合格文を作らない。後者は文節候補の実装を意味しない。

2026-11-10 / Linux・Python 3.12 の実行結果:

| 検証 | 結果 |
| --- | --- |
| 単体＋トランスクリプト | 41/41 PASS（既存21＋stdio追加1＋トランスクリプト19） |
| 回帰ゲート `tools/wp0/run_regression.py` | 5/5 PASS |
| Mozc 読み一致 | 92.4%（549/594）変更前と同じ |
| Mozc 表記一致 | 77.1%（458/594）変更前と同じ |
| Mozc コマンド基準 | 64.2%（380/592）変更前と同じ |
| Mozc 非エンコード行 | 7件（従来どおり） |

19トランスクリプトの結果（全てPASS）:

| ケースファイル（tests/transcripts/） | 主な確認 |
| --- | --- |
| acceptance_sentence.json | 固定辞書、候補選択なしで指定文を確定 |
| acceptance_real_dictionary.json | 実辞書、全文候補で校正を選び指定文を確定 |
| punctuation_faces.json | Q/P/Z// の単独面 |
| native_literals.json | 数字・記号・Shift英字・修飾・リピート |
| converted_literals.json | 変換後の数字/Shift英字/句読点で自動確定 |
| literal_boundaries.json | 辞書語とPOS連接の双方をBOS/EOSで分離、候補も分離 |
| selected_then_continue.json | 選択表示を保持して通常文字／親指文字へ続行 |
| repeat_after_conversion.json | かな押下継続中のリピートでは確定せず、新打鍵のみ確定 |
| escape_twice.json | 変換取消→破棄、up時は操作しない |
| backspace_conversion.json | 変換取消時はトークン保持、その後一文字削除 |
| pending_enter.json | キー解放前のEnterで全確定 |
| pending_literal.json | 未解決打鍵とリテラルの順序 |
| timeouts.json | 80ms直前/丁度、重複なし、自動確定 |
| reset_s2.json | 文字押下中のリセット、遅延up/timeout、再開 |
| reset_s3.json | 親指押下中のリセット、遅延up/timeout、再開 |
| reset_s4.json | 文字→親指のリセット、遅延up/timeout、再開 |
| reset_s5.json | 親指→文字のリセット、遅延up/timeout、再開 |
| reset_converted.json | 変換状態と修飾を破棄し、確定済み文は保持 |
| reset_held_literal.json | Shift/リテラル押下状態も破棄し同じキーで再開 |

変更ファイル一覧:

- `data/nicola_a.json`
- `oyayubi/chord/keys.py`
- `oyayubi/ime/session.py`
- `oyayubi/ime/server.py`
- `tools/wp0/run_mozc_eval.py`
- `tests/test_server.py`
- `tests/transcript.py`
- `tests/test_transcripts.py`
- `tests/transcripts/README.md`
- `tests/transcripts/` の上表19個のJSONファイル
- `docs/STAGE2.md`
- `docs/STATUS.md`
- `docs/EVALUATION.md`

コミットは作成していない。TIPのWindowsビルド・実機検証は今回の対象外。
