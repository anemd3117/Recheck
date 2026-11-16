"""Cache identity and per-test execution settings; controlled adapter fixtures."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import pytest
from recheck.adapters import DockerAdapter, GhidraAdapter
from recheck.models import TestCase, validate_tests


def process(stdout='', **extra):
    return dict(
        returncode=0,
        stdout=stdout,
        stderr='',
        stdout_b64='',
        stderr_b64='',
        error=None,
        timed_out=False,
        cancelled=False,
        truncated=False,
        duration=0.01,
        **extra,
    )


@pytest.mark.parametrize('env', [{'BAD=KEY': 'x'}, {'A': '\x00'}, {'A': 'x' * 32769}])
def test_rejects_invalid_environment(env):
    with pytest.raises(ValueError):
        validate_tests([dict(id='t', env=env)])


def test_registered_settings_reach_docker_as_exact_argv(tmp_path):
    binary = tmp_path / 'target'
    binary.write_bytes(b'fixture never executed')
    test = TestCase(
        id='t',
        env={'VALUE': '한글 = ; $()'},
        limits={'memory_mb': 128, 'cpus': 0.5, 'output_bytes': 2048},
    ).model_dump()
    adapter = DockerAdapter(cli='docker')

    async def probe():
        return dict(ready=True, os='linux', arch='amd64', local_endpoint=True)

    async def call(*args, **kwargs):
        if args[0] == 'create':
            assert 'VALUE=한글 = ; $()' in args
            assert args[args.index('--memory') + 1] == '128m'
            assert args[args.index('--cpus') + 1] == '0.5'
        if args[0] == 'start':
            assert kwargs['output_limit'] == 2048
        if args[0] == 'inspect':
            return process(
                json.dumps(
                    [
                        dict(
                            State=dict(Status='exited', ExitCode=0, OOMKilled=False, Error=''),
                            Image='image',
                        )
                    ]
                )
            )
        return process('id')

    adapter.probe, adapter.call = probe, call
    result = asyncio.run(adapter.execute(binary, test, 'run', 'attempt', tmp_path / 'work'))
    assert result['infrastructure_ok']
    assert result['actual_input']['env'] == test['env']
    assert result['limits']['memory_mb'] == 128


def test_cache_requires_valid_artifact_and_all_analysis_inputs(tmp_path, monkeypatch):
    resources = tmp_path / 'resources/ghidra'
    resources.mkdir(parents=True)
    script = resources / 'RecheckExport.java'
    script.write_text('fixture v1')
    binary = tmp_path / 'binary'
    binary.write_bytes(b'fixture')
    adapter = GhidraAdapter(resources=resources.parent, cache_dir=tmp_path / 'cache')
    versions = {'version': '1'}

    async def probe():
        return dict(
            ready=True,
            launcher='fixture',
            home='fixture-install',
            properties=versions.copy(),
            java={'stderr': 'fixture Java 25'},
        )

    launches = []

    async def execute(argv, **kwargs):
        launches.append(argv)
        at = argv.index('RecheckExport.java')
        Path(argv[at + 1]).write_text(
            json.dumps(
                dict(
                    schema_version='1.0',
                    sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
                    functions=[],
                    complete=True,
                    analysis_timed_out=False,
                )
            ),
            encoding='utf-8',
        )
        Path(argv[at + 2]).write_text('RECHECK_COMPLETE_1', encoding='utf-8')
        return process()

    adapter.probe = probe
    monkeypatch.setattr('recheck.adapters.run_process', execute)

    def run(n, timeout=30):
        return asyncio.run(adapter.analyze(binary, tmp_path / str(n), timeout=timeout))

    assert run(1)['status'] == 'success'
    hit = run(2)
    assert hit['cache']['hit'] and len(launches) == 1 and hit['cache']['extracted_at']
    versions['version'] = '2'
    assert not run(3)['cache']['hit'] and len(launches) == 2
    script.write_text('fixture v2')
    assert not run(4)['cache']['hit'] and len(launches) == 3
    assert not run(5, timeout=31)['cache']['hit'] and len(launches) == 4
    cached = tmp_path / 'cache' / f"{run(6,timeout=31)['cache']['key']}.json"
    value = json.loads(cached.read_text())
    value['artifact']['functions'] = [{'id': 'fake'}]
    cached.write_text(json.dumps(value))
    result = run(7, timeout=31)
    assert not result['cache']['hit'] and result['cache']['rejected'] and len(launches) == 5


@pytest.mark.parametrize(
    'message,expected',
    [
        ('/usr/local/bin/recheck-run-target: 4: exec: /input/target: Input/output error\n', False),
        ('application explicitly returned code 126\n', True),
    ],
)
def test_launcher_failure_is_not_a_valid_program_exit(tmp_path, monkeypatch, message, expected):
    binary = tmp_path / 'target'
    binary.write_bytes(b'fixture')
    adapter = DockerAdapter(cli='docker')

    async def probe():
        return dict(ready=True, os='linux', arch='amd64', local_endpoint=True)

    async def call(*args, **kwargs):
        if args[0] == 'start':
            result = process()
            result.update(returncode=126, stderr=message)
            return result
        if args[0] == 'inspect':
            return process(
                json.dumps(
                    [
                        dict(
                            State=dict(Status='exited', ExitCode=126, OOMKilled=False, Error=''),
                            Image='i',
                        )
                    ]
                )
            )
        return process('id')

    adapter.probe, adapter.call = probe, call
    work = tmp_path / ('long-evidence-' * 12) / ('nested-attempt-' * 4)
    from windows_path_support import legacy_windows_io
    with legacy_windows_io(monkeypatch):
        result = asyncio.run(adapter.execute(binary, {}, 'run', 'a', work))
    assert result['infrastructure_ok'] is expected
    assert result['input_staging']['short_path_mirror']
    assert result['input_staging']['mirror_removed']
    assert not Path(result['input_staging']['mount_source']).exists()


@pytest.mark.skipif(os.name != 'nt', reason='Windows long input paths')
def test_execution_long_inputs_and_auxiliary_file_preserve_normal_tool_paths(tmp_path, monkeypatch):
    from recheck.paths import filesystem_path
    from windows_path_support import legacy_windows_io

    source = tmp_path / ('input-folder-' * 10) / ('nested-input-' * 8)
    filesystem_path(source).mkdir(parents=True)
    binary, auxiliary = source / 'target', source / 'data.txt'
    filesystem_path(binary).write_bytes(b'fixture')
    filesystem_path(auxiliary).write_bytes(b'auxiliary fixture')
    assert len(str(binary)) > 260
    adapter = DockerAdapter(cli='docker')
    calls = []

    async def probe():
        return dict(ready=True, os='linux', arch='amd64', local_endpoint=True)

    async def call(*args, **kwargs):
        calls.append(args)
        if args[0] == 'inspect':
            return process(json.dumps([dict(State=dict(Status='exited', ExitCode=0,
                                                       OOMKilled=False, Error=''), Image='i')]))
        return process('id' if args[0] == 'create' else '')

    adapter.probe, adapter.call = probe, call
    work = tmp_path / ('evidence-folder-' * 10) / ('nested-attempt-' * 6)
    with legacy_windows_io(monkeypatch):
        result = asyncio.run(adapter.execute(binary, {'aux_files': [str(auxiliary)]},
                                             'run', 'a', work))
    assert result['status'] == 'success' and result['infrastructure_ok']
    assert not result.get('storage_errors')
    assert result['actual_input']['auxiliary_files'][0]['name'] == 'data.txt'
    assert all('\\\\?\\' not in str(argument) for call in calls for argument in call)
    sidecar = work / (result['container_name'] + '.json')
    persisted = json.loads(filesystem_path(sidecar).read_text(encoding='utf-8'))
    assert persisted['infrastructure_ok'] and persisted['input_staging']['mirror_removed']


def test_timeout_diagnosis_does_not_confuse_memory_setting_with_oom(tmp_path):
    from recheck.engine import Engine
    from recheck.models import Settings
    from recheck.storage import new_id

    engine = Engine(Settings(), data_dir=tmp_path / 'data')

    class Adapter:
        async def execute(self, *args, **kwargs):
            return dict(
                status='failed',
                exit_code=137,
                timed_out=True,
                cancelled=False,
                truncated=False,
                oom_killed=False,
                limits={'memory_mb': 512},
                stdout_b64='',
                stderr_b64='',
            )

    engine._docker = lambda: Adapter()
    run = dict(
        run_id=new_id('run'),
        attempt_id=new_id('attempt'),
        status='running',
        tests=[dict(id='t')],
        files={
            t: dict(snapshot=str(tmp_path / 'binary'), os='linux')
            for t in ['original', 'reimplementation']
        },
        dynamic=[],
        evidence=[],
        events=[],
        diagnostics=[],
        image={},
    )
    asyncio.run(engine._dynamic(run, ['t'], None))
    assert {d['code'] for d in run['diagnostics']} == {'timeout'}
    assert run['behavior']['denominator'] == 0
