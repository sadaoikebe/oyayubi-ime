# Remove oyayubi-ime: unregister the TIP and delete the app folder.
# User data (learning history, personal dictionary) is first moved to
# %USERPROFILE%\.oyayubi-ime\userdata; -Purge deletes it too.
param([switch]$Purge)
$ErrorActionPreference = 'Stop'

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    $quotedPath = "'" + $PSCommandPath.Replace("'", "''") + "'"
    $quotedArgs = @($args | ForEach-Object { "'" + ([string]$_).Replace("'", "''") + "'" })
    $command = '& ' + $quotedPath + ' ' + ($quotedArgs -join ' ')
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
    $process = Start-Process -FilePath powershell.exe -Verb RunAs -Wait -PassThru `
        -ArgumentList "-NoProfile -ExecutionPolicy Bypass -EncodedCommand $encoded"
    exit $process.ExitCode
}

$dest = Join-Path $env:LOCALAPPDATA 'oyayubi-ime\app'
if (-not (Test-Path -LiteralPath $dest)) {
    Write-Host "$dest はありません。アンインストール済みです。"
    exit 0
}

$python = Join-Path $dest 'runtime\python.exe'
$dll = Join-Path $dest 'NicolaIME3.dll'
if (Test-Path -LiteralPath $python) {
    & $python (Join-Path $dest 'unregister_admin.py') $dll
}

# Stop the resident server if it is running from the app folder.
Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -like "$dest\*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

if (-not $Purge) {
    $keep = Join-Path $env:USERPROFILE '.oyayubi-ime\userdata'
    New-Item -ItemType Directory -Path $keep -Force | Out-Null
    foreach ($f in 'learning.jsonl', 'personal_lexicon.jsonl') {
        $p = Join-Path $dest "data\$f"
        if (Test-Path -LiteralPath $p) {
            Copy-Item -LiteralPath $p -Destination (Join-Path $keep $f) -Force
        }
    }
    Write-Host "ユーザーデータ（学習・個人辞書）を $keep に残しました。"
}

try {
    Remove-Item -LiteralPath $dest -Recurse -Force
} catch {
    Write-Host '一部のファイルを削除できませんでした。使用中のアプリを閉じるか、サインアウトしてから再実行してください。'
    throw
}
Write-Host 'アンインストールしました。DLL を読み込み済みのアプリには、サインアウトするまで残ります。'
