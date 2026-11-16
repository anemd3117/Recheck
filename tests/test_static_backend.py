import asyncio
import base64
import hashlib
import importlib
import importlib.util
import json
import os
import tempfile
from pathlib import Path

import pytest

from recheck.adapters import DockerAdapter

IMAGE_A = 'sha256:' + 'a' * 64
IMAGE_B = 'sha256:' + 'b' * 64
FIXTURE = b'non executable static-analysis fixture'
DIGEST = hashlib.sha256(FIXTURE).hexdigest()


def backend_class():
    assert (
        importlib.util.find_spec('recheck.static_backend') is not None
    ), 'ContainerStaticAdapter must provide managed static analysis'
    return importlib.import_module('recheck.static_backend').ContainerStaticAdapter


def process(stdout='', **changes):
    result = dict(
        returncode=0,
        stdout=stdout,
        stderr='',
        error=None,
        cancelled=False,
        timed_out=False,
        truncated=False,
    )
    result.update(changes)
    return result


def artifact(**changes):
    result = dict(
        schema_version='1.0',
        sha256=DIGEST,
        functions=[
            dict(
                id='00401000',
                name='main',
                address='00401000',
                blocks=[],
                edges=[],
                calls=[],
                mnemonics=[],
            )
        ],
        backend='rea',
        tool='REA MCP / Ghidra',
        backend_versions={'rea': '6.7.0', 'ghidra': '12.1.4', 'worker_contract': 'rea-static-1.0'},
        source={'container_path': '/input/target', 'sha256': DIGEST},
        partial=False,
        partial_reasons=[],
        raw_evidence={
            'files': ['raw_evidence/functions.json'],
            'coverage': {},
            'truncated': False,
            'partial_reasons': [],
        },
    )
    result.update(changes)
    return result


class Runtime:
    def __init__(self, image_id=IMAGE_A, static_ready=True):
        self.image_id, self.static_ready = image_id, static_ready

    async def probe(self):
        return dict(
            ready=False,
            status='missing',
            images={
                'static': dict(
                    ready=self.static_ready,
                    status='ready' if self.static_ready else 'missing',
                    image_id=self.image_id,
                    ref='recheck-static:1',
                ),
                'execution': dict(ready=False, status='missing'),
            },
        )


