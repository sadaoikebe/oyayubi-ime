# yuragi-ime のインストールと更新 (REDESIGN §8.2、docs/INSTALL.md)
#
#   powershell -ExecutionPolicy Bypass -File setup.ps1 [-Yes]
#
# インストールも更新も同じ手順。アプリを開いたままでよい。どこで失敗しても
# 「前の版が動く」か「新しい版が動く」のどちらか (I-5)。
#   1 保守中の印を立てる (Local\yuragi-ime-maintenance。この PowerShell が終われば消える)
#   2 staging\<tag> に取得・照合・展開。足りない共有物 (ランタイム・辞書・モデル) だけ作る
#   3 本物のブローカーで試運転 (--selftest)
#   4 staging\<tag> を versions\<tag> へ
#   5 管理者権限 (UAC 1 回): Program Files への配置、登録、読み返し、旧版の削除
#   6 旧ブローカーに quit、HKCU の Tag と Run キー、言語一覧、利用者データの移動 (初回のみ)。
#     旧名 (oyayubi-ime) の版からは、その製品のキーを消して旧ブローカーを止め、
#     %USERPROFILE%\.oyayubi-ime の中身を移す
#   7 印を下ろし、新ブローカーを起動して ready を確認
#   8 使われていないものの片付け (失敗しても次回また試す)
# 利用者のデータ (%USERPROFILE%\.yuragi-ime) は移すだけで、消さない (I-7)。

param(
    [switch]$Yes
)

# PowerShell 7 (pwsh) から起動されたら、Windows PowerShell 5.1 で起動し直す。
if ($PSVersionTable.PSEdition -eq "Core") {
    $relaunch = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $PSCommandPath)
    if ($Yes) { $relaunch += "-Yes" }
    & "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" @relaunch
    exit $LASTEXITCODE
}

$ErrorActionPreference = "Stop"
# この中で動かす Python は UTF-8 で入出力する (このプロセスの中だけ)。
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Repo = "sadaoikebe/yuragi-ime"
$Tag = "__TAG__"                         # リリース時に workflow が刻む
$EmbedPythonVersion = "3.13.7"
$EmbedPythonSha256 = "__EMBED_SHA256__"  # 同上 (python.org の公式 zip の SHA-256)
$TorchVersion = "2.9.1"
$Local = Join-Path $env:LOCALAPPDATA "yuragi-ime"
$Shared = Join-Path $Local "shared"
$Versions = Join-Path $Local "versions"
$StagingRoot = Join-Path $Local "staging"
$ProgramRoot = Join-Path $env:ProgramFiles "yuragi-ime"
$UserData = Join-Path $env:USERPROFILE ".yuragi-ime"
# 旧名 (oyayubi-ime) の版の置き場所。移行と旧版の片付けにだけ使う。
$OldLocal = Join-Path $env:LOCALAPPDATA "oyayubi-ime"
$OldUserData = Join-Path $env:USERPROFILE ".oyayubi-ime"
$OldProductKey = "HKCU:\Software\oyayubi-ime"
$Clsid = "{A7C4E201-0B3A-4F11-9E61-0C1A0B7E0A01}"
$Layout = "0x0411:$Clsid{A7C4E202-0B3A-4F11-9E61-0C1A0B7E0A01}"
$ProductKey = "HKCU:\Software\yuragi-ime"
$RunKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
# python -m の代わり: 同梱ランタイムは ._pth があるので作業フォルダを import 経路に入れない。
$Boot = "import os,sys,runpy; sys.path.insert(0, os.getcwd()); m = sys.argv.pop(1); runpy.run_module(m, run_name='__main__', alter_sys=True)"
$Utf8NoBom = New-Object Text.UTF8Encoding($false)

function Say([string]$msg) { Write-Host $msg }
function Fail([string]$msg) {
    Write-Host "中止: $msg" -ForegroundColor Red
    Write-Host "動いている版はそのままです。やり直すと続きから進みます。"
    exit 1
}
function Ask([string]$question) {
    if ($Yes) { Say "$question -> はい (-Yes)"; return $true }
    $ans = Read-Host "$question [Y/n]"
    return ($ans -eq "" -or $ans -match "^[Yy]")
}
function Invoke-Checked([string]$name, [scriptblock]$body) {
    Say "== $name"
    & $body
    if ($LASTEXITCODE -ne 0) { Fail "$name に失敗しました (終了コード $LASTEXITCODE)。" }
}

