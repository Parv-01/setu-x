<#
.SYNOPSIS
  SETU-X Phase 2 acceptance run (PowerShell 7+, Windows/Linux/macOS). Same checks as phase2_acceptance.sh.

.DESCRIPTION
  Builds and starts the Docker Compose stack, checks health, the order path, the automated tests,
  failure handling (stopped and paused dependency, pricing down), container hardening, graceful
  shutdown and clean restart. Prints PASS/FAIL per check and writes the full log to
  docs/evidence/phase2_acceptance_<platform>.log. Exit code = number of failed checks.

.EXAMPLE
  pwsh -ExecutionPolicy Bypass -File scripts\phase2_acceptance.ps1
#>
param(
    [string]$BuildCommand = "",          # optional override of the build step
    [int]$GatewayPort = 8080
)

$ErrorActionPreference = 'Continue'
Set-Location (Join-Path $PSScriptRoot '..')
$Gw = "http://127.0.0.1:$GatewayPort"
$EvidenceDir = 'docs/evidence'
New-Item -ItemType Directory -Force -Path $EvidenceDir | Out-Null
$Platform = if ($IsWindows) { 'windows' } elseif ($IsMacOS) { 'macos' } else { 'linux' }
$LogPath = Join-Path $EvidenceDir "phase2_acceptance_$Platform.log"
$script:Lines = [System.Collections.Generic.List[string]]::new()
$script:Results = [System.Collections.Generic.List[string]]::new()
$script:Fails = 0
$OrderBody = '{"sku":"SKU-001","quantity":2,"customer_tier":"standard"}'

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
    $line = '{0,-4} {1,-4} {2}' -f $Id, $status, $Detail
    $script:Results.Add($line)
    Log ">>> $Id $status - $Detail"
}
function Send-Order {
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $r = Invoke-WebRequest -Method Post -Uri "$Gw/api/orders" -ContentType 'application/json' `
            -Body $OrderBody -SkipHttpErrorCheck -TimeoutSec 15 -NoProxy
        $sw.Stop()
        return [pscustomobject]@{ Code = [int]$r.StatusCode; Body = [string]$r.Content; Seconds = $sw.Elapsed.TotalSeconds }
    } catch {
        $sw.Stop()
        return [pscustomobject]@{ Code = 0; Body = $_.Exception.Message; Seconds = $sw.Elapsed.TotalSeconds }
    }
}
function Get-Json([string]$Url) {
    try { return Invoke-RestMethod -Uri $Url -TimeoutSec 5 -NoProxy } catch { return $null }
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
function Wait-Healthy([string]$Service, [int]$TimeoutSec = 90) {
    for ($i = 0; $i -lt $TimeoutSec; $i++) { if ((Health-Of $Service) -eq 'healthy') { return $true }; Start-Sleep 1 }
    return $false
}
function Fmt([double]$s) { return ('{0:N3}' -f $s) }

Log 'SETU-X Phase 2 acceptance (PowerShell)'
Log ("date_utc: " + (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'))
$commit = (git rev-parse --short HEAD 2>$null); if (-not $commit) { $commit = 'unknown' }
Log "git_commit: $commit"
Log ("powershell: " + $PSVersionTable.PSVersion.ToString() + " on $Platform")
Log ("docker: " + (docker version --format 'client {{.Client.Version}} / server {{.Server.Version}}' 2>&1 | Out-String).Trim())
Log ("compose: " + (docker compose version --short 2>&1 | Out-String).Trim())
Log ''

Log '=== A1 docker compose config'
Record 'A1' ((Run 'docker compose config --quiet') -eq 0) 'compose configuration valid'

Log '=== A2 build images'
$buildCmd = if ($BuildCommand) { $BuildCommand } else { 'docker compose --profile test build' }
Record 'A2' ((Run $buildCmd) -eq 0) 'images built'

Log '=== A3 start stack (up -d --wait)'
docker compose down --remove-orphans 2>&1 | Out-Null
$ok = (Run 'docker compose up -d --wait --wait-timeout 120') -eq 0
Run 'docker compose ps' | Out-Null
Record 'A3' $ok 'stack started, compose reports healthy'

Log '=== A4 health status of every service'
$bad = @()
foreach ($s in 'inventory-a', 'pricing', 'gateway') { $h = Health-Of $s; Log "${s}: $h"; if ($h -ne 'healthy') { $bad += $s } }
Record 'A4' ($bad.Count -eq 0) ($(if ($bad) { "unhealthy: $($bad -join ',')" } else { 'inventory-a, pricing, gateway healthy' }))

Log '=== A5 health endpoints return documented responses'
$live = Get-Json "$Gw/health/live"; $ready = Get-Json "$Gw/health/ready"
$probe = "import urllib.request;print(urllib.request.urlopen('http://{0}:8000/health/ready',timeout=2).read().decode())"
$inv = (docker compose exec -T gateway python -c ($probe -f 'inventory-a') 2>&1 | Out-String) | ConvertFrom-Json -ErrorAction SilentlyContinue
$pri = (docker compose exec -T gateway python -c ($probe -f 'pricing') 2>&1 | Out-String) | ConvertFrom-Json -ErrorAction SilentlyContinue
Log ("gateway live:  " + ($live | ConvertTo-Json -Compress)); Log ("gateway ready: " + ($ready | ConvertTo-Json -Compress -Depth 4))
Log ("inventory-a ready: " + ($inv | ConvertTo-Json -Compress -Depth 4)); Log ("pricing ready: " + ($pri | ConvertTo-Json -Compress -Depth 4))
$ok = $live.status -eq 'alive' -and $ready.status -eq 'ready' -and $ready.checks.http_client -eq 'ok' -and `
      $inv.status -eq 'ready' -and $inv.env -eq 'aws-simulated' -and $pri.status -eq 'ready' -and $pri.env -eq 'azure-simulated'
Record 'A5' $ok 'live/ready bodies match documented shape for all services'

Log '=== A6 request through the gateway produces the expected result'
$o = Send-Order; Log "HTTP $($o.Code) in $(Fmt $o.Seconds)s: $($o.Body)"
$j = $o.Body | ConvertFrom-Json -ErrorAction SilentlyContinue
$ok = $o.Code -eq 200 -and $j.status -eq 'confirmed' -and $j.inventory.available -eq 8 -and $j.pricing.total_cents -eq 11000 -and $j.pricing.price_source -eq 'pricing-service'
Record 'A6' $ok 'confirmed; available=8, total_cents=11000 (golden values)'

Log '=== A7 automated tests (unit + integration) in the test container'
Record 'A7' ((Run 'docker compose --profile test run --rm tests') -eq 0) 'unit + integration tests passed'

Log '=== A8 end-to-end tests against the running stack'
Record 'A8' ((Run 'docker compose --profile test run --rm tests pytest -p no:cacheprovider -m e2e') -eq 0) 'e2e tests passed'

Log '=== A9 stopped dependency -> bounded 503, then recovery'
Run 'docker compose stop inventory-a' | Out-Null
$o = Send-Order; Log "inventory stopped: HTTP $($o.Code) in $(Fmt $o.Seconds)s: $($o.Body)"
$j = $o.Body | ConvertFrom-Json -ErrorAction SilentlyContinue
$ok9 = $o.Code -eq 503 -and $j.error.code -eq 'dependency_unavailable' -and $o.Seconds -lt 3.5
Run 'docker compose start inventory-a' | Out-Null; Wait-Healthy 'inventory-a' | Out-Null
$o2 = Send-Order; Log "after restart: HTTP $($o2.Code) in $(Fmt $o2.Seconds)s"
Record 'A9' ($ok9 -and $o2.Code -eq 200) "503 dependency_unavailable in $(Fmt $o.Seconds)s (bound 3.5s); recovered to HTTP $($o2.Code)"

Log '=== A10 unresponsive (paused) dependency -> 503 cut by timeout, no hang'
Run 'docker compose pause inventory-a' | Out-Null
$o = Send-Order; Log "inventory paused: HTTP $($o.Code) in $(Fmt $o.Seconds)s: $($o.Body)"
Run 'docker compose unpause inventory-a' | Out-Null
$ok = $o.Code -eq 503 -and $o.Body -like '*timeout*' -and $o.Seconds -ge 0.8 -and $o.Seconds -lt 1.6
Record 'A10' $ok "503 (timeout) in $(Fmt $o.Seconds)s; expected ~0.8s per-attempt timeout (bound < 1.6s)"

Log '=== A11 pricing stopped -> degraded order with flagged list price'
Run 'docker compose stop pricing' | Out-Null
$o = Send-Order; Log "pricing stopped: HTTP $($o.Code) in $(Fmt $o.Seconds)s: $($o.Body)"
Run 'docker compose start pricing' | Out-Null; Wait-Healthy 'pricing' | Out-Null
$j = $o.Body | ConvertFrom-Json -ErrorAction SilentlyContinue
Record 'A11' ($o.Code -eq 200 -and $j.status -eq 'degraded' -and $j.pricing.price_source -eq 'fallback-list-price') "degraded with fallback-list-price in $(Fmt $o.Seconds)s"

Log '=== A12 container hardening and limits (read back from docker inspect)'
$bad = @()
foreach ($s in 'inventory-a', 'pricing', 'gateway') {
    $i = (docker inspect (Container-Id $s) | ConvertFrom-Json)[0]
    $hc = $i.HostConfig
    Log ("{0}: user={1} ro={2} capdrop={3} nanocpus={4} mem={5} secopt={6} privileged={7}" -f $s, $i.Config.User, $hc.ReadonlyRootfs, ($hc.CapDrop -join ','), $hc.NanoCpus, $hc.Memory, ($hc.SecurityOpt -join ','), $hc.Privileged)
    $okS = $i.Config.User -eq '10001' -and $hc.ReadonlyRootfs -and ($hc.CapDrop -contains 'ALL') -and $hc.NanoCpus -eq 500000000 -and `
           $hc.Memory -eq 268435456 -and ($hc.SecurityOpt -contains 'no-new-privileges:true') -and -not $hc.Privileged
    if (-not $okS) { $bad += $s }
}
$binding = (docker port (Container-Id 'gateway') 8000 | Select-Object -First 1); Log "gateway port binding: $binding"
if ($binding -notlike '127.0.0.1:*') { $bad += 'gateway-port' }
Record 'A12' ($bad.Count -eq 0) ($(if ($bad) { "hardening mismatch: $($bad -join ',')" } else { 'non-root, read-only rootfs, cap_drop ALL, no-new-privileges, 0.5 CPU / 256MiB, port on 127.0.0.1' }))

Log '=== A13 graceful stop (SIGTERM -> exit 0) and clean restart'
Run 'docker compose stop' | Out-Null
$codes = @{}
foreach ($s in 'inventory-a', 'pricing', 'gateway') { $codes[$s] = [int](docker inspect --format '{{.State.ExitCode}}' (Container-Id $s -All)) }
Log ("exit codes: " + (($codes.GetEnumerator() | Sort-Object Name | ForEach-Object { "$($_.Name)=$($_.Value)" }) -join ' '))
docker compose up -d --wait --wait-timeout 120 2>&1 | Out-Null
$o = Send-Order
$ok = ($codes.Values | Where-Object { $_ -ne 0 }).Count -eq 0 -and $o.Code -eq 200
Record 'A13' $ok "all services exited 0 on SIGTERM; restarted; order HTTP $($o.Code)"

Log '=== A14 down removes everything; up again from scratch works'
Run 'docker compose down' | Out-Null
$left = @(docker compose ps -a -q | Where-Object { $_ }).Count
docker compose up -d --wait --wait-timeout 120 2>&1 | Out-Null
$o = Send-Order
Run 'docker compose down' | Out-Null
Record 'A14' ($left -eq 0 -and $o.Code -eq 200) "down left $left containers; fresh up served HTTP $($o.Code); down again"

Log ''
Log '================ SUMMARY ================'
foreach ($r in $script:Results) { Log $r }
Log "checks: $($script:Results.Count)  failed: $script:Fails"
Log ($(if ($script:Fails -eq 0) { 'PHASE 2 ACCEPTANCE: PASS' } else { 'PHASE 2 ACCEPTANCE: FAIL' }))
$script:Lines | Set-Content -Path $LogPath -Encoding utf8
Write-Host "log written to $LogPath"
exit $script:Fails
