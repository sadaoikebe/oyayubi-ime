# いまどこまでできているか

日付: 2026-11-11。設計の正本は [PLAN.md](PLAN.md)。Stage 2の実装・プロトコル・確認事項は [STAGE2.md](STAGE2.md)。確定学習の使い方・設計は [STAGE4.md](STAGE4.md)。真の k-best と到達率の前後比較は [STAGE5A.md](STAGE5A.md)。変換のオフライン数字は [EVALUATION.md](EVALUATION.md) と [WP0-investigation.md](WP0-investigation.md)。

## 本線（残しているもの）

入力はこれだけ。

```text
キー down/up + QPC
  → src/tip（自前 TSF、fail-open）
  → python -m yuragi.ime.server
  → ChordFsm（NICOLA-SPEC のホスト移植。nicola.c はコピーしていない）
  → トークン列をそのまま並べる（Plain / AmbShift / ThumbTap / Literal）
  → トークン同期 Viterbi + Mozc OSS 辞書テキスト
  → composition / 確定
```

作者の常用は US ANSI + NICOLA-A。テーブルは `data/nicola_a.json`。

動いていること:

| 層 | 状態 |
| --- | --- |
| 格子変換 | トークン列から漢字かな交じりへ直行できる。読みを 1 本に潰して既存 IME に渡していない |
| n-best | 厳密 Viterbi＋後ろ向き A* で表記をコスト順に列挙。AJIMEE amb 200文の区間oracle到達率 @3 66.5% / @8 81.0% / @30 85.5%。学習なし |
| オフライン品質 | Mozc 評価読み約 92%。日常 66 件はユーザ辞書後に読み約 89%。残りは既読／記憶のような、両方正しい語 |
| FSM | TIMEOUT 80 ms / OVERLAP 20 ms。Space@0 のあと 80 ms 超えて J は空白＋「と」。テスト 11 件 |
| セッション | 句読点・数字・記号・Shift英字のリテラル、変換後の打ち続けで確定、Enter/Esc/BS、reset通知。時刻つきトランスクリプト19ケースで検証。TIPへの新機能接続はStage 3 |
| 確定学習 | 文節の読み・表記・左隣をローカルJSONLへ保存。1-best・文節候補へ減衰するボーナス、未知かなのユーザー語登録。Shift+F6で候補の忘却、Shift+F7で確定ごとの学習取消（複数回可）。無修飾のF6/F7は慣例どおりひらがな/カタカナ変換。いずれも追記式。ファイルなしは無効。評価は学習を読み込まない |
| TSF | Notepad で「ゆらぎ」として打てる。`かえる`（W 単独→Space+W→Space+I）で「か」が残る |
| 変換エンジンとブローカー | 2026-11 の全体再設計（[REDESIGN.md](REDESIGN.md)）で置き換えた。サインインごとに 1 つのブローカー（`yuragi-broker.exe`）が、エンジン（`python -m yuragi.engine`、標準入出力）を起動・見張りし、TIP とは名前付きパイプで話す。TIP はプロセスを起動しない（ブローカーが消えたときだけ、条件付きで 1 回）。旧 `--resident`（loopback TCP）は削除 |
| 管理 | 単語登録・学習の管理・設定・状態を、設定ウィンドウ（`python -m yuragi.ime.admin_ui`）と端末（`python -m yuragi.ime.manage`）の両方から扱える。両者は同じ関数で同じファイルを触る |
| インストール | 手順と置き場所は [INSTALL.md](INSTALL.md)、設計は [REDESIGN.md](REDESIGN.md) §4・§8。インストール・更新はリリースの `setup.ps1`、アンインストールはリリースの `uninstall.ps1`（`packaging/uninstall-entry.ps1`。入っている版の `packaging/uninstall.ps1` を呼ぶ）を、README の1行ずつで取得して実行する。学習データ・登録単語・設定（`%USERPROFILE%\.yuragi-ime`）は更新でもアンインストールでも残し、消すのは `-RemoveData` のときだけ。リリースは `.github/workflows/release.yml` がビルド・試験・依存ピン留め・モデル固定リビジョン・スクリプト構文を検証してから公開し、まっさらなランナーで入れて消す。使う人向けの説明は [USAGE.md](USAGE.md) |
| 登録 | `ITfInputProcessorProfileMgr` のみ。日本語の第 2 IME。Alt+Shift は言語巡回なので Microsoft IME が当たる。切替は Win+Space かトレイ |

試験窓: `python -m yuragi.ime.host_win32`（TSF を通さない）。

設定ウィンドウ（2026-11-13、旧称 管理ウィンドウ）: `python -m yuragi.ime.admin_ui`（tkinter、標準ライブラリのみ。`--runtime-dir` / `--config` は manage と同じ）。単語登録・学習・設定・状態の 4 タブ。同梱ランタイム（Python 埋め込み版）には tkinter が無いので、リリースが同じ版の通常版 Python から取った tkinter 一式（`python-<版>-tkinter-amd64.zip`）を添え、setup.ps1 がランタイムに展開する（v0.1.0 は無しで、設定ウィンドウが開かなかった）。