# ---------------------------------------------------------------- 1 保守中の印
if ($Tag -like "__*" -or $EmbedPythonSha256 -like "__*") {
    Fail "この setup.ps1 にはリリースの刻印がありません。リリースページの setup.ps1 を使ってください。"
}
$createdNew = $false
$maintenance = New-Object System.Threading.Mutex($true, "Local\yuragi-ime-maintenance", [ref]$createdNew)
if (-not $createdNew) { Fail "別のインストールかアンインストールが動いています。終わってから実行してください。" }

Say "ゆらぎ $Tag のインストール"
Say ""
Say "== 環境チェック"
$arch = $env:PROCESSOR_ARCHITECTURE
if ($env:PROCESSOR_ARCHITEW6432) { $arch = $env:PROCESSOR_ARCHITEW6432 }
if ($arch -eq "ARM64") { Fail "ARM 版の Windows には、まだ対応していません。" }
if (-not [Environment]::Is64BitOperatingSystem) { Fail "64bit 版 Windows が必要です。" }
$os = [Environment]::OSVersion.Version
if ($os.Major -lt 10) { Fail "Windows 10 以降が必要です (検出: $os)。" }
$free = (Get-PSDrive ($env:LOCALAPPDATA.Substring(0, 1))).Free
if ($free -lt 3GB) { Fail "空き容量が足りません (必要 3GB、現在 $([math]::Round($free / 1GB, 1))GB)。" }
Say "  Windows $($os.Major).$($os.Minor) 64bit、空き $([math]::Round($free / 1GB, 1))GB OK"
Say ""
Say "次を取得します (足りないものだけ。2 回目からは大半を取り直しません):"
Say "  GitHub Releases: 本体 (約 30MB)、TIP とブローカー"
Say "  python.org: Python $EmbedPythonVersion 埋め込み版 (約 30MB) / PyTorch 公式: torch CPU 版 (約 200MB) / PyPI: 依存"
Say "  github.com (google/mozc): 辞書 (約 93MB) / HuggingFace: LM モデル (約 500MB)"
if (-not (Ask "続けますか？")) { Fail "取り消しました。" }

function Get-Verified([string]$url, [string]$dest) {
    Invoke-WebRequest -UseBasicParsing $url -OutFile $dest
    $expected = (Invoke-WebRequest -UseBasicParsing "$url.sha256").Content
    if ($expected -is [byte[]]) { $expected = [Text.Encoding]::ASCII.GetString($expected) }
    $expected = $expected.Trim().ToLower()
    $actual = (Get-FileHash -LiteralPath $dest -Algorithm SHA256).Hash.ToLower()
    if ($actual -ne $expected) { Fail "SHA-256 が一致しません: $(Split-Path $dest -Leaf)" }
    Say "  照合 OK: $(Split-Path $dest -Leaf)"
    return $expected
}

# 共有物は「作りかけの名前で作って、完成したら改名」。完成したものは取り直さない。
function New-Shared([string]$final, [scriptblock]$build) {
    if (Test-Path -LiteralPath $final) { return }
    $partial = "$final.partial"
    if (Test-Path -LiteralPath $partial) { Remove-Item -LiteralPath $partial -Recurse -Force }
    New-Item -ItemType Directory -Force $partial | Out-Null
    & $build $partial
    Rename-Item -LiteralPath $partial -NewName (Split-Path $final -Leaf)
}

function Invoke-App([string]$python, [string]$app, [string[]]$moduleAndArgs) {
    Push-Location -LiteralPath $app
    try { & $python -c $Boot @moduleAndArgs } finally { Pop-Location }
}

function Write-EngineJson([string]$path, [string]$app, [string[]]$extra) {
    $engine = [ordered]@{
        format = 1
        python = $script:Python
        pythonw = (Join-Path (Split-Path $script:Python) "pythonw.exe")
        app = $app
        args = @("-c", $Boot, "yuragi.engine", "--dict-dir", $script:Dict,
                 "--cache-dir", $script:Cache, "--lm-model", $script:Model) + $extra
        admin_args = @("-c", $Boot, "yuragi.ime.admin_ui")
    }
    [IO.File]::WriteAllText($path, ($engine | ConvertTo-Json -Depth 4), $Utf8NoBom)
}

