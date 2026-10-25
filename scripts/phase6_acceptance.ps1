<#
.SYNOPSIS
  SETU-X Phase 6 acceptance: dashboard served safely, every dashboard API works, and the dashboard
  driven in a real browser (Chromium via Playwright) in local AND simulated mode (PowerShell 7+).

.DESCRIPTION
  Runs against the real Docker Compose stack. The browser checks (E11, E13) run the browser-tests
  container (compose profile "browser", ~3.5 GB image, downloaded once). With -SkipBrowser they are
  recorded as SKIP (not executed) and the verdict is INCOMPLETE, never PASS.
  Writes docs/evidence/phase6_acceptance_<platform>.log. Exit code = failed + skipped checks.

.EXAMPLE
  pwsh -ExecutionPolicy Bypass -File scripts/phase6_acceptance.ps1
#>
param(
    [string]$BuildCommand = "",
    [int]$GatewayPort = 8080,
    [int]$ControllerPort = 8090,
    [switch]$SkipBrowser
)

$ErrorActionPreference = 'Continue'
Set-Location (Join-Path $PSScriptRoot '..')
. (Join-Path $PSScriptRoot 'acceptance/common.ps1')

$Ctl = "http://127.0.0.1:$ControllerPort"
$Services = 'inventory-a', 'inventory-b', 'pricing', 'controller', 'gateway', 'runner'
New-Item -ItemType Directory -Force -Path 'docs/evidence' | Out-Null
$platform = Write-Header 'SETU-X Phase 6 acceptance (PowerShell)'
$LogPath = "docs/evidence/phase6_acceptance_$platform.log"

function Start-Stack([string]$Mode) {
    docker compose down --remove-orphans 2>&1 | Out-Null
    $env:SETU_MODE = $Mode
    $up = (Run 'docker compose up -d --wait --wait-timeout 150') -eq 0
    $bad = @($Services | Where-Object { (Health-Of $_) -ne 'healthy' })
    return [pscustomobject]@{ Ok = ($up -and $bad.Count -eq 0); Bad = $bad }
}

function Raw([string]$Method, [string]$Url, [hashtable]$Headers = @{}, [string]$Body = $null) {
    # Like Invoke-Http, but with custom headers and no redirect following; returns code + headers.
    $p = @{ Method = $Method; Uri = $Url; Headers = $Headers; SkipHttpErrorCheck = $true; NoProxy = $true
            MaximumRedirection = 0; TimeoutSec = 15; ErrorAction = 'SilentlyContinue' }
    if ($Body) { $p.Body = $Body; $p.ContentType = 'application/json' }
    try { $r = Invoke-WebRequest @p } catch { return [pscustomobject]@{ Code = 0; Headers = @{}; Body = $_.Exception.Message } }
    return [pscustomobject]@{ Code = [int]$r.StatusCode; Headers = $r.Headers; Body = [string]$r.Content }
}

function Header($Resp, [string]$Name) { $v = $Resp.Headers[$Name]; if ($v -is [array]) { return $v[0] } return [string]$v }

Log '=== E1 docker compose config'
Record 'E1' ((Run 'docker compose config --quiet') -eq 0) 'compose configuration valid'

Log '=== E2 build images (services, tests, and browser tests unless -SkipBrowser)'
$buildCmd = if ($BuildCommand) { $BuildCommand } elseif ($SkipBrowser) { 'docker compose --profile test build' } else { 'docker compose --profile test --profile browser build' }
Record 'E2' ((Run $buildCmd) -eq 0) 'images built'

Log '=== E3 start stack (local mode): 6 services healthy'
$st = Start-Stack 'local'
Record 'E3' $st.Ok ($(if ($st.Bad) { "unhealthy: $($st.Bad -join ',')" } else { 'all 6 services healthy' }))

Log '=== E4 automated tests (unit + contract + integration, includes dashboard backend tests)'
Record 'E4' ((Run 'docker compose --profile test run --rm tests') -eq 0) 'unit + contract + integration tests passed'

Log '=== E5 end-to-end tests'
Record 'E5' ((Run 'docker compose --profile test run --rm tests pytest -p no:cacheprovider -m e2e') -eq 0) 'e2e tests passed'

