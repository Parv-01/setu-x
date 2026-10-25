<#
.SYNOPSIS
  Phase 8 measurement campaign: runs Experiments A-G (all scenario files) in the right modes and
  records the machine conditions (PowerShell 7+).

.DESCRIPTION
  1. Checks that src/, configs/, compose and Dockerfile are committed (results must trace to a commit).
  2. Records the machine and Docker resources in results/campaign_<UTC>.json.
  3. Local mode:     A_baseline, C_service_failure, D_policies_hang, D_retries_transient, E_scaling
  4. Simulated mode: B_network_delay, F_placement, G_hybrid_fallback
  5. Restores local mode and prints a table of experiment IDs, status and validation.

  Each scenario runs through scripts/run_experiment.ps1 with -DockerStats; records land in
  results/<experiment_id>/. A failed scenario is reported and the campaign continues.
  On Windows the script asks the OS not to sleep while it runs (released when it ends).
  Full campaign: about 70 minutes. -Quick runs 1 run per condition (a pilot, NOT for findings).

.EXAMPLE
  pwsh -ExecutionPolicy Bypass -File scripts/run_all_experiments.ps1
  pwsh -ExecutionPolicy Bypass -File scripts/run_all_experiments.ps1 -Only F_placement,G_hybrid_fallback
#>
param(
    [string[]]$Only = @(),
    [switch]$Quick,
    [switch]$NoBuild,
    [int]$ControllerPort = 8090
)

$ErrorActionPreference = 'Continue'
Set-Location (Join-Path $PSScriptRoot '..')
$Ctl = "http://127.0.0.1:$ControllerPort"
$Local = 'A_baseline', 'C_service_failure', 'D_policies_hang', 'D_retries_transient', 'E_scaling'
$Simulated = 'B_network_delay', 'F_placement', 'G_hybrid_fallback'
if ($Only.Count) { $Local = @($Local | Where-Object { $_ -in $Only }); $Simulated = @($Simulated | Where-Object { $_ -in $Only }) }
$stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
$label = if ($Quick) { "pilot $stamp (1 run per condition, not for findings)" } else { "phase8 campaign $stamp" }
New-Item -ItemType Directory -Force -Path results | Out-Null

function Say([string]$t) { Write-Host ("[{0}] {1}" -f (Get-Date).ToString('HH:mm:ss'), $t) }

# ---- keep the machine awake (Windows only; released automatically when the process exits)
if ($IsWindows) {
    Add-Type -Namespace SetuX -Name Power -MemberDefinition '[DllImport("kernel32.dll")] public static extern uint SetThreadExecutionState(uint esFlags);'
    [void][SetuX.Power]::SetThreadExecutionState([uint32]'0x80000001')   # ES_CONTINUOUS | ES_SYSTEM_REQUIRED
    Say 'asked Windows not to sleep while the campaign runs'
}

# ---- 1. committed code
$dirty = @(git status --porcelain -- src configs compose.yaml Dockerfile dashboard requirements 2>$null)
$commit = (git rev-parse --short HEAD 2>$null)
if ($dirty.Count) {
    Say "WARNING: uncommitted changes in src/configs/compose/Dockerfile; results will be marked $commit-dirty:"
    $dirty | ForEach-Object { Say "  $_" }
}

# ---- 2. machine conditions
$info = docker info --format '{{json .}}' | ConvertFrom-Json -DateKind String
$machine = [ordered]@{
    campaign = $stamp; label = $label; git_commit = $commit; uncommitted = $dirty
    os = [System.Runtime.InteropServices.RuntimeInformation]::OSDescription
    logical_cpus_host = [Environment]::ProcessorCount
    docker_server = $info.ServerVersion; docker_os = $info.OperatingSystem
    docker_cpus = $info.NCPU; docker_mem_gib = [Math]::Round($info.MemTotal / 1GB, 2)
    compose = (docker compose version --short); powershell = $PSVersionTable.PSVersion.ToString()
    scenarios_local = $Local; scenarios_simulated = $Simulated; quick = [bool]$Quick; experiments = @()
}
if ($IsWindows) {
    try {
        $cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
        $machine.cpu_model = $cpu.Name
        $bat = Get-CimInstance Win32_Battery -ErrorAction SilentlyContinue
        $machine.on_battery = if ($bat) { $bat.BatteryStatus -eq 1 } else { $false }
        $machine.power_plan = ((powercfg /getactivescheme) -join ' ').Trim()
    } catch { }
}
$campaignFile = "results/campaign_$stamp.json"
function Save { $machine | ConvertTo-Json -Depth 6 | Set-Content -Path $campaignFile -Encoding utf8 }
Save
Say "machine: $($machine.cpu_model) | Docker: $($machine.docker_cpus) CPUs, $($machine.docker_mem_gib) GiB | on battery: $($machine.on_battery)"
if ($machine.on_battery) { Say 'WARNING: running on battery; plug in the charger for comparable results' }

