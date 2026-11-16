"""Bounded local performance observations; never starts services or downloads assets."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import ctypes
import hashlib
import importlib
import inspect
import json
import math
import os
from pathlib import Path
import platform
import statistics
import shutil
import tempfile
import threading
import time
import uuid

import psutil

from .adapters import DockerAdapter, GhidraAdapter
from .models import atomic_json, data_root, resources_root
from .ollama import OllamaClient
from .storage import now

_RUN_LOCK = threading.Lock()
_STAGES = ('cpu', 'memory', 'disk', 'ollama', 'ghidra', 'docker')
_MIB = 1024 * 1024
_SCRIPT = """printf 'RECHECK_BENCHMARK_OK\n'
for file in /sys/fs/cgroup/cpu.max /sys/fs/cgroup/memory.max /sys/fs/cgroup/pids.max /sys/fs/cgroup/cpu/cpu.cfs_quota_us /sys/fs/cgroup/cpu/cpu.cfs_period_us /sys/fs/cgroup/memory/memory.limit_in_bytes; do
  if [ -r "$file" ]; then printf '%s=' "$file"; cat "$file"; fi
done"""


class _Cancelled(Exception):
    pass


class _Cancel:
    def __init__(self, external=None):
        self.external, self.local = external, threading.Event()

    def is_set(self):
        return self.local.is_set() or bool(self.external and self.external.is_set())

    def set(self):
        self.local.set()


def _ok(process):
    return process.get('returncode') == 0 and not any(
        process.get(key) for key in ('error', 'timed_out', 'cancelled', 'truncated')
    )


def _small(value, limit=16384):
    encoded = json.dumps(value, ensure_ascii=False, default=str)
    if len(encoded) <= limit:
        return value
    return dict(excerpt=encoded[:limit], truncated=True, original_characters=len(encoded))


def _outcome(status='success', data=None, error=None):
    return dict(status=status, data=data, error=error)


def _error(code, message, detail=None):
    return dict(
        code=code, message=message, detail=str(detail)[:2048] if detail is not None else None
    )


async def _hardware(settings, cancel):
    try:
        module = importlib.import_module('.hardware', __package__)
        return await module.detect_hardware(settings, project_path=None, cancel=cancel)
    except (ImportError, AttributeError):
        return dict(
            source='psutil fallback',
            platform=platform.platform(),
            logical_cpus=psutil.cpu_count(),
            physical_cpus=psutil.cpu_count(logical=False),
            memory_total_bytes=psutil.virtual_memory().total,
            gpu=None,
            limitations=['GPU 정보 수집기를 사용할 수 없습니다.'],
        )


async def _resource_sample(cancel=None):
    try:
        module = importlib.import_module('.hardware', __package__)
        result = await asyncio.to_thread(module.detect_resource_sample)
        result = await result if inspect.isawaitable(result) else result
        if hasattr(module, 'detect_gpu_sample'):
            try:
                result['gpu_sample'] = await asyncio.wait_for(
                    module.detect_gpu_sample(cancel=cancel), timeout=9
                )
            except Exception as exc:
                result['gpu_sample'] = dict(status='unknown', gpus=[], warnings=[str(exc)[:300]])
        return result
    except Exception as exc:
        memory = psutil.virtual_memory()
        return dict(
            created_at=now(),
            ram=dict(
                total_bytes=memory.total, available_bytes=memory.available, used_bytes=memory.used
            ),
            gpu=None,
            gpu_status='unavailable',
            collection_warning=str(exc)[:300],
        )


def ollama_metrics(response, elapsed_seconds):
    """Only native Ollama count/ns values produce token rates; absent metrics stay null."""
    if (
        isinstance(elapsed_seconds, bool)
        or not isinstance(elapsed_seconds, (int, float))
        or not math.isfinite(elapsed_seconds)
        or elapsed_seconds < 0
    ):
        raise ValueError('Ollama 전체 응답 시간이 올바르지 않습니다.')
    keys = (
        'eval_count',
        'eval_duration',
        'prompt_eval_count',
        'prompt_eval_duration',
        'load_duration',
        'total_duration',
    )
    for key in keys:
        value = response.get(key)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            raise ValueError('Ollama 측정값 형식이 올바르지 않습니다: ' + key)
    count, duration = response.get('eval_count'), response.get('eval_duration')
    for count_key, duration_key in (
        ('eval_count', 'eval_duration'),
        ('prompt_eval_count', 'prompt_eval_duration'),
    ):
        if response.get(count_key) and response.get(duration_key) == 0:
            raise ValueError('토큰 수가 있으나 측정 시간이 0입니다: ' + duration_key)
    return dict(
        eval_count=count,
        eval_duration_ns=duration,
        prompt_eval_count=response.get('prompt_eval_count'),
        prompt_eval_duration_ns=response.get('prompt_eval_duration'),
        load_duration_ns=response.get('load_duration'),
        total_duration_ns=response.get('total_duration'),
        total_seconds=elapsed_seconds,
        server_total_seconds=(
            response['total_duration'] / 1e9 if response.get('total_duration') is not None else None
        ),
        tokens_per_second=count / (duration / 1e9) if count and duration else None,
        metrics_available=bool(count and duration),
    )


def speed_variability(samples):
    """Describe short requests without treating speed as answer quality."""
    speeds = [
        sample['tokens_per_second']
        for sample in samples
        if isinstance(sample.get('tokens_per_second'), (int, float))
        and not isinstance(sample.get('tokens_per_second'), bool)
        and math.isfinite(sample['tokens_per_second'])
        and sample['tokens_per_second'] > 0
    ]
    mean = statistics.mean(speeds) if speeds else None
    return dict(
        requests=len(speeds),
        mean_tokens_per_second=mean,
        min_tokens_per_second=min(speeds) if speeds else None,
        max_tokens_per_second=max(speeds) if speeds else None,
        coefficient_of_variation=statistics.pstdev(speeds) / mean if len(speeds) > 1 else None,
        limitation='동일한 짧은 요청의 관찰 범위이며 두 번 측정만으로 장기 안정성이나 응답 품질을 판단할 수 없습니다.',
    )


class BenchmarkRunner:
    def __init__(self, settings, root=None, on_event=None):
        self.settings = settings
        self.root = Path(root) if root is not None else data_root() / 'benchmarks'
        self.on_event = on_event

    async def _event(self, result, stage, status, **extra):
        event = dict(
            event_id='event_' + uuid.uuid4().hex,
            created_at=now(),
            benchmark_id=result['benchmark_id'],
            attempt_id=result['attempt_id'],
            stage=stage,
            status=status,
            **extra,
        )
        with Path(result['events_path']).open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(event, ensure_ascii=False, default=str) + '\n')
            stream.flush()
        if self.on_event:
            try:
                value = self.on_event(event)
                if inspect.isawaitable(value):
                    await value
            except Exception as exc:
                result.setdefault('callback_errors', []).append(str(exc)[:1000])

    def _save(self, result, stage=None):
        result['updated_at'] = now()
        if stage:
            atomic_json(
                Path(result['directory']) / 'attempts' / result['attempt_id'] / (stage + '.json'),
                result['stages'][stage],
            )
        atomic_json(Path(result['result_path']), result)

    def _progress(self, result, stage, data):
        if stage in result.get('stages', {}):
            result['stages'][stage]['data'] = data
            self._save(result, stage)

    async def _bounded(self, work, timeout, cancel):
        task = asyncio.create_task(work)
        started = time.monotonic()
        try:
            while not task.done():
                if cancel.is_set():
                    raise _Cancelled()
                if time.monotonic() - started > timeout:
                    cancel.set()
                    raise TimeoutError('측정 제한 시간을 초과했습니다.')
                await asyncio.wait({task}, timeout=0.05)
            return await task
        finally:
            if not task.done():
                cancel.set()
                task.cancel()
                try:
                    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=12)
                except asyncio.TimeoutError:
                    pass

    async def run(
        self, mode='quick', cancel=None, previous=None, retry_failed=False, hardware=None
    ):
        if mode not in ('quick', 'detailed'):
            raise ValueError('빠른 측정 또는 자세한 측정을 선택하세요.')
        if not _RUN_LOCK.acquire(blocking=False):
            raise ValueError('다른 성능 측정이 진행 중입니다. 완료 후 다시 시도하세요.')
        try:
            old = None
            if previous is not None:
                old = (
                    json.loads(Path(previous).read_text(encoding='utf-8'))
                    if isinstance(previous, (str, Path))
                    else deepcopy(previous)
                )
            if retry_failed and not old:
                raise ValueError('다시 측정할 이전 결과가 필요합니다.')
            settings = self.settings.model_dump()
            if retry_failed and (old.get('mode') != mode or old.get('settings') != settings):
                raise ValueError('측정 설정이 바뀌었습니다. 새 측정을 시작하세요.')
            benchmark_id = 'bench_' + uuid.uuid4().hex
            directory = self.root / benchmark_id
            directory.mkdir(parents=True, exist_ok=False)
            result = dict(
                schema_version='1.0',
                benchmark_id=benchmark_id,
                attempt_id='attempt_' + uuid.uuid4().hex,
                created_at=now(),
                mode=mode,
                status='running',
                hardware=hardware,
                settings=settings,
                directory=str(directory.resolve()),
                result_path=str((directory / 'result.json').resolve()),
                events_path=str((directory / 'events.jsonl').resolve()),
                stages={
                    name: dict(status='pending', data=None, error=None, duration_seconds=None)
                    for name in _STAGES
                },
                parent_benchmark_id=old.get('benchmark_id') if old else None,
                reused_stages={},
                limitations=[
                    '측정 당시의 부하·전원·온도·파일 캐시에 영향을 받습니다. 절대 성능 점수나 장치의 최대 성능이 아닙니다.'
                ],
            )
            self._save(result)
            external = _Cancel(cancel)
            if hardware is None and not external.is_set():
                try:
                    hardware_cancel = _Cancel(external)
                    result['hardware'] = await self._bounded(
                        _hardware(self.settings, hardware_cancel), 30, hardware_cancel
                    )
                except _Cancelled:
                    pass
                except Exception as exc:
                    result['hardware'] = None
                    result['hardware_error'] = _error(
                        'hardware_unavailable', 'PC 정보를 확인하지 못했습니다.', exc
                    )
            self._save(result)
            for stage in _STAGES:
                prior = old.get('stages', {}).get(stage, {}) if retry_failed else {}
                if prior.get('status') in ('success', 'skipped'):
                    result['stages'][stage] = deepcopy(prior)
                    source = old.get('reused_stages', {}).get(stage)
                    result['reused_stages'][stage] = (
                        deepcopy(source)
                        if isinstance(source, dict)
                        else dict(
                            benchmark_id=old.get('benchmark_id'),
                            result_path=old.get('result_path'),
                            created_at=old.get('created_at'),
                            hardware=old.get('hardware'),
                            settings=old.get('settings'),
                        )
                    )
                    self._save(result, stage)
                    await self._event(result, stage, 'reused')
                    continue
                if external.is_set():
                    result['stages'][stage] = dict(
                        status='cancelled',
                        data=None,
                        error=_error('cancelled', '사용자가 측정을 취소했습니다.'),
                        duration_seconds=0,
                    )
                    self._save(result, stage)
                    await self._event(result, stage, 'cancelled')
                    continue
                if mode == 'quick' and stage in ('memory', 'disk', 'ghidra'):
                    result['stages'][stage] = dict(
                        status='skipped',
                        data=None,
                        error=None,
                        duration_seconds=0,
                        reason='자세한 측정에서 실행합니다.',
                    )
                    self._save(result, stage)
                    await self._event(result, stage, 'skipped')
                    continue
                started = time.perf_counter()
                stage_cancel = _Cancel(external)
                record = dict(
                    status='running',
                    data=None,
                    error=None,
                    started_at=now(),
                    attempt_id=result['attempt_id'],
                    duration_seconds=None,
                )
                result['stages'][stage] = record
                self._save(result, stage)
                await self._event(result, stage, 'running')
                try:
                    outcome = await self._bounded(
                        getattr(self, '_' + stage)(mode, stage_cancel, result),
                        dict(
                            cpu=30,
                            memory=30,
                            disk=30,
                            ollama=90 if mode == 'quick' else 180,
                            ghidra=100,
                            docker=70,
                        )[stage],
                        stage_cancel,
                    )
                    record.update(outcome)
                except (_Cancelled, asyncio.CancelledError):
                    record.update(
                        status='cancelled',
                        error=_error('cancelled', '사용자가 측정을 취소했습니다.'),
                    )
                    external.set()
                except (TimeoutError, asyncio.TimeoutError) as exc:
                    record.update(
                        status='failed',
                        error=_error('timeout', '측정 제한 시간을 초과했습니다.', exc),
                    )
                except Exception as exc:
                    record.update(
                        status='failed',
                        error=_error(
                            getattr(exc, 'code', 'measurement_failed'),
                            '성능 측정 중 오류가 발생했습니다.',
                            exc,
                        ),
                    )
                record.update(duration_seconds=time.perf_counter() - started, finished_at=now())
                if isinstance(record.get('data'), dict):
                    record['data'].setdefault('duration_seconds', record['duration_seconds'])
                self._save(result, stage)
                await self._event(result, stage, record['status'])
            active = [
                record['status']
                for record in result['stages'].values()
                if record['status'] != 'skipped'
            ]
            result['status'] = (
                'cancelled'
                if external.is_set() or 'cancelled' in active
                else (
                    'success'
                    if active and all(status == 'success' for status in active)
                    else (
                        'partial'
                        if 'success' in active
                        else (
                            'blocked' if all(status == 'blocked' for status in active) else 'failed'
                        )
                    )
                )
            )
            if result['status'] == 'success' and result.get('hardware_error'):
                result['status'] = 'partial'
            result['completed_at'] = now()
            self._save(result)
            await self._event(result, 'benchmark', result['status'])
            return result
        finally:
            _RUN_LOCK.release()

    async def _cpu(self, mode, cancel, result):
        def measure():
            iterations = 100000 if mode == 'quick' else 200000
            rounds = 2 if mode == 'quick' else 4
            workers = max(1, min(4, psutil.cpu_count() or 1))

            def batch():
                checksum = hashlib.sha256()
                for _ in range(rounds):
                    if cancel.is_set():
                        raise _Cancelled()
                    checksum.update(
                        hashlib.pbkdf2_hmac(
                            'sha256',
                            b'recheck-performance',
                            b'fixed-benchmark-salt',
                            iterations,
                            dklen=32,
                        )
                    )
                return checksum.hexdigest()

            start = time.perf_counter()
            single = batch()
            single_seconds = time.perf_counter() - start
            start = time.perf_counter()
            with ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix='recheck-bench'
            ) as pool:
                checksums = list(pool.map(lambda _: batch(), range(workers)))
            multiple_seconds = time.perf_counter() - start
            return _outcome(
                data=dict(
                    algorithm='PBKDF2-HMAC-SHA256',
                    implementation='hashlib/OpenSSL native C',
                    iterations=iterations,
                    rounds_per_worker=rounds,
                    workers=workers,
                    single_seconds=single_seconds,
                    single_operations=rounds,
                    single_operations_per_second=(
                        rounds / single_seconds if single_seconds else None
                    ),
                    parallel_seconds=multiple_seconds,
                    parallel_operations=rounds * workers,
                    parallel_operations_per_second=(
                        rounds * workers / multiple_seconds if multiple_seconds else None
                    ),
                    checksum=single,
                    checksums_match=all(value == single for value in checksums),
                    limitation='고정된 네이티브 암호 연산의 처리량이며 Python 반복문·범용 CPU 성능 점수가 아닙니다.',
                )
            )

        return await asyncio.to_thread(measure)

    async def _memory(self, mode, cancel, result):
        available = psutil.virtual_memory().available
        if available < 80 * _MIB:
            return _outcome(
                'blocked',
                dict(available_bytes=available, required_headroom_bytes=80 * _MIB),
                _error(
                    'memory_headroom', '사용 가능한 메모리가 부족하여 메모리 측정을 건너뛰었습니다.'
                ),
            )
        before = await _resource_sample(cancel)

        def measure():
            size, rounds = 8 * _MIB, 16
            rss_before = psutil.Process().memory_info().rss
            source = (ctypes.c_ubyte * size)()
            target = (ctypes.c_ubyte * size)()
            rss_allocated = psutil.Process().memory_info().rss
            ctypes.memset(source, 0x5A, size)
            start = time.perf_counter()
            for _ in range(rounds):
                if cancel.is_set():
                    raise _Cancelled()
                ctypes.memmove(target, source, size)
            copied = time.perf_counter() - start
            start = time.perf_counter()
            checksum = hashlib.sha256(memoryview(target).cast('B')).hexdigest()
            read_seconds = time.perf_counter() - start
            expected = hashlib.sha256(memoryview(source).cast('B')).hexdigest()
            return _outcome(
                data=dict(
                    buffer_bytes=size,
                    allocated_bytes=size * 2,
                    rounds=rounds,
                    copy_bytes=size * rounds,
                    copy_seconds=copied,
                    copy_bytes_per_second=size * rounds / copied if copied else None,
                    write_bytes_per_second=size * rounds / copied if copied else None,
                    read_bytes=size,
                    read_seconds=read_seconds,
                    read_bytes_per_second=size / read_seconds if read_seconds else None,
                    sha256=checksum,
                    checksum_matches=checksum == expected,
                    implementation='ctypes C memmove / hashlib SHA256',
                    rss_before_bytes=rss_before,
                    rss_allocated_bytes=rss_allocated,
                    limitation='쓰기는 읽기와 쓰기를 함께 수행하는 C 메모리 복사의 처리량입니다. 캐시 영향을 받으며 읽기에는 체크섬 계산이 포함됩니다. DRAM 최대 대역폭이 아닙니다.',
                )
            )

        outcome = await asyncio.to_thread(measure)
        outcome['data'].update(
            resource_before=_small(before, 8192),
            resource_after=_small(await _resource_sample(cancel), 8192),
            rss_after_bytes=psutil.Process().memory_info().rss,
        )
        return outcome

    async def _disk(self, mode, cancel, result):
        if psutil.virtual_memory().available < 80 * _MIB:
            return _outcome(
                'blocked',
                error=_error(
                    'memory_headroom',
                    '사용 가능한 메모리가 부족하여 파일 읽기·쓰기 측정을 건너뛰었습니다.',
                ),
            )
        before = await _resource_sample(cancel)

        def measure():
            directory = Path(result['directory']) / 'attempts' / result['attempt_id'] / 'disk-work'
            directory.mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(directory).free
            if free < 32 * _MIB:
                return _outcome(
                    'blocked',
                    dict(free_bytes=free),
                    _error('disk_headroom', '측정용 임시 파일을 위한 여유 공간이 부족합니다.'),
                )
            rss_before = psutil.Process().memory_info().rss
            fd, name = tempfile.mkstemp(prefix='recheck-benchmark-', suffix='.tmp', dir=directory)
            path = Path(name)
            os.close(fd)
            data = {}
            cleanup = []
            try:
                block = b'\x5a' * _MIB
                blocks = 16
                expected = hashlib.sha256()
                for _ in range(blocks):
                    expected.update(block)
                start = time.perf_counter()
                with path.open('wb') as stream:
                    for _ in range(blocks):
                        if cancel.is_set():
                            raise _Cancelled()
                        stream.write(block)
                    stream.flush()
                    os.fsync(stream.fileno())
                write_seconds = time.perf_counter() - start
                start = time.perf_counter()
                checksum = hashlib.sha256()
                read_bytes = 0
                with path.open('rb') as stream:
                    while chunk := stream.read(_MIB):
                        if cancel.is_set():
                            raise _Cancelled()
                        checksum.update(chunk)
                        read_bytes += len(chunk)
                read_seconds = time.perf_counter() - start
                data.update(
                    file_bytes=blocks * _MIB,
                    read_bytes=read_bytes,
                    write_seconds=write_seconds,
                    read_seconds=read_seconds,
                    write_bytes_per_second=blocks * _MIB / write_seconds if write_seconds else None,
                    read_bytes_per_second=read_bytes / read_seconds if read_seconds else None,
                    fsync_performed=True,
                    sha256=checksum.hexdigest(),
                    checksum_matches=checksum.digest() == expected.digest(),
                    cleanup_errors=cleanup,
                    location=str(directory),
                    rss_before_bytes=rss_before,
                    limitation='운영체제·장치 캐시 영향을 받습니다. 읽기 시간에는 체크섬 계산이 포함되며 물리 디스크 최대 속도가 아닙니다.',
                )
                return _outcome(data=data)
            finally:
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    cleanup.append(str(exc)[:1000])

        outcome = await asyncio.to_thread(measure)
        if outcome.get('data') is not None:
            outcome['data'].update(
                resource_before=_small(before, 8192),
                resource_after=_small(await _resource_sample(cancel), 8192),
                rss_after_bytes=psutil.Process().memory_info().rss,
            )
        return outcome

    async def _ollama(self, mode, cancel, result):
        model = self.settings.model
        if not model:
            return _outcome(
                'blocked',
                error=_error('model_not_selected', '설정에서 설치된 로컬 모델을 선택하세요.'),
            )
        client = OllamaClient(self.settings.ollama_url, timeout=min(self.settings.llm_timeout, 45))
        data = dict(
            model=model,
            samples=[],
            options=dict(num_ctx=2048, num_predict=64 if mode == 'quick' else 128, temperature=0),
            prompt='Reply with exactly the numbers 1 to 8, separated by spaces. No other text.',
            limitation='짧은 고정 프롬프트의 관찰입니다. 모델별 토큰화·현재 부하·파일 캐시에 영향을 받습니다.',
        )
        try:
            probe = await client.probe(cancel)
            data['server'] = {
                key: probe.get(key) for key in ('connected', 'version', 'url', 'code')
            }
            if not probe.get('connected'):
                return _outcome(
                    'blocked',
                    data,
                    _error('ollama_unavailable', 'Ollama 연결을 확인한 뒤 다시 측정하세요.'),
                )
            models = await client.models(cancel)
            selected = next((entry for entry in models if entry.get('name') == model), None)
            if selected is None:
                return _outcome(
                    'blocked',
                    data,
                    _error(
                        'model_missing',
                        '선택한 모델이 설치되어 있지 않습니다. 모델 목록을 확인하세요.',
                    ),
                )
            data['selected'] = _small(selected, 4096)
            metadata = await client.local_metadata(model, cancel)
            data['metadata'] = _small(
                {key: metadata.get(key) for key in ('details', 'capabilities', 'model_info')}, 8192
            )
            for index in range(1 if mode == 'quick' else 2):
                if cancel.is_set():
                    raise _Cancelled()
                try:
                    running = await client._json('GET', '/api/ps', cancel=cancel)
                    loaded = any(
                        entry.get('name', entry.get('model')) == model
                        for entry in running.get('models', [])
                    )
                except Exception:
                    loaded = None
                before = await _resource_sample(cancel)
                start = time.perf_counter()
                response = await client.chat(
                    model,
                    [dict(role='user', content=data['prompt'])],
                    cancel=cancel,
                    options=data['options'],
                )
                elapsed = time.perf_counter() - start
                sample = ollama_metrics(response, elapsed)
                execution = (
                    response.get('_execution', getattr(client, 'last_execution', None)) or {}
                )
                vram = execution.get('size_vram')
                sample.update(
                    index=index + 1,
                    model_loaded_before_request=loaded,
                    phase='repeat_request' if index else 'first_request',
                    resource_before=_small(before, 8192),
                    resource_after=_small(await _resource_sample(cancel), 8192),
                    execution=_small(execution, 4096),
                    ollama_size_vram_bytes=vram,
                    gpu_execution_status=(
                        'vram_allocated'
                        if isinstance(vram, int) and not isinstance(vram, bool) and vram > 0
                        else 'no_vram_reported' if vram == 0 else 'unknown'
                    ),
                    gpu_execution_note='Ollama의 GPU 메모리 할당 관찰입니다. 모든 연산이 GPU에서 실행되었다는 증거는 아닙니다.',
                    response=dict(
                        content=response.get('message', {}).get('content', '')[:512],
                        done=response.get('done'),
                        done_reason=response.get('done_reason'),
                    ),
                )
                data['samples'].append(sample)
                atomic_json(
                    Path(result['directory'])
                    / 'attempts'
                    / result['attempt_id']
                    / 'ollama-progress.json',
                    data,
                )
                self._progress(result, 'ollama', data)
            data['variability'] = speed_variability(data['samples'])
            available = all(sample['metrics_available'] for sample in data['samples'])
            return _outcome(
                'success' if available else 'partial',
                data,
                (
                    None
                    if available
                    else _error(
                        'metrics_unavailable',
                        '모델 응답은 받았지만 토큰 처리 시간 정보가 없어 속도를 계산하지 않았습니다.',
                    )
                ),
            )
        except (_Cancelled, asyncio.CancelledError):
            return _outcome(
                'cancelled', data, _error('cancelled', '모델 속도 측정을 취소했습니다.')
            )
        except Exception as exc:
            return _outcome(
                'cancelled' if getattr(exc, 'code', None) == 'cancelled' else 'failed',
                data,
                _error(
                    getattr(exc, 'code', 'ollama_failed'), '모델 속도를 측정하지 못했습니다.', exc
                ),
            )
        finally:
            await client.close()

    async def _ghidra(self, mode, cancel, result):
        sample_dir = resources_root() / 'benchmarks'
        sample = sample_dir / 'sample.elf'
        manifest = sample_dir / 'sample.json'
        if not sample.is_file() or not manifest.is_file():
            return _outcome(
                'blocked',
                dict(sample=None),
                _error(
                    'sample_unavailable',
                    '기본 분석용 예제 파일이 없어 분석 시간을 측정하지 않았습니다.',
                ),
            )
        declared = json.loads(manifest.read_text(encoding='utf-8'))
        actual = hashlib.sha256(sample.read_bytes()).hexdigest()
        if sample.stat().st_size > 4 * _MIB or declared.get('sha256') != actual:
            return _outcome(
                'blocked',
                error=_error(
                    'sample_invalid', '기본 분석용 예제의 해시 또는 크기를 확인하지 못했습니다.'
                ),
            )
        artifact = await GhidraAdapter(self.settings.ghidra_path, self.settings.java_home).analyze(
            sample,
            Path(result['directory']) / 'attempts' / result['attempt_id'] / 'ghidra',
            timeout=min(60, self.settings.static_timeout),
            cancel=cancel,
        )
        return _outcome(
            artifact.get('status', 'failed'),
            dict(
                sample_sha256=actual,
                function_count=len(artifact.get('functions', [])),
                initialization_seconds=None,
                analysis_seconds=None,
                function_identification_seconds=None,
                decompilation_seconds=None,
                peak_memory_bytes=None,
                timing_scope='Headless 전체 실행 시간을 측정합니다. 초기화·분석·디컴파일 세부 시간과 최대 메모리는 별도 계측하지 않았습니다.',
                environment=_small(artifact.get('environment'), 8192),
                logs=artifact.get('logs'),
                complete=artifact.get('complete'),
                process_seconds=artifact.get('process', {}).get('duration'),
                sample_manifest=_small(declared, 2048),
            ),
            (
                None
                if artifact.get('status') == 'success'
                else _error(
                    'ghidra_failed',
                    '예제 파일의 분석이 완료되지 않았습니다.',
                    artifact.get('error'),
                )
            ),
        )

    async def _docker(self, mode, cancel, result):
        docker = DockerAdapter(context=self.settings.docker_context)
        probe = await docker.probe()
        data = dict(
            environment=_small(
                {
                    key: probe.get(key)
                    for key in (
                        'ready',
                        'status',
                        'context',
                        'local_endpoint',
                        'os',
                        'arch',
                        'server_version',
                        'reason',
                    )
                },
                4096,
            )
        )
        if not probe.get('ready'):
            return _outcome(
                'blocked',
                data,
                _error('docker_unavailable', 'Docker 연결을 확인한 뒤 다시 측정하세요.'),
            )
        if mode == 'quick':
            return _outcome(data=dict(**data, kind='connection_probe_only', execution_tested=False))
        if probe.get('os') != 'linux' or probe.get('arch') not in ('amd64', 'x86_64'):
            return _outcome(
                'blocked',
                data,
                _error(
                    'docker_unsupported', '자세한 격리 실행 측정은 Linux x86-64 엔진이 필요합니다.'
                ),
            )
        image = await docker.call(
            'image',
            'inspect',
            self.settings.linux_image,
            timeout=10,
            cancel=cancel,
            output_limit=32768,
        )
        if not _ok(image):
            return _outcome(
                'blocked',
                data,
                _error(
                    'image_missing',
                    '기존 분석 이미지가 필요합니다. 이 측정에서는 이미지를 내려받거나 만들지 않습니다.',
                ),
            )
        details = json.loads(image['stdout'])[0]
        if details.get('Os') != 'linux' or details.get('Architecture') != 'amd64':
            return _outcome(
                'blocked',
                data,
                _error(
                    'image_incompatible', '기존 이미지의 운영체제 또는 아키텍처가 맞지 않습니다.'
                ),
            )
        name = 'recheck-bench-' + uuid.uuid4().hex
        memory = int(self.settings.memory_mb) * _MIB
        cpus = float(self.settings.cpu)
        data.update(
            image_id=details['Id'],
            image_name=self.settings.linux_image,
            container_name=name,
            expected_limits=dict(memory_bytes=memory, cpus=cpus, pids=64),
            cleanup_errors=[],
        )
        outcome = _outcome('failed', data)
        try:
            created = await docker.call(
                'create',
                '--name',
                name,
                '--label',
                'org.recheck.managed=true',
                '--label',
                'org.recheck.benchmark=' + result['benchmark_id'],
                '--network',
                'none',
                '--read-only',
                '--user',
                '65534:65534',
                '--cap-drop',
                'ALL',
                '--security-opt',
                'no-new-privileges',
                '--pids-limit',
                '64',
                '--memory',
                str(self.settings.memory_mb) + 'm',
                '--memory-swap',
                str(self.settings.memory_mb) + 'm',
                '--cpus',
                str(cpus),
                '--tmpfs',
                '/tmp:rw,nosuid,nodev,noexec,size=1m,mode=1777',
                '--workdir',
                '/tmp',
                '--entrypoint',
                '/bin/sh',
                details['Id'],
                '-c',
                _SCRIPT,
                timeout=15,
                cancel=cancel,
                output_limit=4096,
            )
            data['create'] = _small(created, 8192)
            self._progress(result, 'docker', data)
            if not _ok(created):
                outcome['error'] = _error(
                    'container_create_failed',
                    '측정용 격리 공간을 만들지 못했습니다.',
                    created.get('stderr'),
                )
                return outcome
            execution = await docker.call(
                'start', '--attach', name, timeout=10, cancel=cancel, output_limit=4096
            )
            data['execution'] = _small(execution, 8192)
            self._progress(result, 'docker', data)
            inspected = await docker.call('inspect', name, timeout=10, output_limit=32768)
            if not _ok(inspected):
                outcome['error'] = _error(
                    'container_inspect_failed', '격리 공간의 상태와 제한을 확인하지 못했습니다.'
                )
                return outcome
            info = json.loads(inspected['stdout'])[0]
            host = info.get('HostConfig', {})
            state = info.get('State', {})
            actual = {
                key: host.get(key)
                for key in (
                    'Memory',
                    'NanoCpus',
                    'PidsLimit',
                    'NetworkMode',
                    'ReadonlyRootfs',
                    'CapDrop',
                    'SecurityOpt',
                )
            }
            checks = dict(
                memory=actual['Memory'] == memory,
                cpu=actual['NanoCpus'] == round(cpus * 1e9),
                pids=actual['PidsLimit'] == 64,
                network=actual['NetworkMode'] == 'none',
                readonly=actual['ReadonlyRootfs'] is True,
                capabilities='ALL' in (actual['CapDrop'] or []),
                no_new_privileges=any(
                    str(value).startswith('no-new-privileges')
                    for value in (actual['SecurityOpt'] or [])
                ),
                nonroot=info.get('Config', {}).get('User') == '65534:65534',
            )
            inside = {}
            for line in execution.get('stdout', '').splitlines():
                if line.startswith('/sys/fs/cgroup/') and '=' in line:
                    key, value = line.split('=', 1)
                    inside[key] = value[:200]
            inside_memory = None
            inside_cpu = None
            inside_pids = None
            try:
                raw_memory = inside.get(
                    '/sys/fs/cgroup/memory.max',
                    inside.get('/sys/fs/cgroup/memory/memory.limit_in_bytes'),
                )
                inside_memory = int(raw_memory) if raw_memory and raw_memory != 'max' else None
            except ValueError:
                pass
            try:
                raw_cpu = inside.get('/sys/fs/cgroup/cpu.max')
                if raw_cpu:
                    quota, period = raw_cpu.split()
                    inside_cpu = (
                        int(quota) / int(period) if quota != 'max' and int(period) > 0 else None
                    )
                elif inside.get('/sys/fs/cgroup/cpu/cpu.cfs_quota_us'):
                    quota = int(inside['/sys/fs/cgroup/cpu/cpu.cfs_quota_us'])
                    period = int(inside['/sys/fs/cgroup/cpu/cpu.cfs_period_us'])
                    inside_cpu = quota / period if quota > 0 and period > 0 else None
            except (ValueError, KeyError):
                pass
            try:
                raw_pids = inside.get('/sys/fs/cgroup/pids.max')
                inside_pids = int(raw_pids) if raw_pids and raw_pids != 'max' else None
            except ValueError:
                pass
            inside_checks = dict(
                memory=inside_memory == memory if inside_memory is not None else None,
                cpu=abs(inside_cpu - cpus) < 0.000001 if inside_cpu is not None else None,
                pids=inside_pids == 64 if inside_pids is not None else None,
            )
            data.update(
                observed_limits=actual,
                limit_checks=checks,
                container_state=state,
                inside_cgroup=inside or None,
                inside_limits=dict(memory_bytes=inside_memory, cpus=inside_cpu, pids=inside_pids),
                inside_limit_checks=inside_checks,
                startup_seconds=execution.get('duration'),
                execution_tested=True,
                limitation='고정된 짧은 명령의 컨테이너 시작 관찰입니다. 컨테이너 내부 값이 없는 항목은 미측정입니다.',
            )
            successful = (
                _ok(execution)
                and execution.get('stdout', '').startswith('RECHECK_BENCHMARK_OK\n')
                and state.get('Status') == 'exited'
                and state.get('ExitCode') == 0
                and not state.get('OOMKilled')
                and not state.get('Error')
            )
            outcome['status'] = (
                'success'
                if successful
                and all(checks.values())
                and not any(value is False for value in inside_checks.values())
                else 'failed'
            )
            if outcome['status'] != 'success':
                outcome['error'] = _error(
                    'container_verification_failed',
                    '실행 결과 또는 실제 자원 제한이 기대값과 다릅니다.',
                )
            return outcome
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            outcome['error'] = _error(
                'container_measurement_failed', '격리 실행 측정을 완료하지 못했습니다.', exc
            )
            return outcome
        finally:
            try:
                removed = await docker.call('rm', '--force', name, timeout=10, output_limit=4096)
                data['cleanup'] = _small(removed, 4096)
                if not _ok(removed) and 'No such container' not in removed.get('stderr', ''):
                    data['cleanup_errors'].append(
                        removed.get('stderr') or removed.get('error') or '정리 실패'
                    )
            except Exception as exc:
                data['cleanup_errors'].append(str(exc)[:1000])
            if data['cleanup_errors'] and outcome['status'] == 'success':
                outcome.update(
                    status='partial',
                    error=_error('cleanup_failed', '측정용 격리 공간을 정리하지 못했습니다.'),
                )
