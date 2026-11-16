"""Prerequisite discovery and install decisions without installing host software."""

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import threading

import pytest


def backend():
    assert importlib.util.find_spec('recheck.prerequisites') is not None, (
        'The missing-tool discovery/install workflow has not been implemented'
    )
    from recheck import prerequisites
    return prerequisites


def process(stdout='', stderr='', returncode=0, **extra):
    return dict(stdout=stdout, stderr=stderr, returncode=returncode,
                error=None, cancelled=False, timed_out=False, truncated=False, **extra)


def fixture_tools(monkeypatch, tmp_path, present=()):
    module = backend()
    paths = {}
    for name in present:
        target = tmp_path / (name + '.exe')
        target.touch()
        paths[name] = [str(target)]
    monkeypatch.setattr(module, '_candidate_paths', lambda name: paths.get(name, []))
    monkeypatch.setattr(module, '_find_winget', lambda: None)
    return module, paths


def test_packaged_app_does_not_require_developer_git_or_python(monkeypatch, tmp_path):
    module, _ = fixture_tools(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    result = asyncio.run(module.inspect_prerequisites())
    tools = {row['id']: row for row in result['tools']}
    assert result['app_runtime'] == {'python': 'bundled', 'git_required': False}
    assert tools['git']['optional'] and tools['python']['optional']
    assert tools['git']['required_for'] == tools['python']['required_for'] == 'development'
    assert not tools['docker']['optional']
    assert result['winget']['download_url'].startswith('https://apps.microsoft.com/')


def test_known_installs_work_without_path_and_scan_never_installs(monkeypatch, tmp_path):
    module = backend()
    program_files = tmp_path / 'Program Files'
    git = program_files / 'Git' / 'cmd' / 'git.exe'
    git.parent.mkdir(parents=True)
    git.touch()
    monkeypatch.setenv('ProgramFiles', str(program_files))
    monkeypatch.setattr(module.shutil, 'which', lambda name: None)
    monkeypatch.setattr(module, '_registry_paths', lambda name: [])
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    calls = []

    async def run(argv, **kwargs):
        calls.append(argv)
        return process('git version 2.51.0')

    monkeypatch.setattr(module, 'run_process', run)
    result = asyncio.run(module.inspect_prerequisites())
    row = next(row for row in result['tools'] if row['id'] == 'git')
    assert row['status'] == 'ready' and Path(row['path']) == git
    assert calls and all('install' not in argv for argv in calls)


def test_python_is_actually_executable_with_no_path(monkeypatch, tmp_path):
    module, paths = fixture_tools(monkeypatch, tmp_path)
    paths['python'] = [sys.executable]
    row = asyncio.run(module.inspect_prerequisites())['tools'][1]
    assert row['id'] == 'python' and row['status'] == 'ready'
    assert Path(row['path']).resolve() == Path(sys.executable).resolve()
    assert tuple(map(int, row['version'].split('.')[:2])) >= (3, 11)


def test_python_store_alias_is_not_executed(monkeypatch, tmp_path):
    module, paths = fixture_tools(monkeypatch, tmp_path)
    alias = tmp_path / 'Microsoft' / 'WindowsApps' / 'python.exe'
    alias.parent.mkdir(parents=True)
    alias.touch()
    paths['python'] = [str(alias)]

    async def forbidden(*args, **kwargs):
        pytest.fail('WindowsApps Python alias must not launch the Store')

    monkeypatch.setattr(module, 'run_process', forbidden)
    row = asyncio.run(module.inspect_prerequisites())['tools'][1]
    assert row['status'] == 'missing'


def test_unsupported_python_does_not_hide_a_later_working_install(monkeypatch, tmp_path):
    module, paths = fixture_tools(monkeypatch, tmp_path, ['python'])
    other = tmp_path / 'good-python.exe'
    other.touch()
    paths['python'].append(str(other))

    async def run(argv, **kwargs):
        version = [3, 10, 9] if Path(argv[0]).name == 'python.exe' else [3, 12, 10]
        return process(json.dumps({'version': version, 'executable': argv[0]}))

    monkeypatch.setattr(module, 'run_process', run)
    row = asyncio.run(module.inspect_prerequisites())['tools'][1]
    assert row['status'] == 'ready' and Path(row['path']) == other


def test_broken_executable_is_missing_not_ready(monkeypatch, tmp_path):
    module, _ = fixture_tools(monkeypatch, tmp_path, ['git'])

    async def run(argv, **kwargs):
        result = process(returncode=None)
        result['error'] = 'could not execute'
        return result

    monkeypatch.setattr(module, 'run_process', run)
    row = asyncio.run(module.inspect_prerequisites())['tools'][0]
    assert row['status'] == 'missing'


def test_pre_cancelled_scan_has_no_subprocesses(monkeypatch, tmp_path):
    module, _ = fixture_tools(monkeypatch, tmp_path, ['git'])

    async def forbidden(*args, **kwargs):
        pytest.fail('Cancelled scan must have no subprocesses')

    monkeypatch.setattr(module, 'run_process', forbidden)
    cancelled = threading.Event()
    cancelled.set()
    result = asyncio.run(module.inspect_prerequisites(cancel=cancelled))
    assert result['status'] == 'cancelled'


def test_unknown_package_cannot_be_installed(monkeypatch, tmp_path):
    module, _ = fixture_tools(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match='Unknown'):
        asyncio.run(module.install_prerequisite('Git.Git & arbitrary'))


def test_winget_missing_returns_official_manual_install(monkeypatch, tmp_path):
    module, _ = fixture_tools(monkeypatch, tmp_path)
    result = asyncio.run(module.install_prerequisite('git'))
    assert result['status'] == 'manual_required'
    assert result['download_url'] == 'https://git-scm.com/downloads/win'
    assert result['winget_url'].startswith('https://apps.microsoft.com/')


def test_ghidra_has_manual_fallback_without_unverified_package_id(monkeypatch, tmp_path):
    module, _ = fixture_tools(monkeypatch, tmp_path)
    monkeypatch.setattr(module, '_find_winget', lambda: 'winget.exe')
    result = asyncio.run(module.install_prerequisite('ghidra'))
    assert result['status'] == 'manual_required'
    assert result['download_url'] == 'https://github.com/NationalSecurityAgency/ghidra/releases'


def test_install_uses_fixed_exact_source_and_rechecks_installed_tool(monkeypatch, tmp_path):
    module, paths = fixture_tools(monkeypatch, tmp_path)
    git = tmp_path / 'installed-git.exe'
    calls = []
    monkeypatch.setattr(module, '_find_winget', lambda: 'winget.exe')

    async def run(argv, **kwargs):
        calls.append(argv)
        if 'install' in argv:
            git.touch()
            paths['git'] = [str(git)]
            return process('installed')
        return process('git version 2.51.0')

    monkeypatch.setattr(module, 'run_process', run)
    result = asyncio.run(module.install_prerequisite('git'))
    install = next(argv for argv in calls if 'install' in argv)
    assert install[:3] == ['winget.exe', 'install', '--id']
    assert install[3] == 'Git.Git'
    assert '--exact' in install and install[install.index('--source') + 1] == 'winget'
    assert '--allow-reboot' not in install and '--ignore-security-hash' not in install
    assert result['status'] == 'ready' and result['tool']['path'] == str(git)
    assert result['tool']['can_install'] is False
    assert calls[-1] == [str(git), '--version']


def test_exit_zero_without_working_install_does_not_claim_ready(monkeypatch, tmp_path):
    module, _ = fixture_tools(monkeypatch, tmp_path)
    monkeypatch.setattr(module, '_find_winget', lambda: 'winget.exe')

    async def run(argv, **kwargs):
        return process('installer exited')

    monkeypatch.setattr(module, 'run_process', run)
    result = asyncio.run(module.install_prerequisite('git'))
    assert result['status'] == 'failed'
    assert result['tool']['status'] == 'missing'


@pytest.mark.parametrize('code', [3010, 1641, 0x8A150109, -1978334967, 0x8A15010A])
def test_reboot_exit_is_truthful_and_does_not_reboot(monkeypatch, tmp_path, code):
    module, _ = fixture_tools(monkeypatch, tmp_path)
    monkeypatch.setattr(module, '_find_winget', lambda: 'winget.exe')
    calls = []

    async def run(argv, **kwargs):
        calls.append(argv)
        return process('v1.12.0') if argv == ['winget.exe', '--version'] else process(returncode=code)

    monkeypatch.setattr(module, 'run_process', run)
    result = asyncio.run(module.install_prerequisite('docker'))
    assert result['status'] == 'reboot_required'
    assert len(calls) == 2 and all('shutdown' not in call for call in calls)


def test_cancelled_install_never_claims_success(monkeypatch, tmp_path):
    module, _ = fixture_tools(monkeypatch, tmp_path)
    monkeypatch.setattr(module, '_find_winget', lambda: 'winget.exe')

    async def run(argv, **kwargs):
        result = process(returncode=None)
        result['cancelled'] = True
        return result

    monkeypatch.setattr(module, 'run_process', run)
    result = asyncio.run(module.install_prerequisite('git'))
    assert result['status'] == 'cancelled'


def test_pre_cancelled_install_has_no_subprocesses(monkeypatch, tmp_path):
    module, _ = fixture_tools(monkeypatch, tmp_path)

    async def forbidden(*args, **kwargs):
        pytest.fail('Cancelled install must have no subprocesses')

    monkeypatch.setattr(module, 'run_process', forbidden)
    cancelled = threading.Event()
    cancelled.set()
    assert asyncio.run(module.install_prerequisite('git', cancel=cancelled))['status'] == 'cancelled'


def test_existing_tool_is_not_reinstalled(monkeypatch, tmp_path):
    module, _ = fixture_tools(monkeypatch, tmp_path, ['git'])
    calls = []

    async def run(argv, **kwargs):
        calls.append(argv)
        return process('git version 2.51.0')

    monkeypatch.setattr(module, 'run_process', run)
    result = asyncio.run(module.install_prerequisite('git'))
    assert result['status'] == 'ready'
    assert calls and all('install' not in call for call in calls)


def test_python_probe_disables_manager_and_legacy_launcher_auto_install(monkeypatch, tmp_path):
    module, _ = fixture_tools(monkeypatch, tmp_path, ['python'])
    monkeypatch.setenv('PYLAUNCHER_ALLOW_INSTALL', '1')
    monkeypatch.setenv('PYLAUNCHER_ALWAYS_INSTALL', '1')
    seen = []

    async def run(argv, **kwargs):
        seen.append(kwargs.get('env', {}))
        return process(json.dumps({'version': [3, 12, 10], 'executable': argv[0]}))

    monkeypatch.setattr(module, 'run_process', run)
    result = asyncio.run(module.inspect_prerequisites())
    assert result['tools'][1]['status'] == 'ready'
    assert seen[0].get('PYTHON_MANAGER_AUTOMATIC_INSTALL') == 'false'
    assert 'PYLAUNCHER_ALLOW_INSTALL' not in seen[0]
    assert 'PYLAUNCHER_ALWAYS_INSTALL' not in seen[0]
    assert os.environ['PYLAUNCHER_ALLOW_INSTALL'] == '1'


def test_install_manager_runtime_is_discovered_without_path(monkeypatch, tmp_path):
    module = backend()
    local = tmp_path / 'Local AppData'
    python = local / 'Python/pythoncore-3.14-64/python.exe'
    python.parent.mkdir(parents=True)
    python.touch()
    monkeypatch.setenv('LOCALAPPDATA', str(local))
    monkeypatch.setattr(module.shutil, 'which', lambda name: None)
    monkeypatch.setattr(module, '_registry_paths', lambda name: [])
    monkeypatch.setattr(sys, 'frozen', True, raising=False)

    async def run(argv, **kwargs):
        return process(json.dumps({'version': [3, 14, 8], 'executable': argv[0]}))

    monkeypatch.setattr(module, 'run_process', run)
    result = asyncio.run(module.inspect_prerequisites())
    assert result['tools'][1]['status'] == 'ready'
    assert Path(result['tools'][1]['path']) == python


@pytest.mark.parametrize('outcome,expected', [
    (process(returncode=1), 'failed'),
    (process(returncode=3010), 'reboot_required'),
    ({**process(returncode=None), 'cancelled': True}, 'cancelled'),
    ({**process(returncode=None), 'timed_out': True}, 'failed'),
])
def test_unsuccessful_install_keeps_retry_available_with_working_winget(
    monkeypatch, tmp_path, outcome, expected
):
    module, _ = fixture_tools(monkeypatch, tmp_path)
    monkeypatch.setattr(module, '_find_winget', lambda: 'winget.exe')

    async def run(argv, **kwargs):
        return process('v1.12.0') if argv == ['winget.exe', '--version'] else outcome

    monkeypatch.setattr(module, 'run_process', run)
    result = asyncio.run(module.install_prerequisite('git'))
    assert result['status'] == expected
    assert result['tool']['can_install'] is True


def test_unusable_winget_does_not_offer_automatic_retry(monkeypatch, tmp_path):
    module, _ = fixture_tools(monkeypatch, tmp_path)
    monkeypatch.setattr(module, '_find_winget', lambda: 'winget.exe')
    calls = []

    async def run(argv, **kwargs):
        calls.append(argv)
        return {**process(returncode=None), 'error': 'Cannot execute'}

    monkeypatch.setattr(module, 'run_process', run)
    result = asyncio.run(module.install_prerequisite('git'))
    assert result['status'] == 'manual_required'
    assert result['tool']['can_install'] is False
    assert calls == [['winget.exe', '--version']]
