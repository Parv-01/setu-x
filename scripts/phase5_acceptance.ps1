<#
.SYNOPSIS
  SETU-X Phase 5 acceptance: experiment runner, open-loop load generator, per-run isolation,
  records, validation and metrics (PowerShell 7+, any OS).

.DESCRIPTION
  Runs against the real Docker Compose stack. Runs the short smoke scenarios only (about 3 minutes
  of load in total); these are harness checks, not experiments. Metrics in summary.json are
  RECOMPUTED here independently from observations.csv (row counts, outcome counts, type-7
  medians) and compared. Writes docs/evidence/phase5_acceptance_<platform>.log.
  Exit code = number of failed checks.

.EXAMPLE
  pwsh -ExecutionPolicy Bypass -File scripts/phase5_acceptance.ps1
#>
param(
    [string]$BuildCommand = "",
    [int]$GatewayPort = 8080,
    [int]$ControllerPort = 8090
)

$ErrorActionPreference = 'Continue'
Set-Location (Join-Path $PSScriptRoot '..')
. (Join-Path $PSScriptRoot 'acceptance/common.ps1')

$Gw = "http://127.0.0.1:$GatewayPort"
$Ctl = "http://127.0.0.1:$ControllerPort"
$Services = 'inventory-a', 'inventory-b', 'pricing', 'controller', 'gateway', 'runner'
New-Item -ItemType Directory -Force -Path 'docs/evidence' | Out-Null
$platform = Write-Header 'SETU-X Phase 5 acceptance (PowerShell)'
$LogPath = "docs/evidence/phase5_acceptance_$platform.log"

function Pctl([double[]]$Values, [double]$Q) {
    # type-7 (linear interpolation) percentile, independent of the Python implementation
    $xs = $Values | Sort-Object
    $h = ($xs.Count - 1) * $Q / 100.0
    $lo = [Math]::Floor($h); $hi = [Math]::Min($lo + 1, $xs.Count - 1)
    return $xs[$lo] + ($h - $lo) * ($xs[$hi] - $xs[$lo])
}

function Run-Experiment([string]$Name) {
    # scripts/run_experiment.ps1 in a child process; returns @{Code; Dir}
    $out = & (Get-Process -Id $PID).Path -NoProfile -File scripts/run_experiment.ps1 -Scenario $Name -ControllerPort $ControllerPort 2>&1 | Out-String
    $code = $LASTEXITCODE
    Log $out.TrimEnd()
    $dir = ($out -split "`n" | Where-Object { $_ -match '^records: (.+)$' } | ForEach-Object { $Matches[1].Trim() } | Select-Object -Last 1)
    return [pscustomobject]@{ Code = $code; Dir = $dir }
}

function Load-Records([string]$Dir) {
    return [pscustomobject]@{
        Manifest = Get-Content (Join-Path $Dir 'manifest.json') -Raw | ConvertFrom-Json -Depth 50 -DateKind String
        Summary  = Get-Content (Join-Path $Dir 'summary.json') -Raw | ConvertFrom-Json -Depth 50 -DateKind String
        Csv      = @(Import-Csv (Join-Path $Dir 'observations.csv'))
        Jsonl    = @(Get-Content (Join-Path $Dir 'observations.jsonl') | Where-Object { $_.Trim() })
        Events   = @(Get-Content (Join-Path $Dir 'events.jsonl') | Where-Object { $_.Trim() } | ForEach-Object { $_ | ConvertFrom-Json -DateKind String })
    }
}

function Wait-Status([string]$Id, [int]$TimeoutSec = 180) {
    [void](Wait-Until { $script:ws = (Invoke-Http 'GET' "$Ctl/api/experiments/$Id").Json; $script:ws.status -in 'completed', 'failed', 'stopped' } $TimeoutSec)
    return $script:ws
}

