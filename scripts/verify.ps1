<#
.SYNOPSIS
  SETU-X Phase 7: end-to-end validation from a CLEAN CHECKOUT (PowerShell 7+, any OS).

.DESCRIPTION
  Clones the committed HEAD of this repository into a temporary folder and validates it there, as
  a separate Compose project ("setu-x-verify", ports 18080/18090, its own results volume), so it
  neither uses uncommitted files nor touches your normal stack or results:

    V1  source state and clean clone           V11 baseline experiment (A_baseline, shortened)
    V2  secret / credential / artifact scan     V12 failure experiment (C_service_failure, shortened)
    V3  compose configuration                   V13 artifacts: records <-> config <-> logs traceability
    V4  build images                            V14 configuration changes at runtime
    V5  config validator (good + bad files)     V15 restart behaviour and persistence
    V6  unit + contract + integration tests     V16 container-level fault (fault_container.ps1)
    V7  lint                                    V17 every README command is exercised or evidenced
    V8  clean start, README quick-start         V18 clean shutdown and removal
    V9  end-to-end tests
    V10 dashboard in a real browser (Playwright; -SkipBrowser records SKIP, verdict INCOMPLETE)

  Experiments are shortened with recorded overrides; they validate the pipeline, they are not the
  Phase 8 experiments. Writes docs/evidence/verify_<platform>.log in the SOURCE repository.
  Exit code = failed + skipped checks.

.EXAMPLE
  pwsh -ExecutionPolicy Bypass -File scripts/verify.ps1
#>
param(
    [string]$BuildCommand = "",
    [string]$UpCommand = "docker compose up --build -d",   # the README command; override only where compose cannot build
    [int]$GatewayPort = 18080,
    [int]$ControllerPort = 18090,
    [switch]$SkipBrowser,
    [switch]$KeepCheckout
)

$ErrorActionPreference = 'Continue'
$Source = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $Source
. (Join-Path $PSScriptRoot 'acceptance/common.ps1')
New-Item -ItemType Directory -Force -Path 'docs/evidence' | Out-Null
$platform = Write-Header 'SETU-X Phase 7 verification from a clean checkout (PowerShell)'
$LogPath = Join-Path $Source "docs/evidence/verify_$platform.log"
$Gw = "http://127.0.0.1:$GatewayPort"
$Ctl = "http://127.0.0.1:$ControllerPort"
$Services = 'inventory-a', 'inventory-b', 'pricing', 'controller', 'gateway', 'runner'
$readmeCovered = [ordered]@{}   # README command -> how it was exercised

function Pctl([double[]]$Values, [double]$Q) {
    $xs = $Values | Sort-Object
    $h = ($xs.Count - 1) * $Q / 100.0
    $lo = [Math]::Floor($h); $hi = [Math]::Min($lo + 1, $xs.Count - 1)
    return $xs[$lo] + ($h - $lo) * ($xs[$hi] - $xs[$lo])
}

function Run-Experiment([string]$Name, [string[]]$ExtraArgs) {
    $args2 = @('-NoProfile', '-File', 'scripts/run_experiment.ps1', '-Scenario', $Name, '-ControllerPort', "$ControllerPort") + $ExtraArgs
    Log "`$ pwsh $($args2 -join ' ')"
    $out = & (Get-Process -Id $PID).Path @args2 2>&1 | Out-String
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
        JsonlN   = @(Get-Content (Join-Path $Dir 'observations.jsonl') | Where-Object { $_.Trim() }).Count
        Events   = @(Get-Content (Join-Path $Dir 'events.jsonl') | Where-Object { $_.Trim() } | ForEach-Object { $_ | ConvertFrom-Json -DateKind String })
    }
}

function All-Healthy { return @($Services | Where-Object { (Health-Of $_) -ne 'healthy' }).Count -eq 0 }

