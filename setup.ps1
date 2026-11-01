param(
    [switch]$SkipCheck
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Error "Python 3.11+ not found. Install it from https://www.python.org/downloads/ and check 'Add python.exe to PATH'."
    exit 1
}

if (-not (Test-Path -LiteralPath $VenvPython)) {
    Write-Host "Creating virtual environment..."
    python -m venv (Join-Path $ProjectRoot ".venv")
}

Write-Host "Installing dependencies (this needs internet)..."
& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -e $ProjectRoot

$EnvFile = Join-Path $ProjectRoot ".env"
if (-not (Test-Path -LiteralPath $EnvFile)) {
    Copy-Item (Join-Path $ProjectRoot ".env.example") $EnvFile
    Write-Host "Created .env from .env.example."
}

if (-not $SkipCheck) {
    & $VenvPython -m ai_video --check
}

Write-Host ""
Write-Host "Done. Next steps:"
Write-Host "  1) Edit .env and fill OPENAI_API_KEY if you want AI images (optional)."
Write-Host "  2) Generate a demo video:"
Write-Host "       .\run.ps1 \"Your topic\" -Mode demo"
Write-Host "  3) Start the web UI:"
Write-Host "       .\.venv\Scripts\python.exe -m ai_video.web"
