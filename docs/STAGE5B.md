# Stage 5b — LM リスコア（実験フラグつき）

2026-11-11 / Linux・Python 3.12.3。
対象ブランチ `docs-and-eval-2026-11-10`、開始HEAD `d9b44d7`。
判断元は `../fable-decisions-2026-11-10.md` の Stage 5 と §8（E6 実験結果）、
参照実装は `../fable-experiments/e6_lm_rescore.py`。
実装は Claude Opus が担当し、全件評価と本記録は司令塔（Muse）が引き継いで完了した
（Opus セッションは全件評価の実行中に SIGTERM で終了。実装と単体テストは完了済みだった）。

## 方式

Stage 5a の本物の k-best 候補列を、因果 LM の対数確率でリスコアする。
E6 の形のまま:

```
score = (Viterbiコスト − 現在の1-bestのコスト) + α × 500 × LM負の対数尤度（自然対数）
```

- モデル: E6 と同じ `rinna/japanese-gpt2-small`（約1.1億パラメータ、CPU 前提）。
  リポジトリには同梱しない。
- α の既定は 1.0、候補数 k の既定は 8（かな区間ごと、1-best を含む）。
- かける場所は変換キー（Space）押下時だけ。未変換表示の 1-best は現行 Viterbi のまま。
- **既定はオフ**。LM を製品の既定にするかは Sadao の未決事項であり、ここでは決めていない。

## 実装

新規 `oyayubi/ime/lm_rescore.py`:

- `CausalLmScorer`: 上文＋候補の NLL（nats）を返す。torch/transformers の import は
  スコアラ生成時に限られ、既定経路に新しい依存はない。
- `LmRescorer.pool`: 変換対象区間について「学習込みの現行 1-best（先頭）＋
  `nbest_tokens` の k-best」を重複除去して最大 k 件にする。
  `choose` は上式で最小のものを選び、同点では現行 1-best を残す。
- 遅延対策（すべて E6 のスコアと厳密に一致する範囲）:
  - 上文 KV キャッシュ: 上文ウィンドウを1回だけモデルに通し全候補で再利用。
    次の上文が前回の延長なら差分トークンだけを通す（増分拡張）。
    ウィンドウは「最後の ctx_tokens(48)〜ctx_max(128)−1 トークン」で、
    開始位置は (ctx_max − ctx_tokens) の倍数ステップでのみ動く決定的な規則とし、
    キャッシュ状態にスコアが依存しないようにした。ctx_max == ctx_tokens で E6 の固定48窓と一致。
  - トークン trie: 候補のトークン列を trie に統合し、
    内部ノードを各1回だけ、祖先のみへの注意マスクと位置IDつきで1回の forward で実行。
    葉（全候補が通る末端）は予測のみで実行しない。
    当初は `share="lcp"`（共通接頭辞＋右パディングバッチ）と `share="none"`（素のパディングバッチ）
    をフォールバックとして残していたが、2026-11-12 の簡潔化で削除した（trie と `_naive` の一致テストが残る）。
  - （上文, 候補）単位のメモ化。
  - 素朴実装 `_naive`（E6 そのまま: 候補ごとに上文＋候補を再実行）を基準として残し、
    実モデルテストで各方式の NLL が一致することを固定した。

`oyayubi/ime/session.py`:

- `ImeSession.lm`（既定 `None`）。Space での変換開始時にだけ
  `_refresh(rescore=True)` が走り、区間ごとに「接続の上文＋同一コンポジションで
  既に選ばれた左側区間」を上文としてリスコアする。打鍵中・未変換表示では LM を呼ばない。
- 上文は「この接続で直前に確定した文字列」。アプリ非依存で `ITfContext` は使わない。
  保持は直近200字（`CONTEXT_CHARS`、切り捨ては4倍長時点）。
- リセット条件: reset op（フォーカス喪失相当）で消去。Enter 確定は文書の続きとして保持。
  トークン未入力状態での Ctrl/Alt/Win ショートカット、および M/O 以外のキー
  （矢印・Home・Tab 等、キャレット移動の可能性）でも消去。Backspace は上文末尾も削る。

`oyayubi/ime/server.py`:

- `--lm-rescore`（既定オフ）でのみ LM を import・生成し、"ready" 前にウォームアップする。
- 追加オプション: `--lm-model` / `--lm-alpha` / `--lm-k` / `--lm-threads` / `--lm-timing`
  （変換ごとの nbest/LM 時間を stderr へ。本文は出さない）。
- 検証時の修正: 当初実装はメインループで `sess.lm` を直接参照し、既存
  `tests/test_server.py` のモックセッション（`lm` 属性なし）で AttributeError になった。
  司令塔の検証で検出し、フラグ有効時にだけ束ねるローカル変数 `rescorer` 方式へ修正した。

## モデルの取得手順

```sh
pip install torch transformers sentencepiece   # LM を使う環境でのみ
# 初回実行時に Hugging Face から rinna/japanese-gpt2-small を自動取得する
python -m oyayubi.ime.server --lm-rescore      # 既定モデル・α=1.0・k=8
```