# ------------------------------------------------------------------------------------------ V1
Log '=== V1 source state and clean clone of HEAD'
$head = (git rev-parse HEAD).Trim()
$dirtySrc = @(git status --porcelain 2>$null)
foreach ($d in $dirtySrc) { Log "uncommitted in source (NOT included in the clone): $d" }
$work = Join-Path ([IO.Path]::GetTempPath()) ("setu-x-verify-" + [Guid]::NewGuid().ToString('N').Substring(0, 8))
$cloneCode = Run "git clone --quiet --no-hardlinks `"$Source`" `"$work`""
Set-Location $work
$cloneHead = (git rev-parse HEAD).Trim()
$cloneDirty = @(git status --porcelain).Count
$env:COMPOSE_PROJECT_NAME = 'setu-x-verify'
$env:GATEWAY_PORT = "$GatewayPort"
$env:CONTROLLER_PORT = "$ControllerPort"
$env:SETU_MODE = 'local'
Log "clone: $work  HEAD $cloneHead  untracked/modified: $cloneDirty  compose project: $env:COMPOSE_PROJECT_NAME  ports: $GatewayPort/$ControllerPort"
Record 'V1' ($cloneCode -eq 0 -and $cloneHead -eq $head -and $cloneDirty -eq 0) "clean clone of $($head.Substring(0, 7)) in a temporary folder ($($dirtySrc.Count) uncommitted source files excluded)"

# ------------------------------------------------------------------------------------------ V2
Log '=== V2 no credentials, private keys, .env files or unvalidated results in the repository'
$tracked = @(git ls-files)
$text = $tracked | Where-Object { $_ -notmatch '\.(png|jpe?g|gif|ico|pdf|pptx|woff2?)$' }
$patterns = @(
    ('-----BEGIN ' + '[A-Z ]*PRIVATE KEY-----'), ('AKIA' + '[0-9A-Z]{16}'), ('ASIA' + '[0-9A-Z]{16}'),
    ('gh' + 'p_[A-Za-z0-9]{36}'), ('github' + '_pat_[A-Za-z0-9_]{20,}'), ('xox' + '[baprs]-[A-Za-z0-9-]{10,}'),
    ('AI' + 'za[0-9A-Za-z_-]{35}'), ('sk-' + '[A-Za-z0-9]{32,}'),
    ('(aws_secret_access_key|client_secret|password|api_key)' + '\s*[:=]\s*[''"][^''"\s]{8,}[''"]'))
$hits = @()
foreach ($f in $text) {
    $m = Select-String -Path $f -Pattern $patterns -AllMatches -ErrorAction SilentlyContinue
    foreach ($x in $m) { $hits += "$($f):$($x.LineNumber)" }
}
$badFiles = @($tracked | Where-Object { $_ -match '(^|/)\.env$|\.pem$|\.key$|(^|/)id_(rsa|ed25519)$|credentials.*\.json$' })
# Results committed to the repository must be complete, validated experiment records (Phase 8).
$resultProblems = @()
$resultDirs = @($tracked | Where-Object { $_ -like 'results/*' } | ForEach-Object { ($_ -split '/')[1] } | Sort-Object -Unique)
foreach ($d in $resultDirs) {
    $sp = "results/$d/summary.json"
    if (-not (Test-Path $sp) -or -not (Test-Path "results/$d/manifest.json") -or -not (Test-Path "results/$d/observations.csv")) { $resultProblems += "$d incomplete"; continue }
    $s = Get-Content $sp -Raw | ConvertFrom-Json -DateKind String
    $rows = @(Import-Csv "results/$d/observations.csv").Count
    if (-not $s.validation.ok -or $rows -ne $s.validation.rows_csv) { $resultProblems += "$d not validated or row count differs" }
}
foreach ($h in $hits) { Log "possible secret: $h" }
foreach ($b in $badFiles) { Log "credential-like file tracked: $b" }
foreach ($r in $resultProblems) { Log "results problem: $r" }
Log "scanned $($text.Count) tracked text files; committed result directories: $($resultDirs.Count)"
Record 'V2' ($hits.Count -eq 0 -and $badFiles.Count -eq 0 -and $resultProblems.Count -eq 0) "no secret patterns or credential files in $($text.Count) tracked files; $($resultDirs.Count) committed result directories, all validated"