- 単語登録: 個人辞書（`personal_file`）の一覧・登録（読み・単語・品詞・コスト）・削除。動いている変換サーバには次の変換から効く
- 学習: 学習履歴の一覧と検索、選択した組の忘却（Shift+F6 と同じ抑制）、単語登録への昇格（履歴を消しても残る）、学習の一時停止／再開、件数。学習ファイルが無い（学習オフ）ときは一時停止も忘却もできない
- 設定: `config.json` に書ける全項目を、日本語の名前・値・説明で表示し（既定値には「（既定）」）、選んで書き換え・既定に戻す。値はエンジンの引数と同じ変換・検査を通し、知らないキーは残す。反映はエンジンの再起動時で、状態タブの「変換エンジンを再起動」か `manage server restart` でその場でできる。tip.json など他所の設定は出さない（`manage config show` で見られる）
- 状態: 変換エンジンの状態・注意・最後に止まった理由、辞書の照合、学習と個人辞書の件数、ファイルの場所を日本語で表示し、変換エンジンの再起動ボタン

ロジックは `AdminModel`（tkinter 非依存、`tests/test_admin_ui.py`）にあり、画面は薄い殻。画面の文言はすべて日本語（`admin_ui.SETTINGS` などの表。CLI は英語のまま）。DPI 対応を申告するので、拡大率のあるモニタでもぼやけない。タスクバーの入力モードのアイコンの右クリックメニュー「設定を開く」から開く（ブローカーが起動する）。

候補窓: Spaceで変換、←→で文節移動、Shift+←→で1トークンずつ文節伸縮、Space / ↑↓で文節候補巡回、PgUp/Downで9件ページ送り、1–9で選択、Enterで全確定。F6で注目文節をひらがなに、F7でカタカナに、F8で半角カタカナ、F9で全角英数、F10で半角英数に変換し、Shift+F6で注目候補の学習を忘却、Shift+F7で直近の確定学習を戻す（確定文字は保持）。未変換の入力中に Tab を押すと、確定履歴と辞書から続きの候補（推測変換）が同じ候補窓に出る。候補が無ければ Tab は素通し。設定 `prediction` で off にできる。詳細は[STAGE4.md](STAGE4.md)。かな候補も末尾にある。既読と記憶のように別読みの語は並べる。左右ラベルは付けない。TIPへの接続はStage 3。

まだ無いもの:

- 上文リスコア

## 方針メモ（2026-11-10）

- 同時打鍵判定は打鍵列の中で閉じる。判定後に残るのは左右のあいまいさだけで、それを変換に渡す
- 文脈依存の同時打鍵判定（英数／かなのモード推定と連動するもの）は、完成版では扱わない。英字＋スペースは時系列的に極めて近接するためタイミングだけでは切り分けられず、これは英数字の親指シフトに将来必要になる open item として切り離す
- そのため、NICOLA-SPEC の 2026-08-29 逐次打鍵ルール（英文の Space ロールオーバー対策）は、完成版の必須要件としない。FSM の現行の打鍵列内判定で閉じる

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

## 既知の問題（今は対処せず、後で再検討するもの）

- FSM の例外経路で、1回の Space 押下から AmbShift が2個出る場合がある
  （2026-11-11 Fable 独立検証で発見・再現済み）。経路: S5 の短い重なりで
  ThumbTap を出してタイマー張り直し（m_time は古いまま）→ 80ms 以内に
  Space を押し直して S4（t1 が80ms以上）→ S4 タイムアウトが AmbShift を
  出して状態を残す → 次の文字が消費済みの Space と再び組になる。
  必要条件が狭い（Space の二度押し、先の文字を約165ms以上保持、次の文字が
  2回目の Space の80〜99ms後）ため、今は対処しない。Stage 1 の実測後に
  左右シフト重なりの扱いを見直すときに再検討し、対処するかはそのとき
  決める。詳細は
  `~/workspace/nicola-projects/doctrine-verification-fable-2026-11-11.md`
  §1.2。あわせて fsm.py 冒頭 docstring と docs/PLAN.md のタイムアウト後の
  状態の記述がコード（S2/S3 は S1 に戻し、S4/S5 は状態を残す）と
  食い違っている点も、そのときに直す。
- LM 非同期化（2026-11-12 実装）の端ケース: 変換の LM 結果を保留中に
  `set_direct(false)` が来てモードが実際には変わらなかった場合、その変換と
  保留中のキーを破棄する（未変換の入力自体は残る）。設計書の擬似コード
  どおりの挙動だが、同期実装の結果とは異なる。TIP が set_direct を送るのは
  再接続直後だけで、その時点では保留中の変換は存在しないはず、というのが
  実装時の読み。実機で問題が観測されたら見直す。設計は
  `~/workspace/nicola-projects/lm-conversion-design-2026-11-12.md`。

## 将来の検討事項

- アンインストールでゴミを残さないこと（2026-11-11 Sadao の条件）。
  今日やることではないが、配布を考える段階では必須。対象は、TSF/COM の
  登録（HKLM・HKCU の両方）、常駐サーバの自動起動登録と実行中プロセス、
  dist の DLL（ロック時の扱い）、%TEMP% 等のログ。学習データ等の
  ユーザーデータは「残滓」とは別扱いで、消すか残すか（残すなら置き場所と
  明示）をそのときに決める。登録解除スクリプトは既にあるので、不足分は
  主に自動起動まわりと、解除後に何も残っていないことの確認手順。
