$ErrorActionPreference = "Stop"
$projectRoot = $PSScriptRoot
$backendPath = Join-Path $projectRoot "backend"
Set-Location $backendPath

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    python -m venv .venv
}
$python = Join-Path (Get-Location) ".venv\Scripts\python.exe"
& $python -m pip install --upgrade pip
& $python -m pip install -r requirements.txt
$env:GRIDWISE_DB_PATH = Join-Path (Get-Location) "gridwise.db"
Write-Host "Starting GridWise EMS at http://127.0.0.1:8000" -ForegroundColor Green
Write-Host "API docs: http://127.0.0.1:8000/docs"
& $python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