Log '=== E6 lint'
Record 'E6' ((Run 'docker compose --profile test run --rm tests ruff check --no-cache src tests') -eq 0) 'ruff clean'

Log '=== E7 dashboard served with a strict Content-Security-Policy; assets load'
$root = Raw 'GET' "$Ctl/"
$page = Raw 'GET' "$Ctl/dashboard/"
$csp = Header $page 'Content-Security-Policy'
$assets = @('css/app.css', 'js/app.js', 'js/api.js', 'js/charts.js', 'js/stats.js') | ForEach-Object {
    $a = Raw 'GET' "$Ctl/dashboard/$_"; [pscustomobject]@{ Path = $_; Code = $a.Code; Type = (Header $a 'Content-Type') } }
Log "GET / -> $($root.Code) $(Header $root 'Location')"
Log "GET /dashboard/ -> $($page.Code); CSP: $csp"
foreach ($a in $assets) { Log ("  {0,-14} {1} {2}" -f $a.Path, $a.Code, $a.Type) }
$assetsOk = @($assets | Where-Object { $_.Code -ne 200 }).Count -eq 0 -and @($assets | Where-Object { $_.Path -like '*.js' -and $_.Type -notmatch 'javascript' }).Count -eq 0
Record 'E7' ($root.Code -eq 307 -and (Header $root 'Location') -eq '/dashboard/' -and $page.Code -eq 200 -and $csp -match "script-src 'self'" -and $csp -notmatch 'unsafe-inline' -and $assetsOk) 'redirect to /dashboard/, CSP without unsafe-inline, 5 assets served with correct types'

Log '=== E8 control surface refuses other web origins and foreign Host headers'
$evil = Raw 'POST' "$Ctl/api/faults" @{ Origin = 'http://evil.example' } '{"target":"inventory-a","mode":"error","duration_s":30}'
$active = @((Invoke-Http 'GET' "$Ctl/api/faults").Json.faults | Where-Object { $_.active }).Count
$rebind = Raw 'GET' "$Ctl/api/placement" @{ Host = 'attacker.example' }
$local = Raw 'POST' "$Ctl/api/placement/reset" @{ Origin = "http://127.0.0.1:$ControllerPort" }
Log "POST /api/faults with Origin http://evil.example -> $($evil.Code) $($evil.Body); active faults afterwards: $active"
Log "GET with Host attacker.example -> $($rebind.Code); POST reset from the dashboard's own origin -> $($local.Code)"
Record 'E8' ($evil.Code -eq 403 -and $active -eq 0 -and $rebind.Code -eq 400 -and $local.Code -eq 200) 'foreign origin 403 (no fault set), DNS-rebinding Host 400, same-origin request accepted'

Log '=== E9 every API the dashboard uses answers'
$paths = '/api/environments', '/api/services', '/api/placement', '/api/faults', '/api/scenarios', '/api/experiments',
         '/api/gateway/routing/decisions', '/api/gateway/resilience/events', '/api/gateway/resilience/breakers',
         '/api/gateway/placement/current', '/api/gateway/dependencies'
$bad9 = @()
foreach ($p in $paths) { $r = Invoke-Http 'GET' "$Ctl$p"; Log ("  {0,-34} HTTP {1}" -f $p, $r.Code); if ($r.Code -ne 200 -or $null -eq $r.Json) { $bad9 += $p } }
$order = Invoke-Http 'POST' "$Ctl/api/gateway/orders" @{ sku = 'SKU-002'; quantity = 1 }
$svc = @((Invoke-Http 'GET' "$Ctl/api/services").Json.services | ForEach-Object instance)
Log "test order via controller: HTTP $($order.Code) $($order.Json.status); /api/services lists: $($svc -join ', ')"
Record 'E9' ($bad9.Count -eq 0 -and $order.Code -eq 200 -and $order.Json.status -eq 'confirmed' -and 'runner' -in $svc) "$($paths.Count) read endpoints 200 JSON; test order confirmed; runner listed in service health"

