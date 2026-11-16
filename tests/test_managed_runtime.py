"""Managed distribution policy exercised without Docker or network access."""

import asyncio
import hashlib
import importlib
import json
import threading
from pathlib import Path

import pytest

from recheck.models import Settings

IDS = {
    'static': 'sha256:' + '1' * 64,
    'execution': 'sha256:' + '2' * 64,
    'ollama': 'sha256:' + '3' * 64,
}


def process(stdout='', returncode=0, **extra):
    return dict(
        stdout=stdout,
        stderr='',
        returncode=returncode,
        cancelled=False,
        timed_out=False,
        truncated=False,
        error=None,
        **extra,
    )


def image(component, **changes):
    value = dict(
        Id=IDS[component],
        Os='linux',
        Architecture='amd64',
        Config={'Labels': {'org.recheck.component': component}},
    )
    value.update(changes)
    return value


class FakeDocker:
    """Substitute only Docker's external I/O; manager policy stays real."""

    def __init__(self, images=(), **environment):
        self.images = {IDS[c]: image(c) for c in images}
        self.environment = dict(
            ready=True, status='ready', os='linux', arch='amd64', local_endpoint=True, **environment
        )
        self.calls = []
        self.probes = 0
        self.on_call = None
        self.container = None
        self.volume = None

    async def probe(self):
        self.probes += 1
        return self.environment

    async def call(self, *args, **options):
        self.calls.append((args, options))
        if self.on_call:
            result = self.on_call(args, options)
            if result is not None:
                return result
        if args[:2] == ('image', 'inspect'):
            data = self.images.get(args[2])
            return process(json.dumps([data])) if data else process(returncode=1)
        raise AssertionError(f'Unexpected Docker mutation: {args}')


@pytest.fixture
def manifest(tmp_path):
    value = {
        'version': 1,
        'versions': {'ghidra': '12.0.4'},
        'images': {
            c: {'ref': f'recheck-{c}:test', 'image_id': IDS[c], 'platform': 'linux/amd64'}
            for c in IDS
        },
    }
    path = tmp_path / 'environment.json'
    path.write_text(json.dumps(value), encoding='utf-8')
    return path, value


def runtime(manifest, docker, tmp_path):
    # This explicit assertion makes the initial RED a missing capability failure.
    assert importlib.util.find_spec('recheck.managed_runtime'), 'Managed runtime is not implemented'
    module = importlib.import_module('recheck.managed_runtime')
    path, value = manifest
    path.write_text(json.dumps(value), encoding='utf-8')
    return module.ManagedRuntime(
        Settings(), docker=docker, manifest_path=path, bundle_dir=tmp_path / 'environment'
    )


def run(awaitable):
    return asyncio.run(awaitable)


def test_probe_accepts_exact_component_images_without_mutations(manifest, tmp_path):
    docker = FakeDocker(['static', 'execution'])
    result = run(runtime(manifest, docker, tmp_path).probe())
    assert result['ready'] and result['status'] == 'ready'
    assert result['images']['static']['image_id'] == 'sha256:' + '1' * 64
    assert not result['images']['ollama']['ready']
    assert all(args[:2] == ('image', 'inspect') for args, _ in docker.calls)


@pytest.mark.parametrize(
    'environment',
    [dict(local_endpoint=False), dict(os='windows'), dict(arch='arm64'), dict(ready=False)],
)
def test_prepare_rejects_remote_or_incompatible_daemon(manifest, tmp_path, environment):
    docker = FakeDocker()
    docker.environment.update(environment)
    result = run(runtime(manifest, docker, tmp_path).prepare())
    assert not result['ready'] and result['status'] == 'blocked'
    assert not docker.calls


@pytest.mark.parametrize(
    'changes',
    [
        dict(Id='sha256:' + '9' * 64),
        dict(Config={'Labels': {'org.recheck.component': 'unrelated'}}),
        dict(Architecture='arm64'),
        dict(Os='windows'),
    ],
)
def test_untrusted_cached_image_is_never_accepted(manifest, tmp_path, changes):
    docker = FakeDocker(['static', 'execution'])
    docker.images[IDS['static']].update(changes)
    result = run(runtime(manifest, docker, tmp_path).prepare())
    assert not result['ready'] and result['images']['static']['status'] == 'invalid'
    assert all(args[:2] == ('image', 'inspect') for args, _ in docker.calls)


