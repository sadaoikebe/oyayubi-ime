# 開発と技術文書

使う人向けの説明は [README](../README.md) と [USAGE.md](USAGE.md) にあります。
この文書は、変換のしくみ・設計・開発の入口です。

## しくみの概要

ノート PC の横長スペースバーでは、左右の親指キーを打ち分けられません。
ゆらぎは、親指シフトの左右のあいまいさを残したまま変換に渡し、辞書と文脈で左右を決めます。

構成（[REDESIGN.md](REDESIGN.md)）:

```text
TIP（src/tip、アプリの中の DLL）
  → 名前付きパイプ
  → ブローカー（src/broker、サインインごとに1つ）
  → 標準入出力
  → 変換エンジン（python -m yuragi.engine）
```

## 技術文書

- [CONVERSION.md](CONVERSION.md) — 変換の仕様（打鍵の判定、トークン、変換、キー操作、文節と候補、学習、n-best、LM、返事の項目、設定）
- [REDESIGN.md](REDESIGN.md) — 全体の設計（TIP・ブローカー・エンジン、通信、置き場所、インストールの手順、利用者に見えるもの、試験と合格条件、定数）
- [INSTALL.md](INSTALL.md) — インストール・更新・アンインストールの置き場所と手順
- [EVALUATION.md](EVALUATION.md) — 変換の評価と診断、実打鍵の記録と再生
- [ZERO_ROW_PENALTY.md](ZERO_ROW_PENALTY.md) — 連接表の全 0 行へのペナルティと、その確認
- [DEVENV.md](DEVENV.md) — 開発環境、Windows でのビルドと登録

同時打鍵の状態機械（S1〜S7）の表と各状態の出口は、`yuragi/chord/fsm.py` 冒頭の説明にあります。

## ビルドと試験

```text
python -m tools.wp0.fetch_mozc_dict   # 初回のみ。OSS 辞書テキスト
python -m unittest $(ls tests/test_*.py | sed 's#/#.#;s#\.py$##')   # 全試験（Linux 可）
cmake -S src/broker -B build/broker && cmake --build build/broker && ctest --test-dir build/broker
cmake -S src/tip -B build/tip && cmake --build build/tip && ctest --test-dir build/tip   # TIP の判断の試験（Linux 可）
python -m yuragi.ime.host_win32      # 試験窓。TSF を通さない
```

管理の道具は 2 つあり、同じ関数で同じファイルを触ります。

- 設定ウィンドウ `python -m yuragi.ime.admin_ui`（tkinter、標準ライブラリだけ）。ロジックは
  tkinter に依存しない `AdminModel`（試験は `tests/test_admin_ui.py`）にあり、画面は薄い殻。
  画面の文言はすべて日本語（`admin_ui.SETTINGS` などの表）。DPI 対応を申告する。
- 端末 `python -m yuragi.ime.manage`（文言は英語）。`word`（add/remove/list）、
  `learning`（list/forget/promote/compact/clear/pause/resume）、`dict`（verify/write）、
  `config`（show/set/unset）、`status`、`server restart`。
  どちらも `--runtime-dir` と `--config` で置き場所を変えられる。

Windows でのビルドと登録は `src\tip\dev-build.ps1` と `src\tip\dev-register.ps1`（[DEVENV.md](DEVENV.md)）。
TIP は [`src/tip/README.md`](../src/tip/README.md)、ブローカーは [`src/broker/README.md`](../src/broker/README.md)。
コンパイラは [DEVENV.md](DEVENV.md)。

リリースは、タグ `v*` を push すると `.github/workflows/release.yml` が作ります
（ビルド・試験・依存の検証のあと、プレリリースとして公開し、まっさらな Windows で入れて消す）。
リリースに置く `setup.ps1` と `uninstall.ps1` の元は `packaging/setup.ps1` と
`packaging/uninstall-entry.ps1` です（[INSTALL.md](INSTALL.md)）。

## 禁止していること

詳細は [CONVERSION.md](CONVERSION.md) §1 にあります。

- 左右あいまい打鍵をいったん1本のひらがなに潰して、既存 IME に渡すこと
- 近接キー誤打など、左右以外のあいまいさを候補に出すこと
- やまぶきR / AutoHotkey などをランタイムに置くこと
- JIS を品質第一にして US / NICOLA-A を後回しにすること

## ライセンス

自前のコードは [MIT](../LICENSE)。Mozc の OSS 辞書（`suffix.txt`・`id.def`・README も含めて全部取得物で、git には無い。例外は検証記録の抜粋 `docs/zero-row-entries.tsv`）は
Google の BSD-3-Clause に加え、IPAdic（NAIST / ICOT）と沖縄辞書の条件が付きます。
条文は [NOTICE.md](../NOTICE.md) と、取得した `third_party/mozc_oss_dict/README.txt`。
インストール時に取得する Python ランタイム・PyTorch などの依存・LM モデルのライセンスも NOTICE.md にまとめてあります。
かな配列と同時打鍵の判定規則は NICOLA 配列規格書（日本語入力コンソーシアム、http://nicola.sunicom.co.jp/spec/kikaku.htm）に基づきます。
