"""Explicit local setup builds, with Docker's external I/O substituted only."""

import asyncio
import hashlib
import json
import threading
from pathlib import Path

import pytest

from recheck.models import Settings


OLD = {c: 'sha256:' + digit * 64 for c, digit in [('static', '1'), ('execution', '2'), ('ollama', '3')]}
NEW = {c: 'sha256:' + digit * 64 for c, digit in [('static', 'a'), ('execution', 'b'), ('ollama', 'c')]}


def process(stdout='', **changes):
    return dict(returncode=0, stdout=stdout, stderr='', error=None, timed_out=False,
                cancelled=False, truncated=False, **changes)


class Docker:
    def __init__(self):
        self.calls = []
        self.images = {}
        self.environment = dict(ready=True, local_endpoint=True, os='linux', arch='amd64')
        self.fault = None
        self.probes = 0

    async def probe(self):
        self.probes += 1
        return self.environment

    async def call(self, *args, **options):
        self.calls.append((args, options))
        if self.fault:
            result = self.fault(args, options)
            if result is not None:
                return result
        if args[0] == 'build':
            component = Path(args[-1]).name
            labels = dict(args[i + 1].split('=', 1) for i, arg in enumerate(args) if arg == '--label')
            labels.update({'org.recheck.component': component, 'org.recheck.managed': 'true',
                           'org.recheck.worker-contract': 'rea-static-1.0'})
            self.images[NEW[component]] = dict(Id=NEW[component], Os='linux', Architecture='amd64',
                                                Config={'Labels': labels})
            Path(args[args.index('--iidfile') + 1]).write_text(NEW[component])
            return process('built')
        if args[:2] == ('image', 'inspect'):
            image = self.images.get(args[2])
            return process(json.dumps([image])) if image else {**process(), 'returncode': 1}
        raise AssertionError('Unexpected Docker operation: ' + str(args))


@pytest.fixture
def configured(tmp_path):
    resources = tmp_path / 'resources'
    (resources / 'runtime').mkdir(parents=True)
    manifest = resources / 'runtime/environment.json'
    manifest.write_text(json.dumps(dict(version=1, images={
        c: dict(image_id=OLD[c], ref='old-' + c + ':1', platform='linux/amd64') for c in OLD
    })))
    for c in OLD:
        context = resources / 'docker' / c
        context.mkdir(parents=True)
        (context / 'Dockerfile').write_text('FROM scratch\nLABEL org.recheck.component="' + c + '"\n')
        (context / 'worker.txt').write_text('reviewed context ' + c)
    receipt = tmp_path / 'state' / 'local-builds.json'
    docker = Docker()
    from recheck.managed_runtime import ManagedRuntime
    runtime = ManagedRuntime(Settings(), docker=docker, manifest_path=manifest,
                             bundle_dir=tmp_path / 'absent-bundle')
    # Existing constructor still works; the source-build capability owns these paths.
    runtime.resources_dir = resources
    runtime.receipt_path = receipt
    return runtime, docker, resources, manifest, receipt


def build(runtime, **kwargs):
    assert hasattr(runtime, 'build_from_sources'), 'Explicit source environment build is missing'
    return asyncio.run(runtime.build_from_sources(**kwargs))


def test_explicit_build_uses_copied_context_and_immutable_iid_without_host_python_git(configured):
    runtime, docker, resources, manifest, receipt = configured
    before = manifest.read_bytes()
    result = build(runtime)
    assert result['ready'] and result['status'] == 'success'
    assert result['static_image_id'] == NEW['static']
    assert result['execution_image_id'] == NEW['execution']
    assert manifest.read_bytes() == before
    assert receipt.is_file()
    builds = [args for args, _ in docker.calls if args[0] == 'build']
    assert len(builds) == 2
    assert all('--iidfile' in args and '--platform' in args and 'linux/amd64' in args for args in builds)
    assert all('--tag' not in args and resources not in Path(args[-1]).parents for args in builds)
    assert not any('ollama' in args[-1] for args in builds)
    fresh = type(runtime)(Settings(), docker=docker, manifest_path=manifest,
                          bundle_dir=runtime.bundle_dir, resources_dir=resources, receipt_path=receipt)
    docker.calls.clear()
    ready = asyncio.run(fresh.probe())
    assert ready['ready'] and ready['static_image_id'] == NEW['static']
    assert ready['execution_image_id'] == NEW['execution']
    assert all(args[:2] == ('image', 'inspect') for args, _ in docker.calls)


