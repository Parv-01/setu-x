<#
.SYNOPSIS
  Run one SETU-X experiment scenario and download its records into results/<experiment_id>/.

.DESCRIPTION
  The load generator runs INSIDE the Compose network (the `runner` container), so latency is
  measured in the same clock domain as the services. This script only starts the experiment
  through the controller (127.0.0.1:8090), shows progress, and downloads the files:
    manifest.json, observations.jsonl, observations.csv, events.jsonl, summary.json
  With -DockerStats it also samples `docker stats` about once per second while the experiment
  runs and writes results/<id>/docker_stats.csv. Those samples are taken on the HOST (host clock,
  ~1-2 s resolution) and are labelled as such; they are context, not per-request measurements.

  The stack must already run in the mode the scenario requires (see `requires_mode` in the file):
    local:      docker compose up -d
    simulated:  $env:SETU_MODE='simulated'; docker compose up -d

.EXAMPLE
  pwsh -File scripts/run_experiment.ps1 -Scenario smoke
  pwsh -File scripts/run_experiment.ps1 -Scenario E_scaling -DockerStats -Label "laptop, on mains power"
  pwsh -File scripts/run_experiment.ps1 -Scenario A_baseline -Runs 2 -DurationS 20   # shortened (recorded as overrides)
#>
param(
    [Parameter(Mandatory = $true)][ValidatePattern('^[A-Za-z0-9][A-Za-z0-9_.-]{0,48}$')][string]$Scenario,
    [string]$Label = '',
    [int]$ControllerPort = 8090,
    [string]$OutDir = 'results',
    [switch]$DockerStats,
    # Optional bounded overrides (same rules as the dashboard; validated by the runner, recorded in the manifest)
    [double]$RateRps = 0,
    [double]$WarmupS = -1,
    [double]$DurationS = 0,
    [int]$Runs = 0
)

$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')
$Ctl = "http://127.0.0.1:$ControllerPort"
$Files = 'manifest.json', 'observations.jsonl', 'observations.csv', 'events.jsonl', 'summary.json'

function Api([string]$Method, [string]$Path, $Body = $null) {
    $p = @{ Method = $Method; Uri = "$Ctl$Path"; SkipHttpErrorCheck = $true; TimeoutSec = 30; NoProxy = $true }
    if ($null -ne $Body) { $p.ContentType = 'application/json'; $p.Body = ($Body | ConvertTo-Json -Compress -Depth 10) }
    $r = Invoke-WebRequest @p
    $json = $null; try { $json = $r.Content | ConvertFrom-Json -Depth 50 -DateKind String } catch { }
    return [pscustomobject]@{ Code = [int]$r.StatusCode; Json = $json; Body = [string]$r.Content }
}

$commit = (git rev-parse --short HEAD 2>$null)
if ($commit) {
    $dirty = @(git status --porcelain -- src configs compose.yaml Dockerfile 2>$null).Count
    if ($dirty -gt 0) { $commit = "$commit-dirty"; Write-Warning "uncommitted changes in src/configs/compose: the manifest will say $commit" }
}
$body = @{ scenario = $Scenario; label = $Label }
$overrides = @{}
if ($RateRps -gt 0) { $overrides.rate_rps = $RateRps }
if ($WarmupS -ge 0) { $overrides.warmup_s = $WarmupS }
if ($DurationS -gt 0) { $overrides.duration_s = $DurationS }
if ($Runs -gt 0) { $overrides.runs = $Runs }
if ($overrides.Count) { $body.overrides = $overrides; Write-Host "overrides: $($overrides | ConvertTo-Json -Compress)" }
if ($commit) { $body.git_commit = $commit }

$start = Api 'POST' '/api/experiments' $body
if ($start.Code -ne 202) {
    $msg = if ($start.Json.error.message) { $start.Json.error.message } else { $start.Body.Substring(0, [Math]::Min(300, $start.Body.Length)) }
    Write-Host "could not start: HTTP $($start.Code) $msg" -ForegroundColor Red
    exit 2
}
$id = $start.Json.experiment_id
Write-Host "experiment $id started ($($start.Json.progress.total_runs) runs)"

$statsJob = $null
$dest = Join-Path $OutDir $id
New-Item -ItemType Directory -Force -Path $dest | Out-Null
if ($DockerStats) {
    $statsPath = (Resolve-Path $dest).Path
    $statsJob = Start-Job -ArgumentList $statsPath -ScriptBlock {
        param($dir)
        $out = Join-Path $dir 'docker_stats.csv'
        'host_time_utc,container,cpu_percent,mem_usage,mem_percent' | Set-Content -Path $out -Encoding utf8
        while ($true) {
            $t = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ss.fffZ')
            $lines = docker stats --no-stream --format '{{.Name}},{{.CPUPerc}},{{.MemUsage}},{{.MemPerc}}' 2>$null
            foreach ($l in $lines) { if ($l -like 'setu-x-*') { "$t,$l" | Add-Content -Path $out -Encoding utf8 } }
            Start-Sleep -Milliseconds 500
        }
    }
}

$last = ''
$status = $null
try {
    while ($true) {
        Start-Sleep -Seconds 1
        $s = Api 'GET' "/api/experiments/$id"
        if ($s.Code -ne 200) { Write-Host "status check failed: HTTP $($s.Code)"; continue }
        $status = $s.Json
        $p = $status.progress
        $line = "[{0}] run {1}/{2} {3} {4} {5}/{6}" -f $status.status, $p.run, $p.total_runs, $p.condition, $p.phase, $p.completed_requests, $p.scheduled_requests
        if ($line -ne $last) { Write-Host $line; $last = $line }
        if ($status.status -in 'completed', 'failed', 'stopped') { break }
    }
} finally {
    if ($statsJob) { Stop-Job $statsJob; Remove-Job $statsJob -Force }
    if ($status -and $status.status -notin 'completed', 'failed', 'stopped') {
        Write-Host 'interrupted: asking the runner to stop (completed runs are kept)'
        [void](Api 'POST' "/api/experiments/$id/stop")
    }
}

foreach ($f in $Files) {
    $r = Invoke-WebRequest -Uri "$Ctl/api/experiments/$id/files/$f" -OutFile (Join-Path $dest $f) -NoProxy -SkipHttpErrorCheck -PassThru
    if ([int]$r.StatusCode -ne 200) { Write-Warning "$f not downloaded (HTTP $($r.StatusCode))" }
}
$summary = Get-Content (Join-Path $dest 'summary.json') -Raw | ConvertFrom-Json -Depth 50 -DateKind String
Write-Host ''
Write-Host "status: $($status.status)   validation: $(if ($summary.validation.ok) { 'ok' } else { 'PROBLEMS' })   rows: $($summary.validation.rows_jsonl)"
foreach ($pr in $summary.validation.problems) { Write-Host "  problem: $pr" -ForegroundColor Yellow }
foreach ($w in $summary.warnings) { Write-Host "  warning: $w" -ForegroundColor Yellow }
if ($status.error) { Write-Host "  error: $($status.error)" -ForegroundColor Red }
foreach ($c in $summary.conditions.PSObject.Properties) {
    $rl = $c.Value.run_level
    Write-Host ("  {0,-24} runs={1} median(per-run p50 latency_ok)={2} ms  median(success)={3}" -f $c.Name, $c.Value.runs, $rl.median_latency_ok_ms.median, $rl.success_rate.median)
}
Write-Host "records: $dest"
exit $(if ($status.status -eq 'completed' -and $summary.validation.ok) { 0 } else { 1 })
