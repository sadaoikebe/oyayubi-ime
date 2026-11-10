# Stage 0 の評価と診断

リポジトリ直下で実行する。標準ライブラリのみ使用する。

```sh
python3 -m tools.wp0.run_mozc_eval --details /tmp/mozc-results.jsonl
python3 -m tools.wp0.run_mozc_eval --unambiguous
python3 -m tools.wp0.run_mozc_eval --no-user-lexicon
python3 -m tools.wp0.fetch_ajimee
python3 -m tools.wp0.run_ajimee_eval --details /tmp/ajimee-results.jsonl
python3 -m unittest tests.test_fsm tests.test_session tests.test_evaluation tests.test_server
python3 -m unittest tests.test_transcripts -v
python3 -m tools.wp0.run_regression
```

## Mozc

従来の読み一致・表記一致を残し、`evaluation.tsv` の4〜5列目による合否を追加した。
3列目は Mozc の実出力であり、正解とは限らない。`Mozc recorded pass` は TSV の
status 列の集計であり、Mozc を新たに実行した値ではない。

- `Conversion Expected [N]`: 1-best を先頭に置き、nbest の表記を重複除去して補った
  上位 N 件に引数の全文があるか（省略時 N=1）。
- `Conversion Match`: 1-best に引数の部分文字列があるか。
- `Not`: 上記判定を反転する。
- `Suggestion` 等は対象外として件数を表示する。入力できない読みも別途表示する。
- `--details` は入力可能な各行の `PASS` / `FAIL` / `SKIP` を JSONL に出す。

Expected N は文全体の候補による**近似**。Mozc 本来の文節候補の順位和とは異なり、
現行 nbest は真の k-best でもない。Fable の E2 スクリプトにあった50件制限と
1-best 重複による N+1 件の扱いは引き継がず、表記を重複除去した N 件を判定する。
実装の参照元:
[Mozc quality_regression_util.cc](https://github.com/google/mozc/blob/master/src/converter/quality_regression_util.cc)。

辞書は全エントリを読み込み、ユーザー辞書適用時も切り捨てない。
旧キャッシュは8件以降を復元できないため、`data/cache/mozc_lex_v2.pkl` に再構築する。
候補列挙へ全エントリを渡すが、セッションの8候補表示と nbest の読み prefix 枝刈りは維持する。
単語の全候補を検査するときは `nbest_tokens(..., k=十分大きな値)` を使う。
文節候補の操作・真の k-best は後続 Stage の範囲。

## AJIMEE の出典・ライセンス

[azooKey/AJIMEE-Bench](https://github.com/azooKey/AJIMEE-Bench) の
`JWTD_v2/v1/evaluation_items.json`（200件）を使用する。
取得元はコミット `401666cd56d1a570c2021798b64b6da4396bfd45` に固定。
SHA-256: `e9eb668fd6aa14b1e26436f429b5550108af0a1dfd443b8cea0bcb3ab3028fca`。

[配布元 README](https://github.com/azooKey/AJIMEE-Bench/blob/401666cd56d1a570c2021798b64b6da4396bfd45/README.md)
を2026-11-10に確認した。データは
[日本語Wikipedia入力誤りデータセット v2](https://nlp.ist.i.kyoto-u.ac.jp/?日本語Wikipedia入力誤りデータセット)
由来で、配布元が指定するライセンスは **CC BY-SA 3.0**。
配布元の `utils.py` / `test_utils.py` は CC0 1.0 と別記されている。
この取得スクリプトはデータと出典・ライセンスを記載した README の原本を
git 対象外の `data/cache/ajimee/` に保存する。データや配布元コードは同梱しない。
データを再配布・改変する場合は、出典・ライセンス表示、改変表示、継承条件を守る。
[CC BY-SA 3.0](https://creativecommons.org/licenses/by-sa/3.0/) も参照。

Fable E3 と同じく、入力のカタカナをひらがなにし、NICOLA 表に無い文字（句読点、
数字、英字等）で分割して素通しする。複数の `expected_output` のいずれかとの
完全一致を採点する。una は読み既知、amb はシフト面の左右不明。
`context_text` 有無ごとの集計を出すが、文脈は変換へ渡さない。
実打鍵、FSM の誤り、句読点入力の可否を評価するものではない。
`--details` の出力はデータ由来の本文を含む。

## サーバ

Stage 2前半の入力・確定・resetプロトコルとトランスクリプトは [STAGE2.md](STAGE2.md)。
NICOLAの句読点を追加しても、このオフライン評価の逆引きは従来どおりかなのみを対象にし、
評価件数と比較条件を維持する。

`python3 -m oyayubi.ime.server` は辞書ロード直後に `gc.freeze()` する。
標準出力は従来どおり TIP への JSON 返信であり、composition/commit を含む。
標準エラーへの本文ログは既定オフ。診断が必要なときだけ `--log-text` を指定する。
未処理例外は本文を含み得るメッセージを出さず、例外の型だけを報告する。
TIP 自身のログ変更は Stage 3 のため、今回変更していない。

## 2026-11-10 の自己検証

Linux / Python 3.12、ユーザー辞書60件あり。変更前は旧8件制限キャッシュ、変更後は
再構築した v2 キャッシュを使い、同じ評価コード・データで比較した。

| 指標 | 変更前 | 変更後 |
| --- | --- | --- |
| Mozc 読み一致 amb（594件） | 92.4% (549) | 92.4% (549) |
| Mozc 表記一致 amb（594件） | 76.1% (452) | 77.1% (458) |
| Mozc 表記一致 una（594件） | 82.3% (489) | 83.5% (496) |
| コマンド合格 amb（592件） | 62.8% (372) | 64.2% (380) |
| コマンド合格 una（592件） | 65.5% (388) | 67.1% (397) |
| AJIMEE 完全一致 amb（200件） | 46.5% (93) | 48.0% (96) |
| AJIMEE 完全一致 una（200件） | 49.5% (99) | 51.0% (102) |
| AJIMEE 読み一致 amb（200件） | 85.5% (171) | 87.5% (175) |

Mozc の非エンコード行は7件、コマンド採点対象外の Suggestion は2件。
「こうせい」の辞書エントリは8件から127件（109表記）になり、
`nbest_tokens(..., k=10000)` の列挙で校正・更生・恒星・攻勢を確認した。
回帰ゲートは5/5合格。

サーバの辞書ロード後、辞書を保持して計測した `gc.collect()` は
freeze 前2161.224 ms、freeze 後0.015 / 0.002 / 0.001 ms（丸め値）。
通常のキー入力→composition→commit の JSON 返信を確認し、標準エラーに本文なし。
不正な vk に私的文字列を入れた場合も、例外名 `ValueError` だけを出力した。

単体テストは21/21合格（既存16件＋追加5件）。既存セッションテストの
「表示に『か』を含む」という検査1件は、辞書拡張後に読み「かえ」が「変え」へ
変換されるため、隣接する既存テストと同様に「変」で始まる表記も許容した。
トークン列の厳密比較は維持している。