# ------------------------------------------------------------------------------------------ V3-V7
Log '=== V3 docker compose config'
$cfg = Run 'docker compose config --quiet'
$readmeCovered['docker compose config'] = 'V3 (executed)'
Record 'V3' ($cfg -eq 0) 'compose configuration valid'

Log '=== V4 build images'
docker compose down -v --remove-orphans 2>&1 | Out-Null
$buildCmd = if ($BuildCommand) { $BuildCommand } elseif ($SkipBrowser) { 'docker compose --profile test build' } else { 'docker compose --profile test --profile browser build' }
Record 'V4' ((Run $buildCmd) -eq 0) 'images built from the clean checkout'

Log '=== V5 configuration validator accepts the shipped files and rejects broken ones'
$good = Run 'docker compose --profile test run --rm --no-deps tests python -m setu_x.config_check configs'
$bad = Run 'docker compose --profile test run --rm --no-deps tests python -m setu_x.config_check tests/fixtures/bad_config'
$readmeCovered['docker compose --profile test run --rm --no-deps tests python -m setu_x.config_check configs'] = 'V5 (executed)'
Record 'V5' ($good -eq 0 -and $bad -ne 0) "shipped configs valid (exit $good); tests/fixtures/bad_config rejected (exit $bad)"

Log '=== V6 unit + contract + integration tests'
Record 'V6' ((Run 'docker compose --profile test run --rm tests') -eq 0) 'all unit, contract and integration tests passed'
$readmeCovered['docker compose --profile test run --rm tests'] = 'V6 (executed)'

Log '=== V7 lint'
Record 'V7' ((Run 'docker compose --profile test run --rm tests ruff check --no-cache src tests') -eq 0) 'ruff clean'

# ------------------------------------------------------------------------------------------ V8
Log '=== V8 clean start and the README quick start'
$up = Run $UpCommand
$healthy = Wait-Until { All-Healthy } 150
$ps = Run 'docker compose ps'
$curlOut = (& curl -s -X POST "$Gw/api/orders" -H 'content-type: application/json' -H 'X-Request-ID: verify-readme-1' -d '{"sku":"SKU-001","quantity":2,"customer_tier":"standard"}' 2>&1 | Out-String)
Log "curl -> $curlOut"
$order = $null; try { $order = $curlOut | ConvertFrom-Json -DateKind String } catch { }
$logs = (docker compose logs gateway --no-log-prefix 2>&1 | Out-String)
$jsonLog = @($logs -split "`n" | Where-Object { $_ -match 'verify-readme-1' }) | Select-Object -First 1
Log "gateway log line for the request: $jsonLog"
$logOk = $false; try { $logOk = ($jsonLog | ConvertFrom-Json).request_id -eq 'verify-readme-1' } catch { }
$readmeCovered['docker compose up --build -d'] = $(if ($UpCommand -eq 'docker compose up --build -d') { 'V8 (executed)' } else { "V8 (executed as '$UpCommand'; images built in V4)" })
$readmeCovered['docker compose ps'] = 'V8 (executed)'
$readmeCovered['curl -s -X POST http://127.0.0.1:8080/api/orders'] = "V8 (executed against port $GatewayPort)"
$readmeCovered['docker compose logs gateway'] = 'V8 (executed; JSON log line found by request ID)'
Record 'V8' ($up -eq 0 -and $healthy -and $order.status -eq 'confirmed' -and $order.inventory.available -eq 8 -and $order.pricing.total_cents -eq 11000 -and $logOk) 'stack up, 6 healthy; README curl order confirmed with the golden values (8 in stock, 11000 cents); structured log line carries the request ID'

