"""MODE/DIAG regressions at real CLI/engine boundaries; targets remain inert."""

import asyncio
import json
import struct
from pathlib import Path

import pytest

from recheck import cli, prerequisites
from recheck.adapters import DockerAdapter, GhidraAdapter
from recheck.engine import Engine
from recheck.models import Settings


def inert_elf(path):
    data = bytearray(64)
    data[:6] = b'\x7fELF\x02\x01'
    struct.pack_into('<HH', data, 16, 2, 62)
    path.write_bytes(data)
    return path


def process(stdout='', stderr='', returncode=0):
    return dict(stdout=stdout, stderr=stderr, returncode=returncode,
                error=None, cancelled=False, timed_out=False, truncated=False)


@pytest.mark.parametrize('scope', ['static', 'dynamic', 'both'])
@pytest.mark.parametrize('unsupported', [True, False])
def test_cli_preflight_rejects_before_registering_run(tmp_path, monkeypatch, capsys, scope, unsupported):
    artifact = tmp_path / 'input.bin'
    artifact.write_bytes(b'unsupported') if unsupported else inert_elf(artifact)
    tests = tmp_path / 'tests.json'
    tests.write_text('[{"id":"baseline"}]')
    engine = Engine(Settings(static_backend='direct'), tmp_path / 'results')

    async def environment(*args, **kwargs):
        return dict(ghidra=dict(ready=False, backend='direct'), docker=dict(ready=False))

    monkeypatch.setattr(engine, 'environment', environment)
    class InertAdapter:
        async def analyze(self, *args, **kwargs):
            return dict(status='blocked', complete=False, functions=[])

        async def prepare_image(self, *args, **kwargs):
            return dict(status='blocked', error='inert missing environment')

    monkeypatch.setattr(engine, '_ghidra', InertAdapter)
    monkeypatch.setattr(engine, '_execution_adapter', lambda run: InertAdapter())
    monkeypatch.setattr(cli, 'Engine', lambda *args, **kwargs: engine)
    code = cli.main(['analyze', '--original', str(artifact), '--reimplementation', str(artifact),
                     '--tests', str(tests), '--analysis-mode', scope, '--ai-mode', 'off'])
    result = json.loads(capsys.readouterr().out)
    assert code == 2
    assert engine.store.list_runs() == []
    assert result['code'] == 'analysis_preflight_blocked'
    assert result['next_actions']


def test_cli_preserves_failure_after_ready_preflight(tmp_path, monkeypatch, capsys):
    artifact = inert_elf(tmp_path / 'input.bin')
    engine = Engine(Settings(static_backend='direct'), tmp_path / 'results')

    async def environment(*args, **kwargs):
        return dict(docker=dict(ready=True, local_endpoint=True, os='linux', arch='amd64'))

    class Runtime:
        async def prepare_image(self, *args, **kwargs):
            return dict(status='failed', error='runtime became unavailable after preflight')

    monkeypatch.setattr(engine, 'environment', environment)
    monkeypatch.setattr(engine, '_execution_adapter', lambda run: Runtime())
    monkeypatch.setattr(cli, 'Engine', lambda *args, **kwargs: engine)
    code = cli.main(['analyze', '--original', str(artifact), '--reimplementation', str(artifact),
                     '--mode', 'dynamic', '--ai-mode', 'off'])
    result = json.loads(capsys.readouterr().out)
    assert code == 2 and result['run_id']
    assert len(engine.store.list_runs()) == 1
    saved = engine.store.load_run(result['run_id'])
    assert saved['stages']['image']['status'] == 'failed'


def test_cli_saved_recheck_also_blocks_unsupported_files_before_start(tmp_path, monkeypatch, capsys):
    artifact = tmp_path / 'unsupported.bin'
    artifact.write_bytes(b'unsupported')
    engine = Engine(Settings(), tmp_path / 'results')

    async def run_saved_suite(*args, **kwargs):
        # The analysis boundary is inert; the CLI must reject before reaching it.
        return dict(status='success', run_id='would-start')

    monkeypatch.setattr(engine, 'recheck_regression', run_saved_suite)
    monkeypatch.setattr(cli, 'Engine', lambda *args, **kwargs: engine)
    code = cli.main(['regression-run', 'suite', '--original', str(artifact),
                     '--reimplementation', str(artifact)])
    result = json.loads(capsys.readouterr().out)
    assert code == 2 and result['code'] == 'analysis_preflight_blocked'
    assert 'run_id' not in result


def test_direct_static_selection_does_not_change_packaged_execution_adapter(tmp_path):
    from recheck.managed_execution import ManagedExecutionAdapter

    engine = Engine(Settings(static_backend='direct'), tmp_path)
    assert isinstance(engine._execution_adapter(dict(mode='dynamic')), ManagedExecutionAdapter)


