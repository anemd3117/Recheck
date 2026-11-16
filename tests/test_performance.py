"""Controlled performance fixtures; no service, model, Ghidra or container is run."""

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from recheck import performance as perf
from recheck.models import Settings


def outcome(status='success', data=None, error=None):
    return dict(status=status, data=data or {'fixture': True}, error=error)


def fixture_stages(monkeypatch, runner, calls, fail=None):
    for stage in perf._STAGES:

        async def execute(mode, cancel, result, name=stage):
            calls.append(name)
            return outcome('failed' if name == fail else 'success', {'fixture': True, 'name': name})

        monkeypatch.setattr(runner, '_' + stage, execute)


def test_quick_stage_selection_atomic_results_and_events(tmp_path, monkeypatch):
    calls = []
    events = []
    runner = perf.BenchmarkRunner(Settings(), root=tmp_path, on_event=events.append)
    fixture_stages(monkeypatch, runner, calls)
    result = asyncio.run(runner.run(hardware={'fixture': True}))
    assert result['status'] == 'success' and result['benchmark_id'].startswith('bench_')
    assert calls == ['cpu', 'ollama', 'docker']
    assert all(
        result['stages'][name]['status'] == 'skipped' for name in ('memory', 'disk', 'ghidra')
    )
    assert json.loads(Path(result['result_path']).read_text(encoding='utf-8')) == result
    assert (
        len(list((Path(result['directory']) / 'attempts' / result['attempt_id']).glob('*.json')))
        == 6
    )
    assert {event['stage'] for event in events} >= set(perf._STAGES)


def test_retry_only_failed_stages_preserves_original_and_attempts(tmp_path, monkeypatch):
    runner = perf.BenchmarkRunner(Settings(), root=tmp_path)
    calls = []
    fixture_stages(monkeypatch, runner, calls, fail='ollama')
    first = asyncio.run(runner.run(hardware={}))
    before = deepcopy(first)
    calls.clear()
    fixture_stages(monkeypatch, runner, calls)
    retried = asyncio.run(runner.run(previous=first, retry_failed=True, hardware={}))
    assert calls == ['ollama'] and retried['status'] == 'success'
    assert (
        first == before
        and json.loads(Path(first['result_path']).read_text(encoding='utf-8')) == before
    )
    assert (
        retried['benchmark_id'] != first['benchmark_id']
        and retried['parent_benchmark_id'] == first['benchmark_id']
    )
    assert retried['stages']['cpu'] == first['stages']['cpu']
    assert retried['stages']['ollama']['attempt_id'] != first['stages']['ollama']['attempt_id']


def test_multiple_retries_keep_original_hardware_date_and_benchmark_provenance(
    tmp_path, monkeypatch
):
    runner = perf.BenchmarkRunner(Settings(), root=tmp_path)
    calls = []
    fixture_stages(monkeypatch, runner, calls, fail='ollama')
    first = asyncio.run(runner.run(hardware={'machine_id': 'original-pc'}))
    second = asyncio.run(
        runner.run(previous=first, retry_failed=True, hardware={'machine_id': 'second-pc'})
    )
    third = asyncio.run(
        runner.run(previous=second, retry_failed=True, hardware={'machine_id': 'third-pc'})
    )
    provenance = third['reused_stages']['cpu']
    assert provenance['benchmark_id'] == first['benchmark_id']
    assert provenance['created_at'] == first['created_at']
    assert provenance['hardware'] == first['hardware']
    assert provenance['settings'] == first['settings']


def test_cancellation_keeps_completed_stage_and_skips_remaining(tmp_path, monkeypatch):
    cancel = threading.Event()
    runner = perf.BenchmarkRunner(Settings(), root=tmp_path)
    calls = []
    fixture_stages(monkeypatch, runner, calls)

    async def cpu(mode, token, result):
        cancel.set()
        return outcome(data={'completed_work': 7})

    monkeypatch.setattr(runner, '_cpu', cpu)
    result = asyncio.run(runner.run(cancel=cancel, hardware={}))
    assert (
        result['status'] == 'cancelled' and result['stages']['cpu']['data']['completed_work'] == 7
    )
    assert all(
        result['stages'][name]['status'] == 'cancelled' for name in perf._STAGES if name != 'cpu'
    )
    assert not calls


