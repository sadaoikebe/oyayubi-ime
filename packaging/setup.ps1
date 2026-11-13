# oyayubi-ime セットアップ (docs/INSTALL.md の実装)
#
# 使い方:
#   powershell -ExecutionPolicy Bypass -File setup.ps1
#
# やること: 環境チェック → 取得元の予告 → リリースから本体と DLL を取得
# (SHA-256 照合) → 専用 Python 環境の構築 → LM 依存とモデルの取得 →
# 辞書キャッシュ構築 → IME 登録 → スモークテスト → インストール記録。
# 足りない前提は、公式の方法で入れられるものは承認を取ってから入れる。
# システム側 (PATH・システムの Python・レジストリの IME 登録以外) は触らない。

param(
    [string]$Tag = "",
    [string]$InstallRoot = "$env:LOCALAPPDATA\oyayubi-ime",
    [switch]$Yes,
    # システムの Python があっても使わず、同梱ランタイムを入れる。
    [switch]$EmbeddedPython
)

# PowerShell 7 (pwsh) から起動されたら、Windows PowerShell 5.1 で起動し直す。
# 実行と検証は、Windows に必ず入っている 5.1 の一経路だけにしてある。
if ($PSVersionTable.PSEdition -eq "Core") {
    $relaunch = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $PSCommandPath)
    foreach ($kv in $PSBoundParameters.GetEnumerator()) {
        if ($kv.Value -is [System.Management.Automation.SwitchParameter]) {
            if ($kv.Value.IsPresent) { $relaunch += "-$($kv.Key)" }
        } else {
            $relaunch += "-$($kv.Key)"
            $relaunch += [string]$kv.Value
        }
    }
    & "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" @relaunch
    exit $LASTEXITCODE
}

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$Repo = "sadaoikebe/oyayubi-ime"
$ReleaseTag = "__TAG__"          # リリース時に workflow が実タグへ置換する
$EmbedPythonVersion = "3.13.7"
$EmbedPythonSha256 = "__EMBED_SHA256__"  # 同上 (python.org の公式 zip の SHA-256)
$TorchVersion = "2.9.1"
$MinPython = [version]"3.11"
$AppDir = Join-Path $InstallRoot "app"
$ManifestPath = Join-Path $InstallRoot "install-manifest.json"
$RuntimeDir = "$env:USERPROFILE\.oyayubi-ime"

function Say([string]$msg) { Write-Host $msg }
function Fail([string]$msg) { Write-Host "中止: $msg" -ForegroundColor Red; exit 1 }

function Ask([string]$question) {
    if ($Yes) { Say "$question -> はい (-Yes)"; return $true }
    $ans = Read-Host "$question [Y/n]"
    return ($ans -eq "" -or $ans -match "^[Yy]")
}

function Test-Tcp443([string]$hostName) {
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $async = $client.BeginConnect($hostName, 443, $null, $null)
        $ok = $async.AsyncWaitHandle.WaitOne(4000)
        if ($ok) { $client.EndConnect($async) }
        $client.Close()
        return $ok
    } catch { return $false }
}

function Get-PythonInfo([string]$exe, [string[]]$prefix) {
    try {
        $out = & $exe @prefix --version 2>&1
        if ($out -match "Python (\d+)\.(\d+)\.(\d+)") {
            $ver = [version]("{0}.{1}.{2}" -f $Matches[1], $Matches[2], $Matches[3])
            $venvOk = $false
            & $exe @prefix -c "import venv, ensurepip" 2>$null
            if ($LASTEXITCODE -eq 0) { $venvOk = $true }
            return [pscustomobject]@{ Exe = $exe; Prefix = $prefix; Version = $ver; VenvOk = $venvOk }
        }
    } catch { }
    return $null
}

function Find-Python {
    $candidates = @()
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) { $candidates += ,@($py.Source, @("-3")) }
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) { $candidates += ,@($python.Source, @()) }
    foreach ($base in @("HKLM:\SOFTWARE\Python\PythonCore", "HKCU:\SOFTWARE\Python\PythonCore")) {
        if (Test-Path $base) {
            foreach ($verKey in Get-ChildItem $base) {
                $installPath = (Get-ItemProperty "$($verKey.PSPath)\InstallPath" -ErrorAction SilentlyContinue)."(default)"
                if ($installPath) { $candidates += ,@((Join-Path $installPath "python.exe"), @()) }
            }
        }
    }
    foreach ($cand in $candidates) {
        $info = Get-PythonInfo $cand[0] $cand[1]
        if ($info -and $info.Version -ge $MinPython) { return $info }
    }
    return $null
}

