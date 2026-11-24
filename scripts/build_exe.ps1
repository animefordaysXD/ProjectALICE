# Build Alice's Windows executables and the distributable folder.
# Usage:  powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Push-Location $root
try {
    $python = Join-Path $root ".venv\Scripts\python.exe"
    if (-not (Test-Path $python)) {
        $command = Get-Command python.exe -ErrorAction SilentlyContinue
        if ($null -eq $command) {
            throw "No venv found. Run: py -3.12 -m venv .venv; .\.venv\Scripts\python.exe -m pip install -e '.[dev]' pyinstaller"
        }
        $python = $command.Source
    }
    & $python -c "import PyInstaller" 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller is missing. Run: .\.venv\Scripts\python.exe -m pip install pyinstaller"
    }

    $common = @(
        "--noconfirm", "--onefile",
        "--paths", "src",
        "--hidden-import", "anthropic",
        "--hidden-import", "win11toast",
        "--collect-all", "sqlite_vec",
        "--collect-all", "model2vec",
        "--collect-all", "win11toast",
        "--collect-all", "winrt",
        "--collect-all", "uiautomation",
        "--collect-all", "comtypes",
        "--add-data", "src/alice/core/migrations;alice/core/migrations"
    )

    & $python -m PyInstaller @common --console --name alice src/alice/__main__.py
    if ($LASTEXITCODE -ne 0) { throw "CLI build failed with exit code $LASTEXITCODE" }

    & $python -m PyInstaller @common --windowed --name alice-gui src/alice/gui_main.py
    if ($LASTEXITCODE -ne 0) { throw "GUI build failed with exit code $LASTEXITCODE" }

    $app = Join-Path $root "dist\alice"
    if (Test-Path $app) { Remove-Item -Recurse -Force $app }
    New-Item -ItemType Directory -Path "$app\config", "$app\logs" -Force | Out-Null

    Copy-Item "dist\alice.exe" "$app\alice.exe" -Force
    Copy-Item "dist\alice-gui.exe" "$app\alice-gui.exe" -Force
    Copy-Item "packaging\alice.yaml" "$app\config\alice.yaml" -Force
    Copy-Item "packaging\*.cmd" $app -Force
    Copy-Item "packaging\README.txt" "$app\README.txt" -Force
    New-Item -ItemType File -Path "$app\messages.txt" -Force | Out-Null

    $zip = Join-Path $root "dist\alice-windows-x64.zip"
    if (Test-Path $zip) { Remove-Item -Force $zip }
    Compress-Archive -Path "$app\*" -DestinationPath $zip

    Write-Host ""
    Write-Host "Built:  $app\alice.exe"
    Write-Host "GUI:    $app\alice-gui.exe"
    Write-Host "Folder: $app"
    Write-Host "Zip:    $zip"
    exit 0
}
finally {
    Pop-Location
}