def test_missing_distribution_is_actionable_and_never_builds_or_pulls(manifest, tmp_path):
    docker = FakeDocker()
    result = run(runtime(manifest, docker, tmp_path).prepare())
    assert not result['ready'] and result['status'] == 'distribution_missing'
    assert 'environment' in result['message'].lower()
    assert all(args[:2] == ('image', 'inspect') for args, _ in docker.calls)


def add_archive(manifest, tmp_path, component='static', contents=b'verified archive'):
    bundle = tmp_path / 'environment'
    bundle.mkdir(exist_ok=True)
    archive = bundle / (component + '.tar')
    archive.write_bytes(contents)
    manifest[1]['images'][component]['archive'] = {
        'filename': archive.name,
        'sha256': hashlib.sha256(contents).hexdigest(),
    }
    return archive


def test_prepare_hashes_archive_then_loads_without_network(manifest, tmp_path):
    archive = add_archive(manifest, tmp_path)
    docker = FakeDocker(['execution'])

    def load(args, options):
        if args[0] == 'load':
            assert args == ('load', '--input', str(archive.resolve()))
            assert 0 < options['timeout'] <= 1800 and options['cancel'] is cancel
            docker.images[IDS['static']] = image('static')
            return process('Loaded image')

    cancel = threading.Event()
    docker.on_call = load
    events = []
    result = run(runtime(manifest, docker, tmp_path).prepare(cancel=cancel, on_event=events.append))
    assert result['ready'] and result['status'] == 'success'
    assert any(event.get('component') == 'static' for event in events)
    assert not any(args[0] in ('pull', 'build', 'create', 'start') for args, _ in docker.calls)


def test_archive_tampering_prevents_load_or_network_fallback(manifest, tmp_path):
    archive = add_archive(manifest, tmp_path)
    archive.write_bytes(b'changed archive')
    docker = FakeDocker(['execution'])
    result = run(runtime(manifest, docker, tmp_path).prepare())
    assert not result['ready'] and result['status'] == 'integrity_failed'
    assert not any(args[0] in ('load', 'pull', 'build') for args, _ in docker.calls)


@pytest.mark.parametrize('name', ['../outside.tar', 'C:\\outside.tar', 'nested/archive.tar'])
def test_archive_path_cannot_escape_distribution_folder(manifest, tmp_path, name):
    manifest[1]['images']['static']['archive'] = {'filename': name, 'sha256': 'a' * 64}
    docker = FakeDocker(['execution'])
    result = run(runtime(manifest, docker, tmp_path).prepare())
    assert not result['ready'] and result['status'] == 'manifest_invalid'
    assert not any(args[0] in ('load', 'pull', 'build') for args, _ in docker.calls)


def test_explicit_prepare_can_pull_only_trusted_immutable_distribution(manifest, tmp_path):
    ref = 'ghcr.io/recheck/static@sha256:' + '4' * 64
    manifest[1]['images']['static'].update(ref=ref, trusted_distribution=True)
    docker = FakeDocker(['execution'])

    def pull(args, options):
        if args[0] == 'pull':
            assert args == ('pull', '--platform', 'linux/amd64', ref)
            assert 0 < options['timeout'] <= 1800
            docker.images[IDS['static']] = image('static')
            return process()

    docker.on_call = pull
    manager = runtime(manifest, docker, tmp_path)
    assert not run(manager.probe())['ready']
    assert not any(args[0] == 'pull' for args, _ in docker.calls)
    assert run(manager.prepare())['ready']


@pytest.mark.parametrize(
    'trusted,ref',
    [(True, 'ghcr.io/recheck/static:latest'), (False, 'ghcr.io/recheck/static@sha256:' + '4' * 64)],
)
def test_prepare_never_pulls_mutable_or_untrusted_reference(manifest, tmp_path, trusted, ref):
    manifest[1]['images']['static'].update(ref=ref, trusted_distribution=trusted)
    docker = FakeDocker(['execution'])
    result = run(runtime(manifest, docker, tmp_path).prepare())
    assert not result['ready']
    assert not any(args[0] in ('load', 'pull', 'build') for args, _ in docker.calls)


def test_cancelled_prepare_does_not_touch_docker(manifest, tmp_path):
    cancel = threading.Event()
    cancel.set()
    docker = FakeDocker()
    result = run(runtime(manifest, docker, tmp_path).prepare(cancel=cancel))
    assert result['status'] == 'cancelled' and not result['ready']
    assert not docker.calls and docker.probes == 0


