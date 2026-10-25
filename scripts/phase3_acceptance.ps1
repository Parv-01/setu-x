<#
.SYNOPSIS
  SETU-X Phase 3 acceptance: placement, routing, provider-adapter boundary (PowerShell 7+, any OS).

.DESCRIPTION
  Builds and starts the 5-service stack and checks: config validation (good and deliberately
  broken), health of all services, the full automated test suite, simulated labels everywhere,
  honest adapter statuses, placement API validation, routing policies (round-robin, weighted,
  disabled primary), gateway behaviour when the controller is down, container hardening, and a
  clean shutdown. Writes docs/evidence/phase3_acceptance_<platform>.log. Exit code = failed checks.

.EXAMPLE
  pwsh -ExecutionPolicy Bypass -File scripts/phase3_acceptance.ps1
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
$platform = Write-Header 'SETU-X Phase 3 acceptance (PowerShell)'
$LogPath = "docs/evidence/phase3_acceptance_$platform.log"

function Order([string]$Sku = 'SKU-002', [int]$Qty = 1) {
    return Invoke-Http 'POST' "$Gw/api/orders" @{ sku = $Sku; quantity = $Qty; customer_tier = 'standard' }
}
function Gateway-Version { return (Invoke-Http 'GET' "$Gw/api/placement/current").Json.placement_version }
function Put-Placement($Placement) {
    $r = Invoke-Http 'PUT' "$Ctl/api/placement" $Placement
    if ($r.Code -eq 200) { $v = $r.Json.version; [void](Wait-Until { (Gateway-Version) -eq $v } 15) }
    return $r
}
function Reset-Placement {
    $r = Invoke-Http 'POST' "$Ctl/api/placement/reset"
    $v = $r.Json.version; [void](Wait-Until { (Gateway-Version) -eq $v } 15)
}
function Served([int]$N) { return @(1..$N | ForEach-Object { (Order).Json.inventory.instance }) }

Log '=== B1 docker compose config'
Record 'B1' ((Run 'docker compose config --quiet') -eq 0) 'compose configuration valid'

Log '=== B2 build images'
$buildCmd = if ($BuildCommand) { $BuildCommand } else { 'docker compose --profile test build' }
Record 'B2' ((Run $buildCmd) -eq 0) 'images built'

Log '=== B3 config validation: shipped config valid, deliberately broken config rejected'
$good = Run 'docker compose --profile test run --rm --no-deps tests python -m setu_x.config_check configs'
$badOut = (docker compose --profile test run --rm --no-deps tests python -m setu_x.config_check tests/fixtures/bad_config 2>&1 | Out-String)
$bad = $LASTEXITCODE; Log $badOut.TrimEnd()
$ok = $good -eq 0 -and $bad -eq 1 -and $badOut -like "*services.inventory.instances.1.env: unknown environment 'gcp-simualted'*"
Record 'B3' $ok "valid config exit 0; broken config exit $bad with field path in message"

Log '=== B4 start stack and check all 5 services healthy'
docker compose down --remove-orphans 2>&1 | Out-Null
$up = (Run 'docker compose up -d --wait --wait-timeout 120') -eq 0
Run 'docker compose ps' | Out-Null
$bad = @($Services | Where-Object { (Health-Of $_) -ne 'healthy' })
Record 'B4' ($up -and $bad.Count -eq 0) ($(if ($bad) { "unhealthy: $($bad -join ',')" } else { 'inventory-a, inventory-b, pricing, controller, gateway healthy' }))

Log '=== B5 automated tests (unit + contract + integration) in the test container'
Record 'B5' ((Run 'docker compose --profile test run --rm tests') -eq 0) 'unit + contract + integration tests passed'

Log '=== B6 end-to-end tests against the running stack'
Record 'B6' ((Run 'docker compose --profile test run --rm tests pytest -p no:cacheprovider -m e2e') -eq 0) 'e2e tests passed'