Log '=== V9 end-to-end tests'
Record 'V9' ((Run 'docker compose --profile test run --rm tests pytest -p no:cacheprovider -m e2e') -eq 0) 'e2e tests passed'
$readmeCovered['docker compose --profile test run --rm tests pytest -p no:cacheprovider -m e2e'] = 'V9 (executed)'

Log '=== V10 dashboard in a real browser'
if ($SkipBrowser) {
    Skip 'V10' 'browser tests (-SkipBrowser)'
    $readmeCovered['docker compose --profile browser run --rm browser-tests'] = 'NOT executed (-SkipBrowser)'
} else {
    Record 'V10' ((Run 'docker compose --profile browser run --rm browser-tests') -eq 0) '13 browser tests (incl. JS helpers identical to the Python statistics)'
    $readmeCovered['docker compose --profile browser run --rm browser-tests'] = 'V10 (executed)'
}
$readmeCovered['node --test dashboard/tests/*.test.mjs'] = $(if (Get-Command node -ErrorAction SilentlyContinue) {
    $n = Run 'node --test dashboard/tests/stats.test.mjs'; "executed here (exit $n)" } else { 'node not installed here; the same helpers are exercised in the browser by V10' })

# ------------------------------------------------------------------------------------------ V11
Log '=== V11 baseline experiment (A_baseline, shortened: 2 runs x 20 s; records reconciled independently)'
$base = Run-Experiment 'A_baseline' @('-Runs', '2', '-WarmupS', '3', '-DurationS', '20', '-Label', 'verify baseline')
$readmeCovered['pwsh -File scripts/run_experiment.ps1 -Scenario smoke'] = 'V11/V12 (executed with A_baseline and C_service_failure)'
$ok11 = $false
if ($base.Code -eq 0 -and $base.Dir) {
    $r = Load-Records $base.Dir
    $m = $r.Manifest; $s = $r.Summary
    $fileSha = (Get-FileHash -Algorithm SHA256 'configs/scenarios/A_baseline.yaml').Hash.ToLower()
    $p = @()
    if ($r.Csv.Count -ne ($m.runs | Measure-Object scheduled -Sum).Sum -or $r.JsonlN -ne $r.Csv.Count) { $p += 'row counts' }
    if ($m.scenario_sha256 -ne $fileSha) { $p += "scenario sha256 $($m.scenario_sha256) != file $fileSha" }
    if ($m.git_commit -ne $cloneHead.Substring(0, $m.git_commit.Length)) { $p += "git commit $($m.git_commit) != $cloneHead" }
    if ($m.overrides.runs -ne 2 -or $m.overrides.duration_s -ne 20) { $p += 'overrides not recorded' }
    foreach ($run in $s.per_run) {
        $lat = [double[]](@($r.Csv | Where-Object { $_.run_id -eq $run.run_id -and $_.phase -eq 'measure' -and $_.outcome -in 'confirmed', 'rejected' }) | ForEach-Object { [double]$_.latency_ms })
        $p50 = Pctl $lat 50; $p95 = Pctl $lat 95
        Log ("{0}: n={1} success={2} p50 csv={3:N3} summary={4} p95 csv={5:N3} summary={6} p99 summary={7} (n<1000: withheld)" -f $run.run_id, $lat.Count, $run.success_rate, $p50, $run.latency_ok_ms.p50, $p95, $run.latency_ok_ms.p95, $run.latency_ok_ms.p99)
        if ([Math]::Abs($p50 - [double]$run.latency_ok_ms.p50) -gt 0.0015 -or [Math]::Abs($p95 - [double]$run.latency_ok_ms.p95) -gt 0.0015) { $p += "$($run.run_id) percentiles differ" }
        if ($run.success_rate -ne 1 -or $null -ne $run.latency_ok_ms.p99) { $p += "$($run.run_id) success or p99 gating" }
    }
    foreach ($x in $p) { Log "problem: $x" }
    $ok11 = $p.Count -eq 0 -and $s.validation.ok -and $s.status -eq 'completed'
}
Record 'V11' $ok11 'completed and validated; scenario sha256 and git commit in the manifest match the checkout; overrides recorded; p50/p95 recomputed from the CSV match; p99 withheld (n < 1000); 100% success'

