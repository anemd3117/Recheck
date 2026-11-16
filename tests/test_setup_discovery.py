"""Discovery fixtures never execute installed tools or contact external services."""

import asyncio
import json
import threading
from pathlib import Path

import httpx
import pytest

from recheck import setup_discovery as setup
from recheck.models import Settings
from recheck.ollama import OllamaClient


def process(stdout='', **extra):
    return {
        **dict(
            returncode=0,
            stdout=stdout,
            stderr='',
            error=None,
            timed_out=False,
            cancelled=False,
            truncated=False,
        ),
        **extra,
    }


def ghidra(path, minimum=21):
    (path / 'support').mkdir(parents=True)
    (path / 'Ghidra').mkdir()
    for name in ('analyzeHeadless.bat', 'analyzeHeadless'):
        (path / 'support' / name).write_text('fixture; never executed')
    (path / 'Ghidra/application.properties').write_text(
        f'application.version=12.1\napplication.java.min={minimum}\n', encoding='utf-8'
    )
    return path


def java(path):
    (path / 'bin').mkdir(parents=True)
    for name in ('java', 'java.exe', 'javac', 'javac.exe'):
        (path / 'bin' / name).write_text('fixture; never executed')
    return path


@pytest.fixture
def isolated(monkeypatch):
    monkeypatch.setattr(setup, '_registry_locations', lambda: [])
    monkeypatch.setattr(
        setup.GhidraAdapter,
        'discover',
        staticmethod(lambda explicit=None: Path(explicit) if explicit else None),
    )
    monkeypatch.setattr(setup.shutil, 'which', lambda name: None)
    for name in (
        'GHIDRA_HOME',
        'GHIDRA_INSTALL_DIR',
        'JAVA_HOME',
        'JAVA_HOME_OVERRIDE',
        'DOCKER_HOST',
    ):
        monkeypatch.delenv(name, raising=False)


def test_bounded_search_finds_nested_install_and_ignores_archives(tmp_path, isolated):
    root = tmp_path / 'Desktop'
    install = ghidra(root / 'LKM/CTF/Wrapper/ghidra_12_PUBLIC')
    ignored = ghidra(root / 'verification/old/ghidra_9_PUBLIC')
    jdk = java(root / 'Tools/jdk25')
    found = setup.find_candidates(Settings(), search_roots=[root], max_depth=5)
    assert str(install) in found['ghidra']
    assert str(ignored) not in found['ghidra']
    assert str(jdk) in found['java']
    assert found['searched_directories'] < 20
    shallow = setup.find_candidates(Settings(), search_roots=[root], max_depth=1)
    assert str(install) not in shallow['ghidra']


def test_search_limits_and_cancel_preserve_unknown(tmp_path, isolated):
    (tmp_path / 'a/b/c').mkdir(parents=True)
    limited = setup.find_candidates(Settings(), search_roots=[tmp_path], max_directories=1)
    assert limited['search_truncated']
    event = threading.Event()
    event.set()
    result = asyncio.run(setup.discover_setup(Settings(), event, search_roots=[tmp_path]))
    assert result['status'] == 'cancelled' and result['patch'] == {}


@pytest.mark.parametrize('suffix', ['bin', 'bin/java.exe', 'bin/java'])
def test_java_root_normalizes_file_or_bin(tmp_path, suffix):
    assert setup.java_root(tmp_path / suffix) == tmp_path


def test_ghidra_root_normalizes_launcher(tmp_path):
    assert setup.ghidra_root(tmp_path / 'support/analyzeHeadless.bat') == tmp_path


def patch_local_probes(monkeypatch):
    async def docker(current, cancel):
        return setup._component(current, current, 'valid', 'fixture')

    async def ollama(current, cancel):
        return setup._component(current, current, 'valid', 'fixture')

    monkeypatch.setattr(setup, '_discover_docker', docker)
    monkeypatch.setattr(setup, '_discover_ollama', ollama)


