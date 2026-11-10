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

## 句読点の出典と Sadao 決定済み事項

`data/nicola_a.json` の単独面 Q=「。」、P=「，」、Z=「．」、/=「・」は
`docs/PLAN.md`「上段/下段（抜粋）」から転記した。以下は Sadao が 2026-11-10 に決定。

| 割り当て / 方針 | 決定内容 |
| --- | --- |
| Q/右親指 | 空欄。PLAN抜粋の「゜」は採用しない |
| Z/右親指 | 空欄。PLAN抜粋の「び」は採用しない |
| ;/左親指 | 空欄。PLAN抜粋の「ぱ」は採用しない |
| //左親指 | 「？」。PLAN抜粋の「゛」は採用しない。データ反映・混在面のリテラル処理とも実装済み |
| Pの読点表示 | 正本「，」を表示「、」に正規化。同じ文字を出すキーの重複を許容 |

「/」左面のデータ反映とリテラル処理は実装済み（下記「リテラル面とかな面の混在」）。
単一 Space の入力は左「？」・右「ぉ」のあいまいトークンのまま保持し、変換・文節候補で解消する。

数字段および他のANSI記号のNICOLA親指面とNICOLAのShift専用記号面は正本に無く、追加しない。
数字・記号自体はリテラル入力し、Shift時は通常のUS ANSI文字を入力する。

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

## Stage 2B — 文節と文節候補（実装順3・4）

2026-11-10、ブランチ `docs-and-eval-2026-11-10` / 開始HEAD `05f1f38`。
Stage 2 前半の全文候補を文節候補に置き換えた。今回も辞書・ユーザー辞書のコスト、
FSMの80ms/20msと同時打鍵規則、1-bestのRID枝刈り、評価用nbestの読みprefix枝刈りは変更していない。

### 文節境界と保持

`decode_tokens()` が返す各 Piece に、トークン区間 `[start,end)` とRIDを付加した。
既存のPOSはLID。探索や採点はそのままで、選ばれた最良経路の情報を捨てずに返す変更。
`e7_chunk_context.py` の `content()` と同じ判定を `id.def` に適用する:

- 名詞・動詞・形容詞のうち、接尾・非自立以外。
- 副詞・連体詞・接続詞・感動詞・接頭詞。

最良経路の各自立語の頭で区切り、後続の助詞・助動詞などは直前の文節に付ける。
先頭に付属語がある場合は、雛形どおり最初の自立語の直前も境界になる。
リテラルは独立した固定表示の区間にし、左右をBOS/EOSで分離する。文節移動では飛ばす。
文節の境界は変換中に変えず、選択した文節だけを置き換える。
未操作の文節も自動再変換しない。数字・巡回で選択した文節には `fixed=true` を付ける。
隣の品詞が変わった場合、再訪した文節の候補だけをその新しい品詞で採点し直す。
固定済み表示が新しいコスト閾値の外に出ても保持し、候補の選択位置を `-1`（強調行なし）とする。

実辞書の「きかんのこうせいをした」は **「機関の／構成を／した」**。
1-bestは「構成」のままで、合格のための辞書コスト調整はしていない。

### 候補探索と打ち切り

`oyayubi/ime/bunsetsu.py` は、既存1-best/nbestとは独立した候補探索を持つ。
対象文節の元のトークン区間で `_spans_from()` の左右両面・全辞書エントリを使い、
トークン区間全体を覆う経路を列挙する。自立語数や語分割を元の最良経路と同じには制限しない。
スコアは `T(左隣の末尾RID, 最初のLID) + Σ単語コスト + Σ内部連接 + T(最後のRID, 右隣の先頭LID)`。
端とリテラルに接する側はBOS/EOS。最良接尾コストの逆向きDPをポテンシャルにしたA*で、
負のコストも含めコスト順を保証する。同表記は最安の経路にまとめ、その経路の読み・両端POSを保持する。
RIDや読みごとのbeam・件数上限は設けない。9件の表示と次ページ有無に必要な分を遅延列挙して保持する。
先頭から↑で末尾へ巡回する操作や、現在の選択位置の探索では、末尾まで列挙する場合がある。

