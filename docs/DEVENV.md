# 開発環境

## Linux（変換エンジン・ブローカー・TIP の判断の試験）

Python 3.12 以上、CMake、C++17 のコンパイラ。Windows が無くても、Python の全試験、ブローカー（POSIX 版）の
試験、TIP の判断（`tip_logic.h`）の試験が回る。CI の Linux ジョブと同じ。

```sh
python -m tools.wp0.fetch_mozc_dict                 # 初回のみ。Mozc OSS 辞書
python -m yuragi.lattice.dict_manifest verify       # 辞書がマニフェストと一致するか
python -m unittest $(ls tests/test_*.py | sed 's#/#.#;s#\.py$##')
cmake -S src/broker -B build/broker -DCMAKE_BUILD_TYPE=Release && cmake --build build/broker && ctest --test-dir build/broker
cmake -S src/tip -B build/tip -DCMAKE_BUILD_TYPE=Release && cmake --build build/tip && ctest --test-dir build/tip
```

- `tests/test_installer_scripts.py` はワークフローの PowerShell も検査するので、PyYAML と `pwsh` を使う。
- LM の試験（`tests/test_lm_rescore.py` の実モデルの部分）は、環境変数 `YURAGI_LM_TESTS=1` と
  `transformers` `sentencepiece` `torch` があるときだけ動く。版は `packaging/requirements.txt` に固定してある
  （torch は PyTorch の CPU 用の配布元から入れる）。

## Windows（TIP の DLL とブローカーの exe）

### 管理者で入れるもの

Visual Studio Build Tools（C++。MSVC、Windows SDK、CMake を含む）。管理者のコマンドプロンプトで:

```bat
winget install --id Microsoft.VisualStudio.BuildTools -e --accept-package-agreements --accept-source-agreements --override "--wait --passive --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"
```

`dev-build.ps1` は `vswhere` で最新の Build Tools を探し、開発者環境を自分で読み込む。

### 利用者の権限で入れるもの

Python 3.13（64 ビット）。CMake と Ninja は Build Tools の CMake コンポーネントに入っている
（無ければ `python -m pip install cmake ninja`）。エンジンを開発用に動かすなら、
`packaging/requirements.txt` の依存と torch（CPU 版で足りる）も。

### ビルドと登録

構成は [REDESIGN.md](REDESIGN.md)。TIP は打鍵をブローカーへ送るだけで、Python を起動しない。

```powershell
powershell -ExecutionPolicy Bypass -File .\src\tip\dev-build.ps1      # ビルドと試験（管理者不要）。CI もこれ
powershell -ExecutionPolicy Bypass -File .\src\tip\dev-register.ps1   # タグ dev として登録（UAC 1 回）
```

- `dev-build.ps1` は `dist\yuragi_tip.dll` と `dist\yuragi-broker.exe` を Release で作る。
- `dev-register.ps1 [-Python <python.exe>]` は、エンジンをリポジトリから指定の Python で動かす形で登録する。
  DLL は `dist\yuragi_tip-a.dll`、`-b.dll`、… のうち、登録中でも使用中でもない最初のものにコピーして
  登録するので、古い DLL を読み込んだアプリを閉じなくても入れ直せる。
- 外すときは、製品版と同じ `packaging\uninstall.ps1`（登録されている DLL と同じフォルダの `elevated.ps1` を使う。
  `dev-register.ps1` が `dist\` に置く）。
- TSF を通さずにセッションを試す窓: `python -m yuragi.ime.host_win32`。

> **注意**: TIP の登録は `ITfInputProcessorProfileMgr` だけで行う。HKCU への手書きの登録や、古い
> `Register` / `AddLanguageProfile` との二重登録をすると、一覧に出ても別のエンジンを掴んだり、
> ホットキーがおかしくなったりする。
