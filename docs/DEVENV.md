# 開発環境

管理者で入れるものと、ユーザー権限で足りるものを分ける。

## あなたが管理者で入れるもの（推奨）

Notepad / VS Code 用の TSF DLL をこの PC でビルドするのに必要。CUDA Toolkit 本体は、PyTorch を pip するだけなら **入れなくてよい**（ドライバ 610 は既にある）。

### 1. Visual Studio 2026 Build Tools（C++）

管理者コマンドプロンプトで:

```bat
winget install --id Microsoft.VisualStudio.BuildTools -e --accept-package-agreements --accept-source-agreements --override "--wait --passive --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"
```

ID は `Microsoft.VisualStudio.2022.BuildTools` ではない。現行は **2026**（18.x）。

入るもの: MSVC、Windows SDK（`msctf.h`）、CMake コンポーネント。所要約 5–8 GB。

確認（新しい「x64 Native Tools Command Prompt for VS 2026」で）:

```bat
cl
cmake --version
```

### 2. 入れなくてよいもの

| 名前 | 理由 |
| --- | --- |
| CUDA Toolkit（nvcc） | 推論だけなら pip の PyTorch 車輪にランタイムが付く |
| Visual Studio 本体（Community） | ビルドだけなら Build Tools で足りる。IDE が欲しければ別途 |
| AutoHotkey | 使わない |
| 自前 `src/tip` の再登録 | 入力全体を止めた。使わない |

## ユーザー権限で入れるもの

Python 3.13 は既にある。GPU は `NVIDIA GeForce RTX 4060 Laptop GPU`、ドライバ `610.74`。

```bat
python -m pip install -U pip
python -m pip install cmake ninja
python -m pip install torch --index-url https://download.pytorch.org/whl/cu124
```

確認:

```bat
python -c "import torch; print(torch.__version__, torch.cuda.is_available(), torch.cuda.get_device_name(0) if torch.cuda.is_available() else '')"
```

`True` と `4060` が出ればよい。

## Notepad 用: 自前 TIP（入れるときだけ管理者）

`src/tip` が TSF 殻。Activate で待たない・サーバが死んだら素通し、が落ちない条件。

```bat
cd C:\Users\marur\oyayubi-ime\src\tip
cmake -B build -G Ninja
cmake --build build
python register_admin.py
```

外す: `python C:\Users\marur\oyayubi-ime\src\tip\unregister_admin.py`

## 入れたあとこちらで進めること

1. fail-open の TIP を Notepad で確認する（サーバ起動前は QWERTY 素通し）
2. 既読／記憶の n-best を、上文つきで小さい GPU モデルが点数する
