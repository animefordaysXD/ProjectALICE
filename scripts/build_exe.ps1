# Build Alice's single-file Windows executable and the distributable folder.
# Usage:  powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Push-Location $root
try {
    $python = Join-Path $root ".venv\Scripts\python.exe"
    if (-not (Test-Path $python)) {
        throw "No venv found. Run: py -3.12 -m venv .venv; .\.venv\Scripts\python.exe -m pip install -e '.[dev]' pyinstaller"
    }
    & $python -c "import PyInstaller" 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller is missing. Run: .\.venv\Scripts\python.exe -m pip install pyinstaller"
    }

    & $python -m PyInstaller --noconfirm --onefile --console --name alice `
        --paths src `
        --hidden-import anthropic `
        --collect-all sqlite_vec `
        --collect-all model2vec `
        --add-data "src/alice/core/migrations;alice/core/migrations" `
        src/alice/__main__.py
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }

    $app = Join-Path $root "dist\alice"
    if (Test-Path $app) { Remove-Item -Recurse -Force $app }
    New-Item -ItemType Directory -Path "$app\config", "$app\logs" -Force | Out-Null

    Copy-Item "dist\alice.exe" "$app\alice.exe" -Force
    Copy-Item "packaging\alice.yaml" "$app\config\alice.yaml" -Force
    Copy-Item "packaging\*.cmd" $app -Force
    Copy-Item "packaging\README.txt" "$app\README.txt" -Force
    New-Item -ItemType File -Path "$app\messages.txt" -Force | Out-Null

    $zip = Join-Path $root "dist\alice-windows-x64.zip"
    if (Test-Path $zip) { Remove-Item -Force $zip }
    Compress-Archive -Path "$app\*" -DestinationPath $zip

    Write-Host ""
    Write-Host "Built:  $app\alice.exe"
    Write-Host "Folder: $app"
    Write-Host "Zip:    $zip"
    exit 0
}
finally {
    Pop-Location
}