class DockerTransport(DockerAdapter):
    """Keep the adapter boundary real; replace only the external Docker CLI transport."""

    def __init__(self):
        super().__init__(cli='docker')
        self.image_id = IMAGE_A
        self.actual_image = None
        self.container_status = 'exited'
        self.owner_override = None
        self.container_missing = False
        self.reject_long_mounts = False
        self.local = True
        self.os, self.arch = 'linux', 'amd64'
        self.labels = {'org.recheck.component': 'static'}
        self.result = artifact()
        self.receipt_changes = {}
        self.worker_process = {}
        self.create_process = {}
        self.missing_result = False
        self.cancel_stage = None
        self.mutate_binary = None
        self.output = None
        self.evidence_dir = 'raw_evidence'
        self.calls = []
        self.created = []

    async def probe(self):
        return dict(
            ready=True,
            os=self.os,
            arch=self.arch,
            local_endpoint=self.local,
            context='desktop-linux',
        )

    async def call(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if args[:2] == ('image', 'inspect'):
            return process(
                json.dumps(
                    [
                        dict(
                            Id=self.image_id,
                            Os='linux',
                            Architecture='amd64',
                            Config={'Labels': self.labels},
                        )
                    ]
                )
            )
        if args[0] == 'create':
            self.created.append(args)
            mounts = [args[i + 1] for i, value in enumerate(args) if value == '--mount']
            output_mount = next(m for m in mounts if 'dst=/output' in m)
            self.output = Path(output_mount.split('src=', 1)[1].split(',', 1)[0])
            if self.reject_long_mounts and any(
                len(m.split('src=', 1)[1].split(',', 1)[0]) >= 220 for m in mounts
            ):
                return process(returncode=1, stderr='Docker Desktop EIO on long bind mount')
            if self.cancel_stage == 'create':
                raise asyncio.CancelledError()
            return process('owned-container\n', **self.create_process)
        if args[0] == 'start':
            if self.cancel_stage == 'start':
                raise asyncio.CancelledError()
            request = json.loads(kwargs['stdin'])
            assert request == dict(
                operation='analyze',
                input='/input/target',
                output='/output',
                timeout_seconds=30,
                max_functions=10000,
                max_decompile=100,
            )
            if not self.missing_result:
                (self.output / self.evidence_dir).mkdir(exist_ok=True)
                (self.output / self.evidence_dir / 'functions.json').write_text('[{"name":"main"}]')
                (self.output / 'result.json').write_text(json.dumps(self.result))
            if self.mutate_binary:
                self.mutate_binary()
            receipt = dict(
                ok=True,
                operation='analyze',
                result_path='/output/result.json',
                sha256=DIGEST,
                partial=self.result.get('partial', False),
                function_count=1,
                backend_versions=self.result['backend_versions'],
            )
            receipt.update(self.receipt_changes)
            result = process(json.dumps(receipt))
            result.update(self.worker_process)
            return result
        if args[0] == 'inspect':
            if self.container_missing:
                return process(returncode=1, stderr='Error: No such object: owned-container')
            return process(
                json.dumps(
                    [
                        dict(
                            Image=self.actual_image or self.image_id,
                            Config={
                                'Labels': {
                                    'org.recheck.managed': 'true',
                                    'org.recheck.component': 'static',
                                    'org.recheck.owner': self.owner_override
                                    or self.created[-1][self.created[-1].index('--name') + 1],
                                }
                            },
                            State=dict(
                                Status=self.container_status, ExitCode=0, OOMKilled=False, Error=''
                            ),
                        )
                    ]
                )
            )
        return process()


@pytest.fixture
def configured(tmp_path):
    resources = tmp_path / 'resources'
    runtime_dir = resources / 'runtime'
    runtime_dir.mkdir(parents=True)
    manifest = runtime_dir / 'environment.json'
    manifest.write_text(
        json.dumps(
            {
                'images': {
                    'static': {
                        'ref': 'recheck-static:1',
                        'image_id': IMAGE_A,
                        'platform': 'linux/amd64',
                    }
                }
            }
        )
    )
    binary = tmp_path / 'target with spaces'
    binary.write_bytes(FIXTURE)
    docker, runtime = DockerTransport(), Runtime()
    return dict(
        tmp_path=tmp_path,
        resources=resources,
        manifest=manifest,
        binary=binary,
        docker=docker,
        runtime=runtime,
    )


def adapter(configured, **kwargs):
    return backend_class()(
        resources=configured['resources'],
        runtime=configured['runtime'],
        docker=configured['docker'],
        **kwargs,
    )


def analyze(configured, *, workspace='workspace', **kwargs):
    return asyncio.run(
        adapter(configured, **kwargs).analyze(
            configured['binary'], configured['tmp_path'] / workspace, timeout=30
        )
    )


def test_static_probe_accepts_static_image_when_execution_image_missing(configured):
    result = asyncio.run(adapter(configured).probe())
    assert result['ready'] is True
    assert result['image_id'] == IMAGE_A


def test_success_retains_evidence_with_a_hardened_owned_container(configured):
    result = analyze(configured)
    assert result['status'] == 'success' and result['complete'] is True
    assert result['sha256'] == DIGEST and result['backend'] == 'rea'
    assert result['functions'][0]['blocks'] == []
    assert result['backend_provenance']['image_id'] == IMAGE_A
    assert Path(result['backend_source']['result']).is_file()
    assert all(
        Path(p).is_relative_to(configured['tmp_path'] / 'workspace')
        for p in result['backend_source']['raw_evidence']
    )
    command = configured['docker'].created[0]
    for flag in ('--read-only', '--network', '--cap-drop', '--security-opt', '--user', '--tmpfs'):
        assert flag in command
    assert command[command.index('--network') + 1] == 'none'
    assert command[command.index('--cap-drop') + 1] == 'ALL'
    assert command[command.index('--security-opt') + 1] == 'no-new-privileges:true'
    assert command[command.index('--user') + 1] == '10001:10001'
    assert command[command.index('--cpus') + 1] == '2'
    assert command[command.index('--memory') + 1] == '4096m'
    assert IMAGE_A in command
    assert not any('docker.sock' in item or 'OPENAI' in item for item in command)
    mounts = [command[i + 1] for i, value in enumerate(command) if value == '--mount']
    assert len(mounts) == 2 and any('dst=/input' in m and 'readonly' in m for m in mounts)
    assert configured['docker'].calls[-1][0][0] == 'rm'


@pytest.mark.parametrize(
    'mutation',
    [
        'artifact_hash',
        'source_hash',
        'receipt_hash',
        'image',
        'missing_output',
        'bad_json',
        'raw_escape',
        'receipt_path',
        'wrong_schema',
        'worker_failed',
    ],
)
def test_untrusted_output_never_becomes_success(configured, mutation):
    docker = configured['docker']
    if mutation == 'artifact_hash':
        docker.result['sha256'] = '0' * 64
    if mutation == 'source_hash':
        docker.result['source']['sha256'] = '0' * 64
    if mutation == 'receipt_hash':
        docker.receipt_changes['sha256'] = '0' * 64
    if mutation == 'image':
        docker.actual_image = IMAGE_B
    if mutation == 'missing_output':
        docker.missing_result = True
    if mutation == 'bad_json':
        docker.worker_process['stdout'] = '{not-json'
    if mutation == 'raw_escape':
        docker.result['raw_evidence']['files'] = ['../outside.json']
    if mutation == 'receipt_path':
        docker.receipt_changes['result_path'] = '/etc/passwd'
    if mutation == 'wrong_schema':
        docker.result['functions'][0]['calls'] = [1]
    if mutation == 'worker_failed':
        docker.worker_process['returncode'] = 1
    result = analyze(configured)
    assert result['status'] == 'failed' and result['complete'] is False
    assert result['cache']['hit'] is False
    assert docker.calls[-1][0][0] == 'rm'


@pytest.mark.parametrize('field', ['partial', 'analysis_timed_out', 'functions_truncated'])
def test_incomplete_worker_output_is_partial_and_never_cached(configured, field):
    docker = configured['docker']
    docker.result[field] = True
    cache = configured['tmp_path'] / 'cache'
    result = analyze(configured, cache_dir=cache)
    assert result['status'] == 'partial' and result['complete'] is False
    assert not list(cache.glob('*.json'))


@pytest.mark.parametrize('status', ['failed', 'cancelled', 'partial'])
def test_worker_status_is_not_promoted(configured, status):
    configured['docker'].result['status'] = status
    result = analyze(configured)
    assert result['status'] == status and result['complete'] is False


@pytest.mark.parametrize('fault', ['remote', 'windows', 'arm64', 'image_id', 'label'])
def test_wrong_engine_or_image_is_blocked_before_create(configured, fault):
    docker = configured['docker']
    if fault == 'remote':
        docker.local = False
    if fault == 'windows':
        docker.os = 'windows'
    if fault == 'arm64':
        docker.arch = 'arm64'
    if fault == 'image_id':
        docker.image_id = IMAGE_B
    if fault == 'label':
        docker.labels = {'org.recheck.component': 'execution'}
    result = analyze(configured)
    assert result['status'] == 'blocked'
    assert not docker.created


def test_input_change_during_worker_execution_invalidates_result(configured):
    configured['docker'].mutate_binary = lambda: configured['binary'].write_bytes(b'changed')
    result = analyze(configured)
    assert result['status'] == 'failed' and 'hash' in result['error'].lower()


@pytest.mark.parametrize('stage', ['create', 'start'])
def test_task_cancellation_cleans_the_owned_container(configured, stage):
    configured['docker'].cancel_stage = stage
    with pytest.raises(asyncio.CancelledError):
        analyze(configured)
    calls = [args for args, _ in configured['docker'].calls]
    assert any(args[0] == 'kill' for args in calls)
    assert calls[-1][0] == 'rm'
    assert calls[-1][-1].startswith('recheck-static-')


@pytest.mark.parametrize('flag', ['timed_out', 'cancelled'])
def test_worker_timeout_or_cancel_retains_terminal_status_and_cleans(configured, flag):
    configured['docker'].worker_process[flag] = True
    result = analyze(configured)
    assert result['status'] == ('cancelled' if flag == 'cancelled' else 'partial')
    assert result['complete'] is False
    assert configured['docker'].calls[-1][0][0] == 'rm'


def test_create_transport_failure_still_removes_possible_orphan(configured):
    configured['docker'].create_process['returncode'] = 1
    result = analyze(configured)
    assert result['status'] == 'failed'
    assert configured['docker'].calls[-1][0][0] == 'rm'


def test_cache_rehydrates_evidence_but_never_reuses_a_different_image(configured):
    cache = configured['tmp_path'] / 'cache'
    first = analyze(configured, workspace='first', cache_dir=cache)
    second = analyze(configured, workspace='second', cache_dir=cache)
    assert first['cache']['hit'] is False and second['cache']['hit'] is True
    assert len(configured['docker'].created) == 1
    assert Path(second['backend_source']['raw_evidence'][0]).read_text() == '[{"name":"main"}]'
    assert Path(second['backend_source']['result']).is_relative_to(
        configured['tmp_path'] / 'second'
    )
    configured['runtime'].image_id = configured['docker'].image_id = IMAGE_B
    data = json.loads(configured['manifest'].read_text())
    data['images']['static']['image_id'] = IMAGE_B
    configured['manifest'].write_text(json.dumps(data))
    third = analyze(configured, workspace='third', cache_dir=cache)
    assert third['status'] == 'success' and third['cache']['hit'] is False
    assert len(configured['docker'].created) == 2


def test_cache_tampering_forces_new_extraction(configured):
    cache = configured['tmp_path'] / 'cache'
    analyze(configured, workspace='first', cache_dir=cache)
    cache_path = next(cache.glob('*.json'))
    data = json.loads(cache_path.read_text())
    data['artifact']['sha256'] = '0' * 64
    cache_path.write_text(json.dumps(data))
    result = analyze(configured, workspace='second', cache_dir=cache)
    assert result['status'] == 'success' and result['cache']['hit'] is False
    assert result['cache']['rejected']
    assert len(configured['docker'].created) == 2


def test_mount_path_injection_is_rejected_before_create(configured):
    result = analyze(configured, workspace='work,dst=/host')
    assert result['status'] == 'failed' and not configured['docker'].created


def test_symlink_input_is_rejected_before_create(configured):
    link = configured['tmp_path'] / 'linked-target'
    try:
        link.symlink_to(configured['binary'])
    except OSError:
        pytest.skip('Symlinks require Windows developer mode or privilege')
    configured['binary'] = link
    result = analyze(configured)
    assert result['status'] == 'failed' and not configured['docker'].created


def test_receipt_versions_must_match_the_artifact(configured):
    configured['docker'].receipt_changes['backend_versions'] = {
        'rea': 'different',
        'ghidra': '12.1.4',
    }
    result = analyze(configured)
    assert result['status'] == 'failed' and result['complete'] is False


def test_worker_cannot_replace_host_process_evidence(configured):
    configured['docker'].result['process'] = {'forged': True}
    result = analyze(configured)
    assert result['status'] == 'success'
    assert 'forged' not in result['process'] and result['process']['returncode'] == 0


def test_cancel_during_environment_probe_never_creates_container(configured):
    cancel = asyncio.Event()
    runtime = configured['runtime']
    original = runtime.probe

    async def probe():
        environment = await original()
        cancel.set()
        return environment

    runtime.probe = probe
    result = asyncio.run(
        adapter(configured).analyze(
            configured['binary'], configured['tmp_path'] / 'workspace', timeout=30, cancel=cancel
        )
    )
    assert result['status'] == 'cancelled' and not configured['docker'].created


def test_worker_result_requires_an_exited_container(configured):
    configured['docker'].container_status = 'running'
    result = analyze(configured)
    assert result['status'] == 'failed' and result['complete'] is False


def test_cleanup_never_removes_a_container_with_different_ownership(configured):
    configured['docker'].owner_override = 'foreign-owner'
    result = analyze(configured)
    assert result['status'] == 'failed' and result['cleanup_errors']
    assert all(args[0] not in ('kill', 'rm') for args, _ in configured['docker'].calls)


def test_released_worker_raw_directory_is_retained_within_workspace(configured):
    configured['docker'].evidence_dir = 'raw'
    configured['docker'].result['raw_evidence']['files'] = ['raw/functions.json']
    result = analyze(configured)
    assert result['status'] == 'success'
    assert Path(result['backend_source']['raw_evidence'][0]).is_file()


def test_cancelled_create_without_an_orphan_remains_cancelled(configured):
    configured['docker'].create_process['cancelled'] = True
    configured['docker'].container_missing = True
    result = analyze(configured)
    assert result['status'] == 'cancelled' and result['cleanup_errors'] == []
    assert all(args[0] not in ('kill', 'rm') for args, _ in configured['docker'].calls)


def test_unknown_worker_contract_is_rejected(configured):
    configured['docker'].result['backend_versions']['worker_contract'] = 'future-incompatible'
    result = analyze(configured)
    assert result['status'] == 'failed' and result['complete'] is False


@pytest.mark.parametrize('flag,expected', [('cancelled', 'cancelled'), ('timed_out', 'partial')])
def test_stopped_worker_without_receipt_retains_verified_provenance(configured, flag, expected):
    configured['docker'].worker_process.update({flag: True, 'stdout': ''})
    result = analyze(configured)
    assert result['status'] == expected and result['complete'] is False
    assert result['backend_provenance']['image_id'] == IMAGE_A
    assert 'JSON' not in result['error'] and 'Expecting value' not in result['error']


@pytest.mark.parametrize('stdout', ['', '{"ok":'])
def test_abrupt_worker_exit_retains_raw_diagnostics_and_explains_recovery(configured, stdout):
    configured['docker'].worker_process.update(
        returncode=137, stdout=stdout, stderr='worker transport stopped'
    )
    result = analyze(configured, cache_dir=configured['tmp_path'] / 'cache')
    assert result['status'] == 'failed' and result['complete'] is False
    assert result['error_code'] == 'static_worker_stopped'
    assert '다시' in result['error'] and '중단' in result['error']
    assert result['process']['returncode'] == 137
    assert result['process']['stdout'] == stdout
    assert result['process']['stderr'] == 'worker transport stopped'
    assert result['error_details']['type'] == 'JSONDecodeError'
    assert result['error_details']['message']
    assert list((configured['tmp_path'] / 'cache').glob('*.json')) == []
    assert result['cleanup_errors'] == []
    assert configured['docker'].calls[-1][0][0] == 'rm'


@pytest.mark.parametrize('stdout', ['not json', '[]', 'null'])
def test_unusable_worker_receipt_is_reported_without_claiming_worker_interruption(configured, stdout):
    configured['docker'].worker_process['stdout'] = stdout
    result = analyze(configured)
    assert result['status'] == 'failed' and result['complete'] is False
    assert result['error_code'] == 'static_worker_receipt_invalid'
    assert '다시' in result['error']
    assert result['process']['stdout'] == stdout
    assert result['error_details']['message']


@pytest.mark.skipif(os.name != 'nt', reason='Docker Desktop Windows long bind limitation')
@pytest.mark.parametrize(
    'outcome', ['success', 'partial', 'malformed', 'cancel-create', 'cancel-start']
)
def test_windows_long_workspace_uses_owned_short_mounts_and_cleans_them(configured, outcome):
    docker = configured['docker']
    docker.reject_long_mounts = True
    if outcome == 'partial':
        docker.result['partial'] = True
    if outcome == 'malformed':
        docker.worker_process['stdout'] = '{not-json'
    if outcome.startswith('cancel-'):
        docker.cancel_stage = outcome.removeprefix('cancel-')
    workspace = Path('deep') / ('nested/' * 24)
    if outcome.startswith('cancel-'):
        with pytest.raises(asyncio.CancelledError):
            analyze(configured, workspace=workspace)
    else:
        result = analyze(configured, workspace=workspace)
        assert (
            result['status']
            == {'success': 'success', 'partial': 'partial', 'malformed': 'failed'}[outcome]
        )
        assert result['short_staging']['removed'] is True
        if outcome != 'malformed':
            final = configured['tmp_path'] / workspace
            assert Path(result['backend_source']['result']).is_relative_to(final)
            assert all(
                Path(p).is_relative_to(final) and Path(p).is_file()
                for p in result['backend_source']['raw_evidence']
            )
            assert (
                json.loads(Path(result['backend_source']['result']).read_text(encoding='utf-8'))[
                    'sha256'
                ]
                == DIGEST
            )
    command = docker.created[0]
    mounts = [command[i + 1] for i, arg in enumerate(command) if arg == '--mount']
    paths = [Path(m.split('src=', 1)[1].split(',', 1)[0]) for m in mounts]
    short_root = Path(tempfile.gettempdir()) / 'Recheck'
    assert all(p.is_relative_to(short_root) and len(str(p)) < 220 for p in paths)
    assert all(not p.exists() for p in paths)