# ---------------------------------------------------------------- 2 staging
Say ""
Say "== 取得と照合 (staging)"
$Stage = Join-Path $StagingRoot $Tag
# staging は自分の作業場: 前回の途中のものがあれば消して作り直す (A-16)。
if (Test-Path -LiteralPath $Stage) { Remove-Item -LiteralPath $Stage -Recurse -Force }
$Download = Join-Path $Stage "download"
New-Item -ItemType Directory -Force $Download | Out-Null
$baseUrl = "https://github.com/$Repo/releases/download/$Tag"
$sourceZip = Join-Path $Download "yuragi-ime-source.zip"
$dllPath = Join-Path $Download "yuragi_tip.dll"
$brokerPath = Join-Path $Download "yuragi-broker.exe"
[void](Get-Verified "$baseUrl/yuragi-ime-source.zip" $sourceZip)
$DllSha = Get-Verified "$baseUrl/yuragi_tip.dll" $dllPath
$BrokerSha = Get-Verified "$baseUrl/yuragi-broker.exe" $brokerPath
$StageApp = Join-Path $Stage "app"
Expand-Archive -LiteralPath $sourceZip -DestinationPath $StageApp

# 同梱ランタイム: Python の版・依存の固定・torch の版で名前が決まる。中身が同じなら共有する。
$requirements = Join-Path $StageApp "packaging\requirements.txt"
$reqHash = (Get-FileHash -LiteralPath $requirements -Algorithm SHA256).Hash.Substring(0, 12).ToLower()
$runtimeId = "py$EmbedPythonVersion-torch$TorchVersion-$reqHash"
$Runtime = Join-Path $Shared "runtime\$runtimeId"
New-Shared $Runtime {
    param($dir)
    Say "== 同梱ランタイム ($runtimeId)"
    $embedZip = Join-Path $Download "python-embed.zip"
    Invoke-WebRequest -UseBasicParsing "https://www.python.org/ftp/python/$EmbedPythonVersion/python-$EmbedPythonVersion-embed-amd64.zip" -OutFile $embedZip
    if ((Get-FileHash -LiteralPath $embedZip -Algorithm SHA256).Hash.ToLower() -ne $EmbedPythonSha256.ToLower()) {
        Fail "Python 埋め込み版の SHA-256 が一致しません。"
    }
    Expand-Archive -LiteralPath $embedZip -DestinationPath $dir
    $pth = Get-ChildItem -LiteralPath $dir -Filter "python*._pth" | Select-Object -First 1 -ExpandProperty FullName
    @(("python" + $EmbedPythonVersion.Substring(0, 4).Replace(".", "") + ".zip"), ".", "Lib\site-packages", "import site") |
        Set-Content -LiteralPath $pth -Encoding ASCII
    $py = Join-Path $dir "python.exe"
    $getPip = Join-Path $Download "get-pip.py"
    Invoke-WebRequest -UseBasicParsing "https://bootstrap.pypa.io/get-pip.py" -OutFile $getPip
    Invoke-Checked "pip (公式 get-pip.py)" { & $py $getPip --no-warn-script-location }
    Invoke-Checked "torch CPU 版 (PyTorch 公式インデックス)" {
        & $py -m pip install --no-warn-script-location --index-url https://download.pytorch.org/whl/cpu "torch==$TorchVersion"
    }
    Invoke-Checked "依存パッケージ (PyPI、バージョン固定)" {
        & $py -m pip install --no-warn-script-location -r $requirements
    }
    Invoke-Checked "依存の読み込み確認" { & $py -c "import torch, transformers, sentencepiece, huggingface_hub" }
}
$script:Python = Join-Path $Runtime "python.exe"

$manifest = Get-Content -LiteralPath (Join-Path $StageApp "data\dictionary-manifest.json") -Raw | ConvertFrom-Json
$script:Dict = Join-Path $Shared "dict\$($manifest.upstream_commit)"
New-Shared $script:Dict {
    param($dir)
    Invoke-Checked "辞書の取得 (google/mozc、上流コミット固定)" {
        Invoke-App $script:Python $StageApp @("tools.wp0.fetch_mozc_dict", "--out", $dir)
    }
    Invoke-Checked "辞書の照合 (SHA-256)" {
        Invoke-App $script:Python $StageApp @("yuragi.lattice.dict_manifest", "verify", "--dict-dir", $dir)
    }
}
$revision = (Select-String -LiteralPath (Join-Path $StageApp "packaging\fetch_model.py") -Pattern '^REVISION = "([0-9a-f]+)"').Matches[0].Groups[1].Value
$script:Model = Join-Path $Shared "model\$revision"
New-Shared $script:Model {
    param($dir)
    Invoke-Checked "LM モデルの取得 (HuggingFace、リビジョン固定)" {
        & $script:Python (Join-Path $StageApp "packaging\fetch_model.py") $dir
    }
}
$script:Cache = Join-Path $Shared "cache"
New-Item -ItemType Directory -Force $script:Cache | Out-Null

