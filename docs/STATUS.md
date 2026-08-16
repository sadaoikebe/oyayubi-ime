# いまどこまでできているか

日付: 2026-08-16。設計の正本は [PLAN.md](PLAN.md)。変換のオフライン数字は [WP0-investigation.md](WP0-investigation.md)。

## 本線（残しているもの）

入力はこれだけ。

```text
キー down/up + QPC
  → src/tip（自前 TSF、fail-open）
  → python -m oyayubi.ime.server
  → ChordFsm（NICOLA-SPEC のホスト移植。nicola.c はコピーしていない）
  → トークン列をそのまま並べる（Plain / AmbShift / ThumbTap）
  → トークン同期 Viterbi + Mozc OSS 辞書テキスト
  → composition / 確定
```

作者の常用は US ANSI + NICOLA-A。テーブルは `data/nicola_a.json`。

動いていること:

| 層 | 状態 |
| --- | --- |
| 格子変換 | トークン列から漢字かな交じりへ直行できる。読みを 1 本に潰して既存 IME に渡していない |
| オフライン品質 | Mozc 評価読み約 92%。日常 66 件はユーザ辞書後に読み約 89%。残りは既読／記憶のような、両方正しい語 |
| FSM | TIMEOUT 80 ms / OVERLAP 20 ms。Space@0 のあと 80 ms 超えて J は空白＋「と」。テスト 11 件 |
| セッション | FSM が出したトークンを append するだけ。ThumbTap は composition 中なら変換、空なら空白 |
| TSF | Notepad で NicolaIME として打てる。`かえる`（W 単独→Space+W→Space+I）で「か」が残る |
| 登録 | `ITfInputProcessorProfileMgr` のみ。日本語の第 2 IME。Alt+Shift は言語巡回なので Microsoft IME が当たる。切替は Win+Space かトレイ |

試験窓: `python -m oyayubi.ime.host_win32`（TSF を通さない）。

候補窓: Space で変換、もう一度 Space / ↑↓ で移動、1–9 で確定。既読と記憶のように別読みの語は並べる。左右ラベルは付けない。

まだ無いもの:

- 常駐サーバ（今はアプリごとに Python。起動直後はローマ字素通し）
- 上文リスコア、確定学習

## 本線に残さなかったもの

理由だけ書く。

| やめたこと | 理由 |
| --- | --- |
| あいまい打鍵を 1 本のひらがなにして MS-IME / ATOK / Mozc に渡す | 「どうぞ」が消える。禁止アーキテクチャ |
| 同側を仮表示や候補の仮定にする | クロスは濁点。同側固定は日本語から濁音を消す |
| 2^k 全読み展開 | 助詞が AmbShift だとすぐ死ぬ。トークン同期ビームで足りた |
| 最初から E2E ニューラル | 打鍵に無い文を出せる。辞書＋ Connector のあとのリスコア用 |
| やまぶきR / AHK / 紅皿をランタイムや UX 雛形にする | 左右を先に固定する失敗例 |
| JIS / NICOLA-J を品質第一 | 常用は US + NICOLA-A。J は第 2 テーブルだけ |
| `nicola.c` をコピー | GPL。仕様だけ移植 |
| stock `mozc_server` / Mozc tip のフォーク | 単一ひらがな前提 |
| PIME を TSF 殻にする | 巨大な既製品。自前 TIP が落ちた理由は Activate 待ちと先食いであり、殻を替える理由にならなかった。プラグインは削除済み |
| 初版の自前 TIP（キーを先に食べ、Activate で辞書を待つ） | Win11 の TextInputHost を止め、全 IME が死んだ |
| セッションで `Plain(W)` を後続の `AmbShift(W)` に上書き | 同じキーだから同じ打鍵、という嘘。`か` のあと `え` で「か」が消えた。FSM の出力を改変しない |
| HKCU 手書き + 古い `Register`/`AddLanguageProfile` の二重登録 | Settings に出ても別エンジンを掴む／ホットキーがおかしくなる |
| オフライン評価スクリプト一式（`run_eval` 等） | 数字は WP0 文書に残した。日次入力のランタイムではない |

## 既存 IME から入れた作法（自前 TIP）

SampleIME / CorvusSKK / Mozc tip を読んで移したもの。PIME は使っていない。

- キーボード無効・閉・フォーカス無しは食べない
- 空 composition の Back / Enter / Esc はアプリへ
- `ActivateEx` で待たない
- IPC 1 回の失敗はそのキーだけ素通し。サーバは殺さない
- キー処理を SEH で囲む
- `RequestEditSession` は SYNC、拒否されたら ASYNC