辞書経路の閾値は **その区間・両隣POSでの最良辞書経路 + 6000（等号を含む）**。
6000は既存の未知1文字ペナルティ `UNK_COST=12000` の半分を採った工学的な余裕幅であり、
学習済みの確率閾値ではない。件数8では有効な同音語まで失う一方、著しく高価な細切れ経路の
組合せは探索量を増やすため、件数によらない相対コスト幅で分離した。
合格文に合わせて辞書や閾値を調整していない。実辞書の「こうせい」（BOS/EOS）では
構成4632に対し校正6273（差1641）、更生6366、恒星6835、攻勢6867で、
6000の幅には余裕があり、辞書候補は167表記。6000を含め6001を除外する境界と、
100同音語を件数上限なしで取得できることを固定辞書で検証した。
将来の実打鍵での候補分布・遅延を見て再検討する値であり、全同音語が必ず閾値内とは主張しない。

辞書候補からは合成の未知1文字アークを除く。単語コストが12000以上の**実在エントリ**まで
未知語扱いで落とさないよう、アークに明示的な `unknown` 属性を付けた。

### かな候補

辞書候補の後ろに、その区間の**全てのあり得る読み**について、ひらがな・カタカナをこの順で付ける。
辞書prefixにない面にも未知1文字アークで到達し、読みごとの最安経路コスト順に並べる。
かな候補には6000の閾値も件数上限も適用しない。
同じ `(位置,RID,読みprefix)` には最安コストだけを保持できるため、同音の表記・品詞経路の
重複探索を抑えつつ、左右の読みを欠落させない。辞書候補と同じ表記になっても末尾のかな候補は残す。
両隣へのPOSは、その読みの最安経路の両端を使う。左右親指のラベルは表示しない。

実辞書の未知語「はーぞぐ」は最良経路では **「は／びゆる」** になる。
合格トランスクリプトでは、先頭文節のかな「は」、次文節のかな「ーぞぐ」を選び、
文節を伸縮せず **「はーぞぐ」** を確定する。未知語全体が1文節になるとの主張はしない。

### キーとプロトコル

| 操作 | 振る舞い |
| --- | --- |
| 最初のSpace | 最初の非リテラル文節に注目して候補を開く。表示は保持 |
| ← / → | 非リテラル文節間を移動。両端では停止 |
| Space / ↓ / ↑ | 注目文節のみ次 / 次 / 前候補。ページ境界を越え、全候補の末尾・先頭で巡回 |
| PageDown / PageUp | 次 / 前の9件ページの先頭候補を選択。ページ範囲外では停止 |
| 1–9 | 表示中ページの行を選択し文節を固定。compositionは継続し、commitしない。空行の番号は消費するだけ |
| 0 / Shift+数字 | 従来のリテラル入力。変換中なら現在の表示を確定して次の入力へ |
| 次の文字 / Enter | 選択を含む全文節の表示を確定。文字なら新compositionを開始 |
| Esc / BS（変換中） | 選択と候補を捨て、元トークンからの最良表示へ戻す |
| reset | 文節・候補・固定・ページ・注目位置も全破棄 |

候補キーのupとHID repeatは再操作しない。未解決Spaceがあるときは既存timeout経路で先に解決する。
Ctrl/Alt/Win併用の素通し、Shift+←→が文節伸縮を行わないことも維持。
かな以外のリテラルしかないcompositionには候補を作らず、数字は通常文字として入力できる。

サーバ返信に次を追加した（既存フィールドも継続）:

- `segments`: 各文節・リテラルの `start/end`（元トークン位置）、`surface/reading`、`literal/fixed`、
  `display_start/display_end`（表示中のUnicodeコードポイント位置）。全て半開区間。