# ------------------------------------------------------------------------------------------ V12
Log '=== V12 failure experiment (C_service_failure: error and hang on inventory-a, 1 run x 25 s)'
$fail = Run-Experiment 'C_service_failure' @('-Runs', '1', '-WarmupS', '3', '-DurationS', '25', '-Label', 'verify failure')
$ok12 = $false
if ($fail.Code -eq 0 -and $fail.Dir) {
    $r2 = Load-Records $fail.Dir
    $p = @()
    foreach ($run in $r2.Manifest.runs) {
        $f = $run.faults[0]; $t0 = [double]$f.actual_measure_offset_s; $t1 = $t0 + [double]$f.spec.duration_s
        $rows = @($r2.Csv | Where-Object { $_.run_id -eq $run.run_id -and $_.phase -eq 'measure' })
        $failed = @($rows | Where-Object { $_.outcome -notin 'confirmed', 'rejected' } | ForEach-Object { [double]$_.measure_offset_s })
        $outside = @($failed | Where-Object { $_ -lt $t0 - 0.15 -or $_ -gt $t1 + 0.15 })
        $before = @($rows | Where-Object { [double]$_.measure_offset_s -lt $t0 - 0.15 })
        $beforeOk = @($before | Where-Object { $_.outcome -eq 'confirmed' }).Count
        $rec = ($r2.Summary.per_run | Where-Object run_id -eq $run.run_id).recovery[0]
        $maxLat = (@($rows | Where-Object { $_.outcome -eq 'unavailable' } | ForEach-Object { [double]$_.latency_ms }) | Measure-Object -Maximum).Maximum
        Log ("{0}: fault {1} at {2:N3}-{3:N3} s; failures {4} (outside window {5}); before fault {6}/{7} ok; slowest failure {8:N0} ms; recovery_s={9}" -f `
            $run.run_id, $f.spec.mode, $t0, $t1, $failed.Count, $outside.Count, $beforeOk, $before.Count, $maxLat, $rec.recovery_s)
        if (-not $f.ok -or $failed.Count -lt 100 -or $outside.Count -gt 0 -or $beforeOk -ne $before.Count -or $null -eq $rec.recovery_s) { $p += $run.run_id }
        if ($f.spec.mode -eq 'hang' -and $maxLat -gt 1500) { $p += "$($run.run_id): hang not cut by the 800 ms timeout" }
    }
    $ok12 = $p.Count -eq 0 -and $r2.Summary.validation.ok
}
Record 'V12' $ok12 'failures only inside the actual fault window, none before it; hang failures bounded by the 800 ms timeout; recovery measured; records validated'

# ------------------------------------------------------------------------------------------ V13
Log '=== V13 artifacts: request rows trace to service logs; events trace to the fault schedule'
$ok13 = $false
if ($base.Dir -and $fail.Dir) {
    $rid = ($r.Csv | Where-Object { $_.phase -eq 'measure' } | Select-Object -First 1).request_id
    $gwLog = (docker compose logs gateway --no-log-prefix 2>&1 | Out-String)
    $invLog = (docker compose logs inventory-a --no-log-prefix 2>&1 | Out-String)
    $inGw = $gwLog -match [regex]::Escape($rid); $inInv = $invLog -match [regex]::Escape($rid)
    $types = @($r2.Events | Where-Object source -eq 'controller' | ForEach-Object type | Sort-Object -Unique)
    $sched = @($r2.Events | Where-Object { $_.source -eq 'runner' -and $_.type -eq 'fault_scheduled' }).Count
    $gwEv = @($r2.Events | Where-Object source -eq 'gateway').Count
    Log "request $rid in gateway log: $inGw, in inventory-a log: $inInv; failure-run controller events: $($types -join ', '); fault_scheduled: $sched; gateway events: $gwEv"
    $ok13 = $inGw -and $inInv -and 'fault_injected' -in $types -and 'fault_cleared' -in $types -and $sched -eq 2
}
Record 'V13' $ok13 'a measured request ID is found in gateway and inventory logs; fault injection/clearing recorded per run alongside the runner''s schedule'

# ------------------------------------------------------------------------------------------ V14
Log '=== V14 configuration changes at runtime (validated, applied, reverted)'
$before = (Invoke-Http 'GET' "$Ctl/api/placement").Json
$badPut = Invoke-Http 'PUT' "$Ctl/api/placement" (@{ call_mode = 'parallel'; services = $before.placement.services; resilience = @{ timeout_ms = 5000; outer_deadline_ms = 1000 } })
$afterBad = (Invoke-Http 'GET' "$Ctl/api/placement").Json.version
$p2 = $before.placement | ConvertTo-Json -Depth 20 | ConvertFrom-Json -Depth 20 -DateKind String
$p2.call_mode = 'sequential'; $p2.resilience.retries = 1
$goodPut = Invoke-Http 'PUT' "$Ctl/api/placement" $p2
$v = $goodPut.Json.version
$applied = Wait-Until { (Invoke-Http 'GET' "$Gw/api/placement/current").Json.placement_version -eq $v } 15
$o = Invoke-Http 'POST' "$Gw/api/orders" @{ sku = 'SKU-002'; quantity = 1 }
$reset = Invoke-Http 'POST' "$Ctl/api/placement/reset"
$back = Wait-Until { (Invoke-Http 'GET' "$Gw/api/placement/current").Json.placement_source -eq 'controller' -and (Invoke-Http 'GET' "$Gw/api/placement/current").Json.placement_version -eq $reset.Json.version } 15
Log "invalid PUT -> $($badPut.Code) ($($badPut.Json.error.message)); version unchanged: $($afterBad -eq $before.version)"
Log "valid PUT -> v$v; gateway applied: $applied; order call_mode=$($o.Json.call_mode) settings.retries=$($o.Json.resilience.settings.retries); reset -> v$($reset.Json.version) source=$($reset.Json.source), gateway followed: $back"
Record 'V14' ($badPut.Code -eq 422 -and $afterBad -eq $before.version -and $applied -and $o.Json.call_mode -eq 'sequential' -and $o.Json.resilience.settings.retries -eq 1 -and $reset.Json.source -eq 'file' -and $back) 'invalid change refused without effect; valid change reached the gateway and changed behaviour; reset restored the files'

# ------------------------------------------------------------------------------------------ V15
Log '=== V15 restart behaviour: controller restart, full stop/start, results persistence'
[void](Invoke-Http 'PUT' "$Ctl/api/placement" $p2)
$r15a = Run 'docker compose restart controller'
$ctlBack = Wait-Until { (Health-Of 'controller') -eq 'healthy' } 60
$src = (Invoke-Http 'GET' "$Ctl/api/placement").Json
$orderDuring = Invoke-Http 'POST' "$Gw/api/orders" @{ sku = 'SKU-002'; quantity = 1 }
$stop = Run 'docker compose stop'
$start = Run 'docker compose start'
$allBack = Wait-Until { All-Healthy } 150
$ids = @((Invoke-Http 'GET' "$Ctl/api/experiments").Json.experiments | ForEach-Object experiment_id)
$baseId = Split-Path $base.Dir -Leaf
$fileBack = Invoke-Http 'GET' "$Ctl/api/experiments/$baseId/files/summary.json"
Log "after controller restart: placement source=$($src.source) version=$($src.version) (runtime change not kept: documented limitation); order served: HTTP $($orderDuring.Code)"
Log "after stop/start: healthy=$allBack; experiments still listed: $($ids.Count) (baseline present: $($baseId -in $ids)); summary download HTTP $($fileBack.Code)"
Record 'V15' ($r15a -eq 0 -and $ctlBack -and $src.source -eq 'file' -and $orderDuring.Code -eq 200 -and $stop -eq 0 -and $start -eq 0 -and $allBack -and $baseId -in $ids -and $fileBack.Code -eq 200) 'controller restart reverts to the files (as documented) without breaking orders; full stop/start recovers; results survive restarts'

# ------------------------------------------------------------------------------------------ V16
Log '=== V16 container-level fault: inventory-a really stopped, fallback serves, container restored'
$pwshExe = (Get-Process -Id $PID).Path
$job = Start-Job -ScriptBlock { param($exe, $dir) & $exe -NoProfile -File (Join-Path $dir 'scripts/fault_container.ps1') -Service inventory-a -Seconds 8 2>&1 | Out-String } -ArgumentList $pwshExe, $work
$stopped = Wait-Until { -not (docker compose ps -q --status running inventory-a) } 30
$during = Invoke-Http 'POST' "$Gw/api/orders" @{ sku = 'SKU-002'; quantity = 1 }
$fc = Receive-Job $job -Wait -AutoRemoveJob
Log $fc.TrimEnd()
$restored = (Health-Of 'inventory-a') -eq 'healthy'
$after = $null
[void](Wait-Until { $script:after = Invoke-Http 'POST' "$Gw/api/orders" @{ sku = 'SKU-002'; quantity = 1 }; $script:after.Json.inventory.instance -eq 'inventory-a' } 20)
$readmeCovered['pwsh -File scripts/fault_container.ps1 -Service inventory-a -Seconds 20'] = 'V16 (executed with -Seconds 8)'
Log "container stopped: $stopped; order during stop: HTTP $($during.Code) via $($during.Json.inventory.instance); restored healthy: $restored; afterwards via $($script:after.Json.inventory.instance)"
$after = $script:after
Record 'V16' ($stopped -and $during.Code -eq 200 -and $during.Json.inventory.instance -eq 'inventory-b' -and $restored -and $after.Json.inventory.instance -eq 'inventory-a') 'stopped primary -> served by inventory-b; primary back and serving after restart'

# ------------------------------------------------------------------------------------------ V17
Log '=== V17 every command in the README is exercised here or evidenced by a passing acceptance log'
$evidence = @{
    'bash scripts/phase2_acceptance.sh'                                 = 'docs/evidence/phase2_acceptance_linux.log'
    'pwsh -ExecutionPolicy Bypass -File scripts/phase2_acceptance.ps1' = 'docs/evidence/phase2_acceptance_windows.log'
    'pwsh -ExecutionPolicy Bypass -File scripts/phase3_acceptance.ps1' = 'docs/evidence/phase3_acceptance_windows.log'
    'pwsh -ExecutionPolicy Bypass -File scripts/phase4_acceptance.ps1' = 'docs/evidence/phase4_acceptance_windows.log'
    'pwsh -ExecutionPolicy Bypass -File scripts/phase5_acceptance.ps1' = 'docs/evidence/phase5_acceptance_windows.log'
    'pwsh -ExecutionPolicy Bypass -File scripts/phase6_acceptance.ps1' = 'docs/evidence/phase6_acceptance_windows.log'
    'pwsh -ExecutionPolicy Bypass -File scripts/verify.ps1'            = 'this run'
}
foreach ($k in $evidence.Keys) {
    if ($evidence[$k] -eq 'this run') { $readmeCovered[$k] = 'this run'; continue }
    $f = Join-Path $Source $evidence[$k]
    $pass = (Test-Path $f) -and ((Get-Content $f -Raw) -match 'ACCEPTANCE: PASS')
    $readmeCovered[$k] = $(if ($pass) { "evidence: $($evidence[$k]) (PASS)" } else { "MISSING evidence $($evidence[$k])" })
}
$readmeCovered['docker compose down'] = 'V18 (executed)'
# collect README commands: lines of fenced code blocks + inline `docker ...`/`pwsh ...`/`python ...` spans
$readme = Get-Content README.md -Raw
$cmds = @()
foreach ($blk in [regex]::Matches($readme, '```[a-z]*\r?\n(.*?)```', 'Singleline')) {
    $joined = $blk.Groups[1].Value -replace '\\\r?\n\s*', ' '
    foreach ($line in $joined -split "`n") { $c = ($line -replace '\s+#.*$', '').Trim(); if ($c) { $cmds += $c } }
}
foreach ($m in [regex]::Matches($readme, '`((?:docker|pwsh|python|node|bash|curl) [^`]+)`')) { $cmds += $m.Groups[1].Value.Trim() }
$unmapped = @()
foreach ($c in ($cmds | Sort-Object -Unique)) {
    $key = $readmeCovered.Keys | Where-Object { $c -eq $_ -or $c.StartsWith($_) } | Select-Object -First 1
    if ($key) { Log ("  [{0}] {1}" -f $readmeCovered[$key], $c) } else { $unmapped += $c; Log "  [NOT COVERED] $c" }
}
$missingEv = @($readmeCovered.Values | Where-Object { $_ -like 'MISSING*' -or $_ -like 'NOT executed*' })
Record 'V17' ($unmapped.Count -eq 0 -and $missingEv.Count -eq 0) "$(@($cmds | Sort-Object -Unique).Count) README commands: all exercised in this run or backed by a PASS acceptance log ($($unmapped.Count) uncovered)"

# ------------------------------------------------------------------------------------------ V18
Log '=== V18 clean shutdown and removal of the verification project'
$bad = @()
foreach ($s in $Services) {
    $i = (docker inspect (Container-Id $s) | ConvertFrom-Json -DateKind String)[0]; $hc = $i.HostConfig
    if (-not ($i.Config.User -eq '10001' -and $hc.ReadonlyRootfs -and ($hc.CapDrop -contains 'ALL') -and ($hc.SecurityOpt -contains 'no-new-privileges:true'))) { $bad += $s }
}
Run 'docker compose stop' | Out-Null
$codes = @{}; foreach ($s in $Services) { $codes[$s] = [int](docker inspect --format '{{.State.ExitCode}}' (Container-Id $s -All)) }
Log ("exit codes: " + (($codes.GetEnumerator() | Sort-Object Name | ForEach-Object { "$($_.Name)=$($_.Value)" }) -join ' '))
$down = Run 'docker compose down -v --remove-orphans'
$left = @(docker compose ps -a -q | Where-Object { $_ }).Count
$vol = (docker volume ls -q --filter name=setu-x-verify_ | Out-String).Trim()
Record 'V18' ($bad.Count -eq 0 -and @($codes.Values | Where-Object { $_ -ne 0 }).Count -eq 0 -and $down -eq 0 -and $left -eq 0 -and -not $vol) 'hardening intact; all 6 exited 0 on stop; containers, network and the verification volume removed'

Set-Location $Source
foreach ($n in 'COMPOSE_PROJECT_NAME', 'GATEWAY_PORT', 'CONTROLLER_PORT', 'SETU_MODE') { Remove-Item "Env:$n" -ErrorAction SilentlyContinue }
if ($KeepCheckout) { Log "checkout kept: $work" } else { Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue; Log "temporary checkout removed" }
Write-Summary 'PHASE 7 VERIFY' $LogPath
exit ($script:Fails + $script:Skips)
