"""Read-only prerequisite checks and explicitly requested Windows installations.

The frozen app carries Python. Git and a standalone Python are developer tools,
not launch requirements. Install commands are fixed package IDs and never shell
text. No installer, service or environment mutation occurs during discovery.
"""

from __future__ import annotations

import inspect
import json
import os
from pathlib import Path
import re
import shutil
import sys

from .process import run_process

WINGET_URL = 'https://apps.microsoft.com/detail/9nblggh4nns1'
TOOL_CATALOG = {
    'git': dict(name='Git', purpose='소스 내려받기·개발용 · 배포판 실행에는 필요 없음',
                required_for='development', optional=True, package_id='Git.Git',
                download_url='https://git-scm.com/downloads/win'),
    'python': dict(name='Python 3.11 이상', purpose='소스 실행·개발용 · 배포판에는 Python 포함',
                   required_for='development', optional=True, package_id='Python.Python.3.12',
                   download_url='https://www.python.org/downloads/windows/'),
    'docker': dict(name='Docker Desktop', purpose='격리된 분석 환경과 프로그램 실행 비교',
                   required_for='execution', optional=False, package_id='Docker.DockerDesktop',
                   download_url='https://docs.docker.com/desktop/setup/install/windows-install/'),
    'java': dict(name='Java JDK 21 이상', purpose='Ghidra 직접 설치 방식에서 사용 · 내장 분석 환경은 포함',
                 required_for='direct_static', optional=True,
                 package_id='EclipseAdoptium.Temurin.21.JDK',
                 download_url='https://adoptium.net/temurin/releases/?version=21'),
    'ghidra': dict(name='Ghidra', purpose='직접 설치 방식의 구조 분석 · 내장 분석 환경은 포함',
                   required_for='direct_static', optional=True, package_id=None,
                   download_url='https://github.com/NationalSecurityAgency/ghidra/releases'),
    'ollama': dict(name='Ollama', purpose='선택 사항 · 내 PC에서 AI를 사용할 때 설치',
                   required_for='local_ai', optional=True, package_id='Ollama.Ollama',
                   download_url='https://ollama.com/download/windows'),
}


def _cancelled(cancel):
    return cancel is not None and cancel.is_set()


def _ok(result):
    return result.get('returncode') == 0 and not any(
        result.get(key) for key in ('error', 'cancelled', 'timed_out', 'truncated')
    )


def _java_major(text):
    match = re.search(r'(?:version\s+"|(?:openjdk|java)\s+)(?:1\.)?(\d+)', text)
    return int(match.group(1)) if match else None


async def _emit(callback, message, **values):
    if callback:
        result = callback(dict(kind='prerequisites', message=message, **values))
        if inspect.isawaitable(result):
            await result


def _registry_paths(tool_id):
    """Known vendor keys only; registry strings are paths, never commands."""
    if os.name != 'nt':
        return []
    import winreg

    found = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            if tool_id == 'git':
                try:
                    with winreg.OpenKey(hive, r'Software\GitForWindows', 0,
                                        winreg.KEY_READ | view) as key:
                        found.append(Path(winreg.QueryValueEx(key, 'InstallPath')[0]) / 'cmd/git.exe')
                except OSError:
                    pass
            branch = {'python': r'Software\Python\PythonCore',
                      'java': r'Software\JavaSoft\JDK'}.get(tool_id)
            if not branch:
                continue
            try:
                with winreg.OpenKey(hive, branch, 0, winreg.KEY_READ | view) as key:
                    for index in range(min(winreg.QueryInfoKey(key)[0], 100)):
                        try:
                            version = winreg.EnumKey(key, index)
                            tail = version + ('\\InstallPath' if tool_id == 'python' else '')
                            with winreg.OpenKey(key, tail) as install:
                                location = winreg.QueryValueEx(
                                    install, '' if tool_id == 'python' else 'JavaHome'
                                )[0]
                            found.append(Path(location) / ('python.exe' if tool_id == 'python'
                                                           else 'bin/java.exe'))
                        except OSError:
                            continue
            except OSError:
                continue
    return found


