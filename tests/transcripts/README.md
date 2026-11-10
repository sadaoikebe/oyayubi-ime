# IME トランスクリプト

実行: `python3 -m unittest tests.test_transcripts -v`

`*.json` 一つを1ケースとし、`test_transcripts.py` が自動発見する。キー操作は
本番サーバと同じ `server.dispatch()` を通る。期待値はテスト時に生成・更新しない。
各イベント直後の composition、候補一覧、選択位置、変換状態、そのイベントの commit、
commit を累積した committed を必須とし、FSM・トークン数・タイマーも検査できる。
`committed` はアプリの文書を模してランナーが保持する。reset で既存の確定文は消えない。

```json
{
  "version": 1,
  "description": "数字を入力して確定",
  "lexicon": [],
  "steps": [
    {
      "event": {"op": "down", "vk": 49, "t": 0},
      "expect": {"composition": "1", "candidates": [], "cand_index": 0, "converted": false, "commit": "", "committed": ""}
    },
    {
      "event": {"op": "up", "vk": 49, "t": 20},
      "expect": {"composition": "1", "candidates": [], "cand_index": 0, "converted": false, "commit": "", "committed": ""}
    },
    {
      "event": {"op": "down", "vk": 13, "t": 100},
      "expect": {"composition": "", "candidates": [], "cand_index": 0, "converted": false, "commit": "1", "committed": "1"}
    }
  ],
  "committed": "1",
  "composition": ""
}
```

`t` はミリ秒、単調非減少。`op` は down/up/timeout/reset。`mods` は省略可能な
`["shift", "ctrl", "alt", "win"]` の部分集合で、イベント時点の修飾スナップショット。
省略時は Shift/Ctrl/Alt/Win 自体の down/up を追跡する。`note` で各ステップを説明できる。

`lexicon` を指定すると小さな決定的辞書を使う。各行は reading/surface と任意の
cost/lid/rid（既定100/1/1）。`connection` に BOS=0 の連接行列も指定できる。
これはキー・セッションの振る舞いを辞書の品質変更から独立させるためで、デコーダ自体は
本番と同じ。`lexicon` を省略すると実 Mozc 辞書＋ユーザー辞書を使う。

- `acceptance_sentence`: 固定辞書で「今日は、機関の校正をした。」。候補選択なし。
- `acceptance_real_dictionary`: 同じ文を実辞書で確定。文節候補の↓で校正を選択。
  実辞書の1-bestは「構成」であり、校正を優遇する辞書変更はしていない。
- `punctuation_faces`, `native_literals`, `converted_literals`: 句読点、数字、US ANSI 記号、Shift英字、修飾・リピート。
- `literal_boundaries`: 連結語と連接コストを意図的に仕込み、リテラルの両側の BOS/EOS を検査。候補も検査。
- `selected_then_continue`: 選択表示を次の通常文字／親指同時打鍵の down で確定。
- `repeat_after_conversion`: 押したまま変換した文字のリピートでは確定せず、押し直した新打鍵で確定。
- `escape_twice`, `backspace_conversion`: 変換取消、再Escで破棄、BSで一文字削除、upで二重操作しない。
- `pending_enter`, `pending_literal`, `timeouts`: 未解決キーの順序、Enter全確定、80ms境界。
- `reset_s2/s3/s4/s5`, `reset_converted`, `reset_held_literal`: 入力中・変換中・Shift押下中の破棄、遅延up/timeout、同じキーで再開。

新ケースは既存ファイルを参考にイベント列と期待値を書く。文節や直接入力を実装する際も
この形式を拡張し、実際の表示・確定をステップごとに固定する。

## Stage 2B / version 2

`bunsetsu_*.json` 8ケースを追加。前段の全文候補は今回の文節候補に置き換わった。
既存ケースの候補表示期待値を更新し、`converted_literals` の数字は1から0へ変更した
（変換中の1–9はページ内の文節候補選択、0は通常文字）。既存の他の表示・確定結果は維持。

version 2は従来の必須項目に加えて、各イベントの `expect` に次を全て記す:

- `segments`: 各区間の `start/end`（トークン位置）、`surface/reading`、`literal/fixed`、
  `display_start/display_end`（表示のUnicodeコードポイント位置）。区間は半開。
- `focus`: 注目文節のindex、なしは-1。リテラルは移動対象外。
- `candidate_page`: 0始まりの9件ページ。`candidates` と `cand_index` はページ内。
  隣の変更で固定候補が閾値外になった場合、表示を保持し `cand_index=-1`（選択行なし）。
- `candidate_kinds`: 各行の `dictionary` / `kana` / `literal`。
  `literal` はあいまいトークンのリテラル面だけで区間を覆う候補。
- `has_next_page`: 次ページの有無。

Space/上下で候補を巡回し、自動的にページも切り替える。PgUp/Downはページ先頭へ。
数字選択は文節の固定だけでcommitせず、次の文字またはEnterで選択済みの全文を確定する。
文節移動だけでは他文節の表示を変えない。候補は辞書経路の末尾に全読みのかな候補を付ける。

固定辞書の疎なPOS連接指定も使える（従来の二次元配列も引き続き有効）:

```json
"connection": {"size": 15, "default": 0, "overrides": [[12, 14, 200], [13, 14, -200]]}
```

`overrides` は `[RID, LID, cost]`。品詞判定は本番の `id.def` を使い、たとえば12–14は副詞。
全8ケースの意図・結果と設計根拠は [docs/STAGE2.md](../../docs/STAGE2.md) の「Stage 2B」節。

## / 左親指「？」 / version 2

- `slash_literal_choice`: Space＋/ の右面が最良表示のときも、コスト閾値外の「？」を数字で選んで確定。
- `slash_kana_choice`: 左面「？」から右面のかな「ぉ」と辞書変換「尾」を選べる。
  辞書コスト15000でもリテラル面に候補を押し出されない。
- `slash_literal_context`: 前後のかなと混在させ、記号を含む辞書語を遮断し、BOS/EOSで分離。
  前後を固定し、中を「？→尾→？」に選び直して「科？区」を確定。

あいまい区間は表示が「？」でも `segments.literal=false` のままで候補選択できる。
Stage 2Aの確定したリテラルトークン（移動で飛ばす `literal=true`）とは区別する。
