# oyayubi-ime: the only part that runs as administrator (REDESIGN §8.1).
# setup.ps1 / uninstall.ps1 pass this script's text with -EncodedCommand (never
# a path the user can rewrite) and read the result file. Three things only:
#   install:   put the DLL, the broker and this script in Program Files\oyayubi-ime\<tag>\
#              (hashes checked where they land), register the DLL, read it back,
#              remove the other versions there (in use: deleted at the next start)
#   uninstall: unregister, read it back, remove Program Files\oyayubi-ime
#   both:      -Leftover <dir> is removed too (in use: at the next start)
# No Python runs here.
param(
    [Parameter(Mandatory = $true)][ValidateSet("install", "uninstall")][string]$Action,
    [Parameter(Mandatory = $true)][string]$ResultFile,
    [string]$Tag = "",
    [string]$DllSource = "",
    [string]$DllSha256 = "",
    [string]$BrokerSource = "",
    [string]$BrokerSha256 = "",
    [string]$ScriptSource = "",
    [string]$ScriptSha256 = "",
    [string]$Leftover = ""
)
$ErrorActionPreference = "Stop"
$Clsid = "{A7C4E201-0B3A-4F11-9E61-0C1A0B7E0A01}"
$Root = Join-Path $env:ProgramFiles "oyayubi-ime"
$InprocKey = "Registry::HKEY_LOCAL_MACHINE\SOFTWARE\Classes\CLSID\$Clsid\InprocServer32"
$pending = New-Object System.Collections.ArrayList

Add-Type -Namespace OyayubiElevated -Name Native -MemberDefinition @'
[DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
public static extern bool MoveFileEx(string existing, string replacement, int flags);
'@

function Write-Result([bool]$ok, [string]$message) {
    $result = [ordered]@{ ok = $ok; message = $message; pending_delete = @($pending) }
    [IO.File]::WriteAllText($ResultFile, ($result | ConvertTo-Json -Depth 3), (New-Object Text.UTF8Encoding($false)))
    if ($ok) { exit 0 } else { exit 1 }
}

function Get-Sha256([string]$path) { (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLower() }

# Removed now, or (a loaded DLL) at the next Windows start: nothing half-deleted
# is left running, and nobody is asked to sign out (§8.3).
function Remove-OrSchedule([string]$path) {
    if (-not (Test-Path -LiteralPath $path)) { return }
    try { Remove-Item -LiteralPath $path -Recurse -Force; return } catch { }
    $items = @(Get-ChildItem -LiteralPath $path -Recurse -Force -ErrorAction SilentlyContinue |
        Sort-Object { $_.FullName.Length } -Descending)
    foreach ($item in $items + @(Get-Item -LiteralPath $path -Force)) {
        if (-not (Test-Path -LiteralPath $item.FullName)) { continue }
        try { Remove-Item -LiteralPath $item.FullName -Force -Recurse -ErrorAction Stop; continue } catch { }
        # MOVEFILE_DELAY_UNTIL_REBOOT
        if ([OyayubiElevated.Native]::MoveFileEx($item.FullName, $null, 4)) { [void]$pending.Add($item.FullName) }
        else { throw "cannot remove or schedule $($item.FullName)" }
    }
}

# Copied next to its place, checked there (only administrators write there),
# then given its name. An identical file already in place (in use) is kept.
function Place([string]$source, [string]$sha256, [string]$dest) {
    if ((Test-Path -LiteralPath $dest) -and (Get-Sha256 $dest) -eq $sha256.ToLower()) { return }
    $tmp = "$dest.new"
    Copy-Item -LiteralPath $source -Destination $tmp -Force
    if ((Get-Sha256 $tmp) -ne $sha256.ToLower()) {
        Remove-Item -LiteralPath $tmp -Force
        throw "SHA-256 mismatch for $(Split-Path $dest -Leaf)"
    }
    Move-Item -LiteralPath $tmp -Destination $dest -Force
}

function Registered() {
    $key = Get-ItemProperty -LiteralPath $InprocKey -ErrorAction SilentlyContinue
    if ($key) { return $key.'(default)' }
    return $null
}

try {
    if ($Action -eq "install") {
        $dir = Join-Path $Root $Tag
        New-Item -ItemType Directory -Force $dir | Out-Null
        $dll = Join-Path $dir "NicolaIME3.dll"
        Place $DllSource $DllSha256 $dll
        Place $BrokerSource $BrokerSha256 (Join-Path $dir "oyayubi-broker.exe")
        # uninstall.ps1 runs this copy: only administrators can change it there.
        Place $ScriptSource $ScriptSha256 (Join-Path $dir "elevated.ps1")
        $proc = Start-Process "$env:SystemRoot\System32\regsvr32.exe" -ArgumentList @("/s", "`"$dll`"") -Wait -PassThru
        if ($proc.ExitCode -ne 0) { Write-Result $false "regsvr32 failed with exit code $($proc.ExitCode)" }
        # I-6: registered is what the registry says, read back.
        if ((Registered) -ne $dll) { Write-Result $false "the registry does not name $dll after registering" }
        foreach ($old in @(Get-ChildItem -LiteralPath $Root -Directory | Where-Object { $_.Name -ne $Tag })) {
            Remove-OrSchedule $old.FullName
        }
    } else {
        $dll = Registered
        if ($dll) {
            $proc = Start-Process "$env:SystemRoot\System32\regsvr32.exe" -ArgumentList @("/u", "/s", "`"$dll`"") -Wait -PassThru
            if ($proc.ExitCode -ne 0 -or (Registered)) {
                Write-Result $false "unregistering $dll failed (regsvr32 exit code $($proc.ExitCode))"
            }
        }
        Remove-OrSchedule $Root
    }
    if ($Leftover) { Remove-OrSchedule $Leftover }
    Write-Result $true "done"
} catch {
    Write-Result $false $_.Exception.Message
}
