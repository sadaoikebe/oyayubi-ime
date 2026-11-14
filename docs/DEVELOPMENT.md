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

方針の正本:

- [STATUS.md](STATUS.md) — いまできていること、やめたこと
- [REDESIGN.md](REDESIGN.md) — 全体の再設計（TIP・ブローカー・エンジン、置き場所、インストール手順）
- [INSTALL.md](INSTALL.md) — インストール・更新・アンインストールの置き場所と手順
- [PLAN.md](PLAN.md) — 調査・設計・WBS・PR 計画（Draft r3、2026-08-16）

段階ごとの記録:

- [STAGE1.md](STAGE1.md) — ノート PC での実打鍵記録・Linux での再生評価
- [STAGE2.md](STAGE2.md) — セッションの入力・確定・リセット
- [STAGE3.md](STAGE3.md) — 常駐サーバと TIP
- [STAGE4.md](STAGE4.md) — 確定学習の有効化・保存形式・候補の忘却／学習取消・全消去
- [STAGE5A.md](STAGE5A.md) — 本物の k-best
- [STAGE5B.md](STAGE5B.md) — LM リスコア

調査と評価:

- [WP0-investigation.md](WP0-investigation.md) — トークン列→漢字かな交じりのオフライン実証
- [EVALUATION.md](EVALUATION.md) — 変換の評価と診断
- [ZERO_ROW_PENALTY.md](ZERO_ROW_PENALTY.md) — 連接表の全0行へのペナルティ
- [DEFENSIVE_CODE_AUDIT.md](DEFENSIVE_CODE_AUDIT.md) — 防御コードの棚卸し
- [CANDIDATE_WINDOW.md](CANDIDATE_WINDOW.md)、[UNREGISTER.md](UNREGISTER.md) — 実機で見つけた不具合の記録

同時打鍵 SM の挙動の正本は、IME リポジトリの外にあります。

- 作者の qmk_userspace リポジトリの `docs/NICOLA-SPEC.md`

## ビルドと試験

```text
python -m tools.wp0.fetch_mozc_dict   # 初回のみ。OSS 辞書テキスト
python -m unittest $(ls tests/test_*.py | sed 's#/#.#;s#\.py$##')   # 全試験（Linux 可）
cmake -S src/broker -B build/broker && cmake --build build/broker && ctest --test-dir build/broker
python -m yuragi.ime.host_win32      # 試験窓。TSF を通さない
```

Windows でのビルドと登録は `src\tip\dev-build.ps1` と `src\tip\dev-register.ps1`（[DEVENV.md](DEVENV.md)）。
TIP は [`src/tip/README.md`](../src/tip/README.md)、ブローカーは [`src/broker/README.md`](../src/broker/README.md)。
コンパイラは [DEVENV.md](DEVENV.md)。

リリースは、タグ `v*` を push すると `.github/workflows/release.yml` が作ります
（ビルド・試験・依存の検証のあと、プレリリースとして公開し、まっさらな Windows で入れて消す）。
リリースに置く `setup.ps1` と `uninstall.ps1` の元は `packaging/setup.ps1` と
`packaging/uninstall-entry.ps1` です（[INSTALL.md](INSTALL.md)）。

## 禁止していること

詳細は設計書にあります。

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
