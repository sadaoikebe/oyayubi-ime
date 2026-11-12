# Windows PowerShell 5.1: save this file as UTF-8 with BOM.
$ErrorActionPreference = 'Stop'

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    # EncodedCommand preserves spaces and quotes in both the script path and arguments.
    $quotedPath = "'" + $PSCommandPath.Replace("'", "''") + "'"
    $quotedArgs = @($args | ForEach-Object { "'" + ([string]$_).Replace("'", "''") + "'" })
    $command = '& ' + $quotedPath + ' ' + ($quotedArgs -join ' ')
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
    $process = Start-Process -FilePath powershell.exe -Verb RunAs -Wait -PassThru `
        -ArgumentList "-NoProfile -ExecutionPolicy Bypass -EncodedCommand $encoded"
    exit $process.ExitCode
}

Push-Location $PSScriptRoot
try {
    if (-not $env:LIB) {
        $vswhere = 'C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe'
        if (Test-Path -LiteralPath $vswhere) {
            $vsPath = & $vswhere -latest -products Microsoft.VisualStudio.Product.BuildTools `
                -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
            if ($LASTEXITCODE -ne 0 -or -not $vsPath) {
                throw 'vswhere で C++ BuildTools が見つかりませんでした。'
            }
        } else {
            $vsPath = 'C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools'
        }
        . (Join-Path $vsPath 'Common7\Tools\Launch-VsDevShell.ps1') -Arch amd64
        if (-not $env:LIB) {
            throw '開発者環境の読み込みに失敗しました（LIB が未設定です）。'
        }
        # Launch-VsDevShell may change the working directory.
        Set-Location $PSScriptRoot
    }

    if (-not (Test-Path -LiteralPath 'build' -PathType Container)) {
        & cmake -B build -G Ninja
        if ($LASTEXITCODE -ne 0) { throw 'CMake の構成に失敗しました。' }
    }
    & cmake --build build
    if ($LASTEXITCODE -ne 0) { throw 'TIP のビルドに失敗しました。' }
    & ctest --test-dir build --output-on-failure
    if ($LASTEXITCODE -ne 0) { throw 'CTest に失敗しました。' }

    $dist = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\dist'))
    $slotA = Join-Path $dist 'NicolaIME3-a.dll'
    $slotB = Join-Path $dist 'NicolaIME3-b.dll'
    $registry = [Microsoft.Win32.RegistryKey]::OpenBaseKey(
        [Microsoft.Win32.RegistryHive]::LocalMachine,
        [Microsoft.Win32.RegistryView]::Registry64)
    try {
        $key = $registry.OpenSubKey('Software\Classes\CLSID\{A7C4E201-0B3A-4F11-9E61-0C1A0B7E0A01}\InprocServer32')
        $registered = $null
        if ($null -ne $key) {
            try { $registered = $key.GetValue('') } finally { $key.Dispose() }
        }
    } finally {
        $registry.Dispose()
    }
    $slot = $slotA
    if ($registered -eq $slotA) { $slot = $slotB }

    try {
        [IO.File]::Copy((Join-Path $dist 'NicolaIME3.dll'), $slot, $true)
    } catch [IO.IOException] {
        $code = $_.Exception.GetBaseException().HResult -band 0xFFFF
        if ($code -eq 32 -or $code -eq 33) {
            throw "コピー先 $slot は使用中です。この DLL を使う古いアプリを終了するか、サインアウトしてから再実行してください。"
        }
        throw
    }

    & python (Join-Path $PSScriptRoot 'unregister_admin.py') $slot
    if ($LASTEXITCODE -ne 0) { throw 'TIP の登録解除に失敗しました。' }
    & python (Join-Path $PSScriptRoot 'register_admin.py') $slot
    if ($LASTEXITCODE -ne 0) { throw 'TIP の登録に失敗しました。' }

    Write-Host ("現在有効な DLL: " + [IO.Path]::GetFileName($slot))
    Write-Host '新しいアプリから新版が読み込まれます。古いアプリは旧版のままなので、全部に行き渡らせたければアプリ再起動かサインアウトをしてください。'
} finally {
    Pop-Location
}