def test_hardware_timeout_does_not_cancel_independent_measurements(tmp_path, monkeypatch):
    cancel = threading.Event()
    runner = perf.BenchmarkRunner(Settings(), root=tmp_path)
    calls = []
    fixture_stages(monkeypatch, runner, calls)
    original = runner._bounded

    async def bounded(work, timeout, token):
        if work.cr_code.co_name == '_hardware':
            work.close()
            token.set()
            raise TimeoutError('fixture hardware timeout')
        return await original(work, timeout, token)

    monkeypatch.setattr(runner, '_bounded', bounded)
    result = asyncio.run(runner.run(cancel=cancel))
    assert not cancel.is_set() and result['hardware_error']['code'] == 'hardware_unavailable'
    assert calls == ['cpu', 'ollama', 'docker'] and result['stages']['cpu']['status'] == 'success'


def test_bounded_timeout_cancels_owned_work_and_releases_run_lock(tmp_path, monkeypatch):
    runner = perf.BenchmarkRunner(Settings(), root=tmp_path)
    cleaned = []

    async def blocked():
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.append(True)

    with pytest.raises(TimeoutError):
        asyncio.run(runner._bounded(blocked(), 0.01, perf._Cancel()))
    assert cleaned
    with pytest.raises(ValueError):
        asyncio.run(runner.run(mode='invalid'))
    calls = []
    fixture_stages(monkeypatch, runner, calls)
    assert asyncio.run(runner.run(hardware={}))['status'] == 'success'


def test_cpu_native_work_contract_is_bounded_with_fixture_hash(tmp_path, monkeypatch):
    calls = []

    def fake_hash(name, password, salt, iterations, dklen):
        calls.append((name, iterations, dklen))
        return b'x' * 32

    monkeypatch.setattr(perf.hashlib, 'pbkdf2_hmac', fake_hash)
    monkeypatch.setattr(perf.psutil, 'cpu_count', lambda: 64)
    runner = perf.BenchmarkRunner(Settings(), root=tmp_path)
    result = asyncio.run(runner._cpu('quick', perf._Cancel(), {}))
    assert result['data']['workers'] == 4 and len(calls) == 10
    assert all(item == ('sha256', 100000, 32) for item in calls)
    assert result['data']['checksums_match'] and result['data']['single_operations'] == 2


def test_low_memory_blocks_allocation_and_disk_write(tmp_path, monkeypatch):
    monkeypatch.setattr(
        perf.psutil, 'virtual_memory', lambda: SimpleNamespace(available=16 * 1024 * 1024)
    )
    runner = perf.BenchmarkRunner(Settings(), root=tmp_path)
    for stage in ('memory', 'disk'):
        result = asyncio.run(getattr(runner, '_' + stage)('detailed', perf._Cancel(), {}))
        assert result['status'] == 'blocked' and result['error']['code'] == 'memory_headroom'
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    'field,value', [('eval_count', True), ('eval_duration', -1), ('load_duration', 1.5)]
)
def test_ollama_metrics_reject_invalid_native_counts_and_nanoseconds(field, value):
    response = dict(eval_count=32, eval_duration=2_000_000_000, load_duration=100)
    response[field] = value
    with pytest.raises(ValueError):
        perf.ollama_metrics(response, 3)


def test_ollama_metrics_use_native_ns_and_leave_missing_values_unknown():
    measured = perf.ollama_metrics(
        dict(
            eval_count=32,
            eval_duration=2_000_000_000,
            prompt_eval_duration=100_000_000,
            load_duration=500_000_000,
            total_duration=2_600_000_000,
        ),
        3,
    )
    assert (
        measured['tokens_per_second'] == 16
        and measured['server_total_seconds'] == 2.6
        and measured['total_seconds'] == 3
    )
    absent = perf.ollama_metrics({}, 1)
    assert (
        absent['tokens_per_second'] is None
        and absent['eval_duration_ns'] is None
        and not absent['metrics_available']
    )


@pytest.mark.parametrize('elapsed', [True, -1, float('nan'), float('inf'), '3'])
def test_ollama_metrics_reject_invalid_client_elapsed_time(elapsed):
    with pytest.raises(ValueError, match='전체 응답 시간'):
        perf.ollama_metrics({}, elapsed)


