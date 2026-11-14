# 開発用: TIP とブローカーのビルドと試験だけ (REDESIGN §8.4)。CI もこれを使う。
# 管理者権限は要らない。登録は dev-register.ps1 で別に行う。
#
#   powershell -ExecutionPolicy Bypass -File .\src\tip\dev-build.ps1 [-Tag dev]
#
# 出力: dist\yuragi_tip.dll と dist\yuragi-broker.exe (Release、タグを埋め込む)。
param([string]$Tag = "dev")
$ErrorActionPreference = 'Stop'
$Repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))

# Visual Studio の開発者環境 (未読み込みのときだけ)。
if (-not $env:LIB) {
    $vswhere = 'C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe'
    $vsPath = $null
    if (Test-Path -LiteralPath $vswhere) {
        $vsPath = & $vswhere -latest -products '*' `
            -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
        if ($LASTEXITCODE -ne 0) { $vsPath = $null }
    }
    if (-not $vsPath -or -not (Test-Path -LiteralPath (Join-Path $vsPath 'VC\Auxiliary\Build\vcvars64.bat') -PathType Leaf)) {
        $vsPath = 'C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools'
    }
    $vcvars64 = Join-Path $vsPath 'VC\Auxiliary\Build\vcvars64.bat'
    if (-not (Test-Path -LiteralPath $vcvars64 -PathType Leaf)) {
        throw 'vcvars64.bat が見つかりません。Visual Studio の C++ ビルドツールをインストールしてください。'
    }
    $environment = & cmd.exe /d /c "`"$vcvars64`" >nul && set"
    if ($LASTEXITCODE -ne 0) { throw 'vcvars64.bat による開発者環境の読み込みに失敗しました。' }
    foreach ($line in $environment) {
        if ($line -match '^([^=]+)=(.*)$') { Set-Item -LiteralPath ('env:' + $Matches[1]) -Value $Matches[2] }
    }
    if (-not $env:LIB) { throw '開発者環境の読み込みに失敗しました（LIB が未設定です）。' }
}

foreach ($part in @('tip', 'broker')) {
    $source = Join-Path $Repo "src\$part"
    $build = Join-Path $Repo "build\$part-release"
    & cmake -S $source -B $build -G Ninja -DCMAKE_BUILD_TYPE=Release "-DYURAGI_TAG=$Tag"
    if ($LASTEXITCODE -ne 0) { throw "${part}: CMake の構成に失敗しました。" }
    & cmake --build $build
    if ($LASTEXITCODE -ne 0) { throw "${part}: ビルドに失敗しました。" }
    & ctest --test-dir $build --output-on-failure
    if ($LASTEXITCODE -ne 0) { throw "${part}: CTest に失敗しました。" }
}
Write-Host "ビルド OK (タグ $Tag): $(Join-Path $Repo 'dist')"
