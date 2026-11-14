# oyayubi-ime のアンインストール (REDESIGN §8.3、docs/INSTALL.md)
#
#   powershell -ExecutionPolicy Bypass -File uninstall.ps1 [-RemoveData] [-Yes]
#
# アプリを開いたままでよい。サインアウトや再起動は求めない。
#   1 保守中の印
#   2 HKCU の Tag と Run キーを消す (最初に: 残った DLL はこれで何も起動しなくなる)、言語一覧から外す
#   3 ブローカーに quit (エンジンも終わる)
#   4 管理者権限: 登録解除、読み返し、Program Files\oyayubi-ime の削除 (使用中の DLL は次の起動時)
#   5 HKCU の製品のキーと TSF の利用者設定、%LOCALAPPDATA%\oyayubi-ime を削除
#   6 結果
# 学習した内容・登録した単語・設定 (%USERPROFILE%\.oyayubi-ime) は残す。
# -RemoveData を付けたときだけ、場所と大きさを見せて確かめてから消す (I-7)。

param(
    [switch]$RemoveData,
    [switch]$Yes
)

if ($PSVersionTable.PSEdition -eq "Core") {
    $relaunch = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $PSCommandPath)
    if ($RemoveData) { $relaunch += "-RemoveData" }
    if ($Yes) { $relaunch += "-Yes" }
    & "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" @relaunch
    exit $LASTEXITCODE
}

$ErrorActionPreference = "Stop"
$Local = Join-Path $env:LOCALAPPDATA "oyayubi-ime"
$UserData = Join-Path $env:USERPROFILE ".oyayubi-ime"
$Clsid = "{A7C4E201-0B3A-4F11-9E61-0C1A0B7E0A01}"
$Layout = "0x0411:$Clsid{A7C4E202-0B3A-4F11-9E61-0C1A0B7E0A01}"
$ProductKey = "HKCU:\Software\oyayubi-ime"
$RunKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$Utf8NoBom = New-Object Text.UTF8Encoding($false)
$InprocKey = "Registry::HKEY_LOCAL_MACHINE\SOFTWARE\Classes\CLSID\$Clsid\InprocServer32"
Set-Location $env:USERPROFILE   # 自分を含むフォルダを消すので外に出る

# 管理者権限で動かすのは、登録されている DLL と同じフォルダ (Program Files。
# 管理者しか書けない) にインストール時に置いた elevated.ps1 だけ。
$ElevatedScript = $null
$registeredDll = (Get-ItemProperty -LiteralPath $InprocKey -ErrorAction SilentlyContinue).'(default)'
if ($registeredDll) {
    $elevatedPath = Join-Path (Split-Path $registeredDll) "elevated.ps1"
    if (-not (Test-Path -LiteralPath $elevatedPath)) {
        Write-Host "中止: $elevatedPath がありません (この版の setup で入れたものではありません)。" -ForegroundColor Red
        exit 1
    }
    $ElevatedScript = [IO.File]::ReadAllText($elevatedPath)
}