Log '=== B7 simulated environments labelled; adapters report honest statuses'
$envs = (Invoke-Http 'GET' "$Ctl/api/environments").Json
$adapters = (Invoke-Http 'GET' "$Ctl/api/adapters").Json.adapters
foreach ($e in $envs.environments) { Log ("env {0,-16} provider={1,-6} simulated={2} real_adapter={3}" -f $e.name, $e.provider, $e.simulated, $e.real_provider_adapter.status) }
foreach ($a in $adapters) { Log ("adapter {0,-13} status={1,-15} implemented={2}" -f $a.name, $a.status, (($a.capabilities.PSObject.Properties | Where-Object Value -eq 'implemented' | ForEach-Object Name) -join ',')) }
$simOk = (@($envs.environments | Where-Object { $_.simulated }).name | Sort-Object) -join ',' -eq 'aws-simulated,azure-simulated,gcp-simulated'
$localOk = @($envs.environments | Where-Object { -not $_.simulated }).name -eq 'local'
$statusMap = @{}; foreach ($a in $adapters) { $statusMap[$a.name] = $a.status }
$adOk = $statusMap['local-docker'] -eq 'local' -and $statusMap['aws'] -eq 'not_configured' -and $statusMap['azure'] -eq 'not_configured' -and $statusMap['gcp'] -eq 'not_configured'
$noneVerified = @($adapters | Where-Object { $_.status -in 'connected', 'deployed', 'verified' }).Count -eq 0
Record 'B7' ($simOk -and $localOk -and $adOk -and $noneVerified) 'aws/azure/gcp envs simulated; adapters local + not_configured; none connected/deployed/verified'

Log '=== B8 order response marks simulated environments'
$o = Order 'SKU-001' 2; Log "HTTP $($o.Code): $($o.Body)"
$j = $o.Json
$ok = $o.Code -eq 200 -and $j.status -eq 'confirmed' -and $j.inventory.instance -eq 'inventory-a' -and $j.inventory.simulated -eq $true -and `
      (($j.simulated_environments -join ',') -eq 'aws-simulated,azure-simulated') -and $j.simulation_notice -and $j.routing[0].reason -eq 'primary instance'
Record 'B8' $ok 'confirmed via inventory-a (primary); simulated_environments + notice present'

Log '=== B9 placement API rejects invalid changes and keeps the current version'
$cur = (Invoke-Http 'GET' "$Ctl/api/placement").Json
$p = $cur.placement | ConvertTo-Json -Depth 20 | ConvertFrom-Json -Depth 20
$p.services.inventory.instances[0].env = 'mars-simulated'
$r1 = Invoke-Http 'PUT' "$Ctl/api/placement" $p; Log "unknown env -> HTTP $($r1.Code): $($r1.Json.error.message)"
$p = $cur.placement | ConvertTo-Json -Depth 20 | ConvertFrom-Json -Depth 20
$p.services.pricing.instances[0].url = 'http://evil.example:8000'
$r2 = Invoke-Http 'PUT' "$Ctl/api/placement" $p; Log "new URL -> HTTP $($r2.Code): $($r2.Json.error.message)"
$after = (Invoke-Http 'GET' "$Ctl/api/placement").Json.version
$ok = $r1.Code -eq 422 -and $r1.Json.error.message -like "*services.inventory.instances.0.env: unknown environment 'mars-simulated'*" -and `
      $r2.Code -eq 422 -and $r2.Json.error.message -like '*must match configs/placement.yaml*' -and $after -eq $cur.version
Record 'B9' $ok "both rejected with 422 naming the problem; version still $after"

