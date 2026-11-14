# yuragi-ime のアンインストールの入口 (docs/INSTALL.md)
#
# リリースには uninstall.ps1 の名前で、setup.ps1 と並べて置く。README の 1 行で取得して実行する。
#   powershell -ExecutionPolicy Bypass -File uninstall.ps1 [-RemoveData] [-Yes]
#
# 入っている版を見つけて、その版に入っている本体 (packaging\uninstall.ps1) を呼ぶ。
# 本体はその版の置き場所を知っているので、消すのは入れた版自身に任せる。
#   1 HKCU の Tag (この利用者に入っている版)
#   2 Tag が無ければ、Program Files に登録が残っている版。本体は Tag を最初に消すので、
#     途中で止まったアンインストールのやり直しはここで見つかる
# 引数はそのまま本体に渡す (-RemoveData を付けたときだけ利用者のデータも消す)。

param(
    [switch]$RemoveData,
    [switch]$Yes
)

$ErrorActionPreference = "Stop"
$Clsid = "{A7C4E201-0B3A-4F11-9E61-0C1A0B7E0A01}"
$InprocKey = "Registry::HKEY_LOCAL_MACHINE\SOFTWARE\Classes\CLSID\$Clsid\InprocServer32"
$ProgramRoot = Join-Path $env:ProgramFiles "yuragi-ime"

$tag = (Get-ItemProperty -LiteralPath "HKCU:\Software\yuragi-ime" -ErrorAction SilentlyContinue).Tag
if (-not $tag) {
    $dll = (Get-ItemProperty -LiteralPath $InprocKey -ErrorAction SilentlyContinue).'(default)'
    if ($dll -and (Split-Path (Split-Path $dll)) -eq $ProgramRoot) { $tag = Split-Path (Split-Path $dll) -Leaf }
}
if ($tag -eq "dev") {
    Write-Host "開発用の登録 (dev) です。リポジトリの packaging\uninstall.ps1 で外してください。"
    exit 1
}
$body = Join-Path $env:LOCALAPPDATA "yuragi-ime\versions\$tag\app\packaging\uninstall.ps1"
if (-not $tag -or -not (Test-Path -LiteralPath $body)) {
    Write-Host "ゆらぎは入っていません。何もせずに終わります。"
    exit 0
}

Write-Host "ゆらぎ $tag をアンインストールします。"
$argv = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $body)
if ($RemoveData) { $argv += "-RemoveData" }
if ($Yes) { $argv += "-Yes" }
& "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" @argv
exit $LASTEXITCODE