Log '=== E10 experiment overrides are bounded'
$o1 = Invoke-Http 'POST' "$Ctl/api/experiments" @{ scenario = 'smoke'; overrides = @{ rate_rps = 500 } }
$o2 = Invoke-Http 'POST' "$Ctl/api/experiments" @{ scenario = 'smoke'; overrides = @{ links = @{ 'local--aws-simulated' = 10 } } }
$o3 = Invoke-Http 'POST' "$Ctl/api/experiments" @{ scenario = 'smoke'; overrides = @{ duration_s = 1 } }
foreach ($x in $o1, $o2, $o3) { Log "HTTP $($x.Code): $($x.Json.error.message)" }
Record 'E10' ($o1.Code -eq 422 -and $o2.Code -eq 422 -and $o3.Code -eq 422) 'rate > 100, link override on a local scenario, window too short for the scheduled fault: all 422'

if ($SkipBrowser) {
    Skip 'E11' 'browser tests, local mode (-SkipBrowser)'
    Skip 'E12' 'records of the dashboard-started experiment (needs E11)'
    Skip 'E13' 'browser tests, simulated mode (-SkipBrowser)'
} else {
    Log '=== E11 dashboard in a real browser (Chromium), local mode'
    Record 'E11' ((Run 'docker compose --profile browser run --rm browser-tests') -eq 0) '12 browser tests: controls send the right requests and change the backend, errors displayed, empty states, one experiment run end to end, comparison'

    Log '=== E12 the experiment started from the dashboard left valid records'
    $exp = @((Invoke-Http 'GET' "$Ctl/api/experiments").Json.experiments | Where-Object { $_.label -eq 'browser test' } | Select-Object -First 1)
    $ok13 = $false
    if ($exp) {
        $id = $exp[0].experiment_id
        $m = (Invoke-Http 'GET' "$Ctl/api/experiments/$id/files/manifest.json").Json
        $s = (Invoke-Http 'GET' "$Ctl/api/experiments/$id/files/summary.json").Json
        $csv = Invoke-Http 'GET' "$Ctl/api/experiments/$id/files/observations.csv"
        $rows = ($csv.Body -split "`n" | Where-Object { $_.Trim() }).Count - 1
        Log "$id status=$($s.status) validation=$($s.validation.ok) overrides=$($m.overrides | ConvertTo-Json -Compress) csv rows=$rows summary rows=$($s.validation.rows_csv)"
        $ok13 = $s.status -eq 'completed' -and $s.validation.ok -and $m.overrides.runs -eq 1 -and $rows -eq $s.validation.rows_csv
    } else { Log 'no experiment labelled "browser test" found' }
    Record 'E12' $ok13 'completed, records validated, overrides recorded in the manifest, CSV row count matches the summary'

    Log '=== E13 dashboard in a real browser, simulated mode'
    $st2 = Start-Stack 'simulated'
    $mode = (Invoke-Http 'GET' "$Ctl/api/environments").Json.mode
    $code12 = Run 'docker compose --profile browser run --rm browser-tests'
    Record 'E13' ($st2.Ok -and $mode -eq 'simulated' -and $code12 -eq 0) "stack mode=$mode; same 12 browser tests incl. configured link delays shown as configured"
}

Log '=== E14 dashboard is self-contained (no CDN or external URLs)'
$ext = @(Get-ChildItem dashboard -Recurse -File -Include *.html, *.js, *.css | Where-Object { $_.FullName -notmatch 'tests' } |
         Select-String -Pattern 'https?://(?!127\.0\.0\.1|localhost|www\.w3\.org/2000/svg)' | ForEach-Object { "$($_.Filename):$($_.LineNumber)" })
foreach ($e in $ext) { Log "external reference: $e" }
Record 'E14' ($ext.Count -eq 0) 'no external script, style or font URLs in the dashboard'

Log '=== E15 hardening of all 6 services, graceful stop, clean down'
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
Record 'E15' ($bad.Count -eq 0 -and @($codes.Values | Where-Object { $_ -ne 0 }).Count -eq 0 -and $left -eq 0) "hardening intact; all 6 exited 0; $left containers left"

Remove-Item Env:SETU_MODE -ErrorAction SilentlyContinue
Write-Summary 'PHASE 6' $LogPath
exit ($script:Fails + $script:Skips)
