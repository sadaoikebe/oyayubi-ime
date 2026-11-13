# oyayubi-ime

横長スペースバーの一般キーボード（作者の常用は US ANSI + NICOLA-A）で、左右あいまいな親指シフトを、普通の Windows IME として使う個人プロジェクトです。

## インストール・更新

Windows 10/11（64bit）の、管理者ではない普段の PowerShell で、次の1行を実行します。更新も同じ1行です。
アプリを開いたままで構いません。

```powershell
iwr -UseBasicParsing https://github.com/sadaoikebe/oyayubi-ime/releases/latest/download/setup.ps1 -OutFile $env:TEMP\oyayubi-setup.ps1; powershell -ExecutionPolicy Bypass -File $env:TEMP\oyayubi-setup.ps1
```

- 取得するもの（約 1GB、2 回目からは差分だけ）を表示して確認してから進みます。IME の登録で管理者の確認が 1 回出ます。
- 終わったら Win+Space で oyayubi-ime を選びます。タスクバーの「あ/A」をクリックで直接入力と切り替え、右クリックで管理ウィンドウ。
- 途中で失敗しても、それまでの版がそのまま動きます。もう一度実行すると続きから進みます。
- 学習した内容・登録した単語・設定は `%USERPROFILE%\.oyayubi-ime` にあり、更新でもアンインストールでも残ります。
- 置き場所と手順は [docs/INSTALL.md](docs/INSTALL.md)、全体の設計は [docs/REDESIGN.md](docs/REDESIGN.md)。

タスクバーの表示:

| 表示 | 意味 |
|---|---|
| 「あ」／「A」 | かな入力／直接入力 |
| 薄い「あ」 | 変換の準備中（サインイン直後の数秒など）。打った文字は預かって、準備ができたら順に入ります |
| 「!」 | 日本語変換が止まっています。右クリック →「もう一度試す」「原因を表示」 |

アンインストール（`<tag>` は入れた版。学習データなども消すときは末尾に `-RemoveData`）:

```powershell
powershell -ExecutionPolicy Bypass -File $env:LOCALAPPDATA\oyayubi-ime\versions\<tag>\app\packaging\uninstall.ps1
```

## 開発

方針の正本:

- [docs/STATUS.md](docs/STATUS.md) — いまできていること、やめたこと
- [docs/STAGE1.md](docs/STAGE1.md) — ノート PC での実打鍵記録・Linux での再生評価
- [docs/STAGE4.md](docs/STAGE4.md) — 確定学習の有効化・保存形式・候補の忘却／学習取消・全消去
- [docs/PLAN.md](docs/PLAN.md) — 調査・設計・WBS・PR 計画（Draft r3、2026-08-16）
- [docs/WP0-investigation.md](docs/WP0-investigation.md) — トークン列→漢字かな交じりのオフライン実証

Notepad / VS Code 用 TSF は自前の小さい TIP（`src/tip`）。

構成（[docs/REDESIGN.md](docs/REDESIGN.md)）: TIP（`src/tip`、アプリの中の DLL）→ 名前付きパイプ →
ブローカー（`src/broker`、サインインごとに 1 つ）→ 標準入出力 → 変換エンジン（`python -m oyayubi.engine`）。

```text
python -m tools.wp0.fetch_mozc_dict   # 初回のみ。OSS 辞書テキスト
python -m unittest $(ls tests/test_*.py | sed 's#/#.#;s#\.py$##')   # 全試験（Linux 可）
cmake -S src/broker -B build/broker && cmake --build build/broker && ctest --test-dir build/broker
python -m oyayubi.ime.host_win32      # 試験窓。TSF を通さない
```

Windows でのビルドと登録は `src\tip\dev-build.ps1` と `src\tip\dev-register.ps1`（[docs/INSTALL.md](docs/INSTALL.md)）。
TIP は [`src/tip/README.md`](src/tip/README.md)、ブローカーは [`src/broker/README.md`](src/broker/README.md)。コンパイラは [`docs/DEVENV.md`](docs/DEVENV.md)。

同時打鍵 SM の挙動正本は IME リポジトリの外にあります。

- [`C:\Users\marur\qmk_userspace\docs\NICOLA-SPEC.md`](C:\Users\marur\qmk_userspace\docs\NICOLA-SPEC.md)

## License

自前のコードは [MIT](LICENSE)。Mozc の OSS 辞書（実行時に取得する本体と、同梱の `suffix.txt` など）は Google の BSD-3-Clause に加え IPAdic（NAIST / ICOT）と沖縄辞書の条件が付く。条文は [NOTICE.md](NOTICE.md) と `third_party/mozc_oss_dict/README.txt`。GPL の `nicola.c` は入れていません。

禁止していること（詳細は設計書）:

- 左右あいまい打鍵をいったん 1 本のひらがなに潰して、既存 IME に渡すこと
- 近接キー誤打など、左右以外のあいまいさを候補に出すこと
- やまぶきR / AutoHotkey などをランタイムに置くこと
- JIS を品質第一にして US / NICOLA-A を後回しにすること