@pytest.mark.parametrize(
    'count_key,duration_key',
    [('eval_count', 'eval_duration'), ('prompt_eval_count', 'prompt_eval_duration')],
)
def test_ollama_metrics_reject_tokens_with_zero_measured_duration(count_key, duration_key):
    with pytest.raises(ValueError, match='측정 시간이 0'):
        perf.ollama_metrics({count_key: 8, duration_key: 0}, 1)


def test_detailed_ollama_two_samples_keep_load_vram_and_options(tmp_path, monkeypatch):
    settings = Settings(model='local:fixture')
    original = settings.model_dump()
    instances = []

    class Client:
        def __init__(self, *args, **kwargs):
            self.calls = []
            self.closed = False
            instances.append(self)

        async def probe(self, cancel):
            return dict(connected=True, version='fixture')

        async def models(self, cancel):
            return [dict(name='local:fixture', digest='same')]

        async def local_metadata(self, *args):
            return dict(details={'format': 'gguf'})

        async def _json(self, *args, **kwargs):
            return dict(models=[{'name': 'local:fixture'}] if self.calls else [])

        async def chat(self, model, messages, **kwargs):
            self.calls.append((model, messages, kwargs))
            return dict(
                eval_count=32,
                eval_duration=2_000_000_000,
                prompt_eval_duration=100,
                load_duration=0 if len(self.calls) > 1 else 500,
                total_duration=2_000_000_600,
                done=True,
                message={'content': '1 2 3 4 5 6 7 8'},
                _execution={'kind': 'local', 'digest': 'same', 'size_vram': 2048},
            )

        async def close(self):
            self.closed = True

    async def sample(cancel=None):
        return {'ram': {'available_bytes': 1234}, 'gpu_sample': {'status': 'fixture', 'gpus': []}}

    monkeypatch.setattr(perf, 'OllamaClient', Client)
    monkeypatch.setattr(perf, '_resource_sample', sample)
    runner = perf.BenchmarkRunner(settings, root=tmp_path)
    result = asyncio.run(
        runner._ollama('detailed', perf._Cancel(), dict(directory=str(tmp_path), attempt_id='a'))
    )
    assert (
        result['status'] == 'success'
        and len(result['data']['samples']) == 2
        and instances[0].closed
    )
    assert [sample['model_loaded_before_request'] for sample in result['data']['samples']] == [
        False,
        True,
    ]
    assert all(
        sample['tokens_per_second'] == 16 and sample['gpu_execution_status'] == 'vram_allocated'
        for sample in result['data']['samples']
    )
    assert all(
        call[2]['options']['num_ctx'] == 2048 and call[2]['options']['num_predict'] == 128
        for call in instances[0].calls
    )
    assert settings.model_dump() == original


def test_ghidra_missing_bundled_sample_is_blocked_without_adapter(tmp_path, monkeypatch):
    monkeypatch.setattr(perf, 'resources_root', lambda: tmp_path)

    class Forbidden:
        def __init__(self, *args, **kwargs):
            raise AssertionError('No sample: no analysis')

    monkeypatch.setattr(perf, 'GhidraAdapter', Forbidden)
    result = asyncio.run(
        perf.BenchmarkRunner(Settings(), tmp_path)._ghidra('detailed', perf._Cancel(), {})
    )
    assert result['status'] == 'blocked' and result['error']['code'] == 'sample_unavailable'


def docker_fixture(monkeypatch, settings, cleanup_fails=False, start_fails=False):
    calls = []

    class Docker:
        def __init__(self, **kwargs):
            pass

        async def probe(self):
            return dict(ready=True, os='linux', arch='amd64', local_endpoint=True)

        async def call(self, *args, **kwargs):
            calls.append((args, kwargs))
            output = ''
            code = 0
            if args[:2] == ('image', 'inspect'):
                output = json.dumps([dict(Id='sha256:fixture', Os='linux', Architecture='amd64')])
            elif args[0] == 'create':
                output = 'owned-container'
            elif args[0] == 'start':
                output = 'RECHECK_BENCHMARK_OK\n/sys/fs/cgroup/cpu.max=100000 100000\n/sys/fs/cgroup/memory.max=536870912\n/sys/fs/cgroup/pids.max=64\n'
                if start_fails:
                    code = 1
            elif args[0] == 'inspect':
                output = json.dumps(
                    [
                        dict(
                            State=dict(Status='exited', ExitCode=0, OOMKilled=False, Error=''),
                            Config=dict(User='65534:65534'),
                            HostConfig=dict(
                                Memory=536870912,
                                NanoCpus=1_000_000_000,
                                PidsLimit=64,
                                NetworkMode='none',
                                ReadonlyRootfs=True,
                                CapDrop=['ALL'],
                                SecurityOpt=['no-new-privileges'],
                            ),
                        )
                    ]
                )
            elif args[0] == 'rm' and cleanup_fails:
                code = 1
            return dict(
                returncode=code,
                stdout=output,
                stderr='fixture failure' if code else '',
                duration=0.01,
            )

    monkeypatch.setattr(perf, 'DockerAdapter', Docker)
    return calls


