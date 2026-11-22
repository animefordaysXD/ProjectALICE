# Stress Alice for a fixed window: synthetic log flood, concurrent reads,
# periodic backups/reports, and deliberate process kills.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\stress_session.ps1 -Hours 6
#   powershell -ExecutionPolicy Bypass -File scripts\stress_session.ps1 -Hours 0.05 -DryRun
param(
    [double]$Hours = 6,
    [string]$Base = "",
    [string]$Alice = "",
    [int]$KillEveryMinutes = 75,
    [switch]$DryRun
)

$ErrorActionPreference = "Continue"
$repo = Split-Path $PSScriptRoot -Parent
$stamp = Get-Date -Format "yyyy-MM-dd_HHmmss"
if (-not $Base) { $Base = Join-Path $env:LOCALAPPDATA "Alice\stress" }
$prefix = Join-Path $Base $stamp
$configRoot = "$prefix-config"
$configDir = Join-Path $configRoot "config"
$configPath = Join-Path $configDir "alice.yaml"
$logRoot = "$prefix-logs"
$dataDir = "$prefix-data"
$outRoot = "$prefix-handover"
$messages = "$prefix-messages.txt"
$metrics = "$prefix-metrics.csv"
$controlLog = "$prefix-control.log"
$generatorOut = "$prefix-generator.out.txt"
$generatorErr = "$prefix-generator.err.txt"
$supervisorOut = "$prefix-supervisor.out.txt"
$supervisorErr = "$prefix-supervisor.err.txt"
$summary = "$prefix-stress-summary.md"

$python = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = (Get-Command python.exe).Source }
if (-not $Alice) { $Alice = Join-Path $repo "dist\alice\alice.exe" }

New-Item -ItemType Directory -Path $prefix, $configDir, $logRoot, $dataDir, $outRoot -Force | Out-Null

function Write-Control([string]$message) {
    $line = "{0} {1}" -f (Get-Date).ToString("yyyy-MM-dd HH:mm:ss"), $message
    Add-Content -Path $controlLog -Value $line -Encoding utf8
    Write-Host $line
}

$repoPosix = $repo -replace '\\', '/'
$logPosix = $logRoot -replace '\\', '/'
$messagesPosix = $messages -replace '\\', '/'
$aliceParentPosix = (Join-Path $env:LOCALAPPDATA "Alice") -replace '\\', '/'

$yaml = @"
models:
  triage:      {provider: opencode, model: opencode-go/deepseek-v4.1-flash}
  reason:      {provider: opencode, model: opencode-go/deepseek-v4-pro}
  heavy:       {provider: opencode, model: opencode-go/kimi-k3.1-pro}
  consolidate: {provider: opencode, model: opencode-go/glm-5.3-flash, batch: true}

budget:
  soft_daily_usd: 1.00
  hard_daily_usd: 2.50
  max_escalations_per_hour: 6
  max_heavy_per_day: 3

triage:
  interval_minutes: 1
  max_batch_tokens: 6000

consolidation:
  hour: 3
  max_events: 500
  max_tokens: 6000

memory:
  core_memory_file: './memory/core.json'
  hard_max_tokens: 2000
  task_ttl_hours: 48
  recall_budget_tokens: 1000
  recall_k: 5
  embedding:
    model: minishlab/potion-multilingual-128M
    dimension: 256

plugins:
  enabled: []

sources:
  - type: logscan
    path: 'C:/'
    globs: ['*.log', '*.txt', '*.jsonl']
    max_files: 5000
    refresh_minutes: 30
    max_depth: 20
    recent_days: 14
    text_only: true
    from_start: false
    exclude_dirs:
      - Windows
      - Program Files
      - Program Files (x86)
      - ProgramData
      - `$Recycle.Bin
      - System Volume Information
      - Recovery
      - PerfLogs
      - MSOCache
      - Config.Msi
      - Documents and Settings
      - `$WinREAgent
      - `$SysReset
      - .git
      - node_modules
      - __pycache__
      - .venv
      - venv
      - site-packages
      - build
      - dist
      - obj
    exclude_paths:
      - '$aliceParentPosix'
      - '$repoPosix'
  - type: logscan
    path: '$logPosix'
    globs: ['*.log*', '*.txt', '*.jsonl']
    max_files: 200
    refresh_minutes: 1
    max_depth: 3
    recent_days: 0
    text_only: true
    from_start: true
    max_lines: 5000
  - {type: message, adapter: stub, path: '$messagesPosix'}

gate:
  min_salience: 0.5
  novelty_window_hours: 24
  source_weights: {logfile: 1.0, message: 1.2}

redaction:
  patterns:
    - {name: AWS_KEY, pattern: '\bAKIA[0-9A-Z]{16}\b'}

notify:
  mode: digest+toast
  quiet_hours: '23:30-23:45'
  max_toasts_per_hour: 10
  min_toast_priority: 1

retention:
  raw_payload_days: 1
  max_raw_mb: 10

backup:
  enabled: true
  dir: backups
  hour: 4
  keep: 3
"@
Set-Content -Path $configPath -Value $yaml -Encoding utf8
Set-Content -Path $messages -Value "stress message initial status`n" -Encoding utf8

$info = @"
stress session: $stamp
config: $configPath
data: $dataDir
logs: $logRoot
handover: $outRoot
metrics: $metrics
alice: $Alice
"@
Set-Content -Path (Join-Path $prefix "stress-info.txt") -Value $info -Encoding utf8
Write-Control "stress session starting: $stamp"

