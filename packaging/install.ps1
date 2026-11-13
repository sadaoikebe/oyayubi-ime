# Install oyayubi-ime: copy this folder to %LOCALAPPDATA%\oyayubi-ime\app
# and register the TIP. Self-elevates; re-running updates in place.
# If it reports the DLL is in use, sign out, sign back in, and run again.
$ErrorActionPreference = 'Stop'

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    # EncodedCommand preserves spaces and quotes in the script path.
    $quotedPath = "'" + $PSCommandPath.Replace("'", "''") + "'"
    $command = '& ' + $quotedPath
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
    $process = Start-Process -FilePath powershell.exe -Verb RunAs -Wait -PassThru `
        -ArgumentList "-NoProfile -ExecutionPolicy Bypass -EncodedCommand $encoded"
    exit $process.ExitCode
}

$src = $PSScriptRoot
$dest = Join-Path $env:LOCALAPPDATA 'oyayubi-ime\app'
$dll = Join-Path $dest 'NicolaIME3.dll'
$python = Join-Path $dest 'runtime\python.exe'

if ($src -ne $dest) {
    New-Item -ItemType Directory -Path $dest -Force | Out-Null
    & robocopy $src $dest /E | Out-Null
    if ($LASTEXITCODE -gt 7) {
        Write-Host 'コピーに失敗しました。IME が使用中の可能性があります。サインアウトしてから再実行してください。'
        exit 1
    }
}

# Register from the installed location. Unregister first (as dev-build
# does); on a first install there is nothing to remove and the script
# just notes that, so its result is not checked.
& $python (Join-Path $dest 'unregister_admin.py') $dll
& $python (Join-Path $dest 'register_admin.py') $dll
if ($LASTEXITCODE -ne 0) { throw 'TIP の登録に失敗しました。' }

Write-Host "Installed: $dest"
Write-Host 'Win+Space で切り替えてください。サーバは初回使用時に自動で起動します。'
Write-Host '管理ウィンドウ: runtime\python.exe -m oyayubi.ime.admin_ui （このフォルダで実行）'