def _candidate_paths(tool_id):
    names = {'git': ('git',), 'python': ('python', 'python3', 'py'),
             'docker': ('docker',), 'java': ('java',), 'ollama': ('ollama',),
             'ghidra': ()}[tool_id]
    values = [shutil.which(name) for name in names]
    values.extend(_registry_paths(tool_id))
    pf = Path(os.environ.get('ProgramFiles', 'C:/Program Files'))
    local = Path(os.environ.get('LOCALAPPDATA', str(Path.home() / 'AppData/Local')))
    if tool_id == 'git':
        values += [pf / 'Git/cmd/git.exe', local / 'Programs/Git/cmd/git.exe']
    elif tool_id == 'python':
        if not getattr(sys, 'frozen', False):
            values.append(sys.executable)
        for base in (local / 'Programs/Python', pf):
            values.extend(sorted(base.glob('Python3*/python.exe'), reverse=True))
        values.extend(sorted((local / 'Python').glob('pythoncore-*/python.exe'), reverse=True))
        values += [Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'py.exe',
                   local / 'Programs/Python/Launcher/py.exe']
    elif tool_id == 'docker':
        values += [pf / 'Docker/Docker/resources/bin/docker.exe',
                   local / 'Programs/DockerDesktop/resources/bin/docker.exe']
    elif tool_id == 'java':
        values = [Path(os.environ[variable]) / ('bin/java.exe' if os.name == 'nt' else 'bin/java')
                  for variable in ('JAVA_HOME_OVERRIDE', 'JAVA_HOME') if os.environ.get(variable)] + values
        for base in (pf / 'Eclipse Adoptium', pf / 'Java', pf / 'Microsoft'):
            values.extend(sorted(base.glob('jdk*/bin/java.exe'), reverse=True))
    elif tool_id == 'ollama':
        from .ollama import discover_ollama

        values += discover_ollama().get('candidates', [])
    elif tool_id == 'ghidra':
        for name in ('GHIDRA_HOME', 'GHIDRA_INSTALL_DIR'):
            if os.environ.get(name):
                values.append(Path(os.environ[name]))
        for base in (pf, Path('C:/Tools'), Path.home() / 'Tools'):
            values.extend(sorted(base.glob('ghidra*'), reverse=True))
    seen, result = set(), []
    for value in values:
        if not value:
            continue
        path = Path(value)
        key = str(path).casefold()
        if key not in seen and not str(path).startswith(('\\\\', '//')):
            seen.add(key)
            result.append(str(path))
    return result


def _find_winget():
    values = [shutil.which('winget')]
    if os.environ.get('LOCALAPPDATA'):
        values.append(Path(os.environ['LOCALAPPDATA']) / 'Microsoft/WindowsApps/winget.exe')
    return next((str(value) for value in values if value and Path(value).is_file()), None)


async def _probe(tool_id, *, cancel=None):
    info = TOOL_CATALOG[tool_id]
    row = dict(id=tool_id, **info, status='missing', path='', version='',
               can_install=False, reason='설치된 실행 도구를 찾지 못했습니다.')
    for candidate in _candidate_paths(tool_id):
        if _cancelled(cancel):
            row['status'] = 'cancelled'
            return row
        path = Path(candidate)
        if tool_id == 'ghidra':
            launcher = path / 'support' / ('analyzeHeadless.bat' if os.name == 'nt'
                                         else 'analyzeHeadless')
            if launcher.is_file() and (path / 'Ghidra/application.properties').is_file():
                row.update(status='ready', path=str(path), reason='Ghidra 설치 폴더를 찾았습니다.')
                return row
            continue
        # Store execution aliases can open the Store instead of running Python.
        if tool_id == 'python' and 'windowsapps' in {part.casefold() for part in path.parts}:
            continue
        if not path.is_file() or path.suffix.lower() in ('.bat', '.cmd'):
            continue
        if tool_id == 'python':
            code = 'import json,sys; print(json.dumps(dict(version=list(sys.version_info[:3]),executable=sys.executable)))'
            argv = [str(path)] + (['-3'] if path.name.lower() in ('py', 'py.exe') else [])
            argv += ['-I', '-c', code]
        else:
            argv = [str(path), '-version' if tool_id == 'java' else '--version']
        options = dict(timeout=8, cancel=cancel, output_limit=32768)
        if tool_id == 'python':
            env = dict(os.environ, PYTHON_MANAGER_AUTOMATIC_INSTALL='false')
            env.pop('PYLAUNCHER_ALLOW_INSTALL', None)
            env.pop('PYLAUNCHER_ALWAYS_INSTALL', None)
            options['env'] = env
        checked = await run_process(argv, **options)
        if checked.get('cancelled'):
            row['status'] = 'cancelled'
            return row
        if not _ok(checked):
            continue
        text = (checked.get('stdout', '') + '\n' + checked.get('stderr', '')).strip()
        if tool_id == 'python':
            try:
                value = json.loads(checked['stdout'])
                version = tuple(value['version'])
                if len(version) < 2 or version[:2] < (3, 11):
                    continue
                executable = str(value['executable'])
                version_text = '.'.join(map(str, version))
            except (KeyError, TypeError, ValueError):
                continue
        elif tool_id == 'java':
            major = _java_major(text)
            if major is None or major < 21 or not path.with_name('javac.exe' if os.name == 'nt' else 'javac').is_file():
                continue
            executable, version_text = str(path), text.splitlines()[0][:160]
        else:
            if not text:
                continue
            executable, version_text = str(path), text.splitlines()[0][:160]
        reason = '실행 가능한 설치를 확인했습니다. PATH를 바꾸지 않고 이 경로를 사용합니다.'
        if tool_id == 'docker':
            reason += ' Docker 엔진·가상화 준비 여부는 분석 환경 확인에서 별도로 검사합니다.'
        row.update(status='ready', path=executable, version=version_text, reason=reason)
        return row
    return row


