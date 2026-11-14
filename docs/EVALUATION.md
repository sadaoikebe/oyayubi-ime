# 変換の評価と診断

リポジトリ直下で実行する。評価の道具は標準ライブラリだけで動く（LM の評価を除く）。
評価は学習履歴・個人辞書・LM を使わない（LM の評価を除く）。手書きの補正辞書
`data/user_lexicon.jsonl` は評価条件に含む（`--no-user-lexicon` で外せる）。

```sh
python3 -m tools.wp0.fetch_mozc_dict          # 初回のみ。Mozc OSS 辞書（git には入れない）
python3 -m tools.wp0.run_regression           # 回帰ゲート
python3 -m tools.wp0.run_mozc_eval --details /tmp/mozc-results.jsonl
python3 -m tools.wp0.run_mozc_eval --unambiguous
python3 -m tools.wp0.run_mozc_eval --no-user-lexicon
python3 -m tools.wp0.fetch_ajimee              # 初回のみ。AJIMEE-Bench のデータ
python3 -m tools.wp0.run_ajimee_eval --details /tmp/ajimee-results.jsonl
python3 -m tools.wp0.run_ajimee_nbest_eval --k 1 3 8 30 --summary /tmp/ajimee-nbest-summary.json
python3 -m tools.wp0.run_ajimee_lm_eval --summary /tmp/ajimee-lm.json   # LM 込み（torch + transformers + sentencepiece が要る）
python3 -m tools.wp0.classify_mozc_errors --output /tmp/mozc-errors      # Mozc 評価の失敗の分類（新しい空のフォルダに出す）
python3 -m tools.replay_stage1 <ログ>          # 実打鍵ログの再生と採点（下の「実打鍵の記録」）
```

回帰ゲート（`run_regression`）は、変換を変えたら必ず通す 5 項目（amb の打鍵）: 「おはよう」と「どうぞ」の
1-best がそのままの表記になる、「どうぞ」の n-best（k=8）に王族・翁が出ない、「きどく」の n-best（k=8）に
既読と記憶の両方が残る、「きどくにない」の n-best（k=8）に既読にないと記憶にないの両方が残る。

条件の呼び方:

- **una**: 読みが分かっている（各文字を単独面・既知の面のトークンにする）。
- **amb**: 親指面は左右不明（`AmbShift`）。実打鍵の FSM は通さない、理想の打鍵。

連接表の全 0 行へのペナルティ（[CONVERSION.md](CONVERSION.md) §4）は、環境変数 `ZERO_ROW_PENALTY` で
変えられる（`ZERO_ROW_PENALTY=0 python3 -m tools.wp0.run_mozc_eval` で無効）。辞書を読み込むたびに読む。

辞書キャッシュは `data/cache/` に作る。名前は辞書マニフェストのハッシュとキャッシュ形式で決まり、
変わったら初回だけ辞書テキストから作り直す。

## Mozc の評価（`run_mozc_eval`）

Mozc OSS 辞書に付く `evaluation.tsv` を使う。

- 読み一致・表記一致: 1-best の読み／表記が TSV の正解と一致するか。
- コマンド基準: TSV の 4〜5 列目の指示で合否を出す。3 列目は Mozc の実出力で、正解とは限らない。
  `Mozc recorded pass` は TSV の status 列の集計で、Mozc を実行した値ではない。
  - `Conversion Expected [N]`: 1-best を先頭に置き、n-best の表記を重複除去して補った上位 N 件に
    引数の全文があるか（省略時 N=1）。文全体の候補による**近似**で、Mozc 本来の文節候補の順位和とは異なる。
  - `Conversion Match`: 1-best に引数の部分文字列があるか。`Not` は判定を反転する。
  - `Suggestion` などは対象外として件数を出す。入力できない読みも別に出す。
- `--details` は入力できる各行の `PASS` / `FAIL` / `SKIP` を JSONL に出す。

単語の全候補を調べるときは `nbest_tokens(..., k=十分大きな値)`（CONVERSION.md §8）。

## AJIMEE-Bench の評価

### 出典・ライセンス