function Invoke-Step([string]$name, [scriptblock]$body) {
    Say "== $name"
    & $body
    if ($LASTEXITCODE -ne 0) { Fail "$name に失敗しました (終了コード $LASTEXITCODE)。" }
}

# ---------------------------------------------------------------- チェック
Say "oyayubi-ime セットアップ"
Say "インストール先: $InstallRoot"
Say ""

# 再実行はそのまま更新になる (専用オプションは無い)。学習データ・
# 個人辞書・設定は保持される。下の各段階は冪等に作ってある。
$Updating = $false
$OldTag = $null
if (Test-Path $ManifestPath) {
    $Updating = $true
    try { $OldTag = (Get-Content $ManifestPath -Raw | ConvertFrom-Json).tag } catch { }
    Say "インストール済み (タグ: $OldTag) を検出しました。更新として実行します。"
    Say "学習データ・個人辞書・設定は保持されます。"
    Say ""
}
if (-not $Updating -and (Test-Path $AppDir) -and (Get-ChildItem $AppDir -Force | Select-Object -First 1)) {
    Fail "$AppDir に未知の内容があります。上書きしません。空にしてから再実行してください。"
}

Say "== 環境チェック"
if (-not [Environment]::Is64BitOperatingSystem) { Fail "64bit 版 Windows が必要です。" }
$os = [Environment]::OSVersion.Version
if ($os.Major -lt 10) { Fail "Windows 10 以降が必要です (検出: $os)。" }
Say "  OS: Windows $($os.Major).$($os.Minor) 64bit OK"
Say "  PowerShell: $($PSVersionTable.PSVersion) ($($PSVersionTable.PSEdition))"

$drive = Split-Path $InstallRoot -Qualifier
$free = (Get-PSDrive ($drive.TrimEnd(':'))).Free
if ($free -lt 3GB) { Fail "空き容量が不足しています (必要 3GB、現在 $([math]::Round($free / 1GB, 1))GB)。" }
Say "  空き容量: $([math]::Round($free / 1GB, 1))GB OK"

$hosts = @("github.com", "raw.githubusercontent.com", "pypi.org", "download.pytorch.org", "huggingface.co", "python.org")
$unreachable = @($hosts | Where-Object { -not (Test-Tcp443 $_) })
if ($unreachable.Count) {
    Fail "次の取得先に届きません: $($unreachable -join ', ')。回線やプロキシを確認して再実行してください。"
}
Say "  ネット接続: 取得先 $($hosts.Count) 件すべて到達 OK"

$python = if ($EmbeddedPython) { $null } else { Find-Python }
$useEmbed = $false
if ($EmbeddedPython) {
    Say "  Python: -EmbeddedPython の指定により、システムの Python は使いません。"
    $useEmbed = $true
} elseif ($python) {
    Say "  Python: $($python.Version) ($($python.Exe)) を検出"
    if (-not $python.VenvOk) {
        Say "  venv が使えません。同梱ランタイム経路に切り替えます。"
        $useEmbed = $true
    }
} else {
    Say "  Python: 3.11 以上が見つかりませんでした。"
    $useEmbed = $true
}
if ($useEmbed) {
    Say ""
    Say "公式の同梱ランタイム (python.org の embeddable Python $EmbedPythonVersion、約 30MB) を"
    Say "インストール先の中にだけ入れます。システムの Python や PATH は変わりません。"
    if (-not (Ask "同梱ランタイムを入れますか？")) {
        Fail "Python 3.11 以上を用意してから再実行してください。"
    }
}

# ---------------------------------------------------------------- 取得
if (-not $Tag) {
    if ($ReleaseTag -notmatch "^__") { $Tag = $ReleaseTag }
    else {
        $latest = Invoke-RestMethod "https://api.github.com/repos/$Repo/releases/latest"
        $Tag = $latest.tag_name
    }
}
Say ""
Say "== 取得 (リリース $Tag)"
Say "  本体ソース (約 30MB)・TIP DLL・torch CPU 版 (約 200MB)・transformers・"
Say "  LM モデル (約 500MB) を、GitHub Releases / PyTorch 公式 / PyPI / HuggingFace から取得します。"
New-Item -ItemType Directory -Force $AppDir | Out-Null
$tmp = Join-Path $InstallRoot "download"
New-Item -ItemType Directory -Force $tmp | Out-Null
$baseUrl = "https://github.com/$Repo/releases/download/$Tag"

