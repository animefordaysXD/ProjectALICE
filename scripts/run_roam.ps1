param(
    [Parameter(Mandatory = $true)][int]$SessionId,
    [string]$Exe = "",
    [string]$Config = "",
    [string]$DataDir = "",
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
if (-not $Exe) {
    $candidate = Join-Path $root "dist\alice\alice.exe"
    if (Test-Path $candidate) {
        $Exe = $candidate
        if (-not $Config) { $Config = Join-Path $root "dist\alice\config\alice.yaml" }
    }
    else {
        $Exe = Join-Path $root ".venv\Scripts\alice.exe"
        if (-not $Config) { $Config = Join-Path $root "config\alice.yaml" }
    }
}
if (-not $Config) { throw "No config path supplied." }
if (-not $DataDir) {
    if ($env:ALICE_DATA_DIR) { $DataDir = $env:ALICE_DATA_DIR }
    elseif ($env:LOCALAPPDATA) { $DataDir = Join-Path $env:LOCALAPPDATA "Alice" }
    else { $DataDir = Join-Path $root "state" }
}
$roamDir = Join-Path $DataDir "roam"
New-Item -ItemType Directory -Force -Path $roamDir | Out-Null
$log = Join-Path $roamDir "supervisor.log"
$workerArgs = @(
    "--config", $Config,
    "--data-dir", $DataDir,
    "roam",
    "--session", "$SessionId",
    "--worker"
)
if ($Force) { $workerArgs += "--now" }
$statusArgs = @(
    "--config", $Config,
    "--data-dir", $DataDir,
    "roam",
    "--session", "$SessionId",
    "--status"
)

"[$(Get-Date -Format o)] supervisor start session=$SessionId force=$Force" |
    Tee-Object -FilePath $log -Append
$attempt = 0
while ($true) {
    $attempt += 1
    "[$(Get-Date -Format o)] worker attempt $attempt" |
        Tee-Object -FilePath $log -Append
    & $Exe @workerArgs 2>&1 | Tee-Object -FilePath $log -Append
    $code = $LASTEXITCODE
    try {
        $status = (& $Exe @statusArgs 2>$null | ConvertFrom-Json)
    }
    catch {
        $status = $null
    }
    if ($status -and ($status.status -in @("completed", "stopped"))) {
        "[$(Get-Date -Format o)] supervisor done: $($status.status)" |
            Tee-Object -FilePath $log -Append
        exit 0
    }
    "[$(Get-Date -Format o)] worker exit=$code; restarting in 10s" |
        Tee-Object -FilePath $log -Append
    Start-Sleep -Seconds 10
}