[azooKey/AJIMEE-Bench](https://github.com/azooKey/AJIMEE-Bench) の
`JWTD_v2/v1/evaluation_items.json`（200 件）を使う。
取得元はコミット `401666cd56d1a570c2021798b64b6da4396bfd45` に固定。
SHA-256: `e9eb668fd6aa14b1e26436f429b5550108af0a1dfd443b8cea0bcb3ab3028fca`。

データは
[日本語Wikipedia入力誤りデータセット v2](https://nlp.ist.i.kyoto-u.ac.jp/?日本語Wikipedia入力誤りデータセット)
由来で、配布元が指定するライセンスは **CC BY-SA 3.0**
（[配布元 README](https://github.com/azooKey/AJIMEE-Bench/blob/401666cd56d1a570c2021798b64b6da4396bfd45/README.md)）。
配布元の `utils.py` / `test_utils.py` は CC0 1.0 と別記されている。
`fetch_ajimee` はデータと、出典・ライセンスを記載した README の原本を、git 対象外の
`data/cache/ajimee/` に保存する。配布元のコードは取得も実行もしない。データはリポジトリに入れない。
データを再配布・改変する場合は、出典・ライセンス表示、改変表示、継承条件を守る
（[CC BY-SA 3.0](https://creativecommons.org/licenses/by-sa/3.0/)）。
`--details` の出力はデータ由来の本文を含む。

### 採点

入力のカタカナをひらがなにし、NICOLA 表に無い文字（句読点、数字、英字など）で区切って素通しする。
複数の `expected_output` のどれかとの完全一致で採点する。`context_text` の有無ごとの集計も出すが、
文脈は変換に渡さない（LM の評価を除く）。実打鍵・FSM の誤り・句読点入力の可否は評価しない。

### 上位 k の到達率（`run_ajimee_nbest_eval`）

amb 条件で、n-best を使う 3 つの指標を分けて出す。

- `chunk_oracle@k`: 各かな区間の上位 k 候補から 1 つずつ選んで連結すると、正解のどれかになる率。
  複数区間では文全体の k 件より多くを調べることになる。
- `sentence@k`: 区間の候補をコストの和の順に組み合わせ、文全体の上位 k に正解が入る率。
  単一区間では上と一致する。
- `with_1best_chunk_oracle@k`: 各区間に `decode_tokens` の 1-best を先頭に足してから表記を重複除去し、
  上位 k で区間 oracle を測る（評価用。`nbest_tokens` 自体はコスト順を保つ）。

`decode_1best` は 1-best の一致率。`--summary` はデータの SHA-256・条件・各指標の件数・列挙時間を JSON に、
`--details` は区間の候補・コスト・文全体の候補・正解などを JSONL に保存する（本文を含む）。
前後比較は、同じ文数・データ・辞書・先頭追加の条件でそろえる。

### LM 込み（`run_ajimee_lm_eval`）

1 項目を 1 回の変換操作として、セッションと同じ経路（区間を左から順に採点し直し、左文脈は項目の
`context_text`（指定時）と同じ項目で既に選んだ表記）で測る。α=0 は LM を呼ばない 1-best。
同じ道具・同じデータでの on/off の比較で見る。α・k・左文脈の有無は `--alpha` `--k` `--context` で選ぶ。
モデルは `--model` でローカルのディレクトリを渡せる。

## 標準入出力のサーバ（試験と診断）

`python3 -m yuragi.ime.server` は 1 セッションを標準入出力の 1 行 1 JSON で動かす
（返事の項目は [CONVERSION.md](CONVERSION.md) §10）。辞書の読み込み直後に `gc.freeze()` する。
標準エラーへの本文のログは既定で出さない。診断が要るときだけ `--log-text`。
処理できない例外は、本文を含みうるメッセージを出さず、例外の型だけを報告する。
LM の時間は `--lm-timing` で標準エラーに出る（本文は出さない）。

操作の記録（トランスクリプト）の試験は `tests/transcripts/`（形式は同じフォルダの README）。

## 実打鍵の記録と再生

ノート PC の標準キーボード（横長の Space 1 本）で、実際のキーイベントからトークンの正解率を測る。
記録は辞書・変換器・学習を使わない。TIP の登録も要らない。

### 記録（Windows）

```powershell
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
python -m yuragi.ime.host_win32 --record "data\recordings\stage1-$stamp.jsonl"
```

- 既存のファイルには書かない（新しいファイル名で記録する）。お題は `data/stage1/prompts.json`
  （自作の 40 文。同音語・句読点・数字を含む）。`--prompts` で替えられる。
- 記録窓に、お題と「入力する読み」が出る。**普段の速度で、打ち直さずに**、表示した読みを NICOLA で打つ。
  「、」は P 単独、「。」は Q 単独、「？」は Space + /。誤打してもそのまま最後まで打つ。
- 文を打ち終えたら、**全キーを離して F12 を押して離す**と次の文へ進む（Fn が要る機種もある）。
  他のキーが押されたままなら進まない。
- フォーカスを別の窓へ移すと、その文は `interrupted` になり採点から外れる。窓へ戻ると次の文へ進む。
- 記録はこの窓が受けたイベントだけ（グローバルなキーフックではない）。
- ログと採点レポートは、お題の本文と打鍵列を含む。`data/recordings/` は git に入れない。

### 再生と採点（どの OS でも）

```sh
python3 -m tools.replay_stage1 data/recordings/stage1-YYYYMMDD-HHMMSS.jsonl --report /tmp/stage1-report.json
python3 -m tools.replay_stage1 tests/fixtures/stage1_synthetic.jsonl --json   # 機械可読のレポートを標準出力へ
```

- 完了した文ごとに新しい `ChordFsm` へキーを時刻順に渡す。各イベントの前に期限の来たタイマーを発火し、
  **期限と同じ時刻のキーよりタイマーを先に処理する**。仮想時計による決定的な再生で、Win32 のタイマー配送の
  遅れは再現しない。
- 期待するトークン列は、ログの `reading` と `data/nicola_a.json` から逆引きする。単独面は `Plain(key)`、
  親指面は `AmbShift(key)`。複数の面にある文字は単独面を優先する。P の「，」は「、」に対応させる。
  ASCII の数字は `literal`。対応の無い文字はエラーにする。
- **トークン正解率** = `max(0, 1 - (S + D + I) / N)`（N = 期待トークン数、S/D/I = 置換・欠落・余分、各 1）。
  全完了文の N と誤り数を合算して計算する（文ごとの率の平均ではない）。一致数も別に出す。
- `ThumbTap`（Space 単独）は採点から外す（`raw_tokens` と `thumb_taps` には残す）。Backspace・Enter・Esc は
  回数だけ数え、過去のトークンを消さない。キーの自動リピートは重複トークンにしない。
- 失敗は編集 1 件ごとに次の型に分ける（**推定であり、物理的な原因の断定ではない**）:

| キー | 条件 |
|---|---|
| `thumb_overlap_shift_loss` | 期待 `AmbShift(k)` → 実 `Plain(k)`。押下時に Space が押されたままで、その押下が先の `AmbShift` に使われている |
| `unintended_chord` | 期待 `Plain(k)` → 実 `AmbShift(k)` |
| `missed_chord` | 期待 `AmbShift(k)` → 実 `Plain(k)` で、上の条件を満たさない |
| `other` | 異なるキーへの置換、挿入、欠落、その他 |

- `interrupted` / `incomplete` の文は一覧を出して外す。壊れた JSON・不正な型・時刻の逆転・文 ID の
  不整合・途中で切れた最終行は、行番号つきのエラー（終了コード 2）。採点できる文が 0 なら率は `null`、
  終了コード 1。

### ログの形式（`oyayubi.stage1.v1`）

UTF-8、1 行 1 JSON オブジェクト。全行に `type` と `ms` がある。`ms` は QPC 起点からの非負の整数ミリ秒で、
単調非減少（`floor((QPC_now - qpc_origin) * 1000 / qpc_frequency)`）。窓がメッセージを処理した時刻で、
キーの接点の時刻ではない。

| type | 追加の項目 |
|---|---|
| `session` | `schema`、`clock="QPC"`、`time_unit="ms"`、`qpc_frequency`（tick/秒）、`qpc_origin`（起点 tick）、`layout="NICOLA-A"`、`keyboard="single-space-laptop"`、`next_prompt_vk`（123 = F12。古いログは 120 = F9。再生は各ログの値に従う）、`prompt_count` |
| `prompt_start` | `id`、`text`（表示文）、`reading`（入力する読み。句読点と数字を含む） |
| `key` | `id`（対象の文）、`vk`（Win32 VK、0〜255）、`action="down"` / `"up"` |
| `prompt_end` | `id`、`status="complete"` / `"interrupted"` / `"incomplete"`、`event_count`、`held_vks`（終了時に押されていたキー） |
| `focus` | `focused`（真偽値） |
| `session_end` | `reason="complete"` / `"window_closed"` |

```jsonl
{"type":"session","ms":0,"schema":"oyayubi.stage1.v1","clock":"QPC","time_unit":"ms","qpc_frequency":10000000,"qpc_origin":123456789,"layout":"NICOLA-A","keyboard":"single-space-laptop","next_prompt_vk":123,"prompt_count":40}
{"type":"prompt_start","ms":0,"id":"daily-01","text":"おはよう、今日は少し早く起きた。","reading":"おはよう、きょうはすこしはやくおきた。"}
{"type":"key","ms":500,"id":"daily-01","vk":32,"action":"down"}
{"type":"key","ms":520,"id":"daily-01","vk":74,"action":"down"}
```

F12 自身の down/up も当該文の `key` として残り、離したあとに `prompt_end` → 次の `prompt_start` が同じ時刻で
並ぶ。最後の文の後は `session_end`。1 ファイル 1 セッションで、行ごとに追記して flush する。

記録の中核は `yuragi/ime/recording.py`、表示と QPC・Win32 のメッセージ処理は `yuragi/ime/host_win32.py`、
再生と採点は `tools/replay_stage1.py`。レポートの形式は `oyayubi.stage1.report.v1`。

> **注意**: 実打鍵ログの推定分類だけで、FSM のタイミング定数を変えない。