def test_valid_existing_paths_kept_without_saving(tmp_path, isolated, monkeypatch):
    g = ghidra(tmp_path / 'ghidra')
    j = java(tmp_path / 'jdk')

    async def fake(argv, **kwargs):
        assert argv[1:] == ['-version']
        return process(stderr='openjdk version "25.0.1"')

    monkeypatch.setattr(setup, 'run_process', fake)
    monkeypatch.setattr('recheck.adapters.run_process', fake)
    patch_local_probes(monkeypatch)
    settings = Settings(ghidra_path=str(g), java_home=str(j), docker_context='custom')
    before = settings.model_dump()
    found = asyncio.run(setup.discover_setup(settings, search_roots=[]))
    assert found['patch'] == {} and found['status'] == 'success'
    assert settings.model_dump() == before
    assert not (tmp_path / 'settings.json').exists()
    assert found['components']['ghidra_path']['status'] == 'valid'


def test_bad_manual_values_are_preserved_and_only_verified_alternatives_proposed(
    tmp_path, isolated, monkeypatch
):
    g = ghidra(tmp_path / 'Tools/ghidra')
    j = java(tmp_path / 'Tools/jdk')

    async def fake(*args, **kwargs):
        return process(stderr='openjdk version "25.0.1"')

    monkeypatch.setattr(setup, 'run_process', fake)
    monkeypatch.setattr('recheck.adapters.run_process', fake)
    patch_local_probes(monkeypatch)
    settings = Settings(ghidra_path='missing-ghidra', java_home='missing-java')
    found = asyncio.run(setup.discover_setup(settings, search_roots=[tmp_path]))
    assert settings.ghidra_path == 'missing-ghidra' and settings.java_home == 'missing-java'
    assert found['patch'] == {'java_home': str(j), 'ghidra_path': str(g)}
    assert found['components']['ghidra_path']['current'] == 'missing-ghidra'
    assert found['components']['ghidra_path']['status'] == 'found'


def test_incompatible_java_is_not_proposed(tmp_path, isolated, monkeypatch):
    g = ghidra(tmp_path / 'ghidra', minimum=25)
    j = java(tmp_path / 'jdk21')

    async def fake(*args, **kwargs):
        return process(stderr='openjdk version "21.0.8"')

    monkeypatch.setattr(setup, 'run_process', fake)
    patch_local_probes(monkeypatch)
    result = asyncio.run(
        setup.discover_setup(Settings(ghidra_path=str(g), java_home=str(j)), search_roots=[])
    )
    assert result['patch'] == {}
    assert result['components']['java_home']['status'] == 'not_found'
    assert result['components']['ghidra_path']['status'] == 'needs_java'


def test_missing_tools_do_not_abort_general_settings(tmp_path, isolated, monkeypatch):
    patch_local_probes(monkeypatch)
    result = asyncio.run(setup.discover_setup(Settings(), search_roots=[]))
    assert result['status'] == 'partial'
    assert result['components']['ghidra_path']['status'] == 'not_found'
    assert result['components']['java_home']['proposed'] is None


def test_docker_valid_custom_context_kept_and_no_switch(monkeypatch):
    monkeypatch.setattr(setup, 'discover_docker_cli', lambda: 'fixture-docker')
    calls = []

    async def call(self, *args, **kwargs):
        calls.append((self.context, args))
        if args[:2] == ('context', 'show'):
            return process('desktop-linux\n')
        if args[:2] == ('context', 'inspect'):
            return process(
                json.dumps([{'Name': args[2], 'Endpoints': {'docker': {'Host': 'npipe://local'}}}])
            )
        return process(json.dumps({'Server': {'Version': '1'}}))

    monkeypatch.setattr(setup.DockerAdapter, 'call', call)
    result = asyncio.run(setup._discover_docker('custom', None))
    assert result['status'] == 'valid' and result['proposed'] == 'custom'
    assert ('custom', ('version', '--format', '{{json .}}')) in calls
    assert all('use' not in args and 'start' not in args for _, args in calls)


