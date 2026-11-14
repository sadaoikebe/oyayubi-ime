# 候補窓が出ない（Notepad）— 2026-11-14

## 症状

Notepad で変換しても候補窓が出ない。毎回出ない。変換そのもの（composition の表記と下線）はできている。
ほかのアプリは未確認。

## 再現と切り分け

`%USERPROFILE%\.oyayubi-ime\tip.json` に `{"log":true}` を置き、`[cand]` の一時ログを
`ApplyReply`・`ShowCands`・`OnLayoutChange`・Position セッション・`PlaceBelow` の `GetTextExt` に入れて、
新しく開いた Notepad で変換した。ログは `%TEMP%\oyayubi_tip.log`。

```text
[cand] ShowCands rows=9 hwnd=00000000002E04DC
[cand] ApplyReply direct=0 converted=1 cands=9 sync=0
[cand] GetTextExt hr=00000000 rect=(1914,718,1914,748) clipped=0
```

| 確かめたこと | 結果 |
| --- | --- |
| 変換できているか | できている（`converted=1`） |
| 候補はサーバから届いているか | 届いている（`cands=9`） |
| 窓は作られているか | 作られている（`hwnd` あり、大きさ 280×204 に設定） |
| 位置 | `GetTextExt` は S_OK。矩形は **幅 0・高さ 30**（キャレット位置） |

## 原因

`PlaceBelow`（`src/tip/oyayubi_tip.cpp`）は「S_OK で空の矩形 = 文字が見えていない」として、
`IsRectEmpty` が真なら窓を `SW_HIDE` にしていた。渡す範囲は composition の末尾に畳んだキャレットなので、
Notepad は幅 0・高さありの矩形を返す。`IsRectEmpty` は幅か高さのどちらかが 0 なら真なので、
見えているキャレットを「見えていない」と判定し、毎回隠していた。

依頼時に疑われていた「キャレット位置が取れないと隠れたままになる作り」は、取れない場合（失敗や
高さ 0）に隠すこと自体は意図どおりで、今回は取れているのに隠していたのが原因だった。

## 直し方

見えていない判定を「高さがない」（`rect.bottom <= rect.top`）に変えた。幅 0 のキャレット矩形は
そのまま使い、その下に窓を置く。同じ関数を使う通知窓（`notice_hwnd_`）も同じく直る。
変換の挙動には触れていない。

直したあと、新しく開いた Notepad で変換して候補窓がキャレットの下に出ることを確かめた
（同じ幅 0 の矩形 `(1476,74,1476,104)` で表示）。

## ついでに直したもの

この PC（CMake 3.31、MSVC 14.51）で `dev-build.ps1` のブローカーのリンクが
`CVT1100: duplicate resource. type:MANIFEST` で失敗した。`broker.rc` がマニフェストを持つのに、
リンカも `/MANIFEST:EMBED` で入れようとするため。`src/broker/CMakeLists.txt` で MSVC のとき
`/MANIFEST:NO` を付けた。