async def inspect_prerequisites(*, cancel=None, on_event=None):
    """Bounded local checks only; never installs or starts services."""
    packaged = bool(getattr(sys, 'frozen', False))
    result = dict(status='cancelled' if _cancelled(cancel) else 'partial',
                  packaged=packaged,
                  app_runtime=dict(python='bundled' if packaged else 'source', git_required=False),
                  winget=dict(path='', ready=False, download_url=WINGET_URL), tools=[])
    if _cancelled(cancel):
        return result
    winget = _find_winget()
    if winget:
        checked = await run_process([winget, '--version'], timeout=8, cancel=cancel, output_limit=32768)
        result['winget'].update(path=winget, ready=_ok(checked))
    for tool_id in TOOL_CATALOG:
        if _cancelled(cancel):
            result['status'] = 'cancelled'
            return result
        await _emit(on_event, TOOL_CATALOG[tool_id]['name'] + ' 설치 상태 확인 중', tool_id=tool_id)
        row = await _probe(tool_id, cancel=cancel)
        row['can_install'] = bool(result['winget']['ready'] and row['package_id']
                                  and row['status'] != 'ready' and os.name == 'nt')
        result['tools'].append(row)
    if _cancelled(cancel) or any(row['status'] == 'cancelled' for row in result['tools']):
        result['status'] = 'cancelled'
    else:
        result['status'] = 'ready' if all(row['optional'] or row['status'] == 'ready'
                                         for row in result['tools']) else 'partial'
    return result


async def install_prerequisite(tool_id, *, cancel=None, on_event=None):
    """Install one allowlisted tool after a user action, then verify it again."""
    if tool_id not in TOOL_CATALOG:
        raise ValueError('Unknown prerequisite: ' + str(tool_id))
    info = TOOL_CATALOG[tool_id]
    result = dict(status='cancelled', tool=dict(id=tool_id, **info, status='missing', can_install=False),
                  download_url=info['download_url'], winget_url=WINGET_URL,
                  message='설치 작업을 중단했습니다.')
    if _cancelled(cancel):
        return result
    current = await _probe(tool_id, cancel=cancel)
    result['tool'] = current
    if current['status'] == 'ready':
        result.update(status='ready', message='이미 실행 가능한 도구가 설치되어 있습니다.')
        return result
    if _cancelled(cancel) or current['status'] == 'cancelled':
        return result
    winget = _find_winget()
    if os.name != 'nt' or not winget or not info['package_id']:
        result.update(status='manual_required', message='공식 설치 페이지에서 설치한 뒤 다시 확인해 주세요. Windows 자동 설치에는 앱 설치 관리자(WinGet)가 필요합니다.')
        return result
    manager = await run_process([winget, '--version'], timeout=8, cancel=cancel, output_limit=32768)
    if manager.get('cancelled') or _cancelled(cancel):
        return result
    if not _ok(manager):
        result.update(status='manual_required', message='Windows 설치 관리자를 실행하지 못했습니다. 공식 설치 페이지에서 설치하거나 앱 설치 관리자(WinGet)를 복구한 뒤 다시 확인해 주세요.')
        return result
    # Preserve the retry action on installer failures and early-return states.
    # The manager was actually executed, rather than inferred from PATH alone.
    current['can_install'] = True
    await _emit(on_event, info['name'] + ' 설치 중 · Windows 권한 요청이 나타나면 확인해 주세요.',
                tool_id=tool_id, status='installing')
    argv = [winget, 'install', '--id', info['package_id'], '--exact', '--source', 'winget',
            '--accept-source-agreements', '--accept-package-agreements', '--disable-interactivity',
            '--no-upgrade']
    executed = await run_process(argv, timeout=1800, cancel=cancel, output_limit=2 * 1024 * 1024)
    result['process'] = executed
    code = executed.get('returncode')
    unsigned = (code & 0xFFFFFFFF) if isinstance(code, int) else None
    if executed.get('cancelled') or _cancelled(cancel) or unsigned in (1602, 0x8A150005, 0x8A15010C):
        result['message'] = '설치 대기를 중단했습니다. 이미 적용된 변경은 남을 수 있으니 준비 상태를 다시 확인해 주세요.'
        return result
    if unsigned in (3010, 1641, 0x8A150109, 0x8A15010A, 0x8A15010B):
        result.update(status='reboot_required', message='설치 관리자가 Windows 재시작을 요청했습니다. 작업을 저장하고 직접 재시작한 뒤 준비 상태를 다시 확인해 주세요.')
        return result
    if executed.get('timed_out'):
        result.update(status='failed', message='설치 대기 시간이 지났습니다. Windows 설치 상태를 확인한 뒤 다시 시도해 주세요.')
        return result
    checked = await _probe(tool_id, cancel=cancel)
    checked['can_install'] = checked['status'] != 'ready'
    result['tool'] = checked
    if _cancelled(cancel) or checked['status'] == 'cancelled':
        return result
    if checked['status'] == 'ready' and (code == 0 or unsigned in (0x8A15010D, 0x8A150061)):
        result.update(status='ready', message='설치와 실행 확인을 마쳤습니다. 분석 환경 준비를 계속할 수 있습니다.')
    else:
        result.update(status='failed', message='설치 후 실행 가능한 도구를 확인하지 못했습니다. 공식 설치 페이지와 상세 기록을 확인한 뒤 다시 검사해 주세요.')
    return result
