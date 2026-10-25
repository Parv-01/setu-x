<#
.SYNOPSIS
  SETU-X Phase 4 acceptance: latency and failure injection, timeouts, retries, circuit breaker,
  fallback, recovery (PowerShell 7+, any OS).

.DESCRIPTION
  Runs against the real Docker Compose stack. Every check prints what it measured. Writes
  docs/evidence/phase4_acceptance_<platform>.log. Exit code = number of failed checks.
  Numbers printed here are acceptance evidence on this machine, not experiment results.

.EXAMPLE
  pwsh -ExecutionPolicy Bypass -File scripts/phase4_acceptance.ps1
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
$Services = 'inventory-a', 'inventory-b', 'pricing', 'controller', 'gateway'
New-Item -ItemType Directory -Force -Path 'docs/evidence' | Out-Null
$platform = Write-Header 'SETU-X Phase 4 acceptance (PowerShell)'
$LogPath = "docs/evidence/phase4_acceptance_$platform.log"

function Order([string]$Sku = 'SKU-002') { return Invoke-Http 'POST' "$Gw/api/orders" @{ sku = $Sku; quantity = 1 } }
function Gateway-Version { return (Invoke-Http 'GET' "$Gw/api/placement/current").Json.placement_version }
function Base-Placement { return ((Invoke-Http 'GET' "$Ctl/api/placement").Json.placement | ConvertTo-Json -Depth 20 | ConvertFrom-Json -Depth 20 -DateKind String) }
function Apply([scriptblock]$Change) {
    $p = Base-Placement; & $Change $p
    $r = Invoke-Http 'PUT' "$Ctl/api/placement" $p
    if ($r.Code -eq 200) { $v = $r.Json.version; [void](Wait-Until { (Gateway-Version) -eq $v } 15) }
    else { Log "PUT placement failed: HTTP $($r.Code) $($r.Body)" }
    return $r.Code -eq 200
}
function Reset-All {
    [void](Invoke-Http 'DELETE' "$Ctl/api/faults")
    $r = Invoke-Http 'POST' "$Ctl/api/placement/reset"; $v = $r.Json.version
    [void](Wait-Until { (Gateway-Version) -eq $v } 15)
}
function Fault([hashtable]$Spec) { return Invoke-Http 'POST' "$Ctl/api/faults" $Spec }
function Matched([string]$Target) {
    $f = (Invoke-Http 'GET' "$Ctl/api/faults").Json.faults | Where-Object target -eq $Target
    return $(if ($f.active) { [int]$f.active.matched } else { -1 })
}

Log '=== C1 docker compose config'
Record 'C1' ((Run 'docker compose config --quiet') -eq 0) 'compose configuration valid'

Log '=== C2 build images'
$buildCmd = if ($BuildCommand) { $BuildCommand } else { 'docker compose --profile test build' }
Record 'C2' ((Run $buildCmd) -eq 0) 'images built'

Log '=== C3 start stack (local mode) and check all 5 services healthy'
docker compose down --remove-orphans 2>&1 | Out-Null
$env:SETU_MODE = 'local'
$up = (Run 'docker compose up -d --wait --wait-timeout 120') -eq 0
$bad = @($Services | Where-Object { (Health-Of $_) -ne 'healthy' })
Record 'C3' ($up -and $bad.Count -eq 0) ($(if ($bad) { "unhealthy: $($bad -join ',')" } else { 'all 5 services healthy' }))

Log '=== C4 automated tests (unit + contract + integration)'
Record 'C4' ((Run 'docker compose --profile test run --rm tests') -eq 0) 'unit + contract + integration tests passed'

Log '=== C5 end-to-end tests'
Record 'C5' ((Run 'docker compose --profile test run --rm tests pytest -p no:cacheprovider -m e2e') -eq 0) 'e2e tests passed'