def test_docker_fixed_owned_execution_checks_inside_and_host_limits(tmp_path, monkeypatch):
    settings = Settings()
    calls = docker_fixture(monkeypatch, settings)
    result = asyncio.run(
        perf.BenchmarkRunner(settings, tmp_path)._docker(
            'detailed', perf._Cancel(), dict(benchmark_id='bench_fixture')
        )
    )
    assert result['status'] == 'success' and all(result['data']['limit_checks'].values())
    assert all(result['data']['inside_limit_checks'].values())
    create = next(args for args, _ in calls if args[0] == 'create')
    assert '--network' in create and '--read-only' in create and '--cap-drop' in create
    assert '--mount' not in create and '--privileged' not in create
    assert create[-1] == perf._SCRIPT and calls[-1][0][0] == 'rm'
    assert calls[-1][0][-1].startswith('recheck-bench-')


def test_docker_cleanup_error_does_not_erase_primary_failure(tmp_path, monkeypatch):
    settings = Settings()
    docker_fixture(monkeypatch, settings, cleanup_fails=True, start_fails=True)
    result = asyncio.run(
        perf.BenchmarkRunner(settings, tmp_path)._docker(
            'detailed', perf._Cancel(), dict(benchmark_id='bench_fixture')
        )
    )
    assert (
        result['status'] == 'failed' and result['error']['code'] == 'container_verification_failed'
    )
    assert result['data']['cleanup_errors'] == ['fixture failure']


def test_quick_docker_probe_never_creates_container(tmp_path, monkeypatch):
    calls = docker_fixture(monkeypatch, Settings())
    result = asyncio.run(
        perf.BenchmarkRunner(Settings(), tmp_path)._docker('quick', perf._Cancel(), {})
    )
    assert result['status'] == 'success' and not result['data']['execution_tested'] and not calls


