# Run Alice for a fixed number of hours, then write a handover report.
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\run_session.ps1 -Hours 6
param(
    [double]$Hours = 6,
    [string]$Alice = "",
    [string]$Config = "",
    [string]$DataDir = "",
    [string]$OutRoot = "",
    [int]$MaxRestarts = 10,
    [int]$HeartbeatMinutes = 10,
    [switch]$DryRun
)

$ErrorActionPreference = "Continue"
$repo = Split-Path $PSScriptRoot -Parent
if (-not $Alice) { $Alice = Join-Path $repo "dist\alice\alice.exe" }
$aliceDir = Split-Path $Alice -Parent
if (-not $Config) { $Config = Join-Path $aliceDir "config\alice.yaml" }
if (-not $DataDir) { $DataDir = Join-Path $env:LOCALAPPDATA "Alice" }
if (-not $OutRoot) { $OutRoot = Join-Path $DataDir "handover" }

$python = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = (Get-Command python.exe).Source }

$started = Get-Date
$end = $started.AddHours($Hours)
$stamp = $started.ToString("yyyy-MM-dd_HHmmss")
$session = Join-Path $OutRoot $stamp
$runLogs = Join-Path $session "run-logs"
$supervisorLog = Join-Path $session "supervisor.log"
$pytestLog = Join-Path $session "pytest.log"
$sessionJson = Join-Path $session "session.json"
New-Item -ItemType Directory -Path $runLogs -Force | Out-Null

function Write-Supervisor([string]$message) {
    $line = "{0} {1}" -f (Get-Date).ToString("yyyy-MM-dd HH:mm:ss"), $message
    Add-Content -Path $supervisorLog -Value $line -Encoding utf8
    Write-Host $line
}

# Keep Windows from sleeping while the session is active. This is a request,
# not a permanent power-plan change.
$sleepGuard = $false
try {
    Add-Type -Name AlicePower -Namespace Win32 -MemberDefinition @'
[DllImport("kernel32.dll")] public static extern uint SetThreadExecutionState(uint esFlags);
'@ -ErrorAction Stop
    $ES_CONTINUOUS = [uint32]"0x80000000"
    $ES_SYSTEM_REQUIRED = [uint32]"0x00000001"
    [Win32.AlicePower]::SetThreadExecutionState($ES_CONTINUOUS -bor $ES_SYSTEM_REQUIRED) | Out-Null
    $sleepGuard = $true
} catch {
    Write-Supervisor "sleep guard unavailable: $($_.Exception.Message)"
}

Write-Supervisor "session started: $started -> $end"
Write-Supervisor "alice=$Alice"
Write-Supervisor "config=$Config"
Write-Supervisor "data=$DataDir"

Write-Supervisor "ensuring the database is initialized"
& $Alice --config $Config --data-dir $DataDir init *> (Join-Path $session "init.txt")
if ($LASTEXITCODE -ne 0) {
    Write-Supervisor "init failed with exit code $LASTEXITCODE; see init.txt"
}
& $Alice --config $Config --data-dir $DataDir status *> (Join-Path $session "status-before.txt")

$runs = @()
$runIndex = 0
$stopReason = "time budget reached"

while ((Get-Date) -lt $end -and $runIndex -lt $MaxRestarts) {
    $runIndex++
    $runStart = Get-Date
    $outPath = Join-Path $runLogs ("run-{0}.out.txt" -f $runIndex)
    $errPath = Join-Path $runLogs ("run-{0}.err.txt" -f $runIndex)
    $arguments = @(
        "--config", "`"$Config`"",
        "--data-dir", "`"$DataDir`"",
        "run"
    )
    if ($DryRun) { $arguments += "--dry-run" }
    $heartbeat = Join-Path $DataDir "run.heartbeat"
    Remove-Item $heartbeat -Force -ErrorAction SilentlyContinue
    $process = Start-Process -FilePath $Alice -ArgumentList $arguments `
        -WorkingDirectory $aliceDir -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput $outPath -RedirectStandardError $errPath
    Write-Supervisor "run $runIndex started (pid $($process.Id))"

    $hung = $false
    while (-not $process.HasExited -and (Get-Date) -lt $end) {
        Start-Sleep -Seconds 15
        if (-not $process.HasExited -and (Test-Path $heartbeat)) {
            $age = (Get-Date) - (Get-Item $heartbeat).LastWriteTime
            if ($age.TotalMinutes -gt $HeartbeatMinutes) {
                Write-Supervisor (
                    "run $runIndex heartbeat stale for " +
                    "$([math]::Round($age.TotalMinutes, 1)) min; killing"
                )
                & taskkill.exe /PID $process.Id /T /F *> $null
                Start-Sleep -Seconds 2
                $hung = $true
                break
            }
        }
    }

    $process.Refresh()
    if ($hung) {
        $reason = "heartbeat timeout"
    } elseif ($process.HasExited) {
        $reason = "exited with code $($process.ExitCode)"
    } else {
        & taskkill.exe /PID $process.Id /T /F *> $null
        Start-Sleep -Seconds 2
        $reason = "stopped at session end"
    }
    $runEnd = Get-Date
    $runs += [pscustomobject]@{
        started = $runStart.ToString("o")
        ended = $runEnd.ToString("o")
        reason = $reason
        pid = $process.Id
    }
    Write-Supervisor "run $runIndex ended: $reason"

    if ((Get-Date) -lt $end) {
        Start-Sleep -Seconds 10
    }
}

if ($runIndex -ge $MaxRestarts -and (Get-Date) -lt $end) {
    $stopReason = "restart limit reached"
}

$ended = Get-Date
Write-Supervisor "session finishing: $stopReason"
& $Alice --config $Config --data-dir $DataDir status *> (Join-Path $session "status-after.txt")
& $Alice --config $Config --data-dir $DataDir backup *> (Join-Path $session "backup-after.txt")
& $Alice --config $Config --data-dir $DataDir report --write *> (Join-Path $session "report-after.txt")

$sessionData = [pscustomobject]@{
    started = $started.ToString("o")
    ended = $ended.ToString("o")
    hours = $Hours
    heartbeat_minutes = $HeartbeatMinutes
    stop_reason = $stopReason
    alice = $Alice
    config = $Config
    data_dir = $DataDir
    runs = $runs
}
$sessionData | ConvertTo-Json -Depth 5 | Set-Content -Path $sessionJson -Encoding utf8

Write-Supervisor "running pytest"
Push-Location $repo
try {
    & $python -m pytest *> $pytestLog
} finally {
    Pop-Location
}

$startIso = $started.ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
$endIso = $ended.ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
$handover = Join-Path $session "handover.md"
& $python (Join-Path $repo "scripts\session_report.py") `
    --db (Join-Path $DataDir "alice.db") `
    --out $handover `
    --start $startIso `
    --end $endIso `
    --label ("{0:0.##}-hour session" -f $Hours) `
    --supervisor $sessionJson `
    --pytest-log $pytestLog `
    --data-dir $DataDir

if ($sleepGuard) {
    try { [Win32.AlicePower]::SetThreadExecutionState([uint32]"0x80000000") | Out-Null } catch {}
}

Write-Supervisor "handover: $handover"
Write-Host "HANDOVER: $handover"
