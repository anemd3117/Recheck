"""Managed execution must use a verified packaged image, never build at runtime."""

import asyncio
import base64
import importlib
import importlib.util
import json
from pathlib import Path

import pytest

from recheck.models import Settings

IMAGE = 'sha256:' + 'c' * 64
WRONG_IMAGE = 'sha256:' + 'd' * 64


def adapter_class():
    assert (
        importlib.util.find_spec('recheck.managed_execution') is not None
    ), 'ManagedExecutionAdapter must run pinned execution images without builds'
    return importlib.import_module('recheck.managed_execution').ManagedExecutionAdapter


def process(stdout='', **changes):
    result = dict(
        returncode=0,
        stdout=stdout,
        stderr='',
        error=None,
        cancelled=False,
        timed_out=False,
        truncated=False,
        stdout_b64=base64.b64encode(stdout.encode()).decode(),
        stderr_b64='',
        duration=0.01,
    )
    result.update(changes)
    return result


class Runtime:
    def __init__(self):
        self.ready = True
        self.image_id = IMAGE

    async def probe(self):
        return dict(
            ready=False,
            images={
                'static': {'ready': False},
                'execution': {
                    'ready': self.ready,
                    'image_id': self.image_id,
                    'ref': 'recheck-execution:1',
                },
            },
        )


def build_configured(tmp_path):
    resources = tmp_path / 'resources'
    (resources / 'runtime').mkdir(parents=True)
    manifest = resources / 'runtime/environment.json'
    manifest.write_text(
        json.dumps(
            {
                'images': {
                    'execution': {
                        'ref': 'recheck-execution:1',
                        'image_id': IMAGE,
                        'platform': 'linux/amd64',
                    }
                }
            }
        )
    )
    runtime = Runtime()
    adapter = adapter_class()(Settings(), runtime=runtime, cli='docker', resources=resources)
    adapter.calls = []
    adapter.engine = dict(ready=True, os='linux', arch='amd64', local_endpoint=True)
    adapter.image_details = dict(
        Id=IMAGE,
        Os='linux',
        Architecture='amd64',
        Config={'Labels': {'org.recheck.component': 'execution'}},
    )
    adapter.actual_image = IMAGE

    async def probe():
        return adapter.engine

    async def call(*args, **kwargs):
        adapter.calls.append(args)
        if args[:2] == ('image', 'inspect'):
            return process(json.dumps([adapter.image_details]))
        if args[0] == 'create':
            return process('owned-container')
        if args[0] == 'start':
            return process('program result')
        if args[0] == 'inspect':
            return process(
                json.dumps(
                    [
                        dict(
                            Image=adapter.actual_image,
                            State=dict(Status='exited', ExitCode=0, OOMKilled=False, Error=''),
                        )
                    ]
                )
            )
        return process()

    adapter.probe, adapter.call = probe, call
    binary = tmp_path / 'fixture'
    binary.write_bytes(b'data only, never executed on the host')
    return adapter, runtime, manifest, binary, tmp_path


@pytest.fixture
def configured(tmp_path):
    return lambda: build_configured(tmp_path)


def test_prepare_uses_execution_image_without_requiring_static_readiness(configured):
    adapter, _, _, _, _ = configured()
    result = asyncio.run(adapter.prepare_image('linux'))
    assert result['status'] == 'success' and result['image'] == IMAGE
    assert result['image_id'] == IMAGE and result['image_ref'] == 'recheck-execution:1'
    assert result['managed'] is True and result['build'] is None
    assert all(args[0] != 'build' for args in adapter.calls)