function Start-Mode([string]$Mode) {
    Say "starting the stack in $Mode mode"
    $env:SETU_MODE = $Mode
    $cmd = if ($NoBuild) { 'docker compose up -d --wait --wait-timeout 180' } else { 'docker compose up -d --build --wait --wait-timeout 180' }
    Invoke-Expression "$cmd 2>&1" | Out-Null
    $mode = (Invoke-RestMethod -NoProxy "$Ctl/api/environments").mode
    if ($mode -ne $Mode) { throw "stack reports mode '$mode', expected '$Mode'" }
    Start-Sleep -Seconds 5   # let the services settle before measuring
}

function Run-One([string]$Name, [string]$Mode) {
    Say "=== $Name ($Mode)"
    $args2 = @('-NoProfile', '-File', 'scripts/run_experiment.ps1', '-Scenario', $Name, '-ControllerPort', "$ControllerPort",
               '-DockerStats', '-Label', $label)
    if ($Quick) { $args2 += @('-Runs', '1') }
    $out = & (Get-Process -Id $PID).Path @args2 2>&1 | Tee-Object -Variable lines | Out-String
    $code = $LASTEXITCODE
    $dir = ($out -split "`n" | Where-Object { $_ -match '^records: (.+)$' } | ForEach-Object { $Matches[1].Trim() } | Select-Object -Last 1)
    $entry = [ordered]@{ scenario = $Name; mode = $Mode; exit_code = $code; dir = $dir }
    if ($dir -and (Test-Path "$dir/summary.json")) {
        $s = Get-Content "$dir/summary.json" -Raw | ConvertFrom-Json -DateKind String
        $entry.experiment_id = $s.experiment_id; $entry.status = $s.status; $entry.validation_ok = $s.validation.ok
        $entry.warnings = @($s.warnings).Count
    }
    $machine.experiments += [pscustomobject]$entry
    Save
    Say ("{0}: exit {1} status {2} validation {3}" -f $Name, $code, $entry.status, $entry.validation_ok)
}

$t0 = Get-Date
try {
    if ($Local.Count) { Start-Mode 'local'; foreach ($s in $Local) { Run-One $s 'local' } }
    if ($Simulated.Count) { Start-Mode 'simulated'; foreach ($s in $Simulated) { Run-One $s 'simulated' } }
} catch {
    Say "STOPPED: $($_.Exception.Message)"
    $machine.error = $_.Exception.Message
} finally {
    Remove-Item Env:SETU_MODE -ErrorAction SilentlyContinue
    Say 'restoring local mode'
    docker compose up -d --wait --wait-timeout 180 2>&1 | Out-Null
    $machine.finished_utc = (Get-Date).ToUniversalTime().ToString('o')
    $machine.duration_min = [Math]::Round(((Get-Date) - $t0).TotalMinutes, 1)
    Save
    if ($IsWindows) { [void][SetuX.Power]::SetThreadExecutionState([uint32]'0x80000000') }
}

Write-Host ''
Write-Host "================ CAMPAIGN $stamp ($($machine.duration_min) min) ================"
$machine.experiments | ForEach-Object { Write-Host ("{0,-22} {1,-10} {2,-34} {3,-10} validation={4} warnings={5}" -f $_.scenario, $_.mode, $_.experiment_id, $_.status, $_.validation_ok, $_.warnings) }
$bad = @($machine.experiments | Where-Object { $_.status -ne 'completed' -or -not $_.validation_ok })
Write-Host "campaign record: $campaignFile"
Write-Host $(if ($bad.Count) { "$($bad.Count) scenario(s) did not complete with valid records" } else { 'all scenarios completed with validated records' })
exit $bad.Count