# 管理者用スクリプトの中身と引数を 1 つのコマンドにして -EncodedCommand 用に符号化する。
# 引数は単一引用符で囲む (中の ' は '' に)。
function New-ElevatedCommand([string]$script, [string[]]$arguments) {
    $quoted = @($arguments | ForEach-Object {
        if ($_ -like "-*") { $_ } else { "'" + $_.Replace("'", "''") + "'" } })
    $command = "& {`n$script`n} " + ($quoted -join " ")
    return [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
}
function Say([string]$msg) { Write-Host $msg }
function Fail([string]$msg) { Write-Host "中止: $msg" -ForegroundColor Red; exit 1 }

# ---------------------------------------------------------------- 1
$createdNew = $false
$maintenance = New-Object System.Threading.Mutex($true, "Local\oyayubi-ime-maintenance", [ref]$createdNew)
if (-not $createdNew) { Fail "別のインストールかアンインストールが動いています。終わってから実行してください。" }

# -RemoveData の確認は最初に (途中で止まらないように)。
if ($RemoveData -and (Test-Path -LiteralPath $UserData)) {
    $size = (Get-ChildItem -LiteralPath $UserData -Recurse -Force -File | Measure-Object -Property Length -Sum).Sum
    Say "学習した内容・登録した単語・設定も消します: $UserData ($([math]::Round($size / 1KB)) KB)"
    if (-not $Yes) {
        $ans = Read-Host "本当に消しますか？ 元に戻せません [y/N]"
        if ($ans -notmatch "^[Yy]") { Fail "取り消しました。何も変えていません。" }
    }
}

# ---------------------------------------------------------------- 2
Say "== 登録の取り消し (この利用者)"
if (Test-Path -LiteralPath $ProductKey) { Remove-ItemProperty -LiteralPath $ProductKey -Name "Tag" -ErrorAction SilentlyContinue }
Remove-ItemProperty -LiteralPath $RunKey -Name "oyayubi-ime" -ErrorAction SilentlyContinue
if ((Test-Path -LiteralPath $ProductKey) -and (Get-ItemProperty -LiteralPath $ProductKey).Tag) { Fail "HKCU の Tag を消せませんでした。" }
$oldUserClsid = "HKCU:\Software\Classes\CLSID\$Clsid"   # v0.1.0 が書いたもの
if (Test-Path -LiteralPath $oldUserClsid) { Remove-Item -LiteralPath $oldUserClsid -Recurse -Force }
Add-Type -Namespace OyayubiUninstall -Name Input -MemberDefinition @'
[DllImport("input.dll", CharSet = CharSet.Unicode)]
public static extern bool InstallLayoutOrTip(string profile, uint flags);
'@
# ILOT_UNINSTALL
if (-not [OyayubiUninstall.Input]::InstallLayoutOrTip($Layout, 1)) { Say "  言語一覧には入っていませんでした。" }

# ---------------------------------------------------------------- 3
Say "== ブローカーの終了"
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$session = (Get-Process -Id $PID).SessionId
$pipe = New-Object System.IO.Pipes.NamedPipeClientStream(".", "oyayubi-ime-$sid-$session", [IO.Pipes.PipeDirection]::InOut)
try {
    $pipe.Connect(1000)
    $writer = New-Object IO.StreamWriter($pipe, $Utf8NoBom)
    $writer.AutoFlush = $true
    $writer.NewLine = "`n"
    $writer.WriteLine('{"op":"hello","proto":1,"tag":"uninstall","bits":64}')
    $writer.WriteLine('{"op":"quit"}')
} catch { } finally { $pipe.Dispose() }
foreach ($b in @(Get-Process -Name "oyayubi-broker" -ErrorAction SilentlyContinue | Where-Object { $_.SessionId -eq $session })) {
    if (-not $b.WaitForExit(15000)) {
        Stop-Process -Id $b.Id -Force
        if (-not $b.WaitForExit(5000)) { Fail "ブローカー (PID $($b.Id)) を終了できませんでした。" }
    }
}
Say "  終了 OK"

# ---------------------------------------------------------------- 4
$leftover = ""
$oldApp = Join-Path $Local "app"   # v0.1.0 の配置: DLL が使用中なら次の起動時に消す
if (Test-Path -LiteralPath $oldApp) { $leftover = $oldApp }
$result = [pscustomobject]@{ ok = $true; message = "not registered"; pending_delete = @() }
if ($ElevatedScript) {
Say "== 登録解除と削除 (管理者権限の確認が 1 回出ます)"
$resultFile = Join-Path $env:TEMP ("oyayubi-elevated-" + [Guid]::NewGuid().ToString("N") + ".json")
$encoded = New-ElevatedCommand $ElevatedScript @("-Action", "uninstall", "-ResultFile", $resultFile, "-Leftover", $leftover)
try {
    $p = Start-Process "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -Verb RunAs -Wait -PassThru `
        -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", $encoded)
} catch { Fail "管理者権限での作業が取り消されました。この利用者ではもう起動しません。もう一度実行すると続きから進みます。" }
if (-not (Test-Path -LiteralPath $resultFile)) { Fail "管理者権限での作業の結果がありません (終了コード $($p.ExitCode))。" }
$result = Get-Content -LiteralPath $resultFile -Raw | ConvertFrom-Json
Remove-Item -LiteralPath $resultFile -Force
if (-not $result.ok) { Fail "登録解除に失敗しました: $($result.message)" }
$registered = Get-ItemProperty -LiteralPath $InprocKey -ErrorAction SilentlyContinue
if ($registered) { Fail "登録が残っています ($($registered.'(default)'))。" }
Say "  登録解除 OK"
} else {
    Say "== この PC には登録されていませんでした (登録解除は不要)。"
    if ($leftover) {
        # 管理者権限なしでは予約削除ができない: 旧 DLL が使用中なら丸ごと残す
        # (半分だけ消すと、旧 DLL が壊れたサーバーを起動し続ける。A-4)。
        $oldDll = Join-Path $leftover "NicolaIME3.dll"
        $free = $true
        if (Test-Path -LiteralPath $oldDll) {
            try { [IO.File]::Move($oldDll, "$oldDll.unused"); [IO.File]::Move("$oldDll.unused", $oldDll) } catch { $free = $false }
        }
        if ($free) { Remove-Item -LiteralPath $leftover -Recurse -Force }
        else { Say "  旧版 ($leftover) の DLL が使用中なので、旧版のフォルダを残しました。アプリを閉じてからもう一度実行すると消えます。" }
    }
}

# ---------------------------------------------------------------- 5
# 登録が消えたあとの、この利用者の分: 製品のキー (Tag と揮発キー Session) と、
# 言語一覧から外したときに Windows が書く TSF の利用者設定 (Enable = 0)。
if (Test-Path -LiteralPath $ProductKey) { Remove-Item -LiteralPath $ProductKey -Recurse -Force }
$userTip = "HKCU:\SOFTWARE\Microsoft\CTF\TIP\$Clsid"
if (Test-Path -LiteralPath $userTip) { Remove-Item -LiteralPath $userTip -Recurse -Force }
$maintenance.ReleaseMutex()
$maintenance.Dispose()
$left = @()
if (Test-Path -LiteralPath $Local) {
    foreach ($item in @(Get-ChildItem -LiteralPath $Local -Force)) {
        # 旧版 (v0.1.0) の配置は管理者権限の側で消した (使用中なら次の起動時に消える)。
        if ($leftover -and $item.FullName -eq $leftover) { continue }
        try { Remove-Item -LiteralPath $item.FullName -Recurse -Force -ErrorAction Stop } catch { $left += $item.FullName }
    }
    if (-not $left.Count) { Remove-Item -LiteralPath $Local -Recurse -Force -ErrorAction SilentlyContinue }
}
if ($RemoveData -and (Test-Path -LiteralPath $UserData)) {
    Remove-Item -LiteralPath $UserData -Recurse -Force
    Say "  $UserData を消しました。"
}

# ---------------------------------------------------------------- 6
Say ""
Say "アンインストールが完了しました。"
if (@($result.pending_delete).Count) {
    Say "アプリを開いたままでも問題ありません。残り $(@($result.pending_delete).Count) 個のファイルは、次に Windows を起動したとき自動で消えます。"
}
if ($left.Count) { Say "消せなかったもの: $($left -join ', ')" }
if (-not $RemoveData) {
    Say "学習した内容・登録した単語・設定は残っています: $UserData"
    Say "  (消すときは -RemoveData を付けて実行してください)"
}
exit 0
