param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Topic,

    [ValidateSet("auto", "api", "demo")]
    [string]$Mode = "auto",

    [ValidateSet("demo", "openai", "comfyui")]
    [string]$ImageBackend,

    [string]$OutputDir = "outputs"
)

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python)) {
    Write-Error "Virtual environment not found. Please follow README.md to install first."
    exit 2
}

Push-Location $ProjectRoot
try {
    $RunArgs = @($Topic, "--mode", $Mode, "--output-dir", $OutputDir)
    if ($PSBoundParameters.ContainsKey("ImageBackend")) {
        $RunArgs += @("--image-backend", $ImageBackend)
    }
    & $Python -m ai_video @RunArgs
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}

