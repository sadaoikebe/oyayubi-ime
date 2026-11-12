# Windows PowerShell 5.1: save this file as UTF-8 with BOM.
param(
    [switch]$Watch
)

$ErrorActionPreference = 'Stop'

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    # EncodedCommand preserves spaces and quotes in both the script path and arguments.
    $quotedPath = "'" + $PSCommandPath.Replace("'", "''") + "'"
    $quotedArgs = @($args | ForEach-Object { "'" + ([string]$_).Replace("'", "''") + "'" })
    $command = '& ' + $quotedPath + ' ' + ($quotedArgs -join ' ')
    if ($Watch) { $command += ' -Watch' }
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
    $process = Start-Process -FilePath powershell.exe -Verb RunAs -Wait -PassThru `
        -ArgumentList "-NoProfile -ExecutionPolicy Bypass -EncodedCommand $encoded"
    exit $process.ExitCode
}

function Invoke-DevBuild {
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
        if ($LASTEXITCODE -ne 0) {
            throw 'vcvars64.bat による開発者環境の読み込みに失敗しました。'
        }
        foreach ($line in $environment) {
            if ($line -match '^([^=]+)=(.*)$') {
                Set-Item -LiteralPath ('env:' + $Matches[1]) -Value $Matches[2]
            }
        }
        if (-not $env:LIB) {
            throw '開発者環境の読み込みに失敗しました（LIB が未設定です）。'
        }
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

    # Never start the server here: this process is elevated.
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" |
        Where-Object { $_.CommandLine -like '*oyayubi.ime.server*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

    Write-Host ("現在有効な DLL: " + [IO.Path]::GetFileName($slot))
    Write-Host '新しいアプリから新版が読み込まれます。古いアプリは旧版のままなので、全部に行き渡らせたければアプリ再起動かサインアウトをしてください。'
    Write-Host '常駐サーバを終了しました。次に新しく開いたアプリで打ち始めると、TIP が通常権限で自動起動します。'
}

function Get-SourceSnapshot {
    # Ignore CMake-generated sources so a build does not trigger another build.
    $buildPath = (Join-Path $PSScriptRoot 'build') + [IO.Path]::DirectorySeparatorChar
    $entries = Get-ChildItem -LiteralPath $PSScriptRoot -Recurse -File |
        Where-Object {
            -not $_.FullName.StartsWith($buildPath, [StringComparison]::OrdinalIgnoreCase) -and
            ($_.Extension -in @('.cpp', '.h', '.def') -or $_.Name -eq 'CMakeLists.txt')
        } |
        Sort-Object FullName |
        ForEach-Object { $_.FullName + '|' + $_.LastWriteTimeUtc.Ticks }
    return ($entries -join "`n")
}

Push-Location $PSScriptRoot
try {
    if (-not $Watch) {
        Invoke-DevBuild
    } else {
        Write-Host 'Watch: ソースを2秒間隔で監視します。Ctrl+C で終了します。'
        $snapshot = Get-SourceSnapshot
        while ($true) {
            try {
                Invoke-DevBuild
            } catch {
                Write-Error -ErrorRecord $_ -ErrorAction Continue
                Write-Host 'Watch: 次のソース変更を待ちます。'
            }

            do {
                Start-Sleep -Seconds 2
                $current = Get-SourceSnapshot
            } while ($current -eq $snapshot)
            # Capture before building so saves during the build trigger the next run.
            $snapshot = $current
            Write-Host 'Watch: ソース変更を検知しました。再ビルドします。'
        }
    }
} finally {
    Pop-Location
}
