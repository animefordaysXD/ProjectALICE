param(
    [string]$DataDir = ""
)

$ErrorActionPreference = "Stop"
if (-not $DataDir) {
    if ($env:ALICE_DATA_DIR) {
        $DataDir = $env:ALICE_DATA_DIR
    }
    elseif ($env:LOCALAPPDATA) {
        $DataDir = Join-Path $env:LOCALAPPDATA "Alice"
    }
    else {
        $DataDir = Join-Path (Split-Path $PSScriptRoot -Parent) "state"
    }
}

$explore = Join-Path $DataDir "exploration"
$latest = Join-Path $explore "latest.json"
if (-not (Test-Path $latest)) {
    "No exploration session has been recorded under $explore."
    exit 0
}

$data = Get-Content $latest -Raw | ConvertFrom-Json
"status:   $($data.status)"
"started:  $($data.started)"
"ended:    $($data.ended)"
"steps:    $($data.steps)"
"current:  $($data.current_title)"
"url:      $($data.current_url)"
"stop:     $($data.stop_reason)"
"tokens:   $($data.input_tokens) in / $($data.output_tokens) out"
"summary:  $($data.summary_path)"

$lock = Join-Path $explore "session.lock"
if (Test-Path $lock) {
    "lock:     PID $((Get-Content $lock -Raw).Trim())"
}

$log = Join-Path $explore "session.log"
if (Test-Path $log) {
    ""
    "--- last log lines ---"
    Get-Content $log -Tail 12
}

if ($data.summary_path -and (Test-Path $data.summary_path)) {
    ""
    "--- summary ---"
    Get-Content $data.summary_path
}