function Start-Stack([string]$Mode) {
    docker compose down --remove-orphans 2>&1 | Out-Null
    $env:SETU_MODE = $Mode
    $up = (Run 'docker compose up -d --wait --wait-timeout 150') -eq 0
    $bad = @($Services | Where-Object { (Health-Of $_) -ne 'healthy' })
    return [pscustomobject]@{ Ok = ($up -and $bad.Count -eq 0); Bad = $bad }
}

Log '=== D1 docker compose config'
Record 'D1' ((Run 'docker compose config --quiet') -eq 0) 'compose configuration valid'

Log '=== D2 build images'
$buildCmd = if ($BuildCommand) { $BuildCommand } else { 'docker compose --profile test build' }
Record 'D2' ((Run $buildCmd) -eq 0) 'images built'

Log '=== D3 start stack (local mode): 6 services healthy, runner not published to the host'
$st = Start-Stack 'local'
$runnerPort = (docker compose port runner 8000 2>$null | Out-String).Trim()
$published = (docker inspect --format '{{json .NetworkSettings.Ports}}' (Container-Id runner) | Out-String).Trim()
Log "runner published ports: $published"
Log "docker compose port runner 8000: '$runnerPort'"
Record 'D3' ($st.Ok -and ($runnerPort -eq '' -or $runnerPort -eq ':0') -and $published -notmatch 'HostPort') ($(if ($st.Bad) { "unhealthy: $($st.Bad -join ',')" } else { 'all 6 services healthy; runner has no host port' }))

Log '=== D4 automated tests (unit + contract + integration)'
Record 'D4' ((Run 'docker compose --profile test run --rm tests') -eq 0) 'unit + contract + integration tests passed'

Log '=== D5 end-to-end tests (includes a smoke experiment through the controller)'
Record 'D5' ((Run 'docker compose --profile test run --rm tests pytest -p no:cacheprovider -m e2e') -eq 0) 'e2e tests passed'

Log '=== D6 lint'
Record 'D6' ((Run 'docker compose --profile test run --rm tests ruff check --no-cache src tests') -eq 0) 'ruff clean'

Log '=== D7 scenario catalogue valid'
$sc = (Invoke-Http 'GET' "$Ctl/api/scenarios").Json.scenarios
foreach ($s in $sc) { Log ("{0,-22} valid={1} mode={2} conditions={3} runs={4} est={5}s {6}" -f $s.name, $s.valid, $s.requires_mode, ($s.conditions -join ','), $s.runs, $s.estimated_s, $s.error) }
$need = 'A_baseline', 'B_network_delay', 'C_service_failure', 'D_policies_hang', 'D_retries_transient', 'E_scaling', 'F_placement', 'G_hybrid_fallback', 'smoke', 'smoke_simulated'
$missing = @($need | Where-Object { $_ -notin @($sc | Where-Object valid | ForEach-Object name) })
Record 'D7' ($missing.Count -eq 0 -and @($sc | Where-Object { -not $_.valid }).Count -eq 0) ($(if ($missing) { "missing/invalid: $($missing -join ',')" } else { "$($sc.Count) scenario files, all valid" }))

Log '=== D8 experiment API is bounded'
$a = Invoke-Http 'POST' "$Ctl/api/experiments" @{ scenario = 'does-not-exist' }
$b = Invoke-Http 'POST' "$Ctl/api/experiments" @{ scenario = '../configs/placement' }
$c = Invoke-Http 'POST' "$Ctl/api/experiments" @{ scenario = 'smoke_simulated' }
$d = Invoke-Http 'GET' "$Ctl/api/experiments/x/files/..%2F..%2Fetc%2Fpasswd"
foreach ($x in $a, $b, $c, $d) { Log "HTTP $($x.Code): $($x.Json.error.code) $($x.Json.error.message)" }
Record 'D8' ($a.Code -eq 404 -and $b.Code -eq 422 -and $c.Code -eq 409 -and $c.Json.error.code -eq 'preflight_failed' -and $d.Code -eq 404) 'unknown scenario 404; path-like name 422; simulated scenario on a local stack refused (409); file path traversal 404'

