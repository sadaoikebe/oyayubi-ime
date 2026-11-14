# oyayubi-broker（REDESIGN §5）

サインインごとに 1 つ動く小さな常駐プログラム。変換エンジン（`python -m yuragi.engine`）を
起動・見張り・決まった回数だけ起動し直し、TIP からの打鍵を名前付きパイプで受けてエンジンへ渡す。
エンジンが使えない間の打鍵は預かる。直らない故障は 1 回だけ通知する。

| ファイル | 中身 |
|---|---|
| `broker_core.h/.cpp` | 判断のすべて（状態機械 §5.4、通信 §5.5、預かり §5.6）。OS に依存しない |
| `support.h/.cpp` | 両 OS 共通: 上限付きログ、`engine.json` の読み込み、`--selftest` |
| `platform_win.cpp` | Windows: 名前付きパイプ + IOCP、ジョブ、環境ブロック、通知とダイアログ |
| `platform_posix.cpp` | 試験用の POSIX 版: UNIX ドメインソケット、`PR_SET_PDEATHSIG` |
| `tests/broker_core_test.cpp` | 中核の単体試験（偽の Platform と時計） |
| `tests/fake_engine.py` | §5.7 の偽エンジン（起動ごとの振る舞いを `plan.json` で決める） |
| `../../tests/test_broker.py` | §5.7 の試験。偽 TIP と偽エンジンを実プロセスで動かす |

```
cmake -S src/broker -B build/broker -DCMAKE_BUILD_TYPE=Release
cmake --build build/broker && ctest --test-dir build/broker
python -m unittest tests.test_broker          # Windows では OYAYUBI_BROKER=dist\oyayubi-broker.exe
```

`engine.json`（インストーラが `%LOCALAPPDATA%\oyayubi-ime\versions\<tag>\` に書く）:

```json
{"format": 1, "python": "<python.exe の絶対パス>", "pythonw": "<pythonw.exe>",
 "app": "<作業フォルダ>", "args": ["-m", "yuragi.engine", "--dict-dir", "...", "--cache-dir", "..."],
 "admin_args": ["-m", "yuragi.ime.admin_ui"]}
```

ログは `%USERPROFILE%\.oyayubi-ime\logs\`（`broker.log`、`engine.log`、`notices.log`。各 1MB × 2 世代）。
