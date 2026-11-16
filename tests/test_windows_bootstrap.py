"""Exercise the bootstrap through PowerShell, without installs or GUI launch."""

import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
pytestmark = pytest.mark.skipif(os.name != 'nt' or not POWERSHELL.is_file(), reason='Windows bootstrap')


def script(tmp_path):
    source = ROOT / 'Setup-Recheck.ps1'
    assert source.is_file(), 'Source ZIP cannot bootstrap without Git or Python on PATH yet'
    target = tmp_path / 'Recheck folder' / source.name
    target.parent.mkdir(parents=True)
    shutil.copyfile(source, target)
    return target


def ps(code, *, no_path=True):
    code = '[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false); ' + code
    encoded = base64.b64encode(code.encode('utf-16le')).decode('ascii')
    env = dict(os.environ)
    if no_path:
        env['PATH'] = ''
    result = subprocess.run([str(POWERSHELL), '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                            env=env, capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def test_packaged_diagnostics_require_neither_git_nor_python(tmp_path):
    bootstrap = script(tmp_path)
    (bootstrap.parent / 'Recheck.exe').touch()
    result = ps(f'& {quote(bootstrap)} -DiagnosticsOnly')
    assert result['mode'] == 'packaged'
    assert result['python_required'] is False and result['git_required'] is False
    assert result['executable'] == str(bootstrap.parent / 'Recheck.exe')
    assert not (bootstrap.parent / '.venv').exists()


def test_source_without_python_is_an_install_plan_not_a_false_ready(tmp_path):
    bootstrap = script(tmp_path)
    (bootstrap.parent / 'launch.py').touch()
    (bootstrap.parent / 'pyproject.toml').touch()
    result = ps(f'. {quote(bootstrap)}; function Get-RecheckPythonCandidates {{ @() }}; '
                f'Get-RecheckLaunchPlan -Root {quote(bootstrap.parent)} | ConvertTo-Json -Depth 5')
    assert result['mode'] == 'source'
    assert result['python_required'] is True and result['git_required'] is False
    assert result['python'] is None
    assert result['needs_python_install'] is True
    assert not (bootstrap.parent / '.venv').exists()


def test_working_python_is_validated_by_real_execution(tmp_path):
    bootstrap = script(tmp_path)
    result = ps(f'. {quote(bootstrap)}; Test-RecheckPython -Candidate {quote(sys.executable)} | ConvertTo-Json -Depth 5')
    assert Path(result['executable']).resolve() == Path(sys.executable).resolve()
    assert tuple(result['version'])[:2] >= (3, 11)


def test_store_alias_is_rejected_without_execution(tmp_path):
    bootstrap = script(tmp_path)
    alias = bootstrap.parent / 'Microsoft/WindowsApps/python.exe'
    alias.parent.mkdir(parents=True)
    alias.write_bytes(b'not executable')
    result = ps(f'. {quote(bootstrap)}; $found=Test-RecheckPython -Candidate {quote(alias)}; '
                '@{rejected=($null -eq $found)} | ConvertTo-Json')
    assert result == {'rejected': True}


def test_invalid_source_folder_does_not_trigger_install_or_create_venv(tmp_path):
    bootstrap = script(tmp_path)
    result = ps(f'& {quote(bootstrap)} -DiagnosticsOnly')
    assert result['mode'] == 'invalid'
    assert not (bootstrap.parent / '.venv').exists()


def test_existing_venv_can_be_discovered_with_empty_path(tmp_path):
    bootstrap = script(tmp_path)
    (bootstrap.parent / 'launch.py').touch()
    (bootstrap.parent / 'pyproject.toml').touch()
    result = ps(f'. {quote(bootstrap)}; function Get-RecheckPythonCandidates {{ '
                f'param($Root); {quote(sys.executable)} }}; '
                f'Get-RecheckLaunchPlan -Root {quote(bootstrap.parent)} | ConvertTo-Json -Depth 5')
    assert result['mode'] == 'source' and result['needs_python_install'] is False
    assert Path(result['python']['executable']).resolve() == Path(sys.executable).resolve()


def test_manager_runtime_candidates_are_found_without_path(tmp_path):
    bootstrap = script(tmp_path)
    local = bootstrap.parent / 'Local AppData'
    python = local / 'Python/pythoncore-3.14-64/python.exe'
    python.parent.mkdir(parents=True)
    python.touch()
    result = ps(f'. {quote(bootstrap)}; $env:LOCALAPPDATA={quote(local)}; '
                f'@{{candidates=@(Get-RecheckPythonCandidates -Root {quote(bootstrap.parent)})}} | ConvertTo-Json -Depth 5')
    assert str(python) in result['candidates']


def test_native_setup_output_does_not_replace_the_numeric_exit_code(tmp_path):
    bootstrap = script(tmp_path)
    result = ps(f'. {quote(bootstrap)}; $script:seen=@(); '
                'function Out-Host { param([Parameter(ValueFromPipeline=$true)]$InputObject) '
                'process { $script:seen += [string]$InputObject } }; '
                f'$code=Invoke-RecheckCommand -File {quote(sys.executable)} '
                "-Arguments @('-c', 'print(123)'); "
                '@{exit_code=$code;exit_type=$code.GetType().FullName;output=$script:seen} | ConvertTo-Json -Depth 5')
    assert result['exit_code'] == 0 and result['exit_type'] == 'System.Int32'
    assert result['output'] == ['123']
