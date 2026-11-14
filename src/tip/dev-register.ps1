# 開発用: dist\ のビルドをこの PC に登録して使う (REDESIGN §8.4)。タグは dev。
#
#   powershell -ExecutionPolicy Bypass -File .\src\tip\dev-register.ps1 [-Python <python.exe>]
#
# - HKCU の Tag を dev にし、%LOCALAPPDATA%\oyayubi-ime\versions\dev\engine.json を
#   このリポジトリに向ける (エンジンは指定の Python で python -m oyayubi.engine)。
# - DLL とブローカーは dist\ から読み込む (開発機に限り Program Files に置かない)。
#   DLL は dist\NicolaIME3-a.dll / -b.dll / ... のうち、登録中でなく使用中でもないものに
#   コピーして登録するので、古い DLL を読み込んだアプリを閉じなくても更新できる。
# - 登録 (regsvr32) だけ管理者権限 (UAC 1 回)。登録後にレジストリを読み返して確かめる。
param([string]$Python = "")
$ErrorActionPreference = 'Stop'
$Repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$Dist = Join-Path $Repo 'dist'
$Clsid = '{A7C4E201-0B3A-4F11-9E61-0C1A0B7E0A01}'
$Layout = "0x0411:$Clsid{A7C4E202-0B3A-4F11-9E61-0C1A0B7E0A01}"
$Utf8NoBom = New-Object Text.UTF8Encoding($false)

if (-not $Python) { $Python = (Get-Command python -ErrorAction Stop).Source }
if (-not (Test-Path -LiteralPath $Python)) { throw "Python が見つかりません: $Python" }
foreach ($f in @('NicolaIME3.dll', 'oyayubi-broker.exe')) {
    if (-not (Test-Path -LiteralPath (Join-Path $Dist $f))) { throw "$f がありません。先に dev-build.ps1 を実行してください。" }
}

# 旧ブローカーを終わらせる (エンジンも終わる)。
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$session = (Get-Process -Id $PID).SessionId
$pipe = New-Object System.IO.Pipes.NamedPipeClientStream('.', "oyayubi-ime-$sid-$session", [IO.Pipes.PipeDirection]::InOut)
try {
    $pipe.Connect(1000)
    $writer = New-Object IO.StreamWriter($pipe, $Utf8NoBom)
    $writer.AutoFlush = $true
    $writer.NewLine = "`n"
    $writer.WriteLine('{"op":"hello","proto":1,"tag":"dev-register","bits":64}')
    $writer.WriteLine('{"op":"quit"}')
} catch { } finally { $pipe.Dispose() }
foreach ($b in @(Get-Process -Name 'oyayubi-broker' -ErrorAction SilentlyContinue | Where-Object { $_.SessionId -eq $session })) {
    if (-not $b.WaitForExit(15000)) { throw "ブローカー (PID $($b.Id)) が終わりません。" }
}

# DLL のスロット: いま登録されていなくて、上書きできる最初のもの (a, b, c, ...)。
# 登録を解いても、読み込んだアプリ (explorer など) は DLL を掴んだままなので、
# 解除して入れ直すと a も b も使用中のことがある。
$inproc = "Registry::HKEY_LOCAL_MACHINE\SOFTWARE\Classes\CLSID\$Clsid\InprocServer32"
$registered = (Get-ItemProperty -LiteralPath $inproc -ErrorAction SilentlyContinue).'(default)'
$slot = $null
foreach ($letter in [char[]]'abcdefghijklmnopqrstuvwxyz') {
    $candidate = Join-Path $Dist "NicolaIME3-$letter.dll"
    if ($candidate -eq $registered) { continue }
    try {
        Copy-Item -LiteralPath (Join-Path $Dist 'NicolaIME3.dll') -Destination $candidate -Force
        $slot = $candidate
        break
    } catch { }
}
if (-not $slot) {
    throw "dist\NicolaIME3-a.dll から -z.dll まですべて使用中です。古い DLL を使うアプリを終了してから再実行してください。"
}
# packaging\uninstall.ps1 は登録済み DLL と同じフォルダの elevated.ps1 を使う。
Copy-Item -LiteralPath (Join-Path $Repo 'packaging\elevated.ps1') -Destination (Join-Path $Dist 'elevated.ps1') -Force
$p = Start-Process "$env:SystemRoot\System32\regsvr32.exe" -ArgumentList @('/s', "`"$slot`"") -Verb RunAs -Wait -PassThru
if ($p.ExitCode -ne 0) { throw "regsvr32 が失敗しました (終了コード $($p.ExitCode))。" }
if ((Get-ItemProperty -LiteralPath $inproc).'(default)' -ne $slot) { throw "登録を読み返すと $slot ではありませんでした。" }

# この利用者の分: タグ、engine.json、言語一覧。
New-Item -Force 'HKCU:\Software\oyayubi-ime' | Out-Null
Set-ItemProperty -LiteralPath 'HKCU:\Software\oyayubi-ime' -Name 'Tag' -Value 'dev'
$version = Join-Path $env:LOCALAPPDATA 'oyayubi-ime\versions\dev'
New-Item -ItemType Directory -Force $version | Out-Null
$engine = [ordered]@{
    format = 1
    python = $Python
    pythonw = (Join-Path (Split-Path $Python) 'pythonw.exe')
    app = $Repo
    args = @('-m', 'oyayubi.engine')
    admin_args = @('-m', 'oyayubi.ime.admin_ui')
}
[IO.File]::WriteAllText((Join-Path $version 'engine.json'), ($engine | ConvertTo-Json -Depth 3), $Utf8NoBom)
Add-Type -Namespace OyayubiDev -Name Input -MemberDefinition @'
[DllImport("input.dll", CharSet = CharSet.Unicode)]
public static extern bool InstallLayoutOrTip(string profile, uint flags);
'@
[void][OyayubiDev.Input]::InstallLayoutOrTip($Layout, 0)

Start-Process -FilePath (Join-Path $Dist 'oyayubi-broker.exe')
Write-Host ("登録した DLL: " + [IO.Path]::GetFileName($slot) + "、ブローカーを起動しました (タグ dev)。")
Write-Host '新しく開いたアプリから新版が読み込まれます。開いていたアプリは旧版の DLL のまま新しいブローカーにつながります。'