Log '=== C6 fault injection only within allowed limits'
$r1 = Fault @{ target = 'inventory-a'; mode = 'error'; duration_s = 1000 }
$r2 = Fault @{ target = 'gateway'; mode = 'error'; duration_s = 5 }
$r3 = Fault @{ target = 'inventory-a'; mode = 'error'; duration_s = 5; probability = 2 }
$r4 = Fault @{ target = 'inventory-a'; mode = 'slow'; duration_s = 5 }
foreach ($x in $r1, $r2, $r3, $r4) { Log "HTTP $($x.Code): $($x.Json.error.message) $($x.Json.error.details | ConvertTo-Json -Compress -Depth 5)" }
$active = @((Invoke-Http 'GET' "$Ctl/api/faults").Json.faults | Where-Object { $_.active }).Count
Record 'C6' ($r1.Code -eq 422 -and $r2.Code -eq 422 -and $r3.Code -eq 422 -and $r4.Code -eq 422 -and $active -eq 0) 'duration>300, non-allow-listed target, probability>1, slow without delay: all 422; no fault active'

Log '=== C7 error fault on primary -> fallback to secondary (fallback on by default)'
[void](Fault @{ target = 'inventory-a'; mode = 'error'; duration_s = 60 })
$res = @(1..5 | ForEach-Object { Order })
$served = $res | ForEach-Object { $_.Json.inventory.instance }
Log "served: $($served -join ', '); fallback_used: $(($res | ForEach-Object { $_.Json.dependencies[0].fallback_used }) -join ',')"
Log "latencies (s): $(($res | ForEach-Object { Fmt $_.Seconds }) -join ', ')"
$ok = @($res | Where-Object { $_.Code -eq 200 -and $_.Json.inventory.instance -eq 'inventory-b' -and $_.Json.dependencies[0].fallback_used }).Count -eq 5
Record 'C7' $ok '5/5 orders HTTP 200 served by inventory-b with fallback_used=true'

Log '=== C8 same fault with fallback disabled -> bounded 503'
$applied = Apply { param($p) $p.resilience.fallback = $false }
$o = Order; Log "HTTP $($o.Code) in $(Fmt $o.Seconds)s: $($o.Json.error.message)"
Record 'C8' ($applied -and $o.Code -eq 503 -and $o.Json.error.code -eq 'dependency_unavailable' -and $o.Seconds -lt 1.0) "503 dependency_unavailable in $(Fmt $o.Seconds)s"
Reset-All

Log '=== C9 bounded retries under a transient fault (p=0.5, seed 42, retries=2, no fallback)'
$applied = Apply { param($p) $p.resilience.retries = 2; $p.resilience.fallback = $false }
[void](Fault @{ target = 'inventory-a'; mode = 'error'; duration_s = 120; probability = 0.5; seed = 42 })
$res = @(1..40 | ForEach-Object { Order })
$okCount = @($res | Where-Object Code -eq 200).Count
$attempts = $res | ForEach-Object { if ($_.Code -eq 200) { $_.Json.dependencies[0].attempts } else { $_.Json.error.details.dependencies[0].attempts } }
$maxAttempts = ($attempts | Measure-Object -Maximum).Maximum
$totalAttempts = ($attempts | Measure-Object -Sum).Sum
$matched = Matched 'inventory-a'
Log "success: $okCount/40; attempts per order max=$maxAttempts total=$totalAttempts; requests that reached inventory-a: $matched"
Record 'C9' ($applied -and $maxAttempts -le 3 -and $matched -eq $totalAttempts -and $okCount -gt 20) "attempts per order <= 1+retries (max $maxAttempts); inventory-a saw exactly $matched requests = gateway attempts; $okCount/40 succeeded"
Reset-All

Log '=== C10 unresponsive dependency (hang): cut by timeout; with fallback -> served by secondary'
[void](Fault @{ target = 'inventory-a'; mode = 'hang'; duration_s = 30 })
$withFb = Order; Log "fallback on : HTTP $($withFb.Code) via $($withFb.Json.inventory.instance) in $(Fmt $withFb.Seconds)s; tried=$($withFb.Json.dependencies[0].tried | ConvertTo-Json -Compress)"
$applied = Apply { param($p) $p.resilience.fallback = $false }
$noFb = Order; Log "fallback off: HTTP $($noFb.Code) in $(Fmt $noFb.Seconds)s: $($noFb.Json.error.message)"
# Lower bounds ("waited for the 0.8 s timeout, did not fail fast") use the GATEWAY's own measurement,
# i.e. the same clock as the timer. The client stopwatch runs on the host, which on Docker Desktop is a
# different clock domain from the Linux VM, so it is only used for the upper bound ("did not hang").
$gwWith = [double]$withFb.Json.dependencies[0].elapsed_ms
$gwWithout = [double]$noFb.Json.error.details.dependencies[0].elapsed_ms
Log ("gateway-measured inventory time: fallback on {0} ms (timeout on a + call to b), fallback off {1} ms" -f $gwWith, $gwWithout)
$ok = $withFb.Code -eq 200 -and $withFb.Json.inventory.instance -eq 'inventory-b' -and $gwWith -ge 795 -and $withFb.Seconds -lt 1.6 -and `
      $applied -and $noFb.Code -eq 503 -and $noFb.Json.error.message -like '*timeout*' -and $gwWithout -ge 795 -and $noFb.Seconds -lt 1.6
Record 'C10' $ok "timeout honoured (gateway-measured $gwWith / $gwWithout ms vs 800 ms timeout); client saw 200 in $(Fmt $withFb.Seconds)s with fallback, 503 timeout in $(Fmt $noFb.Seconds)s without (upper bound 1.6 s)"
Reset-All

Log '=== C11 circuit breaker: opens, stops calls to the failing instance, recovers (recovery time measured)'
$applied = Apply { param($p) $p.resilience.circuit_breaker.enabled = $true; $p.resilience.circuit_breaker.consecutive_failures = 3; $p.resilience.circuit_breaker.reset_timeout_ms = 3000 }
$faultSet = Fault @{ target = 'inventory-a'; mode = 'error'; duration_s = 6 }
$faultEndsAt = (Get-Date).AddSeconds(6)
$res = @(1..8 | ForEach-Object { Order })
$matchedDuringOpen = Matched 'inventory-a'
$states = (Invoke-Http 'GET' "$Gw/api/resilience/breakers").Json.breakers
$aState = ($states | Where-Object instance -eq 'inventory-a').state
$rejections = ($res | ForEach-Object { $_.Json.resilience.breaker_rejections } | Measure-Object -Sum).Sum
Log "8 orders served by: $(($res | ForEach-Object { $_.Json.inventory.instance }) -join ', ')"
Log "inventory-a requests during fault: $matchedDuringOpen (threshold 3); breaker rejections: $rejections; breaker state: $aState"
# wait for the fault to expire, then measure how long until inventory-a serves again
while ((Get-Date) -lt $faultEndsAt) { Start-Sleep -Milliseconds 100 }
$t0 = Get-Date; $recovered = $false
while (((Get-Date) - $t0).TotalSeconds -lt 15) {
    $o = Order
    if ($o.Json.inventory.instance -eq 'inventory-a') { $recovered = $true; break }
    Start-Sleep -Milliseconds 250
}
$recovery = ((Get-Date) - $t0).TotalSeconds
$transitions = (Invoke-Http 'GET' "$Gw/api/resilience/events?type=breaker_transition&limit=10").Json.events | Where-Object instance -eq 'inventory-a'
$seq = (($transitions | Sort-Object seq | ForEach-Object { "$($_.from_state)->$($_.to_state)" }) -join ', ')
Log "recovery: inventory-a served again $(Fmt $recovery)s after the fault expired; transitions: $seq"
$ok = $applied -and $faultSet.Code -eq 200 -and $matchedDuringOpen -eq 3 -and $aState -eq 'open' -and $rejections -ge 5 -and `
      @($res | Where-Object { $_.Code -eq 200 }).Count -eq 8 -and $recovered -and $seq -like '*closed->open*open->half_open*half_open->closed*'
Record 'C11' $ok "breaker opened after 3 failures; inventory-a then received 0 further requests; served again $(Fmt $recovery)s after fault expiry (no traffic during the open period, so the first request after expiry was the half-open probe; recovery under load is measured in Phase 5)"
Reset-All