def test_legacy_missing_image_is_not_readied_or_built_implicitly(tmp_path, monkeypatch):
    adapter = DockerAdapter(cli='inert-docker', resources=tmp_path)
    engine = Engine(Settings(static_backend='direct'), tmp_path / 'results')
    commands = []

    async def probe():
        return dict(ready=True, local_endpoint=True, os='linux', arch='amd64')

    async def leftovers():
        return []

    async def call(*args, **kwargs):
        commands.append(args)
        return process(returncode=1)

    monkeypatch.setattr(adapter, 'probe', probe)
    monkeypatch.setattr(adapter, 'leftovers', leftovers)
    monkeypatch.setattr(adapter, 'call', call)
    monkeypatch.setattr(engine, '_docker', lambda: adapter)
    environment = asyncio.run(engine.environment(analysis_mode='dynamic', include_ai=False))
    assert environment['docker']['ready'] is False
    prepared = asyncio.run(adapter.prepare_image('linux'))
    assert prepared['status'] == 'blocked'
    assert all(command[0] != 'build' for command in commands)


def test_direct_ghidra_uses_same_registry_jdk_as_tool_diagnostic(tmp_path, monkeypatch):
    jdk = tmp_path / 'registered-jdk'
    (jdk / 'bin').mkdir(parents=True)
    for name in ('java.exe', 'java', 'javac.exe', 'javac'):
        (jdk / 'bin' / name).touch()
    ghidra = tmp_path / 'ghidra'
    (ghidra / 'support').mkdir(parents=True)
    (ghidra / 'Ghidra').mkdir()
    for name in ('analyzeHeadless.bat', 'analyzeHeadless'):
        (ghidra / 'support' / name).touch()
    (ghidra / 'Ghidra/application.properties').write_text('application.java.min=21\n')
    monkeypatch.delenv('JAVA_HOME', raising=False)
    monkeypatch.delenv('JAVA_HOME_OVERRIDE', raising=False)
    monkeypatch.setattr(prerequisites.shutil, 'which', lambda name: None)
    monkeypatch.setattr(prerequisites, '_registry_paths', lambda name: [jdk / 'bin/java.exe'] if name == 'java' else [])

    async def run(argv, **kwargs):
        return process(stderr='openjdk version "21.0.8"')

    monkeypatch.setattr(prerequisites, 'run_process', run)
    monkeypatch.setattr('recheck.adapters.run_process', run)
    diagnostic = asyncio.run(prerequisites._probe('java'))
    adapter = GhidraAdapter(home=ghidra)
    actual = asyncio.run(adapter.probe())
    assert diagnostic['status'] == 'ready'
    assert actual['ready'] is True
    assert Path(adapter.environment()['JAVA_HOME']) == jdk


def test_ollama_tool_diagnostic_reuses_actual_discovery_candidates(tmp_path, monkeypatch):
    executable = tmp_path / 'custom-install/ollama.exe'
    executable.parent.mkdir()
    executable.touch()
    monkeypatch.setattr(prerequisites.shutil, 'which', lambda name: None)
    monkeypatch.setattr('recheck.ollama.discover_ollama', lambda: dict(
        installed=True, executable=str(executable), candidates=[str(executable)]))

    async def run(argv, **kwargs):
        return process('ollama version 0.14.0')

    monkeypatch.setattr(prerequisites, 'run_process', run)
    diagnostic = asyncio.run(prerequisites._probe('ollama'))
    assert diagnostic['status'] == 'ready'
    assert diagnostic['path'] == str(executable)


@pytest.mark.parametrize('first_version,first_exit', [('17.0.1', 0), ('21.0.8', 1)])
def test_java_diagnostic_and_ghidra_skip_same_incompatible_candidate(
    tmp_path, monkeypatch, first_version, first_exit,
):
    roots = [tmp_path / name for name in ('jdk17', 'jdk21')]
    for root in roots:
        (root / 'bin').mkdir(parents=True)
        for name in ('java.exe', 'java', 'javac.exe', 'javac'):
            (root / 'bin' / name).touch()
    monkeypatch.delenv('JAVA_HOME', raising=False)
    monkeypatch.delenv('JAVA_HOME_OVERRIDE', raising=False)
    monkeypatch.setattr(prerequisites, '_candidate_paths', lambda tool: [str(root / 'bin/java.exe') for root in roots])

    async def run(argv, **kwargs):
        first = 'jdk17' in argv[0]
        version = first_version if first else '21.0.8'
        return process(stderr=f'openjdk version "{version}"', returncode=first_exit if first else 0)

    monkeypatch.setattr(prerequisites, 'run_process', run)
    monkeypatch.setattr('recheck.adapters.run_process', run)
    diagnostic = asyncio.run(prerequisites._probe('java'))
    adapter = GhidraAdapter()
    actual = asyncio.run(adapter.probe())
    assert diagnostic['status'] == 'ready'
    assert actual['java_major'] == 21
    assert Path(adapter.environment()['JAVA_HOME']) == roots[1]
