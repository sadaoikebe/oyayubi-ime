# oyayubi-ime アンインストール (docs/INSTALL.md の実装)
#
# 使い方:
#   powershell -ExecutionPolicy Bypass -File uninstall.ps1            (全部消す)
#   powershell -ExecutionPolicy Bypass -File uninstall.ps1 -KeepData  (学習データ等を退避してから消す)
#
# 登録解除 → 常駐サーバ停止 → インストール先と実行時ディレクトリの削除。
# 既定は完全に元に戻すこと。-KeepData のときだけ、学習履歴・個人辞書・
# 設定を %USERPROFILE%\oyayubi-ime-userdata\<日時>\ に退避する。

param(
    [string]$InstallRoot = "$env:LOCALAPPDATA\oyayubi-ime",
    [switch]$KeepData
)

$ErrorActionPreference = "Stop"
Set-Location $env:USERPROFILE   # 自分自身を含むフォルダを消すので、外に出ておく
$AppDir = Join-Path $InstallRoot "app"
$ManifestPath = Join-Path $InstallRoot "install-manifest.json"
$RuntimeDir = "$env:USERPROFILE\.oyayubi-ime"

$runtimePython = $null
if (Test-Path $ManifestPath) {
    $manifest = Get-Content $ManifestPath -Raw | ConvertFrom-Json
    $runtimePython = $manifest.runtime_python
}
if (-not $runtimePython -or -not (Test-Path $runtimePython)) {
    foreach ($cand in @((Join-Path $AppDir "runtime\Scripts\python.exe"), (Join-Path $AppDir "runtime\python.exe"))) {
        if (Test-Path $cand) { $runtimePython = $cand; break }
    }
}

# --- KeepData: 消す前にユーザーデータだけ退避する -------------------------
if ($KeepData) {
    $dest = Join-Path "$env:USERPROFILE\oyayubi-ime-userdata" (Get-Date -Format "yyyyMMdd-HHmmss")
    $saved = @()
    foreach ($f in @((Join-Path $AppDir "data\learning.jsonl"),
                     (Join-Path $AppDir "data\personal_lexicon.jsonl"),
                     (Join-Path $RuntimeDir "config.json"))) {
        if (Test-Path $f) {
            New-Item -ItemType Directory -Force $dest | Out-Null
            Copy-Item $f $dest
            $saved += (Split-Path $f -Leaf)
        }
    }
    if ($saved) { Write-Host "退避しました: $dest ($($saved -join ', '))" }
}

# --- IME 登録の解除 (管理者) ----------------------------------------------
$dll = Join-Path $AppDir "NicolaIME3.dll"
$unregisterScript = Join-Path $AppDir "src\tip\unregister_admin.py"
if ((Test-Path $dll) -and (Test-Path $unregisterScript) -and $runtimePython) {
    $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if ($isAdmin) {
        & $runtimePython $unregisterScript $dll
        if ($LASTEXITCODE -ne 0) { Write-Host "登録解除に失敗しました。中止します。" -ForegroundColor Red; exit 1 }
    } else {
        Write-Host "IME の登録解除には管理者権限が必要です。昇格ダイアログが出ます。"
        $proc = Start-Process $runtimePython -ArgumentList @($unregisterScript, $dll) -Verb RunAs -Wait -PassThru
        if ($proc.ExitCode -ne 0) { Write-Host "登録解除に失敗しました。中止します。" -ForegroundColor Red; exit 1 }
    }
    Write-Host "IME の登録を解除しました。"
}

# --- 常駐サーバの停止 (インストール先の Python で動いているものだけ) ------
$servers = Get-CimInstance Win32_Process -Filter "Name like 'python%'" -ErrorAction SilentlyContinue |
    Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($InstallRoot) -and $_.CommandLine -like "*oyayubi.ime.server*" }
foreach ($s in $servers) {
    Stop-Process -Id $s.ProcessId -Force -ErrorAction SilentlyContinue
    Write-Host "常駐サーバを停止しました (PID $($s.ProcessId))。"
}

# --- ファイルの削除 ---------------------------------------------------------
$failed = @()
foreach ($target in @($InstallRoot, $RuntimeDir)) {
    if (Test-Path $target) {
        try { Remove-Item $target -Recurse -Force -ErrorAction Stop }
        catch { $failed += $target }
    }
}
if ($failed) {
    Write-Host "次を削除できませんでした: $($failed -join ', ')" -ForegroundColor Red
    Write-Host "DLL を読み込んでいるアプリがある可能性があります。サインアウトしてから再実行してください。"
    exit 2
}
Write-Host "アンインストールが完了しました。"
