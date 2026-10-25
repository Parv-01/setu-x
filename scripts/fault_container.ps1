<#
.SYNOPSIS
  Stop an allow-listed SETU-X container for a bounded time, then start it again (PowerShell 7+).

.DESCRIPTION
  Models process/instance failure (the container is really stopped), unlike the application-level
  faults injected through the controller (POST /api/faults). Runs on the host because the services
  have no Docker API access by design. Prints UTC timestamps for stop, start and healthy, so the
  window can be matched against experiment observations.

  Allowed services: inventory-a, inventory-b, pricing.   Duration: 1..300 seconds.

.EXAMPLE
  pwsh -File scripts/fault_container.ps1 -Service inventory-a -Seconds 20
#>
param(
    [Parameter(Mandatory = $true)][ValidateSet('inventory-a', 'inventory-b', 'pricing')][string]$Service,
    [Parameter(Mandatory = $true)][ValidateRange(1, 300)][int]$Seconds,
    [switch]$NoWait
)
$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')
function Now { (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ss.fffZ') }

$event = [ordered]@{ type = 'container_fault'; service = $Service; planned_seconds = $Seconds }
$event.stopped_at = Now
docker compose stop $Service | Out-Null
Write-Host "$($event.stopped_at) stopped $Service for $Seconds s"
if ($NoWait) { $event | ConvertTo-Json -Compress; exit 0 }
Start-Sleep -Seconds $Seconds
$event.started_at = Now
docker compose start $Service | Out-Null
Write-Host "$($event.started_at) started $Service"
$deadline = (Get-Date).AddSeconds(90)
do {
    Start-Sleep -Milliseconds 250
    $cid = docker compose ps -q $Service
    $health = if ($cid) { docker inspect --format '{{.State.Health.Status}}' $cid } else { 'missing' }
} while ($health -ne 'healthy' -and (Get-Date) -lt $deadline)
$event.healthy_at = if ($health -eq 'healthy') { Now } else { $null }
Write-Host "$($event.healthy_at) $Service health: $health"
$event | ConvertTo-Json -Compress
exit ($(if ($health -eq 'healthy') { 0 } else { 1 }))
