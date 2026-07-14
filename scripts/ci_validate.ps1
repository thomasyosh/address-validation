# Jenkins / CI helper: validate ASE after an AI model update, then compare vs previous run.
# Pass when English and Chinese match-rate each change by less than 1 percentage point.
#
# Usage:
#   .\scripts\ci_validate.ps1
#   .\scripts\ci_validate.ps1 -Label "build-42" -MaxRateDelta 1

param(
    [string]$Label = "",
    [double]$MaxRateDelta = 1.0
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if (-not $Label -and $env:BUILD_NUMBER) {
    $Label = "jenkins-build-$($env:BUILD_NUMBER)"
}

$validateArgs = @("main.py", "validate", "--compare-with-previous", "--max-rate-delta", "$MaxRateDelta")
if ($Label) {
    $validateArgs += @("--label", $Label)
}

Write-Host "Running validation + match-rate comparison (max delta ${MaxRateDelta}pp)..."
python @validateArgs
exit $LASTEXITCODE
