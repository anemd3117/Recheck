[CmdletBinding()]
param(
    [switch]$DiagnosticsOnly,
    [switch]$InstallPython,
    [switch]$NoLaunch
)

$ErrorActionPreference = 'Stop'

function Test-RecheckPython {
    param([Parameter(Mandatory=$true)][string]$Candidate)
    if ($Candidate -match '(?i)[\\/]WindowsApps[\\/]' -or
        -not (Test-Path -LiteralPath $Candidate -PathType Leaf)) { return $null }
    $runner = New-Object System.Diagnostics.Process
    try {
        $start = New-Object System.Diagnostics.ProcessStartInfo
        $start.FileName = $Candidate
        $selector = if ([IO.Path]::GetFileName($Candidate) -in @('py', 'py.exe')) { '-3 ' } else { '' }
        $start.Arguments = $selector + '-I -c "import json,sys; print(json.dumps(dict(version=list(sys.version_info[:3]),executable=sys.executable)))"'
        $start.UseShellExecute = $false
        $start.CreateNoWindow = $true
        $start.RedirectStandardOutput = $true
        $start.RedirectStandardError = $true
        $start.EnvironmentVariables['PYTHON_MANAGER_AUTOMATIC_INSTALL'] = 'false'
        $start.EnvironmentVariables.Remove('PYLAUNCHER_ALLOW_INSTALL')
        $start.EnvironmentVariables.Remove('PYLAUNCHER_ALWAYS_INSTALL')
        $runner.StartInfo = $start
        [void]$runner.Start()
        if (-not $runner.WaitForExit(8000)) { $runner.Kill(); return $null }
        $output = $runner.StandardOutput.ReadToEnd()
        if ($runner.ExitCode -ne 0) { return $null }
        $info = $output | ConvertFrom-Json
        if ($info.version.Count -lt 2 -or [int]$info.version[0] -lt 3 -or
            ([int]$info.version[0] -eq 3 -and [int]$info.version[1] -lt 11)) { return $null }
        if (-not (Test-Path -LiteralPath $info.executable -PathType Leaf)) { return $null }
        return $info
    } catch {
        return $null
    } finally {
        $runner.Dispose()
    }
}