Log '=== D9 smoke experiment via scripts/run_experiment.ps1'
$r = Run-Experiment 'smoke'
$ok = $r.Code -eq 0 -and $r.Dir -and (Test-Path (Join-Path $r.Dir 'summary.json'))
$files = if ($r.Dir) { @(Get-ChildItem $r.Dir | ForEach-Object Name) } else { @() }
Record 'D9' ($ok -and $files.Count -ge 5) "exit $($r.Code); files: $($files -join ', ')"

Log '=== D10 independent recomputation from observations.csv'
$rec = Load-Records $r.Dir
$m = $rec.Manifest; $s = $rec.Summary
$scheduled = ($m.runs | Measure-Object scheduled -Sum).Sum
$ids = @($rec.Csv | Select-Object -ExpandProperty request_id -Unique).Count
$problems = @()
if ($rec.Csv.Count -ne $scheduled) { $problems += "csv rows $($rec.Csv.Count) != scheduled $scheduled" }
if ($rec.Jsonl.Count -ne $rec.Csv.Count) { $problems += "jsonl rows $($rec.Jsonl.Count) != csv rows $($rec.Csv.Count)" }
if ($ids -ne $rec.Csv.Count) { $problems += "request_id not unique ($ids distinct)" }
foreach ($run in $s.per_run) {
    $rows = @($rec.Csv | Where-Object { $_.run_id -eq $run.run_id -and $_.phase -eq 'measure' })
    $okRows = @($rows | Where-Object { $_.outcome -in 'confirmed', 'rejected' })
    $p50 = if ($okRows.Count) { Pctl ([double[]]($okRows | ForEach-Object { [double]$_.latency_ms })) 50 } else { $null }
    $succ = [Math]::Round($okRows.Count / [Math]::Max(1, $rows.Count), 6)
    Log ("{0,-16} measure_rows csv={1} summary={2}  ok csv={3} summary={4}  p50 csv={5:N3} summary={6}  success csv={7} summary={8}" -f `
        $run.run_id, $rows.Count, $run.measure_rows, $okRows.Count, $run.ok, $p50, $run.latency_ok_ms.p50, $succ, $run.success_rate)
    if ($rows.Count -ne $run.measure_rows -or $okRows.Count -ne $run.ok) { $problems += "$($run.run_id): counts differ" }
    if ([Math]::Abs($p50 - [double]$run.latency_ok_ms.p50) -gt 0.0015) { $problems += "$($run.run_id): p50 differs" }
    if ([Math]::Abs($succ - [double]$run.success_rate) -gt 1e-6) { $problems += "$($run.run_id): success rate differs" }
    if ($run.latency_ok_ms.p95 -ne $null -or $run.latency_ok_ms.p99 -ne $null) { $problems += "$($run.run_id): p95/p99 reported with n < 200" }
}
foreach ($p in $problems) { Log "problem: $p" }
Record 'D10' ($problems.Count -eq 0 -and $s.validation.ok) "rows=$($rec.Csv.Count) scheduled=$scheduled; per-run counts, success rates and type-7 medians match summary.json; p95/p99 withheld (n<200); runner validation ok=$($s.validation.ok)"

Log '=== D11 per-run isolation and clean platform afterwards'
$isoProblems = @()
foreach ($run in $m.runs) {
    Log ("{0,-16} placement v{1} sha {2}  gateway_reset v{3} seed={4}  order={5}" -f $run.run_id, $run.placement_version, $run.placement_sha256.Substring(0, 12), $run.gateway_reset.placement_version, $run.gateway_reset.seed, $run.seed)
    if ($run.gateway_reset.placement_version -ne $run.placement_version -or $run.gateway_reset.placement_sha256 -ne $run.placement_sha256) { $isoProblems += "$($run.run_id): gateway not on the run's placement" }
    $versions = @($rec.Csv | Where-Object { $_.run_id -eq $run.run_id -and $_.placement_version } | Select-Object -ExpandProperty placement_version -Unique)
    if ($versions.Count -ne 1 -or [int]$versions[0] -ne [int]$run.placement_version) { $isoProblems += "$($run.run_id): responses carried versions $($versions -join ',')" }
}
$active = @((Invoke-Http 'GET' "$Ctl/api/faults").Json.faults | Where-Object { $_.active }).Count
$src = (Invoke-Http 'GET' "$Ctl/api/placement").Json.source
$seeds = @($m.runs | Select-Object -ExpandProperty seed -Unique).Count
foreach ($p in $isoProblems) { Log "problem: $p" }
Record 'D11' ($isoProblems.Count -eq 0 -and $active -eq 0 -and $src -eq 'file' -and $seeds -eq $m.runs.Count) "every run synced the gateway to its own placement version before load; distinct seeds per run; afterwards placement source=$src, active faults=$active"

Log '=== D12 scheduled fault confined to its window; recovery measured'
$fProblems = @()
foreach ($run in @($m.runs | Where-Object condition -eq 'error-fault')) {
    $f = $run.faults[0]
    $t0 = [double]$f.actual_measure_offset_s; $t1 = $t0 + [double]$f.spec.duration_s
    $failed = @($rec.Csv | Where-Object { $_.run_id -eq $run.run_id -and $_.outcome -eq 'unavailable' } | ForEach-Object { [double]$_.measure_offset_s })
    $outside = @($failed | Where-Object { $_ -lt $t0 - 0.15 -or $_ -gt $t1 + 0.15 })
    $rcv = ($s.per_run | Where-Object run_id -eq $run.run_id).recovery[0]
    Log ("{0}: fault injected at {1:N3} s (scheduled {2}, late {3} ms) for {4} s; unavailable={5} (outside window: {6}); impacted={7} recovery_s={8}" -f `
        $run.run_id, $t0, $f.at_s, $f.lateness_ms, $f.spec.duration_s, $failed.Count, $outside.Count, $rcv.impacted, $rcv.recovery_s)
    if (-not $f.ok -or $outside.Count -gt 0 -or $failed.Count -lt 15 -or $failed.Count -gt 25 -or -not $rcv.impacted -or $null -eq $rcv.recovery_s) { $fProblems += $run.run_id }
}
foreach ($run in @($s.per_run | Where-Object condition -eq 'healthy')) { if ($run.success_rate -ne 1) { $fProblems += "$($run.run_id) not 100%" } }
$injEv = @($rec.Events | Where-Object { $_.source -eq 'controller' -and $_.type -eq 'fault_injected' }).Count
Record 'D12' ($fProblems.Count -eq 0 -and $injEv -eq 2) "failures only inside the actual fault window (+/-0.15 s), ~20 per fault run, healthy runs 100%; controller fault_injected events in events.jsonl: $injEv"

Log '=== D13 one experiment at a time; stop keeps completed runs'
$first = Invoke-Http 'POST' "$Ctl/api/experiments" @{ scenario = 'smoke'; label = 'acceptance stop test' }
$second = Invoke-Http 'POST' "$Ctl/api/experiments" @{ scenario = 'smoke' }
$id = $first.Json.experiment_id
[void](Wait-Until { [int](Invoke-Http 'GET' "$Ctl/api/experiments/$id").Json.progress.run -ge 2 } 60)
$stop = Invoke-Http 'POST' "$Ctl/api/experiments/$id/stop"
$final = Wait-Status $id 30
$sum = (Invoke-Http 'GET' "$Ctl/api/experiments/$id/files/summary.json").Json
Log "first HTTP $($first.Code); second HTTP $($second.Code) $($second.Json.error.code); stop HTTP $($stop.Code) -> $($final.status); runs completed $($sum.runs_completed)/$($sum.runs_planned); validation ok=$($sum.validation.ok)"
Record 'D13' ($first.Code -eq 202 -and $second.Code -eq 409 -and $final.status -eq 'stopped' -and $sum.runs_completed -ge 1 -and $sum.runs_completed -lt 4 -and $sum.validation.ok) 'second start refused while running; stopped experiment keeps and validates its completed runs'