@pytest.mark.parametrize('mutation', ['context', 'manifest', 'receipt', 'image-label', 'image-platform', 'image-id'])
def test_local_receipt_or_context_or_image_tampering_is_not_ready(configured, mutation):
    runtime, docker, resources, manifest, receipt = configured
    assert build(runtime)['ready']
    if mutation == 'context':
        (resources / 'docker/static/worker.txt').write_text('changed after setup')
    elif mutation == 'manifest':
        value = json.loads(manifest.read_text())
        value['release'] = 'changed after setup'
        manifest.write_text(json.dumps(value))
    elif mutation == 'receipt':
        value = json.loads(receipt.read_text())
        value['images']['static']['image_id'] = 'mutable-tag:latest'
        receipt.write_text(json.dumps(value))
    elif mutation == 'image-label':
        docker.images[NEW['static']]['Config']['Labels']['org.recheck.source-context'] = 'f' * 64
    elif mutation == 'image-platform':
        docker.images[NEW['static']]['Architecture'] = 'arm64'
    elif mutation == 'image-id':
        docker.images[NEW['static']]['Id'] = OLD['static']
    result = asyncio.run(runtime.probe())
    assert not result['images']['static']['ready']


@pytest.mark.parametrize('environment', [dict(local_endpoint=False), dict(os='windows'), dict(arch='arm64'), dict(ready=False)])
def test_build_rejects_remote_or_incompatible_engine_before_mutation(configured, environment):
    runtime, docker, _, _, receipt = configured
    docker.environment.update(environment)
    result = build(runtime)
    assert result['status'] == 'blocked' and not result['ready']
    assert docker.calls == [] and not receipt.exists()


def test_precancelled_build_has_no_docker_or_filesystem_mutations(configured):
    runtime, docker, _, _, receipt = configured
    cancel = threading.Event()
    cancel.set()
    result = build(runtime, cancel=cancel)
    assert result['status'] == 'cancelled' and not result['ready']
    assert docker.calls == [] and docker.probes == 0 and not receipt.parent.exists()


@pytest.mark.parametrize('fault', ['failed', 'cancelled', 'timed_out', 'truncated', 'bad-iid', 'bad-label'])
def test_failed_build_never_saves_a_success_receipt(configured, fault):
    runtime, docker, _, _, receipt = configured
    def fail(args, options):
        if args[0] != 'build':
            if fault == 'bad-label' and args[:2] == ('image', 'inspect'):
                image = docker.images.get(args[2])
                if image:
                    image['Config']['Labels']['org.recheck.managed'] = 'false'
            return None
        if fault in ('bad-iid', 'bad-label'):
            if fault == 'bad-iid':
                Path(args[args.index('--iidfile') + 1]).write_text('untrusted:latest')
                return process()
            return None
        return {**process(), **({'returncode': 1} if fault == 'failed' else {fault: True})}
    docker.fault = fail
    result = build(runtime)
    assert not result['ready']
    assert result['status'] == ('cancelled' if fault == 'cancelled' else 'failed')
    assert not receipt.exists()


def test_build_does_not_accept_context_modified_while_external_build_runs(configured):
    runtime, docker, resources, _, receipt = configured
    def mutate(args, options):
        if args[0] == 'build':
            (resources / 'docker/static/worker.txt').write_text('raced edit')
    docker.fault = mutate
    result = build(runtime)
    assert not result['ready'] and result['status'] == 'failed' and not receipt.exists()


def test_optional_ollama_build_requires_explicit_include_flag(configured):
    runtime, docker, _, _, receipt = configured
    result = build(runtime, include_ollama=True)
    assert result['ready'] and result['ollama_image_id'] == NEW['ollama']
    assert set(json.loads(receipt.read_text())['images']) == {'static', 'execution', 'ollama'}
    assert not any(args[0] in ('run', 'create', 'start', 'exec', 'volume') for args, _ in docker.calls)


