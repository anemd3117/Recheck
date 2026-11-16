"""Conservative recommendations and reversible, selected-key settings updates.

Recommendations are estimates unless a setting was actually observed unchanged.
This module neither runs benchmarks nor changes settings while calculating them.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import uuid
from urllib.parse import urlsplit

import psutil

from .models import Settings, data_root


def _capabilities_after_local_change(current, values):
    """Keep protected proof references whose AI configuration did not change.

    These settings operations only alter local tools and resource limits. Custom
    provider profiles stay unchanged; their credential/proof bindings are still
    checked by ProfileManager whenever the references are used.
    """
    refs = current.capabilities.get('ai_proofs', {})
    if not isinstance(refs, dict):
        return {}
    known = {'legacy-local'} | {profile.profile_id for profile in current.ai_profiles}
    kept = {
        profile_id: ref
        for profile_id, ref in refs.items()
        if profile_id in known and isinstance(ref, str) and re.fullmatch(r'cred_[a-f0-9]{32}', ref)
    }
    local_ai_fields = ('ollama_url', 'model', 'num_ctx', 'num_predict', 'llm_timeout')
    if any(values.get(key) != getattr(current, key) for key in local_ai_fields):
        kept.pop('legacy-local', None)
    return {'ai_proofs': kept} if kept else {}


ALLOWED_KEYS = (
    'num_ctx',
    'num_predict',
    'llm_timeout',
    'memory_mb',
    'cpu',
    'output_limit',
    'static_timeout',
)
SETUP_KEYS = ('ghidra_path', 'java_home', 'docker_context', 'ollama_url', 'model')
_MIB = 1024**2
_GIB = 1024**3
_PROFILES = {
    'stability': dict(context=8192, predict=2048, cores=2, share=0.25, output=_MIB),
    'balanced': dict(context=16384, predict=4096, cores=4, share=0.5, output=4 * _MIB),
    'performance': dict(context=32768, predict=8192, cores=8, share=0.75, output=8 * _MIB),
}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _dict(value):
    return value if isinstance(value, dict) else {}


def _num(value, *, zero=False):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) and (result >= 0 if zero else result > 0) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _endpoint(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = urlsplit(value.rstrip('/'))
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            return None
        return (
            parsed.scheme.lower(),
            parsed.hostname.lower(),
            parsed.port or (443 if parsed.scheme == 'https' else 80),
            parsed.path,
        )
    except ValueError:
        return None


def _same_hardware(current, previous):
    """Reject known differences; require comparable CPU and RAM observations."""
    previous = _dict(previous)
    for name in ('machine_id', 'hostname', 'device_id'):
        if current.get(name) and previous.get(name) and current[name] != previous[name]:
            return False
    compared = []
    for section, name in (('cpu', 'usable_cores'), ('memory', 'total_bytes')):
        a, b = _num(_dict(current.get(section)).get(name)), _num(
            _dict(previous.get(section)).get(name)
        )
        if a is None or b is None or a != b:
            return False
        compared.append(True)
    a, b = _dict(current.get('cpu')).get('name'), _dict(previous.get('cpu')).get('name')
    return bool(compared) and not (a and b and a != b)


def _matching_metadata(metadata, model):
    """Only explicitly identified metadata may describe the selected model."""
    if not model:
        return None
    if isinstance(metadata, list):
        return next(
            (
                row
                for row in metadata
                if isinstance(row, dict) and row.get('model', row.get('name')) == model
            ),
            None,
        )
    metadata = _dict(metadata)
    if metadata.get('model', metadata.get('name')) == model:
        return metadata
    return _dict(metadata.get(model)) or None


def _model_digest(models, model):
    models = models if isinstance(models, list) else []
    row = next(
        (
            row
            for row in models
            if isinstance(row, dict) and row.get('name', row.get('model')) == model
        ),
        {},
    )
    digest = row.get('digest')
    return digest if isinstance(digest, str) and digest else None


def _native(value, *, zero=False):
    return (
        _num(value, zero=zero) if isinstance(value, int) and not isinstance(value, bool) else None
    )


def _context_limit(metadata):
    info = _dict(_dict(metadata).get('model_info'))
    limits = [
        _num(value)
        for key, value in info.items()
        if isinstance(key, str) and (key == 'context_length' or key.endswith('.context_length'))
    ]
    limits += [_num(_dict(metadata).get('context_length'))]
    values = [int(value) for value in limits if value is not None]
    return min(values) if values else None


def _old(timestamp):
    if not timestamp:
        return False
    try:
        stamp = datetime.fromisoformat(str(timestamp).replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            return True
        age = (datetime.now(timezone.utc) - stamp).total_seconds()
        return age > 300 or age < -60
    except (ValueError, TypeError):
        return True


def recommend_settings(settings, hardware, benchmark=None, profile='balanced') -> dict:
    """Recommend only existing settings; all extrapolations are labelled estimates."""
    settings = Settings.model_validate(
        settings.model_dump() if isinstance(settings, Settings) else settings
    )
    if profile not in _PROFILES:
        raise ValueError('지원하지 않는 추천 프로필입니다. 안정성·균형·성능 중 선택하세요.')
    policy, hardware, benchmark = _PROFILES[profile], _dict(hardware), _dict(benchmark)
    memory, cpu = _dict(hardware.get('memory')), _dict(hardware.get('cpu'))
    env = _dict(hardware.get('environment'))
    docker, ollama = _dict(env.get('docker')), _dict(env.get('ollama'))
    timestamp = hardware.get('collected_at')
    warnings, items = [], []
    stale = _old(timestamp)
    if stale:
        warnings.append(
            '하드웨어 조회가 5분 이상 지났거나 조회 시각을 검증하지 못했습니다. 적용 전에 다시 조회하세요.'
        )
    elif not timestamp:
        warnings.append('하드웨어 조회 시각이 없어 현재 자원 상태를 확인할 수 없습니다.')

    budgets = [
        x
        for x in (
            _num(memory.get('safe_budget_bytes'), zero=True),
            (
                _num(memory.get('available_bytes'), zero=True) * 0.7
                if _num(memory.get('available_bytes'), zero=True) is not None
                else None
            ),
            (
                _num(memory.get('total_bytes')) * 0.6
                if _num(memory.get('total_bytes')) is not None
                else None
            ),
        )
        if x is not None
    ]
    budget = min(budgets) if budgets else None
    docker_memory = _num(docker.get('memory_bytes')) if docker.get('ready') else None
    stages = _dict(benchmark.get('stages'))
    reused = _dict(benchmark.get('reused_stages'))

    def source(stage):
        # A retry may be run after moving a result to another PC. Successful
        # copied stages still belong to their original measurement environment.
        return _dict(reused[stage]) if stage in reused else benchmark

    def stage_host_matches(stage):
        return bool(benchmark) and _same_hardware(hardware, source(stage).get('hardware'))

    same_host = bool(benchmark) and _same_hardware(hardware, benchmark.get('hardware'))
    if benchmark and not same_host:
        warnings.append(
            '이전 벤치마크의 CPU·전체 RAM·기기 정보가 현재 조회와 일치하는지 확인되지 않아 실측 추천에 사용하지 않았습니다.'
        )
    llm_stage = _dict(stages.get('ollama'))
    llm_data = _dict(llm_stage.get('data'))
    bench_settings = _dict(source('ollama').get('settings'))
    old_ollama = _dict(
        _dict(_dict(source('ollama').get('hardware')).get('environment')).get('ollama')
    )
    urls = [
        value
        for value in (
            bench_settings.get('ollama_url'),
            _dict(llm_data.get('server')).get('url'),
            old_ollama.get('url'),
        )
        if value
    ]
    same_server = bool(urls) and all(
        _endpoint(value) is not None and _endpoint(value) == _endpoint(settings.ollama_url)
        for value in urls
    )
    same_server = (
        same_server
        and _endpoint(ollama.get('url')) is not None
        and _endpoint(ollama.get('url')) == _endpoint(settings.ollama_url)
    )
    same_model = (
        bool(settings.model)
        and llm_data.get('model') == settings.model
        and (not bench_settings.get('model') or bench_settings['model'] == settings.model)
    )
    current_digest = _model_digest(ollama.get('models'), settings.model)
    selected_digest = _dict(llm_data.get('selected')).get('digest')
    previous_digest = _model_digest(old_ollama.get('models'), settings.model)
    same_digest = bool(
        current_digest
        and selected_digest
        and current_digest == selected_digest
        and (not previous_digest or previous_digest == current_digest)
    )
    llm_valid = (
        stage_host_matches('ollama')
        and same_server
        and same_model
        and same_digest
        and llm_stage.get('status') == 'success'
    )
    if llm_stage and not llm_valid:
        warnings.append(
            'AI 벤치마크의 선택 모델·digest·Ollama 주소·기기 또는 완료 상태가 현재 설정과 일치하지 않아 속도 측정값을 제외했습니다.'
        )
    # Inventory may precede model loading. A successful matching inference's
    # post-request observation is a second budget constraint, not an additional
    # allocation to subtract again from an already post-load host observation.
    inventory_budget = budget
    post_memory = []
    post_memory_warnings = []
    if llm_valid:
        for index, sample in enumerate(
            llm_data.get('samples', []) if isinstance(llm_data.get('samples'), list) else []
        ):
            resource = _dict(_dict(sample).get('resource_after'))
            ram = _dict(resource.get('ram'))
            available = _num(
                resource.get('memory_available_bytes', ram.get('available_bytes')), zero=True
            )
            total = _num(resource.get('memory_total_bytes', ram.get('total_bytes')))
            current_total = _num(memory.get('total_bytes'))
            if available is None or total is None or total != current_total:
                continue
            measured_at = resource.get('collected_at', resource.get('created_at'))
            post_memory.append(
                dict(
                    sample_index=index + 1,
                    available_bytes=available,
                    total_bytes=total,
                    safe_budget_bytes=min(available * 0.7, total * 0.6),
                    measured_at=measured_at,
                    stale=_old(measured_at) or not measured_at,
                )
            )
    post_budget = min((row['safe_budget_bytes'] for row in post_memory), default=None)
    if post_budget is not None:
        budget = min(budget, post_budget) if budget is not None else post_budget
        post_memory_warnings.append(
            '동일 모델 실행 후 관찰한 가용 RAM과 현재 조회 중 더 작은 안전 예산을 사용했습니다. 다른 프로세스의 메모리 변화도 포함될 수 있습니다.'
        )
        if any(row['stale'] for row in post_memory):
            post_memory_warnings.append(
                '모델 실행 후 RAM 관찰이 5분 이상 지났거나 시각이 미확인입니다. 현재 모델·시스템 부하를 반영하려면 다시 측정하세요.'
            )
    warnings.extend(post_memory_warnings)
    budget_timestamp = timestamp
    if post_budget is not None and (inventory_budget is None or post_budget < inventory_budget):
        budget_timestamp = next(
            row['measured_at'] for row in post_memory if row['safe_budget_bytes'] == post_budget
        )
    container_budget = (
        min(budget, docker_memory * 0.7)
        if budget is not None and docker_memory is not None
        else (
            budget
            if budget is not None
            else docker_memory * 0.7 if docker_memory is not None else None
        )
    )
    memory_evidence = dict(
        inventory_safe_budget_bytes=inventory_budget,
        post_inference_safe_budget_bytes=post_budget,
        post_inference_samples=post_memory,
        benchmark_id=source('ollama').get('benchmark_id') if post_memory else None,
    )
    metadata = (
        _matching_metadata(ollama.get('model_metadata'), settings.model)
        if _endpoint(ollama.get('url')) == _endpoint(settings.ollama_url)
        and _endpoint(ollama.get('url')) is not None
        else None
    )
    if metadata is None and llm_valid:
        metadata = _dict(llm_data.get('metadata')) or None
    model_limit = _context_limit(metadata)
    running = ollama.get('running') if isinstance(ollama.get('running'), list) else []
    loaded_context = next(
        (
            _num(row.get('context_length'))
            for row in running
            if isinstance(row, dict) and row.get('name', row.get('model')) == settings.model
        ),
        None,
    )

    def add(
        key,
        value,
        reason,
        evidence,
        *,
        local_warnings=(),
        basis='estimated',
        confidence='low',
        measured_at=None,
        blocked=False,
    ):
        items.append(
            dict(
                key=key,
                current=getattr(settings, key),
                recommended=value,
                reason=reason,
                evidence=evidence,
                measured_at=measured_at,
                basis=basis,
                confidence=confidence,
                warnings=list(local_warnings),
                apply_blocked=blocked,
            )
        )

    # RAM/VRAM cannot determine an exact KV cache size without model/runtime data.
    context_cap = policy['context']
    if budget is not None:
        context_cap = min(
            context_cap,
            (
                2048
                if budget < 2 * _GIB
                else (
                    4096
                    if budget < 4 * _GIB
                    else 8192 if budget < 8 * _GIB else 16384 if budget < 16 * _GIB else 32768
                )
            ),
        )
    else:
        context_cap = min(context_cap, 8192)
    if model_limit is not None:
        context_cap = min(context_cap, model_limit)
    ctx_blocked = context_cap < 1024
    context = settings.num_ctx if ctx_blocked else min(settings.num_ctx, max(1024, context_cap))
    token_warning = '토큰은 문자 수나 byte 수와 다릅니다. 실제 사용 메모리는 모델·양자화·KV 캐시·실행 방식에 따라 달라집니다.'
    ctx_warnings = [
        token_warning,
        '/api/ps의 context_length는 현재 로드 설정이며 모델 최대 지원 길이를 뜻하지 않습니다.',
        *post_memory_warnings,
    ]
    if metadata is None:
        ctx_warnings.append(
            '선택 모델의 최대 컨텍스트를 확인한 메타데이터가 없어 보수적인 추정 상한만 사용했습니다.'
        )
    if ctx_blocked:
        ctx_warnings.append(
            '적용 차단: 모델 메타데이터의 상한이 지원하는 최소 설정 1024토큰보다 작습니다.'
        )
    add(
        'num_ctx',
        context,
        '현재 값이 RAM 기반 보수적 예산·프로필·확인된 모델 상한 안에 있으면 유지합니다.',
        dict(
            safe_budget_bytes=budget,
            model_context_limit=model_limit,
            loaded_context_length=loaded_context,
            profile_cap=policy['context'],
            **memory_evidence,
        ),
        local_warnings=ctx_warnings,
        confidence='medium' if model_limit is not None and budget is not None else 'low',
        blocked=ctx_blocked,
        measured_at=budget_timestamp,
    )
    prediction = min(settings.num_predict, policy['predict'], max(64, context // 2))
    add(
        'num_predict',
        prediction,
        '응답 길이를 컨텍스트의 절반 이하와 프로필 상한으로 제한하며 적정한 현재 값은 유지합니다.',
        dict(recommended_context=context, profile_cap=policy['predict']),
        local_warnings=[
            token_warning,
            '출력 상한이 짧으면 구조화된 응답이 끝나기 전에 중단될 수 있습니다.',
        ],
        measured_at=timestamp,
    )

    rates, prompt_seconds, prompt_counts, loads = [], [], [], []
    if llm_valid:
        for sample in (
            llm_data.get('samples', []) if isinstance(llm_data.get('samples'), list) else []
        ):
            sample = _dict(sample)
            count, duration = _native(sample.get('eval_count')), _native(
                sample.get('eval_duration_ns')
            )
            if count is not None and duration is not None:
                rate = _num(count / duration * 1e9)
                if rate is not None:
                    rates.append(rate)
            count, duration = _native(sample.get('prompt_eval_count')), _native(
                sample.get('prompt_eval_duration_ns')
            )
            if count is not None and duration is not None:
                prompt_seconds.append(duration / 1e9)
                prompt_counts.append(count)
            load = _native(sample.get('load_duration_ns'), zero=True)
            if load is not None:
                loads.append(load / 1e9)
    projected = None
    if rates:
        # A tiny fixed prompt cannot measure prefill for an entire context.
        # Include only its observed duration; extrapolate generation separately.
        projected = prediction / min(rates) + max(loads, default=0) + max(prompt_seconds, default=0)
        projected = math.ceil(projected * 1.5 + 15)
    timeout = (
        max(settings.llm_timeout, min(900, projected))
        if projected is not None
        else settings.llm_timeout
    )
    timeout_warnings = [
        '짧은 고정 프롬프트의 처리 시간은 전체 컨텍스트로 확대하지 않습니다. 큰 실제 입력의 처리 시간과 제한 시간은 별도 검증해야 합니다.',
        '생성 상한을 짧은 응답의 속도로 외삽한 제한 시간은 추정치이며 장문 생성 속도를 보장하지 않습니다.',
    ]
    if projected is not None and projected > 900:
        timeout_warnings.append(
            '예상 시간이 최대 900초를 초과합니다. 출력 토큰 또는 입력 근거를 줄이고 다시 확인하세요.'
        )
    add(
        'llm_timeout',
        timeout,
        '같은 기기·모델·서버에서 성공한 최대 관찰 프롬프트 시간·모델 준비 시간에 생성 상한/최저 생성속도를 더하고 1.5배+15초의 여유를 적용합니다. 더 긴 현재 제한 시간은 유지합니다.',
        dict(
            benchmark_id=source('ollama').get('benchmark_id') if rates else None,
            model_digest=current_digest,
            tokens_per_second=min(rates) if rates else None,
            observed_prompt_seconds=max(prompt_seconds, default=None),
            observed_prompt_token_counts=prompt_counts,
            observed_max_load_seconds=max(loads, default=None),
            projected_seconds=projected,
            prompt_extrapolated_to_context=False,
        ),
        local_warnings=timeout_warnings,
        confidence='medium' if rates else 'low',
        measured_at=source('ollama').get('created_at') if rates else None,
    )

    mem_blocked = container_budget is not None and container_budget < 64 * _MIB
    memory_mb = (
        settings.memory_mb
        if container_budget is None or mem_blocked
        else min(settings.memory_mb, 32768, int(container_budget // _MIB))
    )
    mem_warnings = [
        '안전 예산은 관찰 시점의 권장치이며 실제 메모리를 예약하지 않습니다. 기존 바이너리의 최소 메모리 요구량은 별도 검증해야 합니다.',
        *post_memory_warnings,
    ]
    if container_budget is None:
        mem_warnings.append('가용 RAM과 Docker 메모리 한도를 확인하지 못해 현재 값을 유지했습니다.')
    if mem_blocked:
        mem_warnings.append(
            '적용 차단: 현재 안전 예산이 최소 컨테이너 메모리 64MiB보다 작습니다. 다른 작업 종료 후 다시 조회하세요.'
        )
    if stale and memory_mb != settings.memory_mb:
        mem_blocked = True
        mem_warnings.append('적용 차단: 오래된 가용 메모리 정보입니다. 하드웨어를 다시 조회하세요.')
    add(
        'memory_mb',
        memory_mb,
        '호스트 안전 예산과 연결된 Docker 엔진 메모리의 70% 중 작은 값 안에서 현재 제한을 유지합니다.',
        dict(
            host_safe_budget_bytes=budget,
            docker_memory_bytes=docker_memory,
            effective_budget_bytes=container_budget,
            **memory_evidence,
        ),
        local_warnings=mem_warnings,
        confidence='medium' if container_budget is not None and not stale else 'low',
        measured_at=budget_timestamp,
        blocked=mem_blocked,
    )

    cores = _num(cpu.get('usable_cores'))
    docker_cores = _num(docker.get('cpus')) if docker.get('ready') else None
    cap = min(16, policy['cores'], max(0.1, cores * policy['share'])) if cores is not None else 1.0
    if docker_cores is not None:
        cap = min(cap, docker_cores)
    usage = _num(cpu.get('usage_percent'), zero=True)
    if usage is not None and usage >= 80:
        cap = min(cap, max(0.1, (cores or 1) * 0.25))
    cpu_value = max(0.1, round(min(settings.cpu, cap), 2))
    cpu_stage = _dict(stages.get('cpu'))
    cpu_data = (
        _dict(cpu_stage.get('data'))
        if stage_host_matches('cpu') and cpu_stage.get('status') == 'success'
        else {}
    )
    # Current producer names take precedence, including an explicit unknown.
    # Accept shorter legacy aliases, also retained in evidence below.
    single = _num(
        cpu_data.get('single_operations_per_second', cpu_data.get('single_ops_per_second'))
    )
    multi = _num(
        cpu_data.get('parallel_operations_per_second', cpu_data.get('multi_ops_per_second'))
    )
    add(
        'cpu',
        cpu_value,
        '사용 가능한 코어·Docker 엔진 상한·현재 부하를 고려해 프로필의 일부만 사용하며 적정한 현재 값을 유지합니다.',
        dict(
            usable_cores=cores,
            docker_cpus=docker_cores,
            usage_percent=usage,
            profile_share=policy['share'],
            single_ops_per_second=single,
            multi_ops_per_second=multi,
            observed_throughput_ratio=_num(multi / single) if single and multi else None,
            single_operations_per_second=single,
            parallel_operations_per_second=multi,
            benchmark_id=source('cpu').get('benchmark_id') if single or multi else None,
            benchmark_measured_at=source('cpu').get('created_at') if single or multi else None,
        ),
        local_warnings=[
            'CPU 개수와 짧은 벤치마크는 사용자 바이너리의 병렬 처리 성능을 보장하지 않습니다. 단일/동시 처리량 비율은 참고 근거이며 CPU 할당 증가의 근거로 사용하지 않습니다.'
        ],
        measured_at=timestamp,
    )
    output_cap = policy['output']
    if container_budget is not None and container_budget < 256 * _MIB:
        output_cap = min(output_cap, 256 * 1024)
    add(
        'output_limit',
        min(settings.output_limit, output_cap),
        '출력 메모리 사용을 제한하며 프로필의 상한 안에 있는 현재 값은 유지합니다.',
        dict(
            profile_cap_bytes=policy['output'],
            effective_cap_bytes=output_cap,
            effective_container_budget_bytes=container_budget,
            **memory_evidence,
        ),
        local_warnings=[
            '제한을 넘은 출력은 잘림으로 기록됩니다. 잘린 실행을 완전한 행동 일치로 판정할 수 없습니다. byte 제한은 토큰 수와 다릅니다.'
        ],
        measured_at=budget_timestamp,
    )
    static_stage = _dict(stages.get('ghidra'))
    static_data = _dict(static_stage.get('data'))
    static_seconds = (
        _num(static_data.get('duration_seconds'))
        or _num(static_data.get('process_seconds'))
        or _num(static_stage.get('duration_seconds'))
    )
    static_settings = _dict(source('ghidra').get('settings'))
    ghidra_valid = (
        stage_host_matches('ghidra')
        and static_stage.get('status') == 'success'
        and bool(static_settings)
        and all(
            static_settings.get(key) == getattr(settings, key)
            for key in ('ghidra_path', 'java_home')
        )
    )
    static_estimate = (
        math.ceil(static_seconds * 2 + 30) if ghidra_valid and static_seconds is not None else None
    )
    add(
        'static_timeout',
        (
            max(settings.static_timeout, min(3600, static_estimate))
            if static_estimate is not None
            else settings.static_timeout
        ),
        '같은 Ghidra·Java 설정에서 성공한 예제 분석 시간에 여유를 더하며 그보다 긴 현재 값은 유지합니다. 사용자 바이너리 소요 시간은 추정할 수 없습니다.',
        dict(
            benchmark_id=(
                source('ghidra').get('benchmark_id') if static_estimate is not None else None
            ),
            sample_seconds=static_seconds if static_estimate is not None else None,
            estimated_sample_budget_seconds=static_estimate,
        ),
        measured_at=source('ghidra').get('created_at') if static_estimate is not None else None,
        local_warnings=[
            '작은 예제의 측정값은 크거나 난독화된 바이너리의 분석 시간을 보장하지 않습니다.'
        ],
    )
    blocked = [item['key'] for item in items if item['apply_blocked']]
    warnings += [
        message
        for item in items
        if item['apply_blocked']
        for message in item['warnings']
        if message.startswith('적용 차단:')
    ]
    return dict(
        schema_version='1.0',
        created_at=_now(),
        profile=profile,
        items=items,
        warnings=list(dict.fromkeys(warnings)),
        blocked_keys=blocked,
    )


def _digest(raw):
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


def _raw(path):
    return path.read_bytes() if path.exists() else None


def _encode(settings):
    return json.dumps(settings.model_dump(), ensure_ascii=False, indent=2).encode('utf-8')


def _atomic_bytes(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class SettingsHistory:
    """Backup settings before updates, with stale-input and concurrent-writer checks.

    A GUI containing unsaved edits must use its normal save action first. This
    avoids guessing whether a disk/GUI difference came from another process.
    Both the parsed settings and exact pre-update disk bytes are retained.
    """

    def __init__(self, root=None):
        self.root = Path(root) if root is not None else data_root()
        self.path = self.root / 'settings.json'
        self.directory = self.root / 'settings-history'
        self._fingerprint = _digest(_raw(self.path))

    @contextmanager
    def _locked(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        lock = self.directory / '.lock'
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                owner = json.loads(lock.read_text(encoding='utf-8'))
                alive = psutil.pid_exists(int(owner['pid']))
                if alive:
                    raise ValueError(
                        '다른 설정 저장 또는 복원이 진행 중입니다. 완료 후 다시 시도하세요.'
                    )
                lock.unlink()
                descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except (OSError, KeyError, TypeError, ValueError):
                raise ValueError(
                    '설정 이력 잠금을 확인하지 못했습니다. 다른 Recheck 작업이 끝났는지 확인하세요.'
                ) from None
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                json.dump(dict(pid=os.getpid(), created_at=_now()), stream)
            yield
        finally:
            lock.unlink(missing_ok=True)

    def _write_record(self, record):
        _atomic_bytes(
            self.directory / (record['backup_id'] + '.json'),
            json.dumps(record, ensure_ascii=False, indent=2).encode('utf-8'),
        )

    def _write_settings(self, raw):
        _atomic_bytes(self.path, raw)

    def replace(self, current, updated, *, action='ai_profile_update') -> Settings:
        """신뢰된 설정 화면의 전체 교체. 정확한 구본·CAS·복구 이력을 보존한다."""
        current = Settings.model_validate(
            current.model_dump() if isinstance(current, Settings) else current
        )
        updated = Settings.model_validate(
            updated.model_dump() if isinstance(updated, Settings) else updated
        )
        keys = [
            key
            for key, value in updated.model_dump().items()
            if current.model_dump().get(key) != value
        ]
        with self._locked():
            raw = self._check_current(current)
            return self._commit(current, updated, raw, action=action, selected_keys=keys)

    def _check_current(self, current):
        raw = _raw(self.path)
        if _digest(raw) != self._fingerprint:
            raise ValueError(
                '설정 파일이 다른 작업에서 변경되었습니다. 일반 설정을 다시 불러오고 저장한 뒤 추천을 갱신하세요.'
            )
        try:
            disk = Settings.model_validate_json(raw) if raw is not None else Settings()
            if raw is None and disk != current:
                from .models import initial_settings

                disk = initial_settings()
        except ValueError:
            raise ValueError(
                '저장된 설정 파일을 읽을 수 없습니다. 일반 설정을 확인하고 저장한 뒤 다시 시도하세요.'
            ) from None
        if disk != current:
            raise ValueError(
                '화면의 설정과 저장된 설정이 다릅니다. 미저장 변경을 보호하기 위해 먼저 일반 설정을 저장하고 추천을 갱신하세요.'
            )
        return raw

    def _records(self):
        records = []
        for path in self.directory.glob('backup_*.json'):
            try:
                record = json.loads(path.read_text(encoding='utf-8'))
                if isinstance(record, dict) and record.get('backup_id') == path.stem:
                    records.append(record)
            except (OSError, ValueError):
                continue
        return sorted(
            records,
            key=lambda row: (str(row.get('created_at', '')), row['backup_id']),
            reverse=True,
        )

    def _history_unlocked(self):
        records = self._records()
        for record in records:
            if record.get('status') == 'pending':
                digest = _digest(_raw(self.path))
                record['status'] = (
                    'applied'
                    if digest == record.get('after_sha256')
                    else (
                        'failed'
                        if digest == record.get('before_sha256')
                        else 'interrupted_conflict'
                    )
                )
                record['recovered_from_pending'] = True
                record['recovered_at'] = _now()
                # Persist once: a later successful restore must not retrospectively
                # turn a recovered successful apply into a failed operation.
                self._write_record(record)
        return records

    def list_history(self) -> list[dict]:
        if not self.directory.exists():
            return []
        with self._locked():
            return self._history_unlocked()

    def _commit(self, current, updated, raw, *, action, selected_keys, restored_backup_id=None):
        after = _encode(updated)
        record = dict(
            schema_version='1.0',
            backup_id='backup_' + uuid.uuid4().hex,
            created_at=_now(),
            action=action,
            status='pending',
            selected_keys=list(selected_keys),
            restored_backup_id=restored_backup_id,
            before_settings=current.model_dump(),
            after_settings=updated.model_dump(),
            before_exists=raw is not None,
            before_bytes_base64=base64.b64encode(raw).decode('ascii') if raw is not None else None,
            before_sha256=_digest(raw),
            after_sha256=_digest(after),
        )
        self._write_record(record)
        try:
            # Recheck after the durable backup, immediately before replacement.
            if _digest(_raw(self.path)) != _digest(raw):
                raise ValueError(
                    '백업 중 설정 파일이 변경되어 적용을 중단했습니다. 일반 설정을 다시 불러오세요.'
                )
            self._write_settings(after)
            if _raw(self.path) != after:
                raise OSError('저장 후 설정 파일 검증에 실패했습니다.')
            record.update(status='applied', completed_at=_now())
            self._write_record(record)
        except Exception as exc:
            rollback_error = None
            try:
                current_bytes = _raw(self.path)
                if current_bytes == after:
                    if raw is None:
                        self.path.unlink(missing_ok=True)
                    else:
                        _atomic_bytes(self.path, raw)
                elif current_bytes != raw:
                    rollback_error = '다른 작업이 쓴 파일을 덮어쓰지 않았습니다.'
            except OSError as rollback:
                rollback_error = str(rollback)
            record.update(
                status='failed', completed_at=_now(), error=str(exc), rollback_error=rollback_error
            )
            try:
                self._write_record(record)
            except OSError:
                pass
            self._fingerprint = _digest(_raw(self.path))
            detail = (
                ' 복구 확인 필요: ' + rollback_error
                if rollback_error
                else ' 기존 디스크 설정은 보존했습니다.'
            )
            raise ValueError('설정 저장에 실패했습니다: ' + str(exc) + detail) from exc
        self._fingerprint = _digest(after)
        return updated

    def apply(self, current, recommendations, selected_keys) -> Settings:
        current = Settings.model_validate(
            current.model_dump() if isinstance(current, Settings) else current
        )
        keys = list(selected_keys)
        if not keys or len(set(keys)) != len(keys) or any(key not in ALLOWED_KEYS for key in keys):
            raise ValueError('적용할 항목은 지원하는 일곱 설정에서 중복 없이 선택하세요.')
        recommendations = _dict(recommendations)
        rows = recommendations.get('items')
        if not isinstance(rows, list):
            raise ValueError('추천 결과 형식이 올바르지 않습니다. 추천을 다시 계산하세요.')
        by_key = {}
        for row in rows:
            if (
                not isinstance(row, dict)
                or row.get('key') not in ALLOWED_KEYS
                or row['key'] in by_key
            ):
                raise ValueError('추천 항목이 중복되거나 지원하지 않는 설정입니다.')
            by_key[row['key']] = row
        values = current.model_dump()
        for key in keys:
            row = by_key.get(key)
            if row is None or 'current' not in row or 'recommended' not in row:
                raise ValueError('선택한 항목의 추천값이 없습니다. 추천을 다시 계산하세요.')
            if row.get('apply_blocked') or key in recommendations.get('blocked_keys', []):
                raise ValueError(
                    '안전 확인이 끝나지 않아 이 설정은 적용할 수 없습니다: '
                    + key
                    + '. 하드웨어를 다시 조회하세요.'
                )
            if row['current'] != getattr(current, key) or isinstance(row['current'], bool):
                raise ValueError(
                    '추천을 계산한 뒤 설정이 바뀌었습니다. 추천을 갱신한 뒤 다시 적용하세요.'
                )
            values[key] = row['recommended']
        values['capabilities'] = _capabilities_after_local_change(current, values)
        try:
            updated = Settings.model_validate(values, strict=True)
        except ValueError:
            raise ValueError(
                '추천값이 설정의 허용 범위를 벗어나거나 자료형이 올바르지 않습니다.'
            ) from None
        with self._locked():
            raw = self._check_current(current)
            if 'memory_mb' in keys:
                try:
                    memory = psutil.virtual_memory()
                    total, available = _num(memory.total), _num(memory.available, zero=True)
                    if total is None or available is None:
                        raise ValueError('현재 RAM 조회값이 올바르지 않습니다.')
                    budget = min(total * 0.6, available * 0.7)
                    if budget < 64 * _MIB or updated.memory_mb * _MIB > budget:
                        raise ValueError('현재 가용 RAM 안전 예산보다 컨테이너 메모리가 큽니다.')
                except Exception as exc:
                    raise ValueError(
                        '메모리 설정 적용을 차단했습니다. 현재 RAM을 다시 확인하고 추천을 갱신하세요: '
                        + str(exc)
                    ) from exc
            return self._commit(current, updated, raw, action='apply', selected_keys=keys)

    def apply_setup(self, current, patch) -> Settings:
        """Persist an explicitly accepted discovery patch with the same backup guarantees."""
        current = Settings.model_validate(
            current.model_dump() if isinstance(current, Settings) else current
        )
        if not isinstance(patch, dict) or not patch or any(key not in SETUP_KEYS for key in patch):
            raise ValueError('자동 설정에서 지원하는 경로·연결·모델 항목만 적용할 수 있습니다.')
        if any(
            not isinstance(value, str) or not value.strip() or '\x00' in value or len(value) > 32768
            for value in patch.values()
        ):
            raise ValueError('자동 설정값은 비어 있지 않은 올바른 문자열이어야 합니다.')
        values = current.model_dump()
        values.update(patch)
        values['capabilities'] = _capabilities_after_local_change(current, values)
        try:
            updated = Settings.model_validate(values, strict=True)
        except ValueError:
            raise ValueError('자동 설정값의 자료형 또는 설정 형식을 확인하세요.') from None
        with self._locked():
            raw = self._check_current(current)
            return self._commit(current, updated, raw, action='setup', selected_keys=list(patch))

    def restore(self, current, backup_id=None) -> Settings:
        current = Settings.model_validate(
            current.model_dump() if isinstance(current, Settings) else current
        )
        with self._locked():
            raw = self._check_current(current)
            records = self._history_unlocked()
            if backup_id is not None:
                record = next(
                    (
                        row
                        for row in records
                        if row['backup_id'] == backup_id and row.get('status') == 'applied'
                    ),
                    None,
                )
            else:
                restored = {
                    row.get('restored_backup_id')
                    for row in records
                    if row.get('status') == 'applied' and row.get('action') == 'restore'
                }
                record = next(
                    (
                        row
                        for row in records
                        if row.get('action') in ('apply', 'setup')
                        and row.get('status') == 'applied'
                        and row['backup_id'] not in restored
                    ),
                    None,
                )
            if record is None:
                raise ValueError('복원할 성공한 추천 적용 이력이 없습니다.')
            try:
                values = dict(record['before_settings'])
                values['capabilities'] = {}
                updated = Settings.model_validate(values, strict=True)
            except (KeyError, TypeError, ValueError):
                raise ValueError('선택한 백업 설정이 손상되어 복원할 수 없습니다.') from None
            return self._commit(
                current,
                updated,
                raw,
                action='restore',
                selected_keys=record.get('selected_keys', []),
                restored_backup_id=record['backup_id'],
            )