function Get-Verified([string]$url, [string]$dest) {
    Invoke-WebRequest -UseBasicParsing $url -OutFile $dest
    $shaFile = "$dest.sha256"
    Invoke-WebRequest -UseBasicParsing "$url.sha256" -OutFile $shaFile
    $expected = (Get-Content $shaFile -Raw).Trim().ToLower()
    $actual = (Get-FileHash $dest -Algorithm SHA256).Hash.ToLower()
    if ($actual -ne $expected) { Fail "SHA-256 が一致しません: $dest" }
    Say "  照合 OK: $(Split-Path $dest -Leaf)"
}

if ($Updating) {
    $servers = Get-CimInstance Win32_Process -Filter "Name like 'python%'" -ErrorAction SilentlyContinue |
        Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($InstallRoot) -and $_.CommandLine -like "*oyayubi.ime.server*" }
    foreach ($s in $servers) {
        Stop-Process -Id $s.ProcessId -Force -ErrorAction SilentlyContinue
        Say "  更新のため常駐サーバを停止しました (PID $($s.ProcessId))。"
    }
}
$sourceZip = Join-Path $tmp "oyayubi-ime-source.zip"
$dllPath = Join-Path $tmp "NicolaIME3.dll"
Get-Verified "$baseUrl/oyayubi-ime-source.zip" $sourceZip
Get-Verified "$baseUrl/NicolaIME3.dll" $dllPath
Expand-Archive $sourceZip -DestinationPath $AppDir -Force
try {
    Copy-Item $dllPath (Join-Path $AppDir "NicolaIME3.dll") -Force -ErrorAction Stop
} catch {
    Fail "DLL を置き換えられませんでした。IME を使っているアプリを全部閉じて (またはサインアウトして) から、setup.ps1 を再実行してください。再実行は更新として続きから動きます。"
}

# ---------------------------------------------------------------- 実行環境
Say ""
$runtimePython = $null
if (-not $useEmbed) {
    Invoke-Step "専用 Python 環境 (venv) の作成" {
        & $python.Exe @($python.Prefix) -m venv (Join-Path $AppDir "runtime")
    }
    $runtimePython = Join-Path $AppDir "runtime\Scripts\python.exe"
} else {
    $embedZip = Join-Path $tmp "python-$EmbedPythonVersion-embed-amd64.zip"
    Invoke-WebRequest -UseBasicParsing "https://www.python.org/ftp/python/$EmbedPythonVersion/python-$EmbedPythonVersion-embed-amd64.zip" -OutFile $embedZip
    if ($EmbedPythonSha256 -notmatch "^__") {
        $actual = (Get-FileHash $embedZip -Algorithm SHA256).Hash.ToLower()
        if ($actual -ne $EmbedPythonSha256.ToLower()) { Fail "同梱ランタイムの SHA-256 が一致しません。" }
        Say "  照合 OK: python embeddable"
    } else {
        Say "  注意: この setup は開発用 (ハッシュ未記録) のため、同梱ランタイムは HTTPS とバージョン固定のみで取得します。"
    }
    $runtimeDirPath = Join-Path $AppDir "runtime"
    Expand-Archive $embedZip -DestinationPath $runtimeDirPath -Force
    # app 本体 (oyayubi パッケージ) と site-packages を import 経路に入れる。
    $pthPath = Get-ChildItem $runtimeDirPath -Filter "python*._pth" | Select-Object -First 1 -ExpandProperty FullName
    @("python" + $EmbedPythonVersion.Substring(0, 4).Replace(".", "") + ".zip", ".", "..", "Lib\site-packages", "import site") |
        Set-Content $pthPath -Encoding ASCII
    $runtimePython = Join-Path $runtimeDirPath "python.exe"
    $getPip = Join-Path $tmp "get-pip.py"
    Invoke-WebRequest -UseBasicParsing "https://bootstrap.pypa.io/get-pip.py" -OutFile $getPip
    Invoke-Step "pip のブートストラップ (公式 get-pip.py)" { & $runtimePython $getPip --no-warn-script-location }
}

Say ""
Invoke-Step "torch CPU 版のインストール (PyTorch 公式インデックス)" {
    & $runtimePython -m pip install --no-warn-script-location --index-url https://download.pytorch.org/whl/cpu "torch==$TorchVersion"
}
Invoke-Step "依存パッケージのインストール (PyPI、バージョン固定)" {
    & $runtimePython -m pip install --no-warn-script-location -r (Join-Path $AppDir "packaging\requirements.txt")
}
Invoke-Step "LM 依存の読み込み確認" {
    & $runtimePython -c "import torch, transformers, sentencepiece, huggingface_hub; print('LM deps OK')"
}