def test_missing_source_context_is_actionable_and_has_no_build_side_effect(configured):
    runtime, docker, resources, _, receipt = configured
    (resources / 'docker/execution/Dockerfile').unlink()
    result = build(runtime)
    assert not result['ready'] and result['status'] == 'failed'
    assert not any(args[0] == 'build' for args, _ in docker.calls) and not receipt.exists()


@pytest.mark.parametrize('kind', ['static', 'execution'])
def test_analysis_adapters_use_context_bound_built_ids_instead_of_old_distribution_ids(configured, kind):
    runtime, docker, resources, _, _ = configured
    assert build(runtime)['ready']
    if kind == 'static':
        from recheck.static_backend import ContainerStaticAdapter
        adapter = ContainerStaticAdapter(resources=resources, runtime=runtime, docker=docker)
        result = asyncio.run(adapter.probe())
        assert result['ready'] and result['image_id'] == NEW['static']
    else:
        from recheck.managed_execution import ManagedExecutionAdapter
        adapter = ManagedExecutionAdapter(Settings(), resources=resources, runtime=runtime, cli='docker')
        adapter.probe = docker.probe
        adapter.call = docker.call
        result = asyncio.run(adapter.prepare_image('linux'))
        assert result['status'] == 'success' and result['image_id'] == NEW['execution']


def test_failed_new_build_preserves_previous_receipt_and_does_not_register_partial_success(configured):
    runtime, docker, _, _, receipt = configured
    assert build(runtime)['ready']
    before = receipt.read_bytes()
    def fail_execution(args, options):
        if args[0] == 'build' and Path(args[-1]).name == 'execution':
            return {**process(), 'returncode': 1}
    docker.fault = fail_execution
    result = build(runtime)
    assert result['status'] == 'failed' and not result['ready']
    assert receipt.read_bytes() == before


def test_built_image_receipt_does_not_authorize_loading_a_different_published_archive(configured):
    runtime, docker, _, manifest, receipt = configured
    value = json.loads(manifest.read_text())
    value['images']['static']['archive'] = {
        'filename': 'old.tar',
        'sha256': hashlib.sha256(b'old distribution not a local build').hexdigest(),
    }
    manifest.write_text(json.dumps(value))
    runtime.bundle_dir.mkdir()
    (runtime.bundle_dir / 'old.tar').write_bytes(b'old distribution not a local build')
    assert build(runtime)['ready']
    assert receipt.exists()
    docker.images.pop(NEW['static'])
    result = asyncio.run(runtime.prepare())
    assert not result['ready']
    assert not any(args[0] in ('load', 'pull') for args, _ in docker.calls)


def test_cancelled_second_component_does_not_register_first_component_as_prepared(configured):
    runtime, docker, _, _, receipt = configured
    def stop(args, options):
        if args[0] == 'build' and Path(args[-1]).name == 'execution':
            return {**process(), 'cancelled': True}
    docker.fault = stop
    result = build(runtime)
    assert result['status'] == 'cancelled' and not result['ready'] and not receipt.exists()
    assert not asyncio.run(runtime.probe())['ready']


def test_missing_sources_do_not_break_legacy_pinned_image_probe(configured):
    runtime, docker, resources, _, receipt = configured
    for component in OLD:
        docker.images[OLD[component]] = dict(Id=OLD[component], Os='linux', Architecture='amd64',
            Config={'Labels': {'org.recheck.component': component}})
    (resources / 'docker/static/Dockerfile').unlink()
    assert asyncio.run(runtime.probe())['ready']
    assert not receipt.exists() and not any(args[0] == 'build' for args, _ in docker.calls)


def test_source_setup_keeps_project_python311_path_compatibility(configured, monkeypatch):
    runtime, _, _, _, _ = configured
    # Python3.11 does not have Path.is_junction; that must not block setup.
    with monkeypatch.context() as patch:
        patch.delattr(Path, 'is_junction', raising=False)
        result = build(runtime)
    assert result['ready']