ローカル配置済みのモデルディレクトリを `--lm-model` に渡すこともできる。
今回の評価では `../fable-experiments/.venv` の torch/transformers と
キャッシュ済みモデルをオフライン（`HF_HUB_OFFLINE=1`）で使った。Windows 実機での取得は未検証。

## 評価（AJIMEE、司令塔が実行）

追加ツール: `python -m tools.wp0.run_ajimee_lm_eval`。条件は Stage 5a と同じ
AJIMEE 全200文（SHA-256 `e9eb668f…`）、amb 条件、学習なし。
1項目＝1回の変換操作として、セッションと同じ経路
（区間を左から順に `LmRescorer.rescore`、上文＝項目の `context_text`（ctx=Y時）
＋同一項目で既に選ばれた表記）で測る。k=8、threads=4、variant=full（trie＋KVキャッシュ）。
α=0 は LM を呼ばない現行 1-best で、同一ツール・同一データでの on/off 比較を正とする
（E6 とは文の採点経路が異なるため、E6 数値は再現レンジの目安としてのみ使う）。

| α | 上文 | 完全一致 | 読み一致 | 変換1回 ms 中央値/p95/最大（うちLM中央値） |
| --- | --- | --- | --- | --- |
| 0（オフ） | − | 48.0%（96/200） | 87.5% | − |
| 0.5 | あり | 68.0%（136） | 93.0% | 588.7 / 1883.5 / 2877.9（503.8） |
| 0.5 | なし | 64.0%（128） | 93.0% | 498.2 / 1706.0 / 2520.4（414.3） |
| 1.0 | あり | 72.0%（144） | 93.5% | 599.1 / 1806.9 / 2594.9（522.9） |
| 1.0 | なし | 66.5%（133） | 94.0% | 505.2 / 1603.3 / 2495.0（474.5） |
| 1.5 | あり | 72.5%（145） | 93.5% | 593.9 / 1888.8 / 2996.4（554.9） |
| 1.5 | なし | 66.5%（133） | 94.0% | 576.3 / 1735.7 / 3508.5（494.5） |
| 2.0 | あり | 72.5%（145） | 93.0% | 622.4 / 2002.4 / 2816.5（585.0） |
| 2.0 | なし | 66.0%（132） | 93.5% | 513.9 / 1902.3 / 3537.8（486.5） |

- E6 の目安（46.5% → 65%前後、上文あり 68.5〜69.5%）に対し、自条件では
  α=1.0 で上文なし 66.5%／あり 72.0% と、目安レンジに届くか上回った。
- α は 1.0〜2.0 でほぼ頭打ち（上文あり 72.0→72.5% は1文差）。既定は 1.0 のまま据え置く。
- 遅延はこの VM（共有1.5 CPU 相当）での参考値。変換1回あたり中央値 約0.5〜0.6秒、
  その大半が LM で、n-best 列挙は中央値 約8ms。モデル読み込み 4.5秒、RSS 約1.9GB。
  E6 の素朴実装（1文あたり秒単位）からは短くなったが、実用可否は Sadao の実機での
  実測が要る。threads=1 の全件測定は Opus 側の中断で未実施（スコアは threads 数に依存しない）。

## フラグ off の検証

- `python3 -m unittest discover -s tests`: 103件 OK（実モデル2件は環境フラグなしのため skip）。
  `OYAYUBI_LM_TESTS=1`＋venv では `tests/test_lm_rescore` 9/9 OK
  （trie/lcp/batch が素朴実装と一致、ウィンドウ決定性、上文リセット、打鍵中に LM を呼ばない等）。
- 回帰ゲート 5/5 PASS。
- Mozc 評価（594件）は従来どおり: 読み 92.4%（549/594）、表記 77.1%（458/594）、
  コマンド基準 65.2%（386/592）。
- FSM・辞書コスト・1-best 経路（`decode_tokens` の rid 枝刈り）は変更していない。

## 変更ファイル

- 新規: `oyayubi/ime/lm_rescore.py`、`tests/test_lm_rescore.py`、
  `tools/wp0/run_ajimee_lm_eval.py`、`docs/STAGE5B.md`
- 変更: `oyayubi/ime/session.py`、`oyayubi/ime/server.py`
- コミットは作成していない（司令塔の検証待ち、という引き継ぎ条件のため）。
  評価の summary/details は `../../stage5b-eval/k8-th4.json` / `.jsonl` に保存した。

## 残課題

- 遅延: この VM で中央値 約0.5秒。常駐サーバでは LM をキー応答の同期経路から外した
  （設計 `~/workspace/nicola-projects/lm-conversion-design-2026-11-12.md` の D1〜D8：専用ワーカー、猶予 30ms、`pending` 返信と保留中キーの再生、
  400ms 打ち切りで Viterbi、親指押下での先読み、TIP 予算 250ms）。stdio・評価は同期のまま。
  実機での確認と統計（`python -m oyayubi.ime.client --stats`）は同設計書の §9。
- α の最終決定と、LM リスコアを製品既定にするかは Sadao の判断事項（現状は既定オフのまま）。
- Windows 実機でのモデル取得・サーバ起動（`--lm-rescore`）は未検証。
- `docs/EVALUATION.md` / `docs/STATUS.md` への Stage 5b 追記は未実施。