def test_docker_remote_context_never_contacted_and_local_alternative_proposed(monkeypatch):
    monkeypatch.setattr(setup, 'discover_docker_cli', lambda: 'fixture-docker')
    calls = []

    async def call(self, *args, **kwargs):
        calls.append((self.context, args))
        if args[:2] == ('context', 'show'):
            return process('desktop-linux')
        if args[:2] == ('context', 'inspect'):
            name = args[2]
            host = 'tcp://example.invalid:2376' if name == 'remote' else 'npipe://local'
            return process(json.dumps([{'Name': name, 'Endpoints': {'docker': {'Host': host}}}]))
        assert self.context == 'desktop-linux'
        return process(json.dumps({'Server': {'Version': '1'}}))

    monkeypatch.setattr(setup.DockerAdapter, 'call', call)
    result = asyncio.run(setup._discover_docker('remote', None))
    assert result['status'] == 'found' and result['proposed'] == 'desktop-linux'
    assert not any(context == 'remote' and args[0] == 'version' for context, args in calls)


def test_docker_global_host_override_is_not_contacted(monkeypatch):
    monkeypatch.setenv('DOCKER_HOST', 'tcp://example.invalid:2376')
    monkeypatch.setattr(setup, 'discover_docker_cli', lambda: 'fixture-docker')

    async def call(self, *args, **kwargs):
        if args[:2] == ('context', 'show'):
            return process('desktop-linux')
        if args[:2] == ('context', 'inspect'):
            return process(
                json.dumps(
                    [{'Name': 'desktop-linux', 'Endpoints': {'docker': {'Host': 'npipe://local'}}}]
                )
            )
        assert self.context == 'desktop-linux'
        return process(json.dumps({'Server': {'Version': '1'}}))

    monkeypatch.setattr(setup.DockerAdapter, 'call', call)
    result = asyncio.run(setup._discover_docker('', None))
    assert result['status'] == 'found' and result['proposed'] == 'desktop-linux'


def test_ollama_remote_address_rejected_before_request(monkeypatch):
    requests = []

    def handler(request):
        requests.append(str(request.url))
        assert request.url.host == '127.0.0.1'
        return httpx.Response(200, json={'version': '0.1.0'})

    monkeypatch.setattr(
        setup,
        'OllamaClient',
        lambda url, timeout: OllamaClient(url, timeout, transport=httpx.MockTransport(handler)),
    )
    result = asyncio.run(setup._discover_ollama('http://external.invalid:11434', None))
    assert result['status'] == 'found' and result['proposed'] == setup.DEFAULT_URL
    assert len(requests) == 1
    assert not result['candidates'][0]['valid']


def test_ollama_existing_local_endpoint_preserved(monkeypatch):
    def handler(request):
        assert request.url.port == 12222
        return httpx.Response(200, json={'version': '0.1.0'})

    monkeypatch.setattr(
        setup,
        'OllamaClient',
        lambda url, timeout: OllamaClient(url, timeout, transport=httpx.MockTransport(handler)),
    )
    result = asyncio.run(setup._discover_ollama('http://127.0.0.1:12222', None))
    assert result['status'] == 'valid' and result['proposed'].endswith(':12222')


def test_docker_standard_windows_install_without_path(tmp_path, monkeypatch):
    from recheck.adapters import discover_docker_cli, DockerAdapter

    executable = tmp_path / 'Programs/DockerDesktop/resources/bin/docker.exe'
    executable.parent.mkdir(parents=True)
    executable.write_text('fixture')
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    monkeypatch.setattr('recheck.adapters.shutil.which', lambda name: None)
    assert discover_docker_cli() == str(executable)
    assert DockerAdapter().cli == str(executable)
