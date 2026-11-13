# Assemble the release package for a PC with no Python and no build tools
# (see packaging/README.txt). Run on the development PC.
#
# Requires in the repo:
#   dist\NicolaIME3.dll       (from src\tip\dev-build.ps1)
#   data\cache\mozc_lex_v3.pkl (written by any server run)
# Output: packaging\dist\oyayubi-ime.zip
param([string]$PythonVersion = '3.13.7')
$ErrorActionPreference = 'Stop'

$repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$work = Join-Path $PSScriptRoot 'dist'
$pkg  = Join-Path $work 'oyayubi-ime'

$dll = Join-Path $repo 'dist\NicolaIME3.dll'
if (-not (Test-Path -LiteralPath $dll -PathType Leaf)) {
    throw 'dist\NicolaIME3.dll がありません。先に src\tip\dev-build.ps1 を実行してください。'
}
$pickle = Join-Path $repo 'data\cache\mozc_lex_v3.pkl'
if (-not (Test-Path -LiteralPath $pickle -PathType Leaf)) {
    throw 'data\cache\mozc_lex_v3.pkl がありません。サーバを一度起動すると作られます。'
}

New-Item -ItemType Directory -Path $work -Force | Out-Null

# Official embeddable Python: a zip, no installation, no pip. The server
# needs only the standard library (the LM stays off in the package).
$embedZip = Join-Path $work "python-$PythonVersion-embed-amd64.zip"
if (-not (Test-Path -LiteralPath $embedZip -PathType Leaf)) {
    $url = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip"
    Write-Host "Downloading $url"
    Invoke-WebRequest -Uri $url -OutFile $embedZip
}

if (Test-Path -LiteralPath $pkg) { Remove-Item -LiteralPath $pkg -Recurse -Force }
New-Item -ItemType Directory -Path $pkg | Out-Null

Copy-Item -LiteralPath $dll -Destination (Join-Path $pkg 'NicolaIME3.dll')

# The IME's Python source (runtime only: no tools, no tests).
& robocopy (Join-Path $repo 'oyayubi') (Join-Path $pkg 'oyayubi') /E /XD __pycache__ | Out-Null
if ($LASTEXITCODE -gt 7) { throw 'oyayubi のコピーに失敗しました。' }

# Runtime data: the prebuilt dictionary cache and the small data files.
New-Item -ItemType Directory -Path (Join-Path $pkg 'data\cache') -Force | Out-Null
Copy-Item -LiteralPath $pickle -Destination (Join-Path $pkg 'data\cache\mozc_lex_v3.pkl')
foreach ($f in 'nicola_a.json', 'user_lexicon.jsonl', 'dictionary-manifest.json') {
    Copy-Item -LiteralPath (Join-Path $repo "data\$f") -Destination (Join-Path $pkg "data\$f")
}

# The Mozc dictionary sources travel too: bunsetsu.py reads id.def (POS
# names) at runtime, the manifest check compares the whole set, and with
# the sources present the cache can be rebuilt on the target machine.
& robocopy (Join-Path $repo 'third_party') (Join-Path $pkg 'third_party') /E /XD __pycache__ | Out-Null
if ($LASTEXITCODE -gt 7) { throw 'third_party のコピーに失敗しました。' }

# Bundled runtime. Adding ".." to the ._pth puts the package root on
# sys.path, so `python -m oyayubi.ime.server` works from any cwd.
$runtime = Join-Path $pkg 'runtime'
Expand-Archive -LiteralPath $embedZip -DestinationPath $runtime
$tag = 'python' + ($PythonVersion -replace '^(\d+)\.(\d+).*', '$1$2')
$pth = Join-Path $runtime "$tag._pth"
if (-not (Test-Path -LiteralPath $pth -PathType Leaf)) { throw "$pth が見つかりません。" }
Add-Content -LiteralPath $pth -Value "`r`n.." -Encoding Ascii

# Registration helpers (they take the DLL path as an argument) and the
# install scripts themselves.
foreach ($f in 'register_admin.py', 'unregister_admin.py') {
    Copy-Item -LiteralPath (Join-Path $repo "src\tip\$f") -Destination (Join-Path $pkg $f)
}
foreach ($f in 'install.ps1', 'uninstall.ps1', 'README.txt') {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot $f) -Destination (Join-Path $pkg $f)
}

$zip = Join-Path $work 'oyayubi-ime.zip'
if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip -Force }
Compress-Archive -Path $pkg -DestinationPath $zip
$size = [Math]::Round((Get-Item -LiteralPath $zip).Length / 1MB, 1)
Write-Host "Package: $zip ($size MB)"
Write-Host '対象 PC では zip を展開して install.ps1 を実行してください。'