Log '=== D14 simulated mode: injected 100 ms delay is measured'
$st = Start-Stack 'simulated'
$mode = (Invoke-Http 'GET' "$Ctl/api/environments").Json.mode
$r2 = Run-Experiment 'smoke_simulated'
$ok14 = $false
if ($r2.Code -eq 0 -and $st.Ok -and $mode -eq 'simulated') {
    $rec2 = Load-Records $r2.Dir
    $med = @{}
    foreach ($cond in 'd0', 'd100') {
        $perRun = @()
        foreach ($run in @($rec2.Manifest.runs | Where-Object condition -eq $cond)) {
            $lat = [double[]](@($rec2.Csv | Where-Object { $_.run_id -eq $run.run_id -and $_.phase -eq 'measure' -and $_.outcome -eq 'confirmed' }) | ForEach-Object { [double]$_.latency_ms })
            $perRun += (Pctl $lat 50)
        }
        $med[$cond] = Pctl ([double[]]$perRun) 50
        Log ("{0}: per-run medians (csv) {1}" -f $cond, (($perRun | ForEach-Object { '{0:N3}' -f $_ }) -join ', '))
    }
    $diff = $med['d100'] - $med['d0']
    $inj = @($rec2.Csv | Where-Object { $_.condition -eq 'd100' -and $_.phase -eq 'measure' } | Select-Object -ExpandProperty injected_delay_ms -Unique)
    $cmp = $rec2.Summary.comparisons.results | Where-Object metric -eq 'median_latency_ok_ms'
    Log ("difference of medians (csv) = {0:N3} ms; summary.json = {1} ms, CI95 {2}; injected_delay_ms values for d100: {3}" -f $diff, $cmp.difference_of_medians, ($cmp.ci95 -join '..'), ($inj -join ','))
    $ok14 = ($diff -ge 85 -and $diff -le 125 -and $inj.Count -eq 1 -and [double]$inj[0] -eq 100 -and [Math]::Abs($diff - [double]$cmp.difference_of_medians) -lt 0.01)
}
Record 'D14' $ok14 "stack mode=$mode; median(d100) - median(d0) within 100 +/- 25 ms and equal to summary.json"

Log '=== D15 hardening of all 6 services, graceful stop, clean down (results volume kept)'
$bad = @()
foreach ($s in $Services) {
    $i = (docker inspect (Container-Id $s) | ConvertFrom-Json -DateKind String)[0]; $hc = $i.HostConfig
    if (-not ($i.Config.User -eq '10001' -and $hc.ReadonlyRootfs -and ($hc.CapDrop -contains 'ALL') -and -not $hc.Privileged -and `
              ($hc.SecurityOpt -contains 'no-new-privileges:true'))) { $bad += $s }
}
Run 'docker compose stop' | Out-Null
$codes = @{}; foreach ($s in $Services) { $codes[$s] = [int](docker inspect --format '{{.State.ExitCode}}' (Container-Id $s -All)) }
Log ("exit codes: " + (($codes.GetEnumerator() | Sort-Object Name | ForEach-Object { "$($_.Name)=$($_.Value)" }) -join ' '))
Run 'docker compose down' | Out-Null
$left = @(docker compose ps -a -q | Where-Object { $_ }).Count
$vol = (docker volume ls -q --filter name=setu-results | Out-String).Trim()
Record 'D15' ($bad.Count -eq 0 -and @($codes.Values | Where-Object { $_ -ne 0 }).Count -eq 0 -and $left -eq 0 -and $vol) "hardening intact; all 6 exited 0; $left containers left; results volume: $vol"

Remove-Item Env:SETU_MODE -ErrorAction SilentlyContinue
Write-Summary 'PHASE 5' $LogPath
exit $script:Fails
