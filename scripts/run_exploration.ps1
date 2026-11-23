param(
    [double]$Minutes = 360,
    [double]$StepSeconds = 240,
    [int]$MaxSteps = 500,
    [string]$DataDir = "",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "No venv found. Run the setup steps in README.md first."
}
$config = Join-Path $root "config\alice.yaml"

if (-not $DataDir) {
    if ($env:ALICE_DATA_DIR) {
        $DataDir = $env:ALICE_DATA_DIR
    }
    elseif ($env:LOCALAPPDATA) {
        $DataDir = Join-Path $env:LOCALAPPDATA "Alice"
    }
    else {
        $DataDir = Join-Path $root "state"
    }
}

$explore = Join-Path $DataDir "exploration"
New-Item -ItemType Directory -Force -Path $explore | Out-Null
$lock = Join-Path $explore "session.lock"
if (Test-Path $lock) {
    $old = 0
    $text = (Get-Content $lock -Raw).Trim()
    if ([int]::TryParse($text, [ref]$old) -and $old -gt 0) {
        if (Get-Process -Id $old -ErrorAction SilentlyContinue) {
            throw "An exploration session is already running (PID $old)."
        }
    }
}
Set-Content -Path $lock -Value $PID -Encoding ascii
$env:ALICE_CONFIG = $config
$env:ALICE_DATA_DIR = $DataDir
$log = Join-Path $explore "session.log"

$arguments = @(
    "-m", "alice",
    "--config", $config,
    "explore",
    "--minutes", "$Minutes",
    "--step-seconds", "$StepSeconds",
    "--max-steps", "$MaxSteps"
)
if ($DryRun) { $arguments += "--dry-run" }

try {
    "Starting exploration: $Minutes minutes, one step every $StepSeconds seconds." |
        Tee-Object -FilePath $log -Append
    & $python -m alice --config $config init 2>&1 | Tee-Object -FilePath $log -Append
    if ($LASTEXITCODE -ne 0) {
        throw "alice init failed with exit code $LASTEXITCODE"
    }
    & $python @arguments 2>&1 | Tee-Object -FilePath $log -Append
}
finally {
    Remove-Item $lock -Force -ErrorAction SilentlyContinue
}
