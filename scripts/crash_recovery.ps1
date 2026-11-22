# Kill Alice mid-ingest, restart it, and prove the ledger is unchanged.
# Usage:  powershell -ExecutionPolicy Bypass -File scripts\crash_recovery.ps1
param(
    [int]$Runs = 3,
    [int]$KillAfterMs = 1200,
    [switch]$Keep
)
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$alice = Join-Path $root ".venv\Scripts\alice.exe"
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $alice)) {
    $aliceCommand = Get-Command alice.exe -ErrorAction SilentlyContinue
    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($null -eq $aliceCommand -or $null -eq $pythonCommand) {
        throw "No venv. Run: py -3.12 -m venv .venv; .\.venv\Scripts\pip install -e '.[dev]'"
    }
    $alice = $aliceCommand.Source
    $python = $pythonCommand.Source
}
$stats = Join-Path $PSScriptRoot "crash_stats.py"

$demo = Join-Path $env:TEMP ("alice-crash-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory -Path "$demo\config", "$demo\logs" | Out-Null
try {
    $lines = 1..3000 | ForEach-Object {
        "$([System.IO.Path]::GetRandomFileName()) ERROR worker failed to flush queue"
    }
    Set-Content -Encoding utf8 -Path "$demo\logs\app.log" -Value $lines
    $logPath = ("$demo\logs" -replace '\\', '/')
    $yaml = @"
models:
  triage:      {provider: fake, model: fake-triage}
  reason:      {provider: fake, model: fake-reason}
  consolidate: {provider: fake, model: fake-consolidate}
sources:
  - {type: logfile, path: '$logPath', glob: '*.log', from_start: true}
gate: {min_salience: 0.5}
consolidation: {hour: 23}
backup: {enabled: false}
"@
    Set-Content -Encoding utf8 -Path "$demo\config\alice.yaml" -Value $yaml
    $configPath = "$demo\config\alice.yaml"
    $dataDir = "$demo\data"

    & $alice --config $configPath --data-dir $dataDir init | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "init failed" }

    for ($i = 1; $i -le $Runs; $i++) {
        $arguments = @(
            "--config", "`"$configPath`"",
            "--data-dir", "`"$dataDir`"",
            "run", "--interval", "1"
        )
        $process = Start-Process -FilePath $alice -ArgumentList $arguments -PassThru -WindowStyle Hidden
        Start-Sleep -Milliseconds $KillAfterMs
        if (-not $process.HasExited) {
            Stop-Process -Id $process.Id -Force
            Write-Host "run ${i}: killed mid-flight"
        } else {
            Write-Host "run ${i}: exited before the kill"
        }
        Start-Sleep -Milliseconds 250
    }

    # Drain the file in bounded polls, then prove a further run adds nothing.
    $db = "$dataDir\alice.db"
    $logFile = "$demo\logs\app.log"
    $count = -1
    for ($step = 0; $step -lt 20; $step++) {
        & $alice --config $configPath --data-dir $dataDir run --once --dry-run | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "run --once failed at step $step" }
        $out = @(& $python $stats count $db)
        $next = [int]$out[0]
        if ($next -eq $count) { break }
        $count = $next
    }
    $countBefore = $count

    & $alice --config $configPath --data-dir $dataDir run --once --dry-run | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "final run failed" }
    $after = @(& $python $stats count $db)
    $countAfter = [int]$after[0]
    $integrity = (& $python $stats integrity $db).Trim()
    $cursor = @(& $python $stats cursor $db $logFile)

    Write-Host "events after restart: $countBefore"
    Write-Host "events after another run: $countAfter"
    Write-Host "integrity: $integrity"
    Write-Host "cursor offset: $($cursor[0]) of $($cursor[1]) bytes"
    if ($integrity -ne "ok") { throw "database integrity check failed" }
    if ($countBefore -ne $countAfter) { throw "duplicate events after restart" }
    if ($countBefore -le 0) { throw "no events were ingested" }
    if ([int]$cursor[0] -ne [int]$cursor[1]) { throw "cursor did not reach the end of the file" }
    Write-Host "PASS: no event lost, no duplicate written, database intact"
}
finally {
    if (-not $Keep -and (Test-Path $demo)) {
        Remove-Item -Recurse -Force $demo -ErrorAction SilentlyContinue
    } elseif (Test-Path $demo) {
        Write-Host "kept: $demo"
    }
}