def test_prepare_does_not_start_optional_ollama_for_gpt(manifest, tmp_path):
    docker = FakeDocker(['static', 'execution'])
    result = run(runtime(manifest, docker, tmp_path).prepare())
    assert result['ready'] and not result.get('ollama_url')
    assert not any(args[0] in ('create', 'start', 'run', 'volume') for args, _ in docker.calls)


def test_development_placeholder_is_never_reported_ready(manifest, tmp_path):
    manifest[1]['images']['static']['image_id'] = None
    docker = FakeDocker(['static', 'execution'])
    result = run(runtime(manifest, docker, tmp_path).probe())
    assert not result['ready'] and result['images']['static']['status'] == 'unavailable'


OWNED = {
    'org.recheck.managed': 'true',
    'org.recheck.component': 'ollama',
    'org.recheck.owner': 'managed-runtime-v1',
}
CONTAINER = 'recheck-managed-ollama-v1'
VOLUME = 'recheck-managed-ollama-models-v1'


def owned_container(running=True):
    return {
        'Id': 'owned-container-id',
        'Image': IDS['ollama'],
        'Config': {
            'Labels': dict(OWNED),
            'Env': ['OLLAMA_NO_CLOUD=1', 'OLLAMA_HOST=0.0.0.0:11434'],
        },
        'State': {'Running': running},
        'HostConfig': {
            'PortBindings': {'11434/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '11435'}]}
        },
        'Mounts': [{'Type': 'volume', 'Name': VOLUME, 'Destination': '/root/.ollama'}],
    }


class OllamaDocker(FakeDocker):
    def __init__(self, **environment):
        super().__init__(_COMPONENTS_FIXTURE, **environment)
        self.health = process('NAME ID SIZE MODIFIED')

    async def call(self, *args, **options):
        if args[:2] == ('image', 'inspect'):
            return await super().call(*args, **options)
        self.calls.append((args, options))
        if args[:2] == ('container', 'inspect'):
            return (
                process(json.dumps([self.container])) if self.container else process(returncode=1)
            )
        if args[:2] == ('volume', 'inspect'):
            return process(json.dumps([self.volume])) if self.volume else process(returncode=1)
        if args[:2] == ('volume', 'create'):
            self.volume = {'Name': VOLUME, 'Labels': dict(OWNED)}
            return process(VOLUME)
        if args[0] == 'create':
            self.container = owned_container(running=False)
            return process('owned-container-id')
        if args[0] == 'start':
            self.container['State']['Running'] = True
            return process(CONTAINER)
        if args[0] == 'exec':
            assert options['timeout'] <= 5
            return self.health
        if args[0] == 'stop':
            self.container['State']['Running'] = False
            return process(CONTAINER)
        if args[0] == 'rm':
            self.container = None
            return process(CONTAINER)
        raise AssertionError(f'Unexpected Docker command: {args}')


_COMPONENTS_FIXTURE = ('static', 'execution', 'ollama')


def test_ollama_starts_loopback_cpu_service_with_persistent_owned_models(manifest, tmp_path):
    docker = OllamaDocker()
    manager = runtime(manifest, docker, tmp_path)
    result = run(manager.start_ollama())
    assert result['ready'] and result['ollama_url'] == 'http://127.0.0.1:11435'
    assert manager.ollama_url == result['ollama_url']
    create = next(args for args, _ in docker.calls if args[0] == 'create')
    assert create[create.index('--publish') + 1] == '127.0.0.1:11435:11434'
    assert IDS['ollama'] in create and 'OLLAMA_NO_CLOUD=1' in create
    assert '--gpus' not in create and '--privileged' not in create
    assert 'type=volume,src=' + VOLUME + ',dst=/root/.ollama' in create
    assert not any('docker.sock' in value or 'type=bind' in value for value in create)
    assert any(args[0] == 'exec' for args, _ in docker.calls)


def test_optional_ollama_prepare_imports_image_without_starting_server(manifest, tmp_path):
    docker = OllamaDocker()
    result = run(runtime(manifest, docker, tmp_path).prepare(include_ollama=True))
    assert result['ready'] and result['images']['ollama']['ready']
    assert not any(args[0] in ('create', 'start', 'volume') for args, _ in docker.calls)


@pytest.mark.parametrize('owned_resource', ['container', 'volume'])
def test_ollama_name_collision_preserves_unrelated_resources(manifest, tmp_path, owned_resource):
    docker = OllamaDocker()
    if owned_resource == 'container':
        docker.container = owned_container()
        docker.container['Config']['Labels'] = {}
    else:
        docker.volume = {'Name': VOLUME, 'Labels': {}}
    result = run(runtime(manifest, docker, tmp_path).start_ollama())
    assert not result['ready'] and result['status'] == 'ownership_conflict'
    assert not result.get('ollama_url')
    assert not any(args[0] in ('create', 'start', 'stop', 'rm') for args, _ in docker.calls)
    assert getattr(docker, owned_resource) is not None


@pytest.mark.parametrize('mutation', ['image', 'public_port', 'cloud', 'mount'])
def test_existing_managed_ollama_requires_safe_original_configuration(manifest, tmp_path, mutation):
    docker = OllamaDocker()
    docker.container = owned_container()
    if mutation == 'image':
        docker.container['Image'] = 'sha256:' + '9' * 64
    elif mutation == 'public_port':
        docker.container['HostConfig']['PortBindings']['11434/tcp'][0]['HostIp'] = '0.0.0.0'
    elif mutation == 'cloud':
        docker.container['Config']['Env'] = []
    else:
        docker.container['Mounts'] = [
            {'Type': 'bind', 'Source': 'C:/Users', 'Destination': '/root/.ollama'}
        ]
    result = run(runtime(manifest, docker, tmp_path).start_ollama())
    assert not result['ready'] and not result.get('ollama_url')
    assert not any(args[0] in ('start', 'stop', 'rm', 'exec') for args, _ in docker.calls)


def test_ollama_health_cancellation_cleans_only_owned_container_and_keeps_volume(
    manifest, tmp_path
):
    docker = OllamaDocker()
    docker.health = dict(process(returncode=-1), cancelled=True)
    manager = runtime(manifest, docker, tmp_path)
    result = run(manager.start_ollama())
    assert result['status'] == 'cancelled' and not result.get('ollama_url')
    assert manager.ollama_url is None and docker.container is None
    assert docker.volume['Name'] == VOLUME
    assert not any(args[:2] == ('volume', 'rm') for args, _ in docker.calls)


def test_stop_ollama_removes_only_owned_container_and_retains_models(manifest, tmp_path):
    docker = OllamaDocker()
    manager = runtime(manifest, docker, tmp_path)
    assert run(manager.start_ollama())['ready']
    result = run(manager.stop_ollama())
    assert result['status'] == 'success' and not result.get('ollama_url')
    assert manager.ollama_url is None and docker.container is None and docker.volume
    removals = [args for args, _ in docker.calls if args[0] == 'rm']
    assert removals == [('rm', CONTAINER)]


def test_stop_ollama_refuses_to_touch_unowned_container(manifest, tmp_path):
    docker = OllamaDocker()
    docker.container = owned_container()
    docker.container['Config']['Labels']['org.recheck.owner'] = 'someone-else'
    result = run(runtime(manifest, docker, tmp_path).stop_ollama())
    assert result['status'] == 'ownership_conflict' and docker.container
    assert not any(args[0] in ('stop', 'rm', 'kill') for args, _ in docker.calls)


def test_ollama_async_task_cancellation_also_cleans_created_container(manifest, tmp_path):
    docker = OllamaDocker()
    original = docker.call

    async def call(*args, **options):
        if args[0] == 'exec':
            raise asyncio.CancelledError()
        return await original(*args, **options)

    docker.call = call
    manager = runtime(manifest, docker, tmp_path)
    with pytest.raises(asyncio.CancelledError):
        run(manager.start_ollama())
    assert docker.container is None and docker.volume


def test_probe_revokes_ollama_url_when_service_has_stopped(manifest, tmp_path):
    docker = OllamaDocker()
    manager = runtime(manifest, docker, tmp_path)
    assert run(manager.start_ollama())['ollama_url'] == 'http://127.0.0.1:11435'
    docker.container['State']['Running'] = False
    docker.calls.clear()
    result = run(manager.probe())
    assert result['ready'] and not result.get('ollama_url')
    assert manager.ollama_url is None
    assert not any(args[0] in ('create', 'start', 'stop', 'rm') for args, _ in docker.calls)