def test_cancelled_ollama_second_sample_preserves_first_sample(tmp_path, monkeypatch):
    token = perf._Cancel()
    clients = []

    class Client:
        def __init__(self, *args, **kwargs):
            self.calls = 0
            self.closed = False
            clients.append(self)

        async def probe(self, cancel):
            return {'connected': True}

        async def models(self, cancel):
            return [{'name': 'local:fixture', 'digest': 'same'}]

        async def local_metadata(self, *args):
            return {'details': {'format': 'gguf'}}

        async def _json(self, *args, **kwargs):
            return {'models': []}

        async def chat(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 2:
                token.set()
                raise perf._Cancelled()
            return dict(
                eval_count=8,
                eval_duration=1_000_000_000,
                done=True,
                message={'content': 'ok'},
                _execution={'kind': 'local', 'digest': 'same', 'size_vram': 0},
            )

        async def close(self):
            self.closed = True

    async def sample(cancel=None):
        return {'ram': {}, 'gpu_sample': {'gpus': []}}

    monkeypatch.setattr(perf, 'OllamaClient', Client)
    monkeypatch.setattr(perf, '_resource_sample', sample)
    outcome = asyncio.run(
        perf.BenchmarkRunner(Settings(model='local:fixture'), tmp_path)._ollama(
            'detailed', token, dict(directory=str(tmp_path), attempt_id='fixture')
        )
    )
    assert outcome['status'] == 'cancelled' and len(outcome['data']['samples']) == 1
    assert outcome['data']['samples'][0]['tokens_per_second'] == 8 and clients[0].closed
    saved = json.loads(
        (tmp_path / 'attempts/fixture/ollama-progress.json').read_text(encoding='utf-8')
    )
    assert len(saved['samples']) == 1


def test_missing_selected_model_is_blocked_without_generation(tmp_path, monkeypatch):
    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def probe(self, cancel):
            return {'connected': True}

        async def models(self, cancel):
            return []

        async def close(self):
            pass

        async def chat(self, *args, **kwargs):
            raise AssertionError('Uninstalled model must not be loaded')

    monkeypatch.setattr(perf, 'OllamaClient', Client)
    result = asyncio.run(
        perf.BenchmarkRunner(Settings(model='not-installed'), tmp_path)._ollama(
            'quick', perf._Cancel(), {}
        )
    )
    assert result['status'] == 'blocked' and result['error']['code'] == 'model_missing'


def test_ghidra_rejects_sample_manifest_mismatch(tmp_path, monkeypatch):
    source = tmp_path / 'benchmarks'
    source.mkdir()
    (source / 'sample.elf').write_bytes(b'fixture-not-executed')
    (source / 'sample.json').write_text(json.dumps({'sha256': '0' * 64}), encoding='utf-8')
    monkeypatch.setattr(perf, 'resources_root', lambda: tmp_path)

    class Forbidden:
        def __init__(self, *args, **kwargs):
            raise AssertionError('Wrong sample hash must block analysis')

    monkeypatch.setattr(perf, 'GhidraAdapter', Forbidden)
    result = asyncio.run(
        perf.BenchmarkRunner(Settings(), tmp_path)._ghidra('detailed', perf._Cancel(), {})
    )
    assert result['status'] == 'blocked' and result['error']['code'] == 'sample_invalid'


def test_low_disk_space_creates_no_measurement_file(tmp_path, monkeypatch):
    monkeypatch.setattr(
        perf.psutil, 'virtual_memory', lambda: SimpleNamespace(available=1024 * 1024 * 1024)
    )
    monkeypatch.setattr(
        perf.shutil, 'disk_usage', lambda path: SimpleNamespace(free=8 * 1024 * 1024)
    )

    async def sample(cancel=None):
        return {'ram': {}}

    monkeypatch.setattr(perf, '_resource_sample', sample)

    def forbidden(*args, **kwargs):
        raise AssertionError('Low free space must not create a measurement file')

    monkeypatch.setattr(perf.tempfile, 'mkstemp', forbidden)
    result = asyncio.run(
        perf.BenchmarkRunner(Settings(), tmp_path)._disk(
            'detailed', perf._Cancel(), dict(directory=str(tmp_path), attempt_id='fixture')
        )
    )
    assert result['status'] == 'blocked' and result['error']['code'] == 'disk_headroom'


def test_changed_settings_cannot_reuse_previous_measurements(tmp_path, monkeypatch):
    runner = perf.BenchmarkRunner(Settings(), tmp_path)
    calls = []
    fixture_stages(monkeypatch, runner, calls)
    first = asyncio.run(runner.run(hardware={}))
    runner.settings.cpu = 2
    with pytest.raises(ValueError, match='설정이 바뀌었습니다'):
        asyncio.run(runner.run(previous=first, retry_failed=True, hardware={}))
    assert asyncio.run(runner.run(hardware={}))['status'] == 'success'


def test_benchmark_runner_rejects_concurrent_measurements(tmp_path, monkeypatch):
    runner = perf.BenchmarkRunner(Settings(), tmp_path)
    other = perf.BenchmarkRunner(Settings(), tmp_path)
    calls = []
    fixture_stages(monkeypatch, runner, calls)

    async def exercise():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def cpu(mode, cancel, result):
            entered.set()
            await release.wait()
            return outcome()

        monkeypatch.setattr(runner, '_cpu', cpu)
        task = asyncio.create_task(runner.run(hardware={}))
        await entered.wait()
        try:
            with pytest.raises(ValueError, match='다른 성능 측정'):
                await other.run(hardware={})
        finally:
            release.set()
            await task

    asyncio.run(exercise())


def test_variability_describes_observed_speeds_without_inventing_missing_data():
    observed = perf.speed_variability(
        [
            {'tokens_per_second': 10.0},
            {'tokens_per_second': 20.0},
            {'tokens_per_second': None},
            {'tokens_per_second': float('nan')},
        ]
    )
    assert observed['requests'] == 2
    assert observed['mean_tokens_per_second'] == 15
    assert observed['coefficient_of_variation'] == pytest.approx(1 / 3)
    assert perf.speed_variability([{'tokens_per_second': 10}])['coefficient_of_variation'] is None
