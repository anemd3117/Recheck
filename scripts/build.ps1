param([string]$Output = '', [switch]$IncludeEnvironment)
$ErrorActionPreference = 'Stop'
$workspace = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $workspace
if (-not $Output) {
    $Output = Join-Path $workspace ('verification/builds/' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff'))
}
$Output = [IO.Path]::GetFullPath($Output)
& .venv/Scripts/python scripts/verify_source.py --output (Join-Path $Output 'source-checks')
if ($LASTEXITCODE -ne 0) { throw 'Source verification failed; live distributions were not changed' }
$packageArgs = @('scripts/package_verified.py', '--output', (Join-Path $Output 'package'))
if ($IncludeEnvironment) { $packageArgs += '--include-environment' }
& .venv/Scripts/python @packageArgs
if ($LASTEXITCODE -ne 0) { throw 'Staged packaging failed; live distributions were not changed' }
Write-Output ('Staged executable: ' + (Join-Path $Output 'package/staged-dist/Recheck/Recheck.exe'))
Write-Output 'Run native verification before scripts/release_distribution.py publishes this payload.'
Write-Output 'Default portable build includes Recheck.exe, its runtime, dependency notices and Qt/PySide sources; offline container TARs require -IncludeEnvironment.'