Log '=== C12 real container failure (stop) with fallback; container fault script'
$stop = (pwsh -NoProfile -File scripts/fault_container.ps1 -Service inventory-a -Seconds 60 -NoWait 2>&1 | Out-String); Log $stop.TrimEnd()
$o = Order; Log "inventory-a stopped: HTTP $($o.Code) via $($o.Json.inventory.instance) in $(Fmt $o.Seconds)s; tried=$($o.Json.dependencies[0].tried | ConvertTo-Json -Compress)"
Run 'docker compose start inventory-a' | Out-Null; [void](Wait-Healthy 'inventory-a' 60)
$o2 = Order
$cycle = (pwsh -NoProfile -File scripts/fault_container.ps1 -Service pricing -Seconds 2 2>&1 | Out-String); $cycleCode = $LASTEXITCODE; Log $cycle.TrimEnd()
$ok = $o.Code -eq 200 -and $o.Json.inventory.instance -eq 'inventory-b' -and $o2.Json.inventory.instance -eq 'inventory-a' -and $cycleCode -eq 0 -and $cycle -like '*healthy_at*'
Record 'C12' $ok "stopped primary -> 200 via inventory-b in $(Fmt $o.Seconds)s; back to inventory-a after restart; fault_container.ps1 full cycle exit $cycleCode"

Log '=== C13 simulated mode: configured link delay injected and reported separately'
$env:SETU_MODE = 'simulated'
Run 'docker compose up -d --no-deps --wait --wait-timeout 60 gateway' | Out-Null
$v = (Invoke-Http 'GET' "$Ctl/api/placement").Json.version; [void](Wait-Until { (Gateway-Version) -eq $v } 15)
$o = Order; $d = $o.Json.dependencies
Log "simulated: HTTP $($o.Code) total=$($o.Json.timings_ms.total) ms; injected inventory=$($d[0].injected_delay_ms) pricing=$($d[1].injected_delay_ms) critical_path=$($o.Json.resilience.injected_delay_ms_critical_path)"
$lr = Invoke-Http 'PUT' "$Ctl/api/links" @{ link_rtt_ms = @{ 'local--aws-simulated' = 150 } }
[void](Wait-Until { (Gateway-Version) -eq $lr.Json.version } 15)
$o2 = Order
Log "after PUT local--aws-simulated=150: total=$($o2.Json.timings_ms.total) ms; injected inventory=$($o2.Json.dependencies[0].injected_delay_ms)"
$ok = $o.Code -eq 200 -and $o.Json.environment_mode -eq 'simulated' -and $d[0].injected_delay_ms -eq 40 -and $d[1].injected_delay_ms -eq 55 -and `
      $o.Json.resilience.injected_delay_ms_critical_path -eq 55 -and $o.Json.timings_ms.total -ge 55 -and `
      $lr.Code -eq 200 -and $o2.Json.dependencies[0].injected_delay_ms -eq 150 -and $o2.Json.timings_ms.total -ge 150
Record 'C13' $ok "injected 40/55 ms (critical path 55, measured total $($o.Json.timings_ms.total) ms); runtime change to 150 ms applied (total $($o2.Json.timings_ms.total) ms)"
Reset-All
$env:SETU_MODE = 'local'
Run 'docker compose up -d --no-deps --wait --wait-timeout 60 gateway' | Out-Null

Log '=== C14 resilience and fault events are observable'
$gwTypes = @((Invoke-Http 'GET' "$Gw/api/resilience/events?limit=2000").Json.events | ForEach-Object type | Sort-Object -Unique)
$ctlTypes = @((Invoke-Http 'GET' "$Ctl/api/events?limit=1000").Json.events | ForEach-Object type | Sort-Object -Unique)
Log "gateway event types (since gateway restart): $($gwTypes -join ', ')"
Log "controller event types: $($ctlTypes -join ', ')"
$ok = 'fault_injected' -in $ctlTypes -and 'fault_cleared' -in $ctlTypes -and 'links_updated' -in $ctlTypes -and 'placement_updated' -in $ctlTypes
Record 'C14' $ok 'controller recorded fault_injected, fault_cleared, placement_updated, links_updated (gateway events shown in C9-C11 output)'

Log '=== C15 hardening unchanged, graceful stop and clean down'
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
Record 'C15' ($bad.Count -eq 0 -and @($codes.Values | Where-Object { $_ -ne 0 }).Count -eq 0 -and $left -eq 0) "hardening intact; all 5 exited 0; $left containers left"

Remove-Item Env:SETU_MODE -ErrorAction SilentlyContinue
Write-Summary 'PHASE 4' $LogPath
exit $script:Fails