# ---------------------------------------------------------------- 3 試運転
# 利用者のデータには触らない: 試運転のエンジンは staging の中のフォルダを使う。
$selftestData = Join-Path $Stage "selftest-data"
New-Item -ItemType Directory -Force $selftestData | Out-Null
$stageEngine = Join-Path $Stage "engine.json"
Write-EngineJson $stageEngine $StageApp @("--data-dir", $selftestData)
Say ""
Say "== 試運転 (本物のブローカーでエンジンを起動し、1 回変換。初回は辞書の準備に数分)"
$selftestOut = Join-Path $Stage "selftest.txt"
$proc = Start-Process -FilePath $brokerPath -ArgumentList @("--selftest", "`"$stageEngine`"", "--data-dir", "`"$selftestData`"") `
    -Wait -PassThru -RedirectStandardOutput $selftestOut
if ($proc.ExitCode -ne 0) {
    Fail "試運転に失敗しました (終了コード $($proc.ExitCode))。記録: $(Join-Path $selftestData 'logs')"
}
Say "  試運転 OK"

# ---------------------------------------------------------------- 旧ブローカー
# 旧名の版のブローカーは、パイプが oyayubi-ime-…、プロセスが oyayubi-broker。
function Connect-Broker([int]$timeoutMs, [string]$pipePrefix = "yuragi-ime") {
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $session = (Get-Process -Id $PID).SessionId
    $pipe = New-Object System.IO.Pipes.NamedPipeClientStream(".", "$pipePrefix-$sid-$session", [IO.Pipes.PipeDirection]::InOut)
    try { $pipe.Connect($timeoutMs) } catch { $pipe.Dispose(); return $null }
    $writer = New-Object IO.StreamWriter($pipe, $Utf8NoBom)
    $writer.AutoFlush = $true
    $writer.NewLine = "`n"
    $reader = New-Object IO.StreamReader($pipe, $Utf8NoBom)
    $writer.WriteLine('{"op":"hello","proto":1,"tag":"setup","bits":64}')
    return @{ Pipe = $pipe; Reader = $reader; Writer = $writer }
}
function Read-BrokerLine($conn, [int]$timeoutMs) {
    $task = $conn.Reader.ReadLineAsync()
    if ($task.Wait($timeoutMs)) { return $task.Result }
    return $null
}
function Stop-Broker([string]$pipePrefix = "yuragi-ime", [string]$processName = "yuragi-broker") {
    $conn = Connect-Broker 1000 $pipePrefix
    if ($conn) {
        [void](Read-BrokerLine $conn 2000)
        try { $conn.Writer.WriteLine('{"op":"quit"}') } catch { }
        $conn.Pipe.Dispose()
    }
    $session = (Get-Process -Id $PID).SessionId
    $brokers = @(Get-Process -Name $processName -ErrorAction SilentlyContinue | Where-Object { $_.SessionId -eq $session })
    foreach ($b in $brokers) {
        if (-not $b.WaitForExit(15000)) {
            Say "  ブローカーが終わらないので終了させます (PID $($b.Id))。"
            Stop-Process -Id $b.Id -Force
            if (-not $b.WaitForExit(5000)) { Fail "ブローカー (PID $($b.Id)) を終了できませんでした。" }
        }
    }
}

# 利用者データを $from から $to へ、中身ごと移す。移すだけで消さない (I-7): $to に同じ名前が
# あれば旧い方を残す。空になった $from だけ消す。返すのは利用者に見せる記録。
function Move-UserData([string]$from, [string]$to) {
    $notes = @()
    foreach ($item in @(Get-ChildItem -LiteralPath $from -Force)) {
        $dest = Join-Path $to $item.Name
        if (Test-Path -LiteralPath $dest) {
            $notes += "$($item.Name) は両方にあるので、旧い方 ($($item.FullName)) を残しました"
        } else {
            Move-Item -LiteralPath $item.FullName -Destination $dest
        }
    }
    if (@(Get-ChildItem -LiteralPath $from -Force).Count -eq 0) {
        Remove-Item -LiteralPath $from -Force   # 空のときだけ (-Recurse なし)
        $notes += "$from の中身を $to へ移しました"
    }
    return $notes
}

# ---------------------------------------------------------------- 4 versions へ
$Version = Join-Path $Versions $Tag
New-Item -ItemType Directory -Force $Versions | Out-Null
if (Test-Path -LiteralPath $Version) {
    # 同じ版の入れ直し: その版のエンジンが作業フォルダを使っているので先に止める。
    Say "  同じ版 ($Tag) を入れ直します。"
    Stop-Broker
    $aside = "$Version.old-" + [Guid]::NewGuid().ToString("N").Substring(0, 8)
    Rename-Item -LiteralPath $Version -NewName (Split-Path $aside -Leaf)
}
Move-Item -LiteralPath $Stage -Destination $Version
Remove-Item -LiteralPath (Join-Path $Version "selftest-data") -Recurse -Force -ErrorAction SilentlyContinue
$App = Join-Path $Version "app"
Write-EngineJson (Join-Path $Version "engine.json") $App @()

# ---------------------------------------------------------------- 5 管理者権限
# v0.1.0 の旧配置 (%LOCALAPPDATA%\oyayubi-ime\app) は、その DLL を読み込んだアプリが
# 残っていれば、次の Windows 起動時に消す (今消すと旧 DLL が壊れたサーバーを起動し続ける)。
$OldApp = Join-Path $OldLocal "app"
$leftover = ""
if (Test-Path -LiteralPath $OldApp) { $leftover = $OldApp }

# 管理者用スクリプトの中身と引数を 1 つのコマンドにして -EncodedCommand 用に符号化する。
# 引数は単一引用符で囲む (中の ' は '' に)。
function New-ElevatedCommand([string]$script, [string[]]$arguments) {
    $quoted = @($arguments | ForEach-Object {
        if ($_ -like "-*") { $_ } else { "'" + $_.Replace("'", "''") + "'" } })
    $command = "& {`n$script`n} " + ($quoted -join " ")
    return [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
}
$ElevatedPath = Join-Path $App "packaging\elevated.ps1"   # from the verified source zip
$ElevatedSha = (Get-FileHash -LiteralPath $ElevatedPath -Algorithm SHA256).Hash.ToLower()
function Invoke-Elevated([string[]]$arguments) {
    $script = [IO.File]::ReadAllText($ElevatedPath)
    $resultFile = Join-Path $env:TEMP ("yuragi-elevated-" + [Guid]::NewGuid().ToString("N") + ".json")
    $encoded = New-ElevatedCommand $script ($arguments + @("-ResultFile", $resultFile))
    try {
        $p = Start-Process "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -Verb RunAs -Wait -PassThru `
            -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", $encoded)
    } catch {
        Fail "管理者権限での作業が取り消されました。"
    }
    if (-not (Test-Path -LiteralPath $resultFile)) { Fail "管理者権限での作業の結果がありません (終了コード $($p.ExitCode))。" }
    $result = Get-Content -LiteralPath $resultFile -Raw | ConvertFrom-Json
    Remove-Item -LiteralPath $resultFile -Force
    return $result
}

Say ""
Say "== 配置と登録 (管理者権限の確認が 1 回出ます)"
$result = Invoke-Elevated @("-Action", "install", "-Tag", $Tag, "-DllSource", (Join-Path $Version "download\yuragi_tip.dll"),
    "-DllSha256", $DllSha, "-BrokerSource", (Join-Path $Version "download\yuragi-broker.exe"),
    "-BrokerSha256", $BrokerSha, "-ScriptSource", $ElevatedPath, "-ScriptSha256", $ElevatedSha,
    "-Leftover", $leftover)
if (-not $result.ok) { Fail "配置と登録に失敗しました: $($result.message)" }
$DllFinal = Join-Path $ProgramRoot "$Tag\yuragi_tip.dll"
$BrokerFinal = Join-Path $ProgramRoot "$Tag\yuragi-broker.exe"
# I-6: 「登録した」と言うのは、読み返して確かめてから。
$registered = (Get-ItemProperty -LiteralPath "Registry::HKEY_LOCAL_MACHINE\SOFTWARE\Classes\CLSID\$Clsid\InprocServer32" -ErrorAction SilentlyContinue).'(default)'
if ($registered -ne $DllFinal) { Fail "登録を読み返すと $registered でした ($DllFinal のはず)。" }
Say "  登録 OK: $DllFinal"
$pendingDelete = @($result.pending_delete)

# ---------------------------------------------------------------- 6 切り替え
Say ""
Say "== 切り替え"
Stop-Broker
New-Item -Force $ProductKey | Out-Null
Set-ItemProperty -LiteralPath $ProductKey -Name "Tag" -Value $Tag
Set-ItemProperty -LiteralPath $RunKey -Name "yuragi-ime" -Value "`"$BrokerFinal`""
if ((Get-ItemProperty -LiteralPath $ProductKey).Tag -ne $Tag) { Fail "HKCU の Tag を書けませんでした。" }
# 旧版 (v0.1.0) が利用者側に書いた COM 登録は、HKLM の登録より優先されるので消す。
$oldUserClsid = "HKCU:\Software\Classes\CLSID\$Clsid"
if (Test-Path -LiteralPath $oldUserClsid) { Remove-Item -LiteralPath $oldUserClsid -Recurse -Force }
Add-Type -Namespace YuragiSetup -Name Input -MemberDefinition @'
[DllImport("input.dll", CharSet = CharSet.Unicode)]
public static extern bool InstallLayoutOrTip(string profile, uint flags);
'@
if (-not [YuragiSetup.Input]::InstallLayoutOrTip($Layout, 0)) { Fail "言語一覧に追加できませんでした。" }

# 旧名の版 (開発用の登録): 製品のキーを消すと、旧 DLL を読み込んだアプリは何も起動しなくなる。
# そのうえで旧ブローカーを止める (利用者データのファイルを開いているので、移す前に)。
if (Test-Path -LiteralPath $OldProductKey) { Remove-Item -LiteralPath $OldProductKey -Recurse -Force }
Stop-Broker "oyayubi-ime" "oyayubi-broker"

New-Item -ItemType Directory -Force $UserData | Out-Null
$migrationNotes = @()
# 旧名の版の利用者データ (%USERPROFILE%\.oyayubi-ime) を .yuragi-ime へ。
if (Test-Path -LiteralPath $OldUserData) { $migrationNotes += @(Move-UserData $OldUserData $UserData) }

# 学習履歴と個人辞書は %USERPROFILE%\.yuragi-ime へ (A-15、§4.2)。移すだけで消さない。
$configPath = Join-Path $UserData "config.json"
$oldData = Join-Path $OldApp "data"
if (Test-Path -LiteralPath $oldData) {
    $config = [pscustomobject]@{}
    if (Test-Path -LiteralPath $configPath) {
        try { $config = [IO.File]::ReadAllText($configPath) | ConvertFrom-Json } catch { $config = $null }
    }
    foreach ($name in @("learning.jsonl", "personal_lexicon.jsonl")) {
        $from = Join-Path $oldData $name
        $to = Join-Path $UserData $name
        if ((Test-Path -LiteralPath $from) -and -not (Test-Path -LiteralPath $to)) {
            Move-Item -LiteralPath $from -Destination $to
            $migrationNotes += "$name を $UserData へ移しました"
        } elseif (Test-Path -LiteralPath $from) {
            $migrationNotes += "$name は両方にあるので、旧い方 ($from) を残しました"
        }
    }
    if ($config -ne $null) {
        $names = $config.PSObject.Properties.Name
        # 旧版の学習の入/切は「ファイルがあるか」と「.paused」だった (§6.7)。
        if (-not ($names -contains "learning")) {
            $state = $null
            if (Test-Path -LiteralPath (Join-Path $oldData "learning.jsonl.paused")) { $state = "paused" }
            elseif (-not (Test-Path -LiteralPath (Join-Path $UserData "learning.jsonl"))) { $state = "off" }
            if ($state) { $config | Add-Member -NotePropertyName "learning" -NotePropertyValue $state; $migrationNotes += "学習: $state (旧版の設定を引き継ぎ)" }
        }
        # 旧版の setup が書いたモデルの場所は旧配置の中: 新しい版は engine.json で渡す。
        if (($names -contains "lm_model") -and ([string]$config.lm_model).StartsWith($OldApp)) {
            $config.PSObject.Properties.Remove("lm_model")
        }
        [IO.File]::WriteAllText($configPath, ($config | ConvertTo-Json -Depth 4), $Utf8NoBom)
    } else {
        $migrationNotes += "config.json を読めなかったので変えていません"
    }
}

# ---------------------------------------------------------------- 7 起動と確認
$maintenance.ReleaseMutex()
$maintenance.Dispose()
Say ""
Say "== 起動の確認 (最大 60 秒)"
Start-Process -FilePath $BrokerFinal
$deadline = (Get-Date).AddSeconds(60)
$state = ""
$conn = $null
while ((Get-Date) -lt $deadline -and -not $conn) { $conn = Connect-Broker 1000 }
if ($conn) {
    while ((Get-Date) -lt $deadline) {
        $line = Read-BrokerLine $conn ([Math]::Max(0, [int]($deadline - (Get-Date)).TotalMilliseconds))
        if ($null -eq $line) { break }
        $msg = $line | ConvertFrom-Json
        if ($msg.state) { $state = $msg.state }
        if ($state -eq "ready" -or $state -eq "down" -or $state -eq "not-installed") { break }
    }
    $conn.Pipe.Dispose()
}
if ($state -ne "ready") {
    Write-Host "変換エンジンが起動しませんでした (状態: $(if ($state) { $state } else { '応答なし' }))。" -ForegroundColor Red
    Write-Host "記録: $(Join-Path $UserData 'logs')。次のサインインでも起動を試みます。"
    exit 1
}
Say "  起動 OK"

# ---------------------------------------------------------------- 8 片付け
# 失敗はインストールの失敗にしない。次回の setup がまた試す。
$keep = @($Runtime, $script:Dict, $script:Model)
$cacheName = ""
try {
    Push-Location -LiteralPath $App
    $cacheName = (& $script:Python -c "import os,sys; sys.path.insert(0, os.getcwd()); from yuragi.lattice.mozc_dict import cache_name; print(cache_name())").Trim()
    Pop-Location
} catch { Pop-Location }
$cleanup = @()
$cleanup += @(Get-ChildItem -LiteralPath $Versions -Directory | Where-Object { $_.Name -ne $Tag })
foreach ($kind in @("runtime", "dict", "model")) {
    $dir = Join-Path $Shared $kind
    if (Test-Path -LiteralPath $dir) { $cleanup += @(Get-ChildItem -LiteralPath $dir -Directory | Where-Object { $keep -notcontains $_.FullName }) }
}
if ($cacheName) { $cleanup += @(Get-ChildItem -LiteralPath $script:Cache -File | Where-Object { $_.Name -ne $cacheName }) }
if (Test-Path -LiteralPath $StagingRoot) { $cleanup += @(Get-Item -LiteralPath $StagingRoot) }
# 旧名の版の %LOCALAPPDATA%\oyayubi-ime (v0.1.0 の install-manifest.json、開発用の登録の
# versions\dev)。v0.1.0 の app が次の Windows 起動時の削除待ちなら、次の setup で消す。
if ((Test-Path -LiteralPath $OldLocal) -and -not (Test-Path -LiteralPath $OldApp)) { $cleanup += @(Get-Item -LiteralPath $OldLocal) }
$left = @()
foreach ($item in $cleanup) {
    try { Remove-Item -LiteralPath $item.FullName -Recurse -Force -ErrorAction Stop } catch { $left += $item.FullName }
}

Say ""
Say "完了しました ($Tag)。"
foreach ($note in $migrationNotes) { Say "  $note" }
if ($pendingDelete.Count) {
    Say "  旧版のファイルが $($pendingDelete.Count) 個、使用中のため残っています。次に Windows を起動したとき自動で消えます。"
}
if ($left.Count) { Say "  片付けられなかったもの ($($left.Count) 件) は、次の setup でまた片付けます。" }
Say "入力切替 (Win+Space) で「ゆらぎ」を選ぶと、タスクバーに「ゆ」が出ます。"
Say "開いていたアプリもそのまま使えます。"
Say "更新: このコマンドをもう一度実行してください。"
Say "アンインストール: powershell -ExecutionPolicy Bypass -File `"$(Join-Path $App 'packaging\uninstall.ps1')`""
Say "  学習した内容・登録した単語・設定 ($UserData) は残ります。"
exit 0