function Get-RecheckPythonCandidates {
    param([string]$Root)
    $candidates = @()
    if ($Root) { $candidates += Join-Path $Root '.venv\Scripts\python.exe' }
    foreach ($name in @('python.exe', 'python3.exe', 'py.exe')) {
        $command = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue
        if ($command) { $candidates += @($command | ForEach-Object { $_.Source }) }
    }
    foreach ($hive in @('HKCU:', 'HKLM:')) {
        foreach ($branch in @('Software\Python\PythonCore', 'Software\WOW6432Node\Python\PythonCore')) {
            $key = $hive + '\' + $branch
            foreach ($version in @(Get-ChildItem -LiteralPath $key -ErrorAction SilentlyContinue)) {
                $install = Get-Item -LiteralPath ($version.PSPath + '\InstallPath') -ErrorAction SilentlyContinue
                if ($install) {
                    $location = $install.GetValue('')
                    if ($location) { $candidates += Join-Path $location 'python.exe' }
                }
            }
        }
    }
    if ($env:LOCALAPPDATA) {
        $candidates += @(Get-ChildItem -Path (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python3*\python.exe') -ErrorAction SilentlyContinue |
                        Sort-Object FullName -Descending | ForEach-Object { $_.FullName })
        $candidates += @(Get-ChildItem -Path (Join-Path $env:LOCALAPPDATA 'Python\pythoncore-*\python.exe') -ErrorAction SilentlyContinue |
                        Sort-Object FullName -Descending | ForEach-Object { $_.FullName })
        $candidates += Join-Path $env:LOCALAPPDATA 'Programs\Python\Launcher\py.exe'
    }
    if ($env:ProgramFiles) {
        $candidates += @(Get-ChildItem -Path (Join-Path $env:ProgramFiles 'Python3*\python.exe') -ErrorAction SilentlyContinue |
                        Sort-Object FullName -Descending | ForEach-Object { $_.FullName })
    }
    if ($env:SystemRoot) { $candidates += Join-Path $env:SystemRoot 'py.exe' }
    return @($candidates | Where-Object { $_ } | Select-Object -Unique)
}

function Get-RecheckLaunchPlan {
    param([Parameter(Mandatory=$true)][string]$Root)
    $exe = Join-Path $Root 'Recheck.exe'
    if (Test-Path -LiteralPath $exe -PathType Leaf) {
        return [pscustomobject]@{mode='packaged'; executable=$exe; python=$null;
                                python_required=$false; git_required=$false; needs_python_install=$false}
    }
    if (-not (Test-Path -LiteralPath (Join-Path $Root 'launch.py') -PathType Leaf) -or
        -not (Test-Path -LiteralPath (Join-Path $Root 'pyproject.toml') -PathType Leaf)) {
        return [pscustomobject]@{mode='invalid'; executable=$null; python=$null;
                                python_required=$false; git_required=$false; needs_python_install=$false}
    }
    $python = $null
    foreach ($candidate in @(Get-RecheckPythonCandidates -Root $Root)) {
        $python = Test-RecheckPython -Candidate $candidate
        if ($python) { break }
    }
    return [pscustomobject]@{mode='source'; executable=(Join-Path $Root 'launch.py'); python=$python;
                            python_required=$true; git_required=$false; needs_python_install=($null -eq $python)}
}

function Get-RecheckWinGet {
    $command = Get-Command winget.exe -CommandType Application -ErrorAction SilentlyContinue
    if ($command) { return @($command)[0].Source }
    if ($env:LOCALAPPDATA) {
        $known = Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps\winget.exe'
        if (Test-Path -LiteralPath $known -PathType Leaf) { return $known }
    }
    return $null
}

function Invoke-RecheckCommand {
    param([Parameter(Mandatory=$true)][string]$File, [string[]]$Arguments)
    # Installation progress stays on screen; only the numeric status is returned.
    & $File @Arguments | Out-Host
    return [int]$LASTEXITCODE
}

function Invoke-RecheckSetup {
    param([Parameter(Mandatory=$true)][string]$Root, [switch]$AllowPythonInstall, [switch]$SkipLaunch)
    $plan = Get-RecheckLaunchPlan -Root $Root
    if ($plan.mode -eq 'packaged') {
        Write-Host 'This ZIP includes Recheck and Python. Git and Python on PATH are not required.'
        if (-not $SkipLaunch) { Start-Process -FilePath $plan.executable -WorkingDirectory $Root }
        return 0
    }
    if ($plan.mode -eq 'invalid') { throw 'Extract the complete Recheck ZIP first. Recheck.exe or the source project was not found.' }
    Write-Host 'Source mode: Git is not required for a downloaded source ZIP.'
    Write-Host 'For the easiest start, use the Windows ZIP containing Recheck.exe: https://github.com/leekmny/Recheck/releases'
    if ($plan.needs_python_install) {
        Write-Host 'No working Python 3.11 or newer was found. PATH-only detection is not used.'
        $approved = [bool]$AllowPythonInstall
        if (-not $approved) {
            $reply = Read-Host 'Install Python 3.12 through Microsoft WinGet? This accepts its package terms. [y/N]'
            $approved = $reply -in @('y', 'yes')
        }
        if (-not $approved) { Write-Host 'Cancelled. No Python installation was started.'; return 1 }
        $winget = Get-RecheckWinGet
        if (-not $winget) {
            Write-Host 'Install Python from https://www.python.org/downloads/windows/ then run this again.'
            Write-Host 'WinGet/App Installer: https://apps.microsoft.com/detail/9nblggh4nns1'
            return 1
        }
        $installCode = Invoke-RecheckCommand -File $winget -Arguments @(
            'install', '--id', 'Python.Python.3.12', '--exact', '--source', 'winget',
            '--accept-source-agreements', '--accept-package-agreements', '--disable-interactivity', '--no-upgrade'
        )
        $unsignedCode = [BitConverter]::ToUInt32([BitConverter]::GetBytes([int]$installCode), 0)
        if ($unsignedCode -in @(3010, 1641, 2316632329, 2316632330, 2316632331)) {
            Write-Host 'The installer requires a restart. Save your work, restart Windows yourself, then run this again.'
            return 1
        }
        if ($installCode -ne 0 -and $unsignedCode -notin @(2316632333, 2316632161)) {
            Write-Host ('Python installation did not complete. Installer exit code: ' + $installCode)
            return 1
        }
        $plan = Get-RecheckLaunchPlan -Root $Root
        if ($plan.needs_python_install) { throw 'The installer exited, but Python still cannot run. Install from python.org and retry.' }
    }
    $python = $plan.python.executable
    $venvPython = Join-Path $Root '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        $venvCode = Invoke-RecheckCommand -File $python -Arguments @('-m', 'venv', (Join-Path $Root '.venv'))
        if ($venvCode -ne 0) { throw 'Could not create the project Python environment.' }
    }
    if (-not (Test-RecheckPython -Candidate $venvPython)) { throw 'The project .venv is not executable. Preserve it and repair or choose a fresh source folder.' }
    Write-Host 'Preparing Python packages in this project only. Existing user settings and global PATH are preserved.'
    Push-Location -LiteralPath $Root
    try {
        if (Test-Path -LiteralPath (Join-Path $Root 'requirements-lock.txt')) {
            $packageCode = Invoke-RecheckCommand -File $venvPython -Arguments @('-m', 'pip', 'install', '-r', (Join-Path $Root 'requirements-lock.txt'))
            if ($packageCode -ne 0) { throw 'Package installation failed. Check the network and pip output, then run this again.' }
            $packageCode = Invoke-RecheckCommand -File $venvPython -Arguments @('-m', 'pip', 'install', '--no-deps', '--no-build-isolation', '-e', $Root)
        } else {
            $packageCode = Invoke-RecheckCommand -File $venvPython -Arguments @('-m', 'pip', 'install', '-e', $Root)
        }
        if ($packageCode -ne 0) { throw 'Recheck installation failed. Check the pip output, then run this again.' }
        if (-not $SkipLaunch) {
            return (Invoke-RecheckCommand -File $venvPython -Arguments @((Join-Path $Root 'launch.py')))
        }
        return 0
    } finally {
        Pop-Location
    }
}

# Dot-sourcing exposes the same planning functions without installs or launching.
if ($MyInvocation.InvocationName -eq '.') { return }
if ($DiagnosticsOnly) {
    Get-RecheckLaunchPlan -Root $PSScriptRoot | ConvertTo-Json -Depth 5
    exit 0
}
try {
    $resultCode = Invoke-RecheckSetup -Root $PSScriptRoot -AllowPythonInstall:$InstallPython -SkipLaunch:$NoLaunch
    exit $resultCode
} catch {
    Write-Host ('Recheck setup stopped: ' + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
