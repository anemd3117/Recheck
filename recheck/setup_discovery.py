"""Read-only local setup discovery. Returns proposals; never saves or starts services."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timezone
import inspect
import json
import os
from pathlib import Path
import re
import shutil
import time

from .adapters import DockerAdapter, GhidraAdapter, _ok, discover_docker_cli
from .models import Settings
from .ollama import DEFAULT_URL, OllamaClient, OllamaError, cancelled
from .process import run_process
from .paths import plain_local_path

_SKIP = {
    '.git',
    '.venv',
    'venv',
    'node_modules',
    '__pycache__',
    'verification',
    'dist',
    'build',
    '$recycle.bin',
    'windows',
    'appdata',
}


def _path(value):
    return Path(os.path.expandvars(str(value).strip().strip('"'))).expanduser()


def _unique(values):
    seen, result = set(), []
    for value in values:
        if not value:
            continue
        path = _path(value)
        key = str(plain_local_path(path)).casefold()
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def ghidra_root(value):
    path = _path(value)
    if path.name.lower().startswith('analyzeheadless'):
        path = path.parent.parent
    elif path.name.lower() == 'support':
        path = path.parent
    return path


def java_root(value):
    path = _path(value)
    if path.name.lower() in ('java', 'java.exe', 'javac', 'javac.exe'):
        path = path.parent
    if path.name.lower() == 'bin':
        path = path.parent
    return path


def _java_executable(root):
    if str(plain_local_path(root)).startswith(('\\\\', '//')):
        return None
    return next(
        (root / 'bin' / name for name in ('java.exe', 'java') if (root / 'bin' / name).is_file()),
        None,
    )


def _ghidra_properties(root):
    if str(plain_local_path(root)).startswith(('\\\\', '//')):
        return None
    launcher = root / 'support' / ('analyzeHeadless.bat' if os.name == 'nt' else 'analyzeHeadless')
    props = root / 'Ghidra/application.properties'
    if not launcher.is_file() or not props.is_file():
        return None
    try:
        values = dict(
            line.split('=', 1)
            for line in props.read_text(encoding='utf-8').splitlines()
            if '=' in line and not line.lstrip().startswith('#')
        )
        values = {k.strip(): v.strip() for k, v in values.items()}
        int(values.get('application.java.min', '21') or '21')
        if values.get('application.java.max'):
            int(values['application.java.max'])
        return values
    except (OSError, ValueError, UnicodeError):
        return None


def _registry_locations():
    """Read known vendor/uninstall locations only; never execute registry strings."""
    result = []
    if os.name != 'nt':
        return result
    import winreg

    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for branch in (
            r'Software\Microsoft\Windows\CurrentVersion\Uninstall',
            r'Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall',
        ):
            try:
                with winreg.OpenKey(hive, branch) as key:
                    for index in range(min(winreg.QueryInfoKey(key)[0], 3000)):
                        try:
                            with winreg.OpenKey(key, winreg.EnumKey(key, index)) as child:
                                name = winreg.QueryValueEx(child, 'DisplayName')[0]
                                if any(
                                    word in str(name).lower()
                                    for word in (
                                        'ghidra',
                                        'jdk',
                                        'java',
                                        'temurin',
                                        'adoptium',
                                        'docker',
                                        'ollama',
                                    )
                                ):
                                    location = winreg.QueryValueEx(child, 'InstallLocation')[0]
                                    if location:
                                        result.append(location)
                        except OSError:
                            continue
            except OSError:
                continue
        for branch in (r'Software\JavaSoft\JDK', r'Software\JavaSoft\Java Development Kit'):
            try:
                with winreg.OpenKey(hive, branch) as key:
                    for index in range(min(winreg.QueryInfoKey(key)[0], 100)):
                        try:
                            with winreg.OpenKey(key, winreg.EnumKey(key, index)) as child:
                                result.append(winreg.QueryValueEx(child, 'JavaHome')[0])
                        except OSError:
                            continue
            except OSError:
                continue
    return result


def _default_roots():
    home = Path.home()
    roots = [home / name for name in ('Desktop', 'Downloads', 'Documents')]
    for variable in ('OneDrive', 'OneDriveConsumer', 'OneDriveCommercial'):
        if os.getenv(variable):
            roots.extend(Path(os.environ[variable]) / name for name in ('Desktop', 'Documents'))
    roots += [Path('C:/Tools'), Path('D:/Tools')]
    for variable in ('ProgramFiles', 'ProgramFiles(x86)'):
        if os.getenv(variable):
            roots.append(Path(os.environ[variable]))
    if os.getenv('LOCALAPPDATA'):
        roots.append(Path(os.environ['LOCALAPPDATA']) / 'Programs')
    return roots


def find_candidates(
    settings, cancel=None, *, search_roots=None, max_depth=5, max_directories=2400, timeout=6
):
    """Bounded directory search; no drive roots, links, file content or user data scan."""
    ghidra = [settings.ghidra_path, os.getenv('GHIDRA_HOME'), os.getenv('GHIDRA_INSTALL_DIR')]
    discovered = GhidraAdapter.discover()
    if discovered:
        ghidra.append(discovered)
    headless = shutil.which('analyzeHeadless') or shutil.which('analyzeHeadless.bat')
    if headless:
        ghidra.append(headless)
    java = [
        settings.java_home,
        os.getenv('JAVA_HOME'),
        os.getenv('JAVA_HOME_OVERRIDE'),
        shutil.which('java'),
    ]
    registry = _registry_locations()
    roots = _unique(search_roots if search_roots is not None else _default_roots() + registry)
    # Registry locations can directly identify installs even if bounded traversal ends early.
    for location in registry:
        ghidra.append(location)
        java.append(location)
    queue = deque(
        (root, 0)
        for root in roots
        if root.parent != root and not str(plain_local_path(root)).startswith(('\\\\', '//'))
    )
    visited, count, deadline = set(), 0, time.monotonic() + timeout
    truncated = False
    while queue:
        if cancelled(cancel):
            break
        if count >= max_directories or time.monotonic() >= deadline:
            truncated = True
            break
        folder, depth = queue.popleft()
        key = str(folder).casefold()
        if key in visited or folder.is_symlink() or getattr(folder, 'is_junction', lambda: False)():
            continue
        visited.add(key)
        count += 1
        try:
            if not folder.is_dir():
                continue
            if _ghidra_properties(folder):
                ghidra.append(folder)
                continue
            if _java_executable(folder):
                java.append(folder)
                continue
            if depth < max_depth:
                children = []
                with os.scandir(folder) as entries:
                    for index, entry in enumerate(entries):
                        if index >= 5000:
                            truncated = True
                            break
                        if (
                            entry.name.casefold() not in _SKIP
                            and not entry.name.startswith('.')
                            and entry.is_dir(follow_symlinks=False)
                        ):
                            children.append(Path(entry.path))
                children.sort(
                    key=lambda p: (
                        not any(
                            x in p.name.lower() for x in ('ghidra', 'jdk', 'java', 'tools', 'ctf')
                        ),
                        p.name.casefold(),
                    )
                )
                queue.extend((child, depth + 1) for child in children)
        except OSError:
            continue
    return {
        'ghidra': [str(p) for p in _unique(ghidra_root(p) for p in ghidra if p)],
        'java': [str(p) for p in _unique(java_root(p) for p in java if p)],
        'searched_directories': count,
        'search_truncated': truncated,
        'search_roots': [str(p) for p in roots],
    }


async def _progress(callback, message):
    if callback:
        result = callback(message)
        if inspect.isawaitable(result):
            await result


def _component(
    current, proposed=None, status='not_found', reason='', candidates=None, validation=None
):
    return dict(
        current=current,
        proposed=proposed,
        status=status,
        reason=reason,
        candidates=candidates or [],
        validation=validation or {},
    )


async def discover_setup(settings: Settings, cancel=None, on_progress=None, *, search_roots=None):
    """Return validated candidate patch; no writes, installs, context switch, or service start."""
    result = dict(
        schema_version='1.0',
        checked_at=datetime.now(timezone.utc).isoformat(),
        status='partial',
        patch={},
        components={},
        warnings=[],
    )
    components = result['components']
    await _progress(on_progress, '설치 폴더와 환경 변수에서 Ghidra·JDK를 찾는 중')
    found = await asyncio.to_thread(find_candidates, settings, cancel, search_roots=search_roots)
    result['search'] = found
    if found['search_truncated']:
        result['warnings'].append(
            '탐색 범위 또는 시간 상한에 도달했습니다. 찾지 못한 설치는 직접 선택할 수 있습니다.'
        )
    if cancelled(cancel):
        result['status'] = 'cancelled'
        return result

    ghidra_candidates = []
    for value in found['ghidra']:
        root = ghidra_root(value)
        props = _ghidra_properties(root)
        ghidra_candidates.append(
            dict(
                value=str(root),
                valid=bool(props),
                version=(props or {}).get('application.version'),
                properties=props or {},
            )
        )
    selected_ghidra = next((c for c in ghidra_candidates if c['valid']), None)
    props = selected_ghidra['properties'] if selected_ghidra else {}
    required = int(props.get('application.java.min', '21') or '21')
    maximum = int(props['application.java.max']) if props.get('application.java.max') else None
    await _progress(on_progress, 'Java 실행과 Ghidra 요구 버전을 확인하는 중')
    java_candidates = []
    # Limit actual trusted Java version probes even when many JDKs are installed.
    for value in found['java']:
        if cancelled(cancel):
            break
        root = java_root(value)
        executable = _java_executable(root)
        if not executable:
            java_candidates.append(
                dict(value=str(root), valid=False, reason='bin/java 실행 파일이 없습니다.')
            )
            continue
        if not any((root / 'bin' / name).is_file() for name in ('javac.exe', 'javac')):
            java_candidates.append(
                dict(
                    value=str(root),
                    valid=False,
                    reason='JDK의 bin/javac 컴파일러가 없습니다. JRE는 제안하지 않습니다.',
                )
            )
            continue
        if sum('process' in item for item in java_candidates) >= 8:
            result['warnings'].append('JDK 실행 검사는 최대 8개 설치로 제한했습니다.')
            break
        process = await run_process(
            [str(executable), '-version'], timeout=5, cancel=cancel, output_limit=16384
        )
        text = process.get('stderr', '') + process.get('stdout', '')
        match = re.search(r'(?:version\s+"|(?:openjdk|java)\s+)(?:1\.)?(\d+)', text)
        major = int(match.group(1)) if match else None
        valid = (
            _ok(process)
            and major is not None
            and major >= required
            and (maximum is None or major <= maximum)
        )
        java_candidates.append(
            dict(
                value=str(root),
                valid=valid,
                major=major,
                process=process,
                reason=(
                    'Ghidra 요구 Java 버전과 일치합니다.'
                    if valid
                    else f'Ghidra에는 Java {required} 이상'
                    + (f' {maximum} 이하' if maximum else '')
                    + '이 필요합니다.'
                ),
            )
        )
    selected_java = next((c for c in java_candidates if c['valid']), None)
    if selected_java:
        proposed = selected_java['value']
        preserved = (
            settings.java_home and str(_path(settings.java_home)).casefold() == proposed.casefold()
        )
        components['java_home'] = _component(
            settings.java_home,
            proposed,
            'valid' if preserved else 'found',
            (
                '현재 유효한 JDK 경로를 유지합니다.'
                if preserved
                else '실행 파일과 Java 버전을 확인한 JDK 경로입니다.'
            ),
            java_candidates,
            selected_java,
        )
        if not preserved:
            result['patch']['java_home'] = proposed
    else:
        components['java_home'] = _component(
            settings.java_home,
            reason='호환 Java 실행을 확인하지 못했습니다. 기존 입력은 유지합니다.',
            candidates=java_candidates,
        )
    if selected_ghidra:
        proposed = selected_ghidra['value']
        validation = selected_ghidra.copy()
        if selected_java and not cancelled(cancel):
            adapter = GhidraAdapter(home=proposed, java_home=selected_java['value'])
            validation = await adapter.probe()
        preserved = bool(
            settings.ghidra_path and ghidra_root(settings.ghidra_path) == Path(proposed)
        )
        status = ('valid' if preserved else 'found') if validation.get('ready') else 'needs_java'
        components['ghidra_path'] = _component(
            settings.ghidra_path,
            proposed,
            status,
            (
                '현재 유효한 Ghidra 경로를 유지합니다.'
                if preserved
                else 'Headless와 설치 메타데이터를 확인했습니다.'
            ),
            ghidra_candidates,
            validation,
        )
        if not preserved and validation.get('ready'):
            result['patch']['ghidra_path'] = proposed
    else:
        components['ghidra_path'] = _component(
            settings.ghidra_path,
            reason='범위 안에서 유효한 Ghidra Headless 설치를 찾지 못했습니다.',
            candidates=ghidra_candidates,
        )
    if cancelled(cancel):
        result['status'] = 'cancelled'
        return result

    await _progress(on_progress, 'Docker의 현재 로컬 컨텍스트를 확인하는 중')
    components['docker_context'] = await _discover_docker(settings.docker_context, cancel)
    docker = components['docker_context']
    if docker['status'] == 'found' and docker['proposed'] != settings.docker_context:
        result['patch']['docker_context'] = docker['proposed']
    if cancelled(cancel):
        result['status'] = 'cancelled'
        return result
    await _progress(on_progress, '로컬 Ollama 연결을 확인하는 중')
    components['ollama_url'] = await _discover_ollama(settings.ollama_url, cancel)
    ollama = components['ollama_url']
    if ollama['status'] == 'found' and ollama['proposed'] != settings.ollama_url:
        result['patch']['ollama_url'] = ollama['proposed']
    result['status'] = (
        'cancelled'
        if cancelled(cancel)
        else (
            'success'
            if all(c['status'] in ('valid', 'found') for c in components.values())
            else 'partial'
        )
    )
    return result


async def _discover_docker(current, cancel):
    cli = discover_docker_cli()
    if not cli:
        return _component(
            current,
            reason='PATH에서 Docker CLI를 찾지 못했습니다. 설치나 서비스 시작은 하지 않았습니다.',
        )
    adapter = DockerAdapter(cli=cli)
    show = await adapter.call('context', 'show', timeout=4, cancel=cancel)
    active = show.get('stdout', '').strip() if _ok(show) else ''
    candidates, selected = [], None
    for context in dict.fromkeys([current] + ([active] if active else [])):
        if cancelled(cancel):
            break
        inspected = await adapter.call(
            'context', 'inspect', *([context] if context else []), timeout=4, cancel=cancel
        )
        try:
            info = json.loads(inspected.get('stdout', ''))[0]
            endpoint = info.get('Endpoints', {}).get('docker', {}).get('Host', '')
            local = endpoint.startswith(('npipe:', 'unix:'))
        except (ValueError, IndexError, TypeError, AttributeError):
            info, local = {}, False
        candidate = dict(
            value=context or active, valid=False, local_endpoint=local, context_info=info
        )
        if _ok(inspected) and local:
            # Only now may a version request contact the endpoint; remote contexts stay read-only metadata.
            effective_context = info.get('Name') or context or active
            if not effective_context:
                candidates.append(candidate)
                continue
            probe = DockerAdapter(cli=cli, context=effective_context)
            version = await probe.call(
                'version', '--format', '{{json .}}', timeout=6, cancel=cancel
            )
            try:
                data = json.loads(version.get('stdout', ''))
                candidate.update(valid=_ok(version) and bool(data.get('Server')), version=data)
            except (ValueError, AttributeError):
                pass
            candidate['process'] = version
        candidates.append(candidate)
        if candidate['valid'] and selected is None:
            selected = candidate
            # A valid explicit context is never replaced by the current global context.
            break
    if selected:
        host_override = os.getenv('DOCKER_HOST', '')
        unsafe_override = bool(host_override and not host_override.startswith(('npipe:', 'unix:')))
        preserve = (not current and not unsafe_override) or current == selected['value']
        return _component(
            current,
            current if preserve else selected['value'],
            'valid' if preserve else 'found',
            (
                '현재 유효한 로컬 Docker 연결을 유지합니다.'
                if preserve
                else '현재 Docker 컨텍스트의 로컬 엔진 연결을 확인했습니다.'
            ),
            candidates,
            selected,
        )
    return _component(
        current,
        reason='로컬 Docker 엔진 연결을 확인하지 못했습니다. 현재 컨텍스트와 입력은 변경하지 않았습니다.',
        candidates=candidates,
    )


async def _discover_ollama(current, cancel):
    candidates = []
    for url in dict.fromkeys([current, DEFAULT_URL]):
        if cancelled(cancel):
            break
        client = None
        try:
            client = OllamaClient(url, timeout=5)
            probe = await client.probe(cancel)
            candidates.append(dict(value=url, valid=bool(probe.get('connected')), validation=probe))
            if probe.get('connected'):
                preserve = url == current
                return _component(
                    current,
                    url,
                    'valid' if preserve else 'found',
                    (
                        '현재 유효한 로컬 Ollama 주소를 유지합니다.'
                        if preserve
                        else '기본 루프백 주소에서 Ollama API 연결을 확인했습니다.'
                    ),
                    candidates,
                    probe,
                )
        except (OllamaError, ValueError) as exc:
            candidates.append(dict(value=url, valid=False, reason=str(exc)))
        finally:
            if client:
                await client.close()
    return _component(
        current,
        reason='로컬 Ollama 연결을 확인하지 못했습니다. 기존 주소는 유지하며 서버를 시작하지 않았습니다.',
        candidates=candidates,
    )