- `focus`: 注目文節の `segments` 内index、なしは `-1`。
- `candidates`: 注目文節の現在ページだけ、最大9件。`cand_index` もページ内index、選択行なしは `-1`。
- `candidate_page`: 0始まり。`has_next_page`: 次ページの有無。
- `candidate_kinds`: 各候補の `dictionary` / `kana` / `literal`（混在面対応で追加）。候補自体は文字列。

Stage 3のTIP側でこの情報を表示・UTF-16座標へ変換する。今回TIP・常駐化・直接入力切替は実装していない。

### 2B 検証

トランスクリプトversion 2は、従来の全表示項目に加えて、文節列・注目位置・ページ・候補種別・
次ページ有無を**各イベント直後に明示**する。期待値は静的JSONで、実行時に生成しない。

| 追加ケース（`tests/transcripts/`） | 確認内容 | 結果 |
| --- | --- | --- |
| `bunsetsu_acceptance_real.json` | 実辞書。今日は／、／機関の／構成をを保持し、機関の・校正をを数字で選択。左右再訪でも保持。続く「した」の打鍵で自動確定し、最終文「今日は、機関の校正をした。」 | PASS |
| `bunsetsu_unknown_real.json` | 実辞書。は／びゆるの末尾かな候補をページ送り・数字選択し「はーぞぐ」を確定 | PASS |
| `bunsetsu_context_fixed.json` | 両隣の選択POSを変えると中の候補順位だけ更新し、表示は自動変更しない | PASS |
| `bunsetsu_fixed_outside_window.json` | 隣の変更で固定候補が閾値外になっても表示を保持、選択行なしから再選択 | PASS |
| `bunsetsu_pages.json` | 9件ページ、Space/上下の境界越え・巡回、PgUp/Down、数字、リピート、閾値6000/6001 | PASS |
| `bunsetsu_kana_absent.json` | 辞書prefixなしでも左右両面の全読みが出てカタカナで確定 | PASS |
| `bunsetsu_literal_navigation.json` | リテラルを跨がない語/POS、移動時のスキップ、Esc・BS・resetで固定解除 | PASS |
| `bunsetsu_control_pending.json` | 保留Spaceと操作順、数字up/リピート、修飾矢印、Shift数字で継続確定 | PASS |

従来19ケースも保持した。全文8候補→文節9候補＋かな末尾という仕様変更に必要な
候補一覧・位置の期待値だけを更新。`converted_literals.json` の数字入力確認は、
1–9が候補選択になるため1から0へ変更し、同じ「変換表示を確定して数字を入力」の確認を残した。
それ以外の従来ケースのイベント・表示・確定結果・FSM期待値は維持した。

単体テスト `tests/test_bunsetsu.py` は4件。負の連接、同RIDへの別経路、左右面、別分割、
同表記のPOS違いを含む全経路総当たりと、全16通りの両隣POSで候補の表記・読み・コスト順を照合する。
さらに件数上限・遅延取得・未知prefix・高コストの実在語・content品詞判定を確認する。

```sh
python3 -m unittest tests.test_fsm tests.test_session tests.test_evaluation tests.test_server tests.test_bunsetsu tests.test_transcripts -v
python3 -m tools.wp0.run_regression
python3 -m tools.wp0.run_mozc_eval --details /tmp/oyayubi-stage2b-mozc.jsonl
```

| 検証 | 2B結果 | 前段との差 |
| --- | --- | --- |
| 単体＋トランスクリプト | 53/53 PASS（従来41＋候補探索4＋トランスクリプト8） | 12件追加 |
| 回帰ゲート | 5/5 PASS | 維持 |
| Mozc 読み一致 | 92.4%（549/594） | 同じ |
| Mozc 表記一致 | 77.1%（458/594） | 同じ |
| Mozc コマンド基準 | 64.2%（380/592） | 同じ |
| Mozc 非エンコード行 | 7件 | 同じ |