@pytest.mark.parametrize(
    'fault',
    [
        'missing',
        'unconfigured',
        'mutable',
        'identity',
        'label',
        'image_os',
        'engine_os',
        'engine_arch',
        'remote',
    ],
)
def test_invalid_packaged_execution_environment_is_blocked_without_build(configured, fault):
    adapter, runtime, manifest, _, _ = configured()
    if fault == 'missing':
        runtime.ready = False
    if fault == 'unconfigured':
        data = json.loads(manifest.read_text())
        data['images']['execution']['image_id'] = None
        manifest.write_text(json.dumps(data))
    if fault == 'mutable':
        runtime.image_id = 'recheck-execution:latest'
    if fault == 'identity':
        adapter.image_details['Id'] = WRONG_IMAGE
    if fault == 'label':
        adapter.image_details['Config']['Labels'] = {'org.recheck.component': 'static'}
    if fault == 'image_os':
        adapter.image_details['Os'] = 'windows'
    if fault == 'engine_os':
        adapter.engine['os'] = 'windows'
    if fault == 'engine_arch':
        adapter.engine['arch'] = 'arm64'
    if fault == 'remote':
        adapter.engine['local_endpoint'] = False
    result = asyncio.run(adapter.prepare_image('linux'))
    assert result['status'] == 'blocked' and result['error']
    assert all(args[0] not in ('build', 'create') for args in adapter.calls)


def test_linux_execute_without_explicit_image_uses_pinned_packaged_image(configured):
    adapter, _, _, binary, tmp_path = configured()
    result = asyncio.run(adapter.execute(binary, {}, 'run', 'attempt', tmp_path / 'workspace'))
    assert result['status'] == 'success' and result['image_id'] == IMAGE
    create = next(args for args in adapter.calls if args[0] == 'create')
    assert IMAGE in create and 'recheck-analysis-linux:1' not in create
    assert result['managed_environment']['image_id'] == IMAGE
    persisted = json.loads(
        next((tmp_path / 'workspace').glob('recheck-*.json')).read_text(encoding='utf-8')
    )
    assert persisted['managed_environment']['image_id'] == IMAGE


def test_explicit_untrusted_image_is_blocked_before_container_creation(configured):
    adapter, _, _, binary, tmp_path = configured()
    result = asyncio.run(
        adapter.execute(binary, {}, 'run', 'attempt', tmp_path / 'workspace', image=WRONG_IMAGE)
    )
    assert result['status'] == 'blocked' and result['infrastructure_ok'] is False
    assert all(args[0] != 'create' for args in adapter.calls)


def test_actual_execution_image_mismatch_invalidates_and_persists_failure(configured):
    adapter, _, _, binary, tmp_path = configured()
    adapter.actual_image = WRONG_IMAGE
    result = asyncio.run(adapter.execute(binary, {}, 'run', 'attempt', tmp_path / 'workspace'))
    assert result['status'] == 'failed' and result['infrastructure_ok'] is False
    persisted = json.loads(
        next((tmp_path / 'workspace').glob('recheck-*.json')).read_text(encoding='utf-8')
    )
    assert persisted['status'] == 'failed' and persisted['infrastructure_ok'] is False


def test_pe_execution_is_blocked_by_linux_engine_before_creation(configured):
    adapter, _, _, binary, tmp_path = configured()
    result = asyncio.run(
        adapter.execute(binary, {}, 'run', 'attempt', tmp_path / 'workspace', target_os='windows')
    )
    assert result['status'] == 'blocked'
    assert all(args[0] != 'create' for args in adapter.calls)


def test_missing_actual_execution_image_invalidates_success(configured):
    adapter, _, _, binary, tmp_path = configured()
    adapter.actual_image = None
    result = asyncio.run(adapter.execute(binary, {}, 'run', 'attempt', tmp_path / 'workspace'))
    assert result['status'] == 'failed' and result['infrastructure_ok'] is False


def test_windows_preparation_never_builds_an_unverified_runtime(configured):
    adapter, _, _, _, _ = configured()
    adapter.engine.update(os='windows')
    result = asyncio.run(adapter.prepare_image('windows'))
    assert result['status'] == 'blocked' and result['managed'] is True
    assert result['error_code'] == 'windows_runtime_unverified'
    assert all(args[0] not in ('build', 'create') for args in adapter.calls)


def test_explicit_windows_image_cannot_bypass_unverified_runtime_gate(configured):
    adapter, _, _, binary, tmp_path = configured()
    adapter.engine.update(os='windows')
    result = asyncio.run(adapter.execute(binary, {}, 'run', 'attempt', tmp_path / 'workspace', target_os='windows', image=IMAGE))
    assert result['status'] == 'blocked' and result['infrastructure_ok'] is False
    assert result['error_code'] == 'windows_runtime_unverified'
    assert all(args[0] != 'create' for args in adapter.calls)