# ---------------------------------------------------------------- 辞書とモデル
Say ""
Invoke-Step "辞書の取得 (google/mozc 上流、約 93MB)" {
    & $runtimePython -m tools.wp0.fetch_mozc_dict
}
Invoke-Step "辞書マニフェストの照合 (SHA-256)" {
    Push-Location $AppDir; & $runtimePython -m oyayubi.ime.manage dict verify; Pop-Location
}
if ($Updating) {
    Remove-Item (Join-Path $AppDir "data\cache") -Recurse -Force -ErrorAction SilentlyContinue
}
Invoke-Step "辞書キャッシュの構築 (初回と辞書更新時のみ数分かかります)" {
    Push-Location $AppDir; & $runtimePython -c "from oyayubi.lattice.mozc_dict import load_lex; load_lex()"; Pop-Location
}
Invoke-Step "LM モデルの取得 (HuggingFace、リビジョン固定)" {
    & $runtimePython (Join-Path $AppDir "packaging\fetch_model.py") (Join-Path $AppDir "model")
}

# config.json にモデルのローカルパスを書く (既存の設定は保持する)。
New-Item -ItemType Directory -Force $RuntimeDir | Out-Null
$configPath = Join-Path $RuntimeDir "config.json"
$config = [pscustomobject]@{}
if (Test-Path $configPath) { $config = Get-Content $configPath -Raw | ConvertFrom-Json }
$modelPath = Join-Path $AppDir "model"
if ($config.PSObject.Properties.Name -contains "lm_model") { $config.lm_model = $modelPath }
else { $config | Add-Member -NotePropertyName "lm_model" -NotePropertyValue $modelPath }
$config | ConvertTo-Json -Depth 4 | Set-Content $configPath -Encoding UTF8

# 学習はファイルがあるときだけ有効になる。新規インストールでは空のファイルを
# 作って最初から学習を有効にする。更新では触れない (利用者が消して無効に
# していれば、そのまま無効)。
$learningPath = Join-Path $AppDir "data\learning.jsonl"
if (-not $Updating -and -not (Test-Path $learningPath)) {
    New-Item -ItemType Directory -Force (Split-Path $learningPath) | Out-Null
    New-Item -ItemType File $learningPath | Out-Null
}

# ---------------------------------------------------------------- 登録とスモーク
Say ""
Say "== IME の登録 (管理者権限が必要です)"
$dllFinal = Join-Path $AppDir "NicolaIME3.dll"
$registerScript = Join-Path $AppDir "src\tip\register_admin.py"
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if ($isAdmin) {
    & $runtimePython $registerScript $dllFinal
    if ($LASTEXITCODE -ne 0) { Fail "TIP の登録に失敗しました。" }
} else {
    $proc = Start-Process $runtimePython -ArgumentList @($registerScript, $dllFinal) -Verb RunAs -Wait -PassThru
    if ($proc.ExitCode -ne 0) { Fail "TIP の登録に失敗しました (終了コード $($proc.ExitCode))。" }
}
Say "  登録 OK"

Say ""
Invoke-Step "スモークテスト (実辞書での変換)" {
    Push-Location $AppDir; & $runtimePython (Join-Path $AppDir "packaging\smoke.py"); Pop-Location
}

# ---------------------------------------------------------------- 記録
$manifest = [ordered]@{
    tag = $Tag
    installed_at = (Get-Date).ToString("o")
    install_root = $InstallRoot
    app_dir = $AppDir
    runtime_python = $runtimePython
    python_route = $(if ($useEmbed) { "embeddable" } else { "venv" })
    runtime_dir = $RuntimeDir
}
$manifest | ConvertTo-Json -Depth 4 | Set-Content $ManifestPath -Encoding UTF8
Remove-Item $tmp -Recurse -Force

Say ""
if ($Updating) { Say "更新が完了しました (タグ: $Tag)。" } else { Say "完了しました。" }
Say "入力切替 (Win+Space) で oyayubi-ime を選んでください。タスクバーに「あ」が出ます。"
Say "管理ウィンドウ: タスクバーの「あ」を右クリック →「管理ウィンドウを開く」"
if (Test-Path $learningPath) { Say "学習: 有効 (無効にするには $learningPath を削除)" }
else { Say "学習: 無効 ($learningPath を作ると有効になります)" }
Say "更新: このインストールと同じコマンドをもう一度実行してください。"
Say "アンインストール: powershell -ExecutionPolicy Bypass -File `"$(Join-Path $AppDir 'packaging\uninstall.ps1')`""
Say "  (全部消えます。学習データを残すときは末尾に -KeepData)"