コマンド基準の評価は比較可能性のため従来の全文nbest近似を維持し、今回の文節候補を採点に混ぜていない。
候補操作・両面の到達性は追加トランスクリプトと総当たり照合で別途固定した。

### 2B 変更ファイル一覧

- 追加: `oyayubi/ime/bunsetsu.py`、`tests/test_bunsetsu.py`、上表の `bunsetsu_*.json` 8件。
- 変更: `oyayubi/ime/session.py`、`oyayubi/ime/server.py`、`tools/wp0/decode.py`、`tools/wp0/token_viterbi.py`。
- 変更: `tests/transcript.py`、`tests/test_server.py`、`tests/transcripts/README.md`、`docs/STAGE2.md`。
- 候補表示の期待値更新: `tests/transcripts/acceptance_real_dictionary.json`、`acceptance_sentence.json`、
  `backspace_conversion.json`、`converted_literals.json`、`escape_twice.json`、`literal_boundaries.json`、
  `native_literals.json`、`punctuation_faces.json`、`repeat_after_conversion.json`、`reset_converted.json`、
  `selected_then_continue.json`、`timeouts.json`。

コミットは作成していない。

## リテラル面とかな面の混在 — / 左親指「？」（実装済み）

2026-11-10、ブランチ `docs-and-eval-2026-11-10` / 開始HEAD `e692ad9`。
Sadaoの決定を反映し、`data/nicola_a.json` の `/` は単独「・」／左「？」／右「ぉ」。
Terraの `//left` 保留注記も実装済みに更新した。Space＋/ の入力自体は左右未確定の1トークンである。

### リテラル面の判定と根拠

`tools/wp0/expand.py:is_literal_face()` を単独面・親指面・評価用の逆引きで共有する。
読みとして辞書探索に渡す文字集合は **U+3041–U+3096（ぁ–ゖ）と U+30FC（ー）**。
面にこの集合以外の文字を含む場合はリテラル面とし、1 Unicodeコードポイントであることを要求する。
未定義のnull面は従来どおり除外する。

これはStage 2Aの単独面判定をそのまま一般化したもの。小書き「ぉ」、濁音「ゔ」、長音「ー」を
かな読みとして維持し、句読点・記号・数字・英字・片仮名・独立した濁点/半濁点はリテラルとする。
Unicodeの句読点カテゴリだけでは数字・英字などのStage 2Aリテラルと揃わず、
「ひらがな以外」だけでは長音「ー」を失うため、この既存の読み集合を根拠にする。
NICOLA面の正本「，」→表示「、」も両面で共通化。明示的な `kind=literal` は既に表示文字なので保持する。
今回、片仮名面や新しいキー割り当てを追加したわけではない。

### デコードと候補

`_spans_from()` はリテラル面を辞書prefix・未知かなに渡さず、表記＝読み＝その表示文字、
単語コスト0の専用1トークンアークを作る。`Piece.literal` で辞書アークと区別する。
同じトークンのかな面は従来どおり探索でき、かな側を通る辞書語は複数トークンに跨がれる。

`decode_tokens()`、`nbest_tokens()`、文節候補のA*は同じアークと連接計算を使う。
リテラルへ入る際は直前のかな列をEOSで閉じ、次のかな列はBOSから開始する。
したがって辞書語もPOS連接もリテラルを跨がない。内部の境界状態は通常のRID=0とも区別し、
先頭・末尾・連続リテラルの空のかな列には余分なBOS→EOSコストを加えない。
これはStage 2Aで各かな列を独立に変換して連結する意味と同じ。
1-bestのRID枝刈りとnbestの読みprefix枝刈りは、それぞれ従来のまま維持する。