$duration = [int]($Hours * 3600) + 300
$genArgs = @(
    "scripts\stress_logs.py",
    "--root", "`"$logRoot`"",
    "--messages", "`"$messages`"",
    "--duration", $duration,
    "--interval", "0.1"
)
$generator = Start-Process -FilePath $python -ArgumentList $genArgs `
    -WorkingDirectory $repo -PassThru -WindowStyle Hidden `
    -RedirectStandardOutput $generatorOut -RedirectStandardError $generatorErr
Write-Control "generator started (pid $($generator.Id))"

$supArgs = @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", "`"$repo\scripts\run_session.ps1`"",
    "-Hours", $Hours,
    "-Alice", "`"$Alice`"",
    "-Config", "`"$configPath`"",
    "-DataDir", "`"$dataDir`"",
    "-OutRoot", "`"$outRoot`"",
    "-MaxRestarts", "40",
    "-HeartbeatMinutes", "10"
)
if ($DryRun) { $supArgs += "-DryRun" }
$supervisor = Start-Process -FilePath powershell.exe -ArgumentList $supArgs `
    -PassThru -WindowStyle Hidden `
    -RedirectStandardOutput $supervisorOut -RedirectStandardError $supervisorErr
Write-Control "supervisor started (pid $($supervisor.Id))"

$start = Get-Date
$end = $start.AddHours($Hours)
$nextKill = $start.AddMinutes($KillEveryMinutes)
$sample = 0

while ((Get-Date) -lt $end -and -not $supervisor.HasExited) {
    Start-Sleep -Seconds 60

    $procs = @()
    try {
        $procs = @(
            Get-CimInstance Win32_Process -Filter "Name='alice.exe'" -ErrorAction SilentlyContinue |
                Where-Object {
                    $_.CommandLine -and $_.CommandLine -like "*$dataDir*"
                }
        )
    } catch {}
    if (-not $procs) {
        $procs = @(Get-Process alice -ErrorAction SilentlyContinue)
    }

    $normalized = @()
    foreach ($proc in $procs) {
        $procId = if ($proc.ProcessId) { $proc.ProcessId } else { $proc.Id }
        if ($procId) {
            $normalized += [pscustomobject]@{ Id = [int]$procId }
        }
    }
    $rss = 0.0
    foreach ($proc in $normalized) {
        try {
            $rss += (Get-Process -Id $proc.Id -ErrorAction SilentlyContinue).WorkingSet64 / 1MB
        } catch {}
    }

    $metricArgs = @(
        "scripts\stress_metrics.py",
        "--db", "`"$dataDir\alice.db`"",
        "--csv", "`"$metrics`"",
        "--data-dir", "`"$dataDir`"",
        "--rss-mb", [math]::Round($rss, 1),
        "--processes", $normalized.Count
    )
    if ($sample % 10 -ne 0) { $metricArgs += "--skip-integrity" }
    & $python @metricArgs *> (Join-Path $prefix "metrics-$sample.out.txt")

    if ((Get-Date) -ge $nextKill -and $normalized.Count -gt 0) {
        $victim = $normalized | Sort-Object Id | Select-Object -First 1
        Write-Control "stress kill: pid $($victim.Id)"
        & taskkill.exe /PID $victim.Id /T /F *> $null
        $nextKill = (Get-Date).AddMinutes($KillEveryMinutes)
    }

    if ($sample -gt 0 -and $sample % 30 -eq 0) {
        Write-Control "concurrent report and backup (sample $sample)"
        & $Alice --config $configPath --data-dir $dataDir report --write *> (Join-Path $prefix "report-$sample.txt")
        & $Alice --config $configPath --data-dir $dataDir backup *> (Join-Path $prefix "backup-$sample.txt")
    }
    if ($sample -gt 0 -and $sample % 120 -eq 0) {
        Write-Control "dry-run consolidation (sample $sample)"
        & $Alice --config $configPath --data-dir $dataDir consolidate --day (Get-Date -Format "yyyy-MM-dd") --dry-run *> (Join-Path $prefix "consolidate-$sample.txt")
    }
    $sample++
}

if (-not $supervisor.HasExited) {
    Write-Control "waiting for supervisor to finish"
    $supervisor.WaitForExit(900000) | Out-Null
}
if (-not $supervisor.HasExited) {
    Write-Control "supervisor did not exit; stopping it"
    & taskkill.exe /PID $supervisor.Id /T /F *> $null
}

Set-Content -Path (Join-Path $logRoot "STOP") -Value "stop" -Encoding utf8
Start-Sleep -Seconds 3
if (-not $generator.HasExited) {
    & taskkill.exe /PID $generator.Id /T /F *> $null
}

$sessionJson = Get-ChildItem $outRoot -Filter "session.json" -Recurse -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
$handover = if ($sessionJson) { Join-Path $sessionJson.DirectoryName "handover.md" } else { "" }

$reportArgs = @(
    "scripts\stress_report.py",
    "--csv", "`"$metrics`"",
    "--out", "`"$summary`""
)
if ($sessionJson) {
    $reportArgs += @("--session-json", "`"$($sessionJson.FullName)`"")
}
if ($handover) {
    $reportArgs += @("--handover", "`"$handover`"")
}
& $python @reportArgs *> (Join-Path $prefix "stress-report.out.txt")

Set-Content -Path (Join-Path $Base "LATEST-STRESS.txt") -Value @"
stress summary: $summary
handover: $handover
metrics: $metrics
control: $controlLog
data: $dataDir
"@ -Encoding utf8

Write-Control "stress session finished"
Write-Host "STRESS SUMMARY: $summary"
Write-Host "HANDOVER: $handover"
