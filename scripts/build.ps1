param([string]$Output = '')
$ErrorActionPreference = 'Stop'
$workspace = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $workspace
if (-not $Output) {
    $Output = Join-Path $workspace ('verification/builds/' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff'))
}
$Output = [IO.Path]::GetFullPath($Output)
& .venv/Scripts/python scripts/verify_source.py --output (Join-Path $Output 'source-checks')
if ($LASTEXITCODE -ne 0) { throw 'Source verification failed; live distributions were not changed' }
& .venv/Scripts/python scripts/package_verified.py --output (Join-Path $Output 'package')
if ($LASTEXITCODE -ne 0) { throw 'Staged packaging failed; live distributions were not changed' }
Write-Output ('Staged executable: ' + (Join-Path $Output 'package/staged-dist/Recheck/Recheck.exe'))
Write-Output 'Run native verification before scripts/release_distribution.py publishes this payload.'