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
- `acceptance_real_dictionary`: 同じ文を実辞書で確定。既存の全文候補の↓で校正を選択。
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