文節候補には `kind=literal` を追加。リテラル面だけで区間を覆う候補はそのまま1回表示し、
ひらがな・カタカナの重複候補にはしない。かなとリテラルが混ざる経路のかな候補も、
リテラル部分を保持する。リテラル面はコスト閾値外でも末尾の制限なし探索で必ず到達できる。
辞書候補の幅6000は変更せず、その基準を**実際の辞書語を少なくとも1つ含む最良経路**に置く。
リテラルだけの安い経路を基準にして「ぉ」側の既存辞書候補を脱落させないためである。
両探索の接尾コストは逆向きDPで計算し、候補の件数上限は追加していない。

最良経路でリテラル面を採った場合は前後と独立した区間にする。ただし、元はあいまいトークンなので
`segments.literal=false` とし、注目・選択・再訪できる。「？」から「ぉ」側へも選び直せる。
選択候補の両端POSは境界ならBOS/EOSとして隣の候補採点に渡す。
Stage 2Aの明示的なリテラルトークンは従来どおり `segments.literal=true`、移動対象外。

評価用の逆引きは読み集合だけをキーにする。「？」を新しい評価対象として読み扱いせず、
「ぉ」の逆引きには**「？」を含むあいまいトークン全体**を返す。評価時にもリテラル面を隠さない。

### 追加検証と変更ファイル

| 追加テスト | 確認 |
| --- | --- |
| `tests/test_literal_faces.py`（4件） | 読み集合、1-best/nbestの独立列とのスコア一致、記号辞書語の遮断、連続リテラル、非自立POS隣接時の区間分離、全9通りの両隣POS、負コスト・閾値外のリテラル到達、高コストの実在辞書語、かな候補中のリテラル保持 |
| `slash_literal_choice.json` | Space＋/ のかな側1-bestから、文節候補の「？」を選んで確定 |
| `slash_kana_choice.json` | 同じトークンの「？」表示から「ぉ」・辞書変換「尾」を選べる。辞書コスト15000でも候補を保持 |
| `slash_literal_context.json` | 前後のかなと固定表示を保持し、中を「？→尾→？」に選び直して「科？区」を確定 |

トランスクリプトはversion 2。全イベントの表示・文節・候補種別・FSM・タイマー・確定文字を
静的JSONに記し、本番の `server.dispatch()` を通す。既存ケースの期待値は変更していない。

```sh
python3 -m unittest tests.test_fsm tests.test_session tests.test_evaluation tests.test_server tests.test_bunsetsu tests.test_literal_faces tests.test_transcripts -v
python3 -m tools.wp0.run_regression
python3 -m tools.wp0.run_mozc_eval --details /tmp/oyayubi-slash-mozc.jsonl
```

| 検証 | 結果 | 変更前との差 |
| --- | --- | --- |
| 単体＋トランスクリプト | 60/60 PASS | 既存53件維持＋単体4件＋トランスクリプト3件 |
| 回帰ゲート | 5/5 PASS | 維持 |
| Mozc 読み一致 | 92.4%（549/594） | 同じ |
| Mozc 表記一致 | 77.1%（458/594） | 同じ |
| Mozc コマンド基準 | 64.2%（380/592） | 同じ |
| Mozc 非エンコード行 | 7件 | 同じ |

変更ファイル:

- `data/nicola_a.json`
- `tools/wp0/expand.py`、`tools/wp0/decode.py`、`tools/wp0/token_viterbi.py`、`tools/wp0/run_mozc_eval.py`
- `oyayubi/ime/bunsetsu.py`、`oyayubi/ime/session.py`
- `tests/test_literal_faces.py`、`tests/transcripts/slash_literal_choice.json`、
  `tests/transcripts/slash_kana_choice.json`、`tests/transcripts/slash_literal_context.json`
- `tests/transcripts/README.md`、`docs/STAGE2.md`

FSMのタイミング定数・同時打鍵判定、枝刈りの分離、辞書・ユーザー辞書コストは変更していない。
コミットは作成していない。
