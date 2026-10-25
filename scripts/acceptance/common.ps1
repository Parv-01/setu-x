# Shared helpers for SETU-X acceptance scripts (PowerShell 7+, Windows/Linux/macOS).
# Dot-source from a phase script:  . (Join-Path $PSScriptRoot 'acceptance/common.ps1')

$script:Lines = [System.Collections.Generic.List[string]]::new()
$script:Results = [System.Collections.Generic.List[string]]::new()
$script:Fails = 0

function Log([string]$Text) { Write-Host $Text; $script:Lines.Add($Text) }

function Run([string]$Cmd) {
    # Runs a command line, logs its combined output, returns the exit code.
    Log "`$ $Cmd"
    $out = Invoke-Expression "$Cmd 2>&1" | Out-String
    $code = $LASTEXITCODE
    if ($out.Trim()) { Log $out.TrimEnd() }
    return $code
}

function Record([string]$Id, [bool]$Ok, [string]$Detail) {
    $status = if ($Ok) { 'PASS' } else { 'FAIL' }
    if (-not $Ok) { $script:Fails++ }
    $script:Results.Add(('{0,-4} {1,-4} {2}' -f $Id, $status, $Detail))
    Log ">>> $Id $status - $Detail"
}

function Invoke-Http([string]$Method, [string]$Url, $Body = $null) {
    # Returns {Code, Body(string), Json, Seconds}; never throws on HTTP errors.
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $p = @{ Method = $Method; Uri = $Url; SkipHttpErrorCheck = $true; TimeoutSec = 15; NoProxy = $true }
        if ($null -ne $Body) {
            $p.ContentType = 'application/json'
            $p.Body = if ($Body -is [string]) { $Body } else { $Body | ConvertTo-Json -Depth 20 -Compress }
        }
        $r = Invoke-WebRequest @p
        $sw.Stop()
        $json = $null; try { $json = $r.Content | ConvertFrom-Json -Depth 50 -DateKind String } catch { }
        return [pscustomobject]@{ Code = [int]$r.StatusCode; Body = [string]$r.Content; Json = $json; Seconds = $sw.Elapsed.TotalSeconds }
    } catch {
        $sw.Stop()
        return [pscustomobject]@{ Code = 0; Body = $_.Exception.Message; Json = $null; Seconds = $sw.Elapsed.TotalSeconds }
    }
}

function Container-Id([string]$Service, [switch]$All) {
    $a = if ($All) { '-a' } else { '' }
    return (Invoke-Expression "docker compose ps $a -q $Service" | Select-Object -First 1)
}

function Health-Of([string]$Service) {
    $cid = Container-Id $Service
    if (-not $cid) { return 'missing' }
    return (docker inspect --format '{{.State.Health.Status}}' $cid)
}

function Wait-Until([scriptblock]$Condition, [int]$TimeoutSec = 30) {
    $end = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $end) { if (& $Condition) { return $true }; Start-Sleep -Milliseconds 250 }
    return $false
}

function Wait-Healthy([string]$Service, [int]$TimeoutSec = 90) {
    return (Wait-Until { (Health-Of $Service) -eq 'healthy' } $TimeoutSec)
}

function Fmt([double]$s) { return ('{0:N3}' -f $s) }

function Write-Header([string]$Title) {
    Log $Title
    Log ("date_utc: " + (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'))
    $commit = (git rev-parse --short HEAD 2>$null); if (-not $commit) { $commit = 'unknown' }
    $dirty = @(git status --porcelain -- . ':!docs/evidence' 2>$null).Count
    Log "git_commit: $commit (uncommitted files outside docs/evidence: $dirty)"
    $platform = if ($IsWindows) { 'windows' } elseif ($IsMacOS) { 'macos' } else { 'linux' }
    Log ("powershell: " + $PSVersionTable.PSVersion.ToString() + " on $platform")
    Log ("docker: " + (docker version --format 'client {{.Client.Version}} / server {{.Server.Version}}' 2>&1 | Out-String).Trim())
    Log ("compose: " + (docker compose version --short 2>&1 | Out-String).Trim())
    Log ''
    return $platform
}

function Write-Summary([string]$Phase, [string]$LogPath) {
    Log ''
    Log '================ SUMMARY ================'
    foreach ($r in $script:Results) { Log $r }
    Log "checks: $($script:Results.Count)  failed: $script:Fails"
    Log ($(if ($script:Fails -eq 0) { "$Phase ACCEPTANCE: PASS" } else { "$Phase ACCEPTANCE: FAIL" }))
    $script:Lines | Set-Content -Path $LogPath -Encoding utf8
    Write-Host "log written to $LogPath"
}