Log '=== B10 round-robin routing via the controller'
$p = $cur.placement | ConvertTo-Json -Depth 20 | ConvertFrom-Json -Depth 20
$p.services.inventory.routing = 'round-robin'
$r = Put-Placement $p; Log "PUT round-robin -> HTTP $($r.Code), version $($r.Json.version); gateway now on version $(Gateway-Version)"
$served = Served 4; Log "served: $($served -join ', ')"
$dec = (Invoke-Http 'GET' "$Gw/api/routing/decisions?limit=4").Json.decisions | Where-Object service -eq 'inventory'
Log ("latest decision reasons: " + (($dec | ForEach-Object reason) -join ' | '))
$ok = $r.Code -eq 200 -and ($served -join ',') -in 'inventory-a,inventory-b,inventory-a,inventory-b', 'inventory-b,inventory-a,inventory-b,inventory-a' -and `
      @($dec | Where-Object { $_.reason -like 'round-robin position*' }).Count -ge 1
Record 'B10' $ok "alternating: $($served -join ',')"

Log '=== B11 weighted routing 3:1'
$p = $cur.placement | ConvertTo-Json -Depth 20 | ConvertFrom-Json -Depth 20
$p.services.inventory.routing = 'weighted'; $p.services.inventory.instances[0].weight = 3; $p.services.inventory.instances[1].weight = 1
$r = Put-Placement $p
$served = Served 8
$counts = $served | Group-Object | ForEach-Object { "$($_.Name)=$($_.Count)" }
Log "served: $($served -join ', ')"
$ok = $r.Code -eq 200 -and @($served | Where-Object { $_ -eq 'inventory-a' }).Count -eq 6 -and @($served | Where-Object { $_ -eq 'inventory-b' }).Count -eq 2
Record 'B11' $ok "8 orders -> $($counts -join ', ') (expected 6/2)"

Log '=== B12 disabled primary routes to secondary; reset restores primary'
$p = $cur.placement | ConvertTo-Json -Depth 20 | ConvertFrom-Json -Depth 20
$p.services.inventory.instances[0].enabled = $false
$r = Put-Placement $p
$o = Order; $reason = $o.Json.routing[0].reason; Log "served by $($o.Json.inventory.instance): $reason"
Reset-Placement
$o2 = Order; Log "after reset served by $($o2.Json.inventory.instance) (gateway placement version $(Gateway-Version))"
$ok = $r.Code -eq 200 -and $o.Json.inventory.instance -eq 'inventory-b' -and $reason -like "primary 'inventory-a' disabled*" -and $o2.Json.inventory.instance -eq 'inventory-a'
Record 'B12' $ok 'disabled primary -> inventory-b with reason; reset -> inventory-a'

Log '=== B13 controller down: gateway keeps serving on the last valid placement'
Run 'docker compose stop controller' | Out-Null
$seen = Wait-Until { (Invoke-Http 'GET' "$Gw/health/ready").Json.info.controller_reachable -eq $false } 15
$info = (Invoke-Http 'GET' "$Gw/health/ready").Json.info
$o = Order; Log "controller stopped: order HTTP $($o.Code); info: $($info | ConvertTo-Json -Compress)"
Run 'docker compose start controller' | Out-Null; [void](Wait-Healthy 'controller' 60)
$back = Wait-Until { (Invoke-Http 'GET' "$Gw/health/ready").Json.info.controller_reachable -eq $true } 20
Record 'B13' ($seen -and $o.Code -eq 200 -and $back) "order HTTP $($o.Code) while controller down; gateway reported controller_reachable=false, then true after restart"

Log '=== B14 container hardening for all 5 services (docker inspect)'
$bad = @()
foreach ($s in $Services) {
    $i = (docker inspect (Container-Id $s) | ConvertFrom-Json)[0]; $hc = $i.HostConfig
    Log ("{0}: user={1} ro={2} capdrop={3} nanocpus={4} mem={5} secopt={6} privileged={7} mounts={8}" -f $s, $i.Config.User, $hc.ReadonlyRootfs, ($hc.CapDrop -join ','), $hc.NanoCpus, $hc.Memory, ($hc.SecurityOpt -join ','), $hc.Privileged, (($i.Mounts | ForEach-Object { "$($_.Destination):$(if ($_.RW) {'rw'} else {'ro'})" }) -join ','))
    $rwMounts = @($i.Mounts | Where-Object { $_.RW -and $_.Type -ne 'tmpfs' }).Count
    if (-not ($i.Config.User -eq '10001' -and $hc.ReadonlyRootfs -and ($hc.CapDrop -contains 'ALL') -and $hc.NanoCpus -eq 500000000 -and `
              $hc.Memory -eq 268435456 -and ($hc.SecurityOpt -contains 'no-new-privileges:true') -and -not $hc.Privileged -and $rwMounts -eq 0)) { $bad += $s }
}
foreach ($pair in @(@('gateway', 8000), @('controller', 8000))) {
    $b = (docker port (Container-Id $pair[0]) $pair[1] | Select-Object -First 1); Log "$($pair[0]) port: $b"
    if ($b -notlike '127.0.0.1:*') { $bad += "$($pair[0])-port" }
}
Record 'B14' ($bad.Count -eq 0) ($(if ($bad) { "mismatch: $($bad -join ',')" } else { 'non-root, read-only rootfs + read-only config mounts, cap_drop ALL, no-new-privileges, limits, ports on 127.0.0.1' }))

Log '=== B15 graceful stop (exit 0) and clean down'
Run 'docker compose stop' | Out-Null
$codes = @{}; foreach ($s in $Services) { $codes[$s] = [int](docker inspect --format '{{.State.ExitCode}}' (Container-Id $s -All)) }
Log ("exit codes: " + (($codes.GetEnumerator() | Sort-Object Name | ForEach-Object { "$($_.Name)=$($_.Value)" }) -join ' '))
Run 'docker compose down' | Out-Null
$left = @(docker compose ps -a -q | Where-Object { $_ }).Count
Record 'B15' ((@($codes.Values | Where-Object { $_ -ne 0 }).Count -eq 0) -and $left -eq 0) "all 5 exited 0; $left containers left after down"

Write-Summary 'PHASE 3' $LogPath
exit $script:Fails
