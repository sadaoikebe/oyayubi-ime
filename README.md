# oyayubi-ime

横長スペースバーの一般キーボード（作者の常用は US ANSI + NICOLA-A）で、左右あいまいな親指シフトを、普通の Windows IME として使う個人プロジェクトです。

方針の正本:

- [docs/STATUS.md](docs/STATUS.md) — いまできていること、やめたこと
- [docs/PLAN.md](docs/PLAN.md) — 調査・設計・WBS・PR 計画（Draft r3、2026-08-16）
- [docs/WP0-investigation.md](docs/WP0-investigation.md) — トークン列→漢字かな交じりのオフライン実証

Notepad / VS Code 用 TSF は自前の小さい TIP（`src/tip`）。

```text
python -m tools.wp0.fetch_mozc_dict   # 初回のみ。OSS 辞書テキスト
python -m unittest tests.test_fsm tests.test_session
python -m oyayubi.ime.host_win32      # 試験窓。TSF を通さない
```

TIP の fail-open 規則と登録手順は [`src/tip/README.md`](src/tip/README.md)。コンパイラは [`docs/DEVENV.md`](docs/DEVENV.md)。

同時打鍵 SM の挙動正本は IME リポジトリの外にあります。

- [`C:\Users\marur\qmk_userspace\docs\NICOLA-SPEC.md`](C:\Users\marur\qmk_userspace\docs\NICOLA-SPEC.md)

禁止していること（詳細は設計書）:

- 左右あいまい打鍵をいったん 1 本のひらがなに潰して、既存 IME に渡すこと
- 近接キー誤打など、左右以外のあいまいさを候補に出すこと
- やまぶきR / AutoHotkey などをランタイムに置くこと
- JIS を品質第一にして US / NICOLA-A を後回しにすること
