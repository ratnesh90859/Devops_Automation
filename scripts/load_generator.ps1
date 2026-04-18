param(
    [ValidateSet("crash","slow","leak","normal")]
    [string]$Mode = "crash",
    [string]$AppUrl = "https://order-api-546580006264.asia-south1.run.app",
    [int]$NormalRps = 2,
    [int]$FaultRps = 3,
    [int]$CycleMs = 1000
)

$faultMap = @{ crash="/crash"; slow="/slow"; leak="/leak"; normal="/orders" }
$faultEndpoint = $faultMap[$Mode]
$normalEndpoints = @("/orders", "/health", "/")
$stats = @{ ok=0; errors=0; total=0; cycles=0 }
$startTime = Get-Date

Write-Host ""
Write-Host "Continuous Load Generator - Mode: $Mode" -ForegroundColor Cyan
Write-Host "Target : $AppUrl" -ForegroundColor Cyan
Write-Host "Fault  : $faultEndpoint  [$FaultRps per cycle]" -ForegroundColor Yellow
Write-Host "Normal : /orders,/health  [$NormalRps per cycle]" -ForegroundColor Green
Write-Host "Cycle  : every $CycleMs ms  |  Press Ctrl+C to stop" -ForegroundColor Cyan
Write-Host ""

function Invoke-Hit {
    param([string]$Url)
    try {
        $null = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 10 -ErrorAction Stop
        $script:stats.ok++
    } catch {
        $script:stats.errors++
    }
    $script:stats.total++
}

try {
    while ($true) {
        $stats.cycles++

        if ($Mode -ne "normal") {
            for ($i = 0; $i -lt $FaultRps; $i++) {
                Invoke-Hit -Url "$AppUrl$faultEndpoint"
            }
        }

        for ($i = 0; $i -lt $NormalRps; $i++) {
            $ep = $normalEndpoints[$i % $normalEndpoints.Length]
            Invoke-Hit -Url "$AppUrl$ep"
        }

        $elapsed  = [int](((Get-Date) - $startTime).TotalSeconds)
        $errPct   = if ($stats.total -gt 0) { [math]::Round($stats.errors * 100.0 / $stats.total, 1) } else { 0 }
        $rps      = if ($elapsed -gt 0) { [math]::Round($stats.total / $elapsed, 1) } else { 0 }
        $color    = if ($errPct -gt 20) { "Red" } elseif ($errPct -gt 5) { "Yellow" } else { "Green" }

        Write-Host "`r  [+$elapsed s] Cycles=$($stats.cycles)  Total=$($stats.total)  OK=$($stats.ok)  Errors=$($stats.errors)  ErrRate=$errPct%  RPS=$rps   " -NoNewline -ForegroundColor $color

        Start-Sleep -Milliseconds $CycleMs
    }
} finally {
    Write-Host ""
    Write-Host ""
    Write-Host "--- Final Summary ---" -ForegroundColor Cyan
    Write-Host "  Total    : $($stats.total)" -ForegroundColor White
    Write-Host "  OK       : $($stats.ok)" -ForegroundColor Green
    Write-Host "  Errors   : $($stats.errors)" -ForegroundColor Red
    $pct = if ($stats.total -gt 0) { [math]::Round($stats.errors * 100.0 / $stats.total, 1) } else { 0 }
    Write-Host "  ErrRate  : $pct%" -ForegroundColor Yellow
    Write-Host "  Duration : $([int](((Get-Date) - $startTime).TotalSeconds))s" -ForegroundColor White
}
