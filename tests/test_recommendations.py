"""Settings recommendations and history fixtures; never write user settings."""

import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from recheck import recommendations as module
from recheck.models import Settings
from recheck.recommendations import ALLOWED_KEYS, SettingsHistory, recommend_settings

MIB, GIB = 1024**2, 1024**3


def hardware(**changes):
    value = dict(
        collected_at=datetime.now(timezone.utc).isoformat(),
        cpu=dict(name='Fixture CPU', usable_cores=8, usage_percent=10),
        memory=dict(total_bytes=16 * GIB, available_bytes=10 * GIB, safe_budget_bytes=7 * GIB),
        environment=dict(
            docker=dict(ready=True, cpus=4, memory_bytes=8 * GIB),
            ollama=dict(
                url='http://127.0.0.1:11434',
                running=[],
                models=[dict(name='fixture:1', digest='sha256:fixture')],
            ),
        ),
        gpus=[],
    )
    value.update(changes)
    return value


def benchmark(settings, host):
    return dict(
        benchmark_id='bench_fixture',
        created_at='2026-01-01T00:00:00+00:00',
        hardware=deepcopy(host),
        settings=settings.model_dump(),
        stages=dict(
            ollama=dict(
                status='success',
                data=dict(
                    model=settings.model,
                    server=dict(url=settings.ollama_url),
                    selected=dict(name=settings.model, digest='sha256:fixture'),
                    metadata=dict(model_info={'gemma.context_length': 32768}),
                    samples=[
                        dict(
                            eval_count=10,
                            eval_duration_ns=1_000_000_000,
                            prompt_eval_count=100,
                            prompt_eval_duration_ns=1_000_000_000,
                            load_duration_ns=2_000_000_000,
                        )
                    ],
                ),
            ),
            ghidra=dict(status='success', data=dict(process_seconds=200)),
        ),
    )


def by_key(result):
    return {item['key']: item for item in result['items']}


def test_retry_recommendations_validate_original_stage_hardware_and_measurement_date():
    settings = Settings(model='fixture:1')
    host = hardware()
    original = benchmark(settings, host)
    original['stages']['cpu'] = dict(
        status='success',
        data=dict(single_operations_per_second=10, parallel_operations_per_second=20),
    )
    retry = deepcopy(original)
    retry.update(benchmark_id='bench_retry', created_at='2026-02-01T00:00:00+00:00')
    retry['reused_stages'] = {
        stage: {
            key: deepcopy(original[key])
            for key in ('benchmark_id', 'created_at', 'hardware', 'settings')
        }
        for stage in ('cpu', 'ollama', 'ghidra')
    }
    items = by_key(recommend_settings(settings, host, retry))
    assert items['llm_timeout']['evidence']['benchmark_id'] == original['benchmark_id']
    assert items['llm_timeout']['measured_at'] == original['created_at']
    assert items['cpu']['evidence']['benchmark_measured_at'] == original['created_at']
    for provenance in retry['reused_stages'].values():
        provenance['hardware']['cpu']['name'] = 'Different PC'
    items = by_key(recommend_settings(settings, host, retry))
    assert items['llm_timeout']['evidence']['tokens_per_second'] is None
    assert items['cpu']['evidence']['single_operations_per_second'] is None
    assert items['static_timeout']['evidence']['sample_seconds'] is None


def saved(tmp_path, settings):
    path = tmp_path / 'settings.json'
    path.write_text(
        json.dumps(settings.model_dump(), ensure_ascii=False, indent=3), encoding='utf-8'
    )
    return path


def proposal(current, **values):
    return dict(
        items=[
            dict(key=key, current=getattr(current, key), recommended=value)
            for key, value in values.items()
        ]
    )


def test_appropriate_defaults_are_kept_and_only_existing_keys_are_recommended():
    settings = Settings(model='fixture:1')
    result = recommend_settings(settings, hardware())
    assert set(by_key(result)) == set(ALLOWED_KEYS)
    assert all(item['current'] == item['recommended'] for item in result['items'])
    assert result['blocked_keys'] == []
    assert all(item['basis'] == 'estimated' for item in result['items'])
    assert all(item['confidence'] in ('low', 'medium', 'high') for item in result['items'])
    assert result['created_at'] and result['schema_version']


@pytest.mark.parametrize('profile', ['stability', 'balanced', 'performance'])
def test_profiles_only_produce_schema_valid_settings(profile):
    settings = Settings(
        num_ctx=131072, num_predict=16384, output_limit=16777216, cpu=16, memory_mb=32768
    )
    result = recommend_settings(settings, hardware(), profile=profile)
    values = settings.model_dump()
    values.update({item['key']: item['recommended'] for item in result['items']})
    Settings.model_validate(values, strict=True)
    assert all(item['warnings'] for item in result['items'])


@pytest.mark.parametrize(
    'value',
    [
        None,
        {},
        {'memory': None, 'cpu': None, 'environment': None},
        {
            'memory': {
                'safe_budget_bytes': float('nan'),
                'available_bytes': 'NaN',
                'total_bytes': True,
            },
            'cpu': {'usable_cores': float('inf'), 'usage_percent': -1},
            'environment': {'docker': None, 'ollama': None},
        },
    ],
)
def test_missing_none_and_invalid_numbers_preserve_safe_defaults(value):
    result = recommend_settings(Settings(), value)
    items = by_key(result)
    assert items['memory_mb']['recommended'] == 512
    assert items['llm_timeout']['recommended'] == 120
    json.dumps(result, allow_nan=False)


def test_unknown_profile_is_a_korean_error():
    with pytest.raises(ValueError, match='프로필'):
        recommend_settings(Settings(), hardware(), profile='unlimited')


def test_memory_uses_latest_available_ram_and_docker_upper_bound():
    host = hardware()
    host['memory'].update(safe_budget_bytes=8 * GIB, available_bytes=400 * MIB)
    host['environment']['docker']['memory_bytes'] = 100 * MIB
    result = by_key(recommend_settings(Settings(), host))
    assert result['memory_mb']['recommended'] == 70
    assert result['num_ctx']['recommended'] == 2048
    assert result['output_limit']['recommended'] == 256 * 1024


def test_budget_below_minimum_blocks_memory_instead_of_clamping_up():
    host = hardware()
    host['memory']['safe_budget_bytes'] = 63 * MIB
    result = recommend_settings(Settings(), host)
    item = by_key(result)['memory_mb']
    assert item['apply_blocked'] is True
    assert item['recommended'] == 512
    assert 'memory_mb' in result['blocked_keys']
    assert any('64MiB' in line for line in result['warnings'])


def test_zero_budget_is_not_treated_as_missing():
    host = hardware()
    host['memory']['safe_budget_bytes'] = 0
    assert by_key(recommend_settings(Settings(), host))['memory_mb']['apply_blocked']


def test_stale_ram_change_is_blocked_but_not_automatically_saved():
    host = hardware()
    host['collected_at'] = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
    host['memory']['safe_budget_bytes'] = 128 * MIB
    result = recommend_settings(Settings(), host)
    assert by_key(result)['memory_mb']['apply_blocked']
    assert any('다시 조회' in line for line in result['warnings'])


def test_ps_loaded_context_is_not_model_maximum():
    host = hardware()
    host['environment']['ollama']['running'] = [dict(name='fixture:1', context_length=2048)]
    item = by_key(recommend_settings(Settings(model='fixture:1'), host))['num_ctx']
    assert item['recommended'] == 8192
    assert item['evidence']['loaded_context_length'] == 2048
    assert item['evidence']['model_context_limit'] is None
    assert any('모델 최대' in line for line in item['warnings'])


@pytest.mark.parametrize(
    'metadata,expected',
    [
        ({'name': 'fixture:1', 'model_info': {'gemma.context_length': 4096}}, 4096),
        ({'fixture:1': {'model_info': {'gemma.context_length': 2048}}}, 2048),
        ([{'model': 'fixture:1', 'context_length': 4096}], 4096),
        ({'model': 'other:1', 'context_length': 1024}, 8192),
        ({'context_length': 1024}, 8192),
    ],
)
def test_context_metadata_must_identify_selected_model(metadata, expected):
    host = hardware()
    host['environment']['ollama']['model_metadata'] = metadata
    assert (
        by_key(recommend_settings(Settings(model='fixture:1'), host))['num_ctx']['recommended']
        == expected
    )


def test_metadata_from_another_endpoint_is_excluded():
    host = hardware()
    host['environment']['ollama'].update(
        url='http://127.0.0.1:9999', model_metadata=dict(model='fixture:1', context_length=1024)
    )
    assert (
        by_key(recommend_settings(Settings(model='fixture:1'), host))['num_ctx']['recommended']
        == 8192
    )


def test_model_limit_below_settings_minimum_is_explicitly_blocked():
    host = hardware()
    host['environment']['ollama']['model_metadata'] = dict(model='fixture:1', context_length=512)
    item = by_key(recommend_settings(Settings(model='fixture:1'), host))['num_ctx']
    assert item['apply_blocked']
    assert item['recommended'] == 8192


def test_matching_native_metrics_inform_an_estimate_not_a_claimed_measured_timeout():
    settings, host = Settings(model='fixture:1'), hardware()
    result = by_key(recommend_settings(settings, host, benchmark(settings, host)))
    assert result['llm_timeout']['recommended'] > 120
    assert result['llm_timeout']['basis'] == 'estimated'
    assert result['llm_timeout']['measured_at'] == '2026-01-01T00:00:00+00:00'
    assert result['llm_timeout']['evidence']['tokens_per_second'] == 10
    assert result['static_timeout']['recommended'] == 430


@pytest.mark.parametrize('profile', ['stability', 'balanced', 'performance'])
def test_tiny_prompt_latency_is_not_linearly_expanded_to_full_context(profile):
    settings, host = Settings(model='fixture:1', llm_timeout=300), hardware()
    old = benchmark(settings, host)
    old['stages']['ollama']['data']['samples'] = [
        dict(
            eval_count=16,
            eval_duration_ns=127898000,
            prompt_eval_count=28,
            prompt_eval_duration_ns=555402000,
            load_duration_ns=13057818300,
        ),
        dict(
            eval_count=16,
            eval_duration_ns=101162000,
            prompt_eval_count=28,
            prompt_eval_duration_ns=7603887000,
            load_duration_ns=9786500,
        ),
    ]
    item = by_key(recommend_settings(settings, host, old, profile))['llm_timeout']
    assert item['recommended'] == 300
    assert item['evidence']['observed_prompt_seconds'] == pytest.approx(7.603887)
    assert item['evidence']['observed_prompt_token_counts'] == [28, 28]
    assert item['evidence']['projected_seconds'] < 100
    assert item['evidence']['prompt_extrapolated_to_context'] is False
    assert item['basis'] == 'estimated'
    assert any('큰 실제 입력' in line for line in item['warnings'])


def test_actual_slow_generation_still_informs_timeout_estimate():
    settings, host = Settings(model='fixture:1', llm_timeout=30), hardware()
    old = benchmark(settings, host)
    item = by_key(recommend_settings(settings, host, old))['llm_timeout']
    assert item['recommended'] == 327  # (2048/10 + 2s load + 1s prompt)*1.5 + 15
    assert item['evidence']['projected_seconds'] == 327


def post_sample(host, available, *, age_seconds=0, nested=False):
    stamp = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat()
    total = host['memory']['total_bytes']
    if nested:
        return dict(created_at=stamp, ram=dict(total_bytes=total, available_bytes=available))
    return dict(collected_at=stamp, memory_total_bytes=total, memory_available_bytes=available)


@pytest.mark.parametrize('nested', [False, True])
def test_post_inference_ram_observation_constrains_context_and_container_budget(nested):
    settings, host = Settings(model='fixture:1', memory_mb=8192), hardware()
    old = benchmark(settings, host)
    after = post_sample(host, 4 * GIB, nested=nested)
    old['stages']['ollama']['data']['samples'][0]['resource_after'] = after
    items = by_key(recommend_settings(settings, host, old))
    expected_budget = 4 * GIB * 0.7
    assert items['memory_mb']['recommended'] == int(expected_budget // MIB)
    assert items['memory_mb']['evidence']['host_safe_budget_bytes'] == expected_budget
    assert items['memory_mb']['evidence']['inventory_safe_budget_bytes'] == 7 * GIB
    assert items['num_ctx']['recommended'] == 4096
    assert items['memory_mb']['evidence']['post_inference_safe_budget_bytes'] == expected_budget
    assert items['memory_mb']['measured_at'] == after.get('collected_at', after.get('created_at'))
    assert any('다른 프로세스' in line for line in items['memory_mb']['warnings'])


def test_lower_current_ram_is_used_without_subtracting_model_consumption_twice():
    settings, host = Settings(model='fixture:1', memory_mb=8192), hardware()
    old = benchmark(settings, host)
    old['stages']['ollama']['data']['samples'][0]['resource_after'] = post_sample(host, 5 * GIB)
    host['memory'].update(available_bytes=2 * GIB, safe_budget_bytes=int(2 * GIB * 0.7))
    item = by_key(recommend_settings(settings, host, old))['memory_mb']
    assert item['evidence']['host_safe_budget_bytes'] == int(2 * GIB * 0.7)
    assert item['recommended'] == int(int(2 * GIB * 0.7) // MIB)
    assert item['measured_at'] == host['collected_at']


@pytest.mark.parametrize('incompatible', ['model', 'digest', 'server', 'hardware', 'status'])
def test_incompatible_post_inference_ram_is_not_reused(incompatible):
    settings, host = Settings(model='fixture:1', memory_mb=8192), hardware()
    old = benchmark(settings, host)
    old['stages']['ollama']['data']['samples'][0]['resource_after'] = post_sample(host, 64 * MIB)
    if incompatible == 'model':
        old['stages']['ollama']['data']['model'] = 'other:1'
    elif incompatible == 'digest':
        old['stages']['ollama']['data']['selected']['digest'] = 'sha256:old'
    elif incompatible == 'server':
        old['stages']['ollama']['data']['server']['url'] = 'http://127.0.0.1:9999'
    elif incompatible == 'hardware':
        old['hardware']['cpu']['usable_cores'] = 2
    else:
        old['stages']['ollama']['status'] = 'failed'
    item = by_key(recommend_settings(settings, host, old))['memory_mb']
    assert item['evidence']['post_inference_safe_budget_bytes'] is None
    assert not item['apply_blocked']
    assert item['recommended'] == int(8 * GIB * 0.7 // MIB)


def test_stale_post_inference_observation_is_conservative_and_explicitly_warned():
    settings, host = Settings(model='fixture:1'), hardware()
    old = benchmark(settings, host)
    old['stages']['ollama']['data']['samples'][0]['resource_after'] = post_sample(
        host, 4 * GIB, age_seconds=600
    )
    result = recommend_settings(settings, host, old)
    item = by_key(result)['memory_mb']
    assert item['evidence']['post_inference_safe_budget_bytes'] == 4 * GIB * 0.7
    assert item['evidence']['post_inference_samples'][0]['stale'] is True
    assert any('모델 실행 후 RAM 관찰이 5분' in line for line in item['warnings'])
    assert any('모델 실행 후 RAM 관찰이 5분' in line for line in result['warnings'])


@pytest.mark.parametrize(
    'bad_available,bad_total',
    [(None, 16 * GIB), ('NaN', 16 * GIB), (float('inf'), 16 * GIB), (GIB, None), (GIB, 8 * GIB)],
)
def test_invalid_or_different_host_post_memory_is_ignored(bad_available, bad_total):
    settings, host = Settings(model='fixture:1'), hardware()
    old = benchmark(settings, host)
    old['stages']['ollama']['data']['samples'][0]['resource_after'] = dict(
        memory_available_bytes=bad_available, memory_total_bytes=bad_total
    )
    item = by_key(recommend_settings(settings, host, old))['memory_mb']
    assert item['evidence']['post_inference_safe_budget_bytes'] is None
    assert item['evidence']['host_safe_budget_bytes'] == 7 * GIB


@pytest.mark.parametrize(
    'change', ['model', 'server', 'hardware_endpoint', 'cpu', 'ram', 'status', 'unknown_endpoint']
)
def test_incompatible_benchmark_does_not_influence_llm_timeout(change):
    settings, host = Settings(model='fixture:1'), hardware()
    old = benchmark(settings, host)
    if change == 'model':
        old['stages']['ollama']['data']['model'] = 'other:1'
    elif change == 'server':
        old['settings']['ollama_url'] = 'http://127.0.0.1:9999'
    elif change == 'hardware_endpoint':
        old['hardware']['environment']['ollama']['url'] = 'http://127.0.0.1:9999'
    elif change == 'cpu':
        old['hardware']['cpu']['usable_cores'] = 2
    elif change == 'ram':
        old['hardware']['memory']['total_bytes'] = 32 * GIB
    elif change == 'status':
        old['stages']['ollama']['status'] = 'partial'
    else:
        old.pop('settings')
        old['stages']['ollama']['data'].pop('server')
        old['hardware']['environment']['ollama'].pop('url')
    result = recommend_settings(settings, host, old)
    item = by_key(result)['llm_timeout']
    assert item['recommended'] == 120
    assert item['evidence']['tokens_per_second'] is None
    assert item['measured_at'] is None
    assert any('AI 벤치마크' in line for line in result['warnings'])


def test_cpu_budget_respects_usable_affinity_engine_and_load():
    settings, host = Settings(cpu=16), hardware()
    host['cpu'].update(usable_cores=2, usage_percent=90)
    host['environment']['docker']['cpus'] = 0.25
    assert by_key(recommend_settings(settings, host))['cpu']['recommended'] == 0.25


def test_digest_changes_or_unknown_digest_exclude_old_ai_measurements():
    settings, host = Settings(model='fixture:1'), hardware()
    old = benchmark(settings, host)
    host['environment']['ollama']['models'][0]['digest'] = 'sha256:updated'
    result = recommend_settings(settings, host, old)
    assert by_key(result)['llm_timeout']['recommended'] == 120
    assert any('digest' in line for line in result['warnings'])
    host['environment']['ollama']['models'] = []
    assert (
        by_key(recommend_settings(settings, host, old))['llm_timeout']['evidence'][
            'tokens_per_second'
        ]
        is None
    )


def test_cpu_benchmark_scaling_is_supporting_evidence_without_claiming_target_scaling():
    settings, host = Settings(model='fixture:1'), hardware()
    old = benchmark(settings, host)
    # Match the actual BenchmarkRunner._cpu output contract.
    old['stages']['cpu'] = dict(
        status='success',
        data=dict(single_operations_per_second=10, parallel_operations_per_second=25),
    )
    item = by_key(recommend_settings(settings, host, old))['cpu']
    assert item['recommended'] == settings.cpu
    assert item['evidence']['observed_throughput_ratio'] == 2.5
    assert item['evidence']['single_operations_per_second'] == 10
    assert item['evidence']['parallel_operations_per_second'] == 25
    assert item['evidence']['benchmark_id'] == 'bench_fixture'
    assert any('CPU 할당 증가의 근거로 사용하지 않습니다' in line for line in item['warnings'])


def test_legacy_cpu_benchmark_field_names_remain_readable():
    settings, host = Settings(model='fixture:1'), hardware()
    old = benchmark(settings, host)
    old['stages']['cpu'] = dict(
        status='success', data=dict(single_ops_per_second=10, multi_ops_per_second=25)
    )
    evidence = by_key(recommend_settings(settings, host, old))['cpu']['evidence']
    assert evidence['single_ops_per_second'] == evidence['single_operations_per_second'] == 10
    assert evidence['multi_ops_per_second'] == evidence['parallel_operations_per_second'] == 25
    assert evidence['observed_throughput_ratio'] == 2.5
    assert evidence['benchmark_id'] == 'bench_fixture'


@pytest.mark.parametrize('single,multi', [(20, 30), (None, None), (float('nan'), -1)])
def test_current_cpu_keys_take_precedence_over_conflicting_legacy_values(single, multi):
    settings, host = Settings(model='fixture:1'), hardware()
    old = benchmark(settings, host)
    old['stages']['cpu'] = dict(
        status='success',
        data=dict(
            single_operations_per_second=single,
            parallel_operations_per_second=multi,
            single_ops_per_second=10,
            multi_ops_per_second=25,
        ),
    )
    evidence = by_key(recommend_settings(settings, host, old))['cpu']['evidence']
    expected_single = 20 if single == 20 else None
    expected_multi = 30 if multi == 30 else None
    assert evidence['single_operations_per_second'] == expected_single
    assert evidence['parallel_operations_per_second'] == expected_multi
    assert evidence['observed_throughput_ratio'] == (1.5 if expected_single else None)


@pytest.mark.parametrize('incompatible', ['failed', 'different_host'])
def test_current_cpu_measurements_require_success_on_matching_hardware(incompatible):
    settings, host = Settings(model='fixture:1'), hardware()
    old = benchmark(settings, host)
    old['stages']['cpu'] = dict(
        status='success',
        data=dict(single_operations_per_second=10, parallel_operations_per_second=25),
    )
    if incompatible == 'failed':
        old['stages']['cpu']['status'] = 'failed'
    else:
        old['hardware']['cpu']['usable_cores'] = 2
    evidence = by_key(recommend_settings(settings, host, old))['cpu']['evidence']
    assert evidence['single_operations_per_second'] is None
    assert evidence['parallel_operations_per_second'] is None
    assert evidence['observed_throughput_ratio'] is None
    assert evidence['benchmark_id'] is None


def test_hardware_supporting_dates_are_kept_for_context_output_and_cpu():
    host = hardware()
    result = by_key(recommend_settings(Settings(), host))
    assert all(
        result[key]['measured_at'] == host['collected_at']
        for key in ('num_ctx', 'num_predict', 'cpu', 'memory_mb', 'output_limit')
    )
    assert all(result[key]['basis'] == 'estimated' for key in result)


@pytest.mark.parametrize('invalid', ['10', 10.0, True, float('nan'), None])
def test_malformed_native_token_counter_is_not_used_as_measured_speed(invalid):
    settings, host = Settings(model='fixture:1'), hardware()
    old = benchmark(settings, host)
    old['stages']['ollama']['data']['samples'][0]['eval_count'] = invalid
    item = by_key(recommend_settings(settings, host, old))['llm_timeout']
    assert item['recommended'] == 120
    assert item['evidence']['tokens_per_second'] is None


def test_apply_and_restore_invalidate_saved_model_capability_cache(tmp_path):
    current = Settings(capabilities=dict(tools=True, json=True))
    saved(tmp_path, current)
    updated = SettingsHistory(tmp_path).apply(current, proposal(current, num_ctx=4096), ['num_ctx'])
    assert updated.capabilities == {}
    assert (
        Settings.model_validate_json((tmp_path / 'settings.json').read_bytes()).capabilities == {}
    )
    restored = SettingsHistory(tmp_path).restore(updated)
    assert restored.num_ctx == current.num_ctx
    assert restored.capabilities == {}
    assert (
        Settings.model_validate_json((tmp_path / 'settings.json').read_bytes()).capabilities == {}
    )


def test_discovered_setup_patch_backs_up_selected_fields_and_restores_after_restart(tmp_path):
    current = Settings(model='before:1', capabilities={'tools': True}, llm_timeout=240)
    path = saved(tmp_path, current)
    original = path.read_bytes()
    patch = dict(
        ghidra_path='C:/Tools/Ghidra',
        java_home='C:/Tools/JDK',
        docker_context='desktop-linux',
        ollama_url='http://127.0.0.1:11434',
        model='installed:1',
    )
    updated = SettingsHistory(tmp_path).apply_setup(current, patch)
    assert all(getattr(updated, key) == value for key, value in patch.items())
    assert updated.llm_timeout == current.llm_timeout and updated.capabilities == {}
    history = SettingsHistory(tmp_path).list_history()
    assert (
        len(history) == 1 and history[0]['action'] == 'setup' and history[0]['status'] == 'applied'
    )
    assert base64.b64decode(history[0]['before_bytes_base64']) == original
    assert Settings.model_validate_json(path.read_bytes()) == updated
    restored = SettingsHistory(tmp_path).restore(updated)
    assert restored.model == current.model and restored.ghidra_path == current.ghidra_path
    assert restored.llm_timeout == current.llm_timeout and restored.capabilities == {}


@pytest.mark.parametrize(
    'patch',
    [
        {},
        None,
        {'cpu': 2},
        {'capabilities': {}},
        {'model': True},
        {'ghidra_path': None},
        {'java_home': ''},
        {'docker_context': '  '},
        {'model': 'bad\x00model'},
    ],
)
def test_discovered_setup_rejects_unapproved_keys_or_invalid_strings_without_writing(
    tmp_path, patch
):
    current = Settings()
    with pytest.raises(ValueError, match='자동 설정'):
        SettingsHistory(tmp_path).apply_setup(current, patch)
    assert not (tmp_path / 'settings.json').exists()
    assert not (tmp_path / 'settings-history').exists()


def test_discovered_setup_preserves_unsaved_edits_and_other_writer_changes(tmp_path):
    current = Settings()
    path = saved(tmp_path, current)
    history = SettingsHistory(tmp_path)
    with pytest.raises(ValueError, match='화면의 설정'):
        history.apply_setup(Settings(model='unsaved:1'), {'java_home': 'C:/Tools/JDK'})
    external = Settings(model='other-writer:1')
    saved(tmp_path, external)
    with pytest.raises(ValueError, match='다른 작업'):
        history.apply_setup(current, {'java_home': 'C:/Tools/JDK'})
    assert Settings.model_validate_json(path.read_bytes()) == external


def test_apply_selected_only_backs_exact_disk_and_survives_restart(tmp_path):
    current = Settings(model='모델:1', llm_timeout=90)
    path = saved(tmp_path, current)
    original_bytes = path.read_bytes()
    result = SettingsHistory(tmp_path).apply(
        current, proposal(current, num_ctx=4096, llm_timeout=300), ['num_ctx']
    )
    assert result.num_ctx == 4096
    assert result.llm_timeout == 90
    assert result.model == current.model
    history = SettingsHistory(tmp_path).list_history()
    assert len(history) == 1 and history[0]['status'] == 'applied'
    assert history[0]['before_settings'] == current.model_dump()
    assert base64.b64decode(history[0]['before_bytes_base64']) == original_bytes
    assert Settings.model_validate_json(path.read_bytes()) == result
    restored = SettingsHistory(tmp_path).restore(result)
    assert restored == current
    history = SettingsHistory(tmp_path).list_history()
    assert len(history) == 2 and history[0]['action'] == 'restore'
    assert history[0]['before_settings'] == result.model_dump()


def test_apply_with_no_file_backups_absence_and_uses_explicit_root(tmp_path, monkeypatch):
    other = tmp_path / 'must-not-exist'
    monkeypatch.setattr(module, 'data_root', lambda: other)
    current = Settings()
    result = SettingsHistory(tmp_path).apply(current, proposal(current, num_ctx=4096), ['num_ctx'])
    assert result.num_ctx == 4096
    assert not other.exists()
    history = SettingsHistory(tmp_path).list_history()[0]
    assert history['before_exists'] is False and history['before_bytes_base64'] is None


def test_unsaved_gui_values_are_preserved_by_refusing_before_mutation(tmp_path):
    disk = Settings(model='saved:1')
    path = saved(tmp_path, disk)
    before = path.read_bytes()
    unsaved = disk.model_copy(update=dict(model='unsaved:1'))
    with pytest.raises(ValueError, match='먼저 일반 설정을 저장'):
        SettingsHistory(tmp_path).apply(unsaved, proposal(unsaved, num_ctx=4096), ['num_ctx'])
    assert path.read_bytes() == before
    assert SettingsHistory(tmp_path).list_history() == []


def test_external_file_change_after_history_creation_is_not_overwritten(tmp_path):
    current = Settings()
    path = saved(tmp_path, current)
    history = SettingsHistory(tmp_path)
    external = current.model_copy(update=dict(model='external:1'))
    saved(tmp_path, external)
    with pytest.raises(ValueError, match='다른 작업에서 변경'):
        history.apply(current, proposal(current, num_ctx=4096), ['num_ctx'])
    assert Settings.model_validate_json(path.read_bytes()) == external


@pytest.mark.parametrize(
    'keys,values',
    [
        (['model'], {'model': 'other'}),
        ([], {'num_ctx': 4096}),
        (['num_ctx', 'num_ctx'], {'num_ctx': 4096}),
        (['num_ctx'], {'num_ctx': 1}),
        (['num_ctx'], {'num_ctx': '4096'}),
        (['cpu'], {'cpu': float('nan')}),
        (['num_ctx'], {'num_ctx': True}),
    ],
)
def test_apply_rejects_unsupported_keys_duplicates_empty_and_bad_ranges(tmp_path, keys, values):
    current = Settings()
    path = saved(tmp_path, current)
    before = path.read_bytes()
    with pytest.raises(ValueError):
        SettingsHistory(tmp_path).apply(current, proposal(current, **values), keys)
    assert path.read_bytes() == before
    assert SettingsHistory(tmp_path).list_history() == []


def test_old_recommendation_is_rejected_before_backup(tmp_path):
    old = Settings()
    current = old.model_copy(update=dict(num_ctx=4096))
    saved(tmp_path, current)
    with pytest.raises(ValueError, match='설정이 바뀌었습니다'):
        SettingsHistory(tmp_path).apply(current, proposal(old, num_ctx=2048), ['num_ctx'])
    assert SettingsHistory(tmp_path).list_history() == []


def test_blocked_recommendation_cannot_be_applied_through_api(tmp_path):
    current = Settings()
    saved(tmp_path, current)
    recommended = proposal(current, memory_mb=64)
    recommended['blocked_keys'] = ['memory_mb']
    with pytest.raises(ValueError, match='안전 확인'):
        SettingsHistory(tmp_path).apply(current, recommended, ['memory_mb'])


@pytest.mark.parametrize('available', [0, 63 * MIB, 100 * MIB])
def test_ram_drop_at_apply_time_is_rechecked_before_backup(tmp_path, monkeypatch, available):
    current = Settings()
    path = saved(tmp_path, current)
    before = path.read_bytes()
    monkeypatch.setattr(
        module.psutil,
        'virtual_memory',
        lambda: SimpleNamespace(total=16 * GIB, available=available),
    )
    with pytest.raises(ValueError, match='현재 RAM을 다시 확인'):
        SettingsHistory(tmp_path).apply(current, proposal(current, memory_mb=128), ['memory_mb'])
    assert path.read_bytes() == before
    assert SettingsHistory(tmp_path).list_history() == []


def test_ram_recheck_allows_safe_change_and_does_not_query_for_other_keys(tmp_path, monkeypatch):
    current = Settings()
    saved(tmp_path, current)
    monkeypatch.setattr(
        module.psutil, 'virtual_memory', lambda: SimpleNamespace(total=16 * GIB, available=GIB)
    )
    updated = SettingsHistory(tmp_path).apply(
        current, proposal(current, memory_mb=128), ['memory_mb']
    )
    assert updated.memory_mb == 128
    monkeypatch.setattr(
        module.psutil, 'virtual_memory', lambda: pytest.fail('unrelated setting read RAM')
    )
    SettingsHistory(tmp_path).apply(updated, proposal(updated, num_ctx=4096), ['num_ctx'])


def test_failed_write_restores_exact_original_bytes(tmp_path, monkeypatch):
    current = Settings()
    path = saved(tmp_path, current)
    original_bytes = path.read_bytes()
    history = SettingsHistory(tmp_path)
    real_write = history._write_settings

    def fail_after_replace(raw):
        real_write(raw)
        raise OSError('fixture write failure after replacement')

    monkeypatch.setattr(history, '_write_settings', fail_after_replace)
    with pytest.raises(ValueError, match='설정 저장에 실패'):
        history.apply(current, proposal(current, num_ctx=4096), ['num_ctx'])
    assert path.read_bytes() == original_bytes
    assert history.list_history()[0]['status'] == 'failed'
    assert not (tmp_path / 'settings-history' / '.lock').exists()


def test_failed_first_write_restores_file_absence(tmp_path, monkeypatch):
    current, history = Settings(), SettingsHistory(tmp_path)

    def fail(raw):
        module._atomic_bytes(history.path, raw)
        raise OSError('fixture failure')

    monkeypatch.setattr(history, '_write_settings', fail)
    with pytest.raises(ValueError):
        history.apply(current, proposal(current, num_ctx=4096), ['num_ctx'])
    assert not history.path.exists()
    assert history.list_history()[0]['before_exists'] is False


def test_failed_write_does_not_roll_back_another_writer(tmp_path, monkeypatch):
    current = Settings()
    path = saved(tmp_path, current)
    external = current.model_copy(update=dict(model='external:1'))
    history = SettingsHistory(tmp_path)

    def fail(raw):
        saved(tmp_path, external)
        raise OSError('fixture concurrent writer')

    monkeypatch.setattr(history, '_write_settings', fail)
    with pytest.raises(ValueError, match='다른 작업이 쓴 파일'):
        history.apply(current, proposal(current, num_ctx=4096), ['num_ctx'])
    assert Settings.model_validate_json(path.read_bytes()) == external
    assert history.list_history()[0]['rollback_error']


def test_concurrent_change_during_backup_is_preserved(tmp_path, monkeypatch):
    current = Settings()
    path = saved(tmp_path, current)
    external = current.model_copy(update=dict(model='other:1'))
    history, original_write = SettingsHistory(tmp_path), SettingsHistory._write_record

    def change_after_backup(record):
        original_write(history, record)
        if record['status'] == 'pending':
            saved(tmp_path, external)

    monkeypatch.setattr(history, '_write_record', change_after_backup)
    with pytest.raises(ValueError, match='백업 중 설정 파일이 변경'):
        history.apply(current, proposal(current, num_ctx=4096), ['num_ctx'])
    assert Settings.model_validate_json(path.read_bytes()) == external


def test_interrupted_pending_operation_recovers_observed_status_on_restart(tmp_path):
    current = Settings()
    saved(tmp_path, current)
    updated = SettingsHistory(tmp_path).apply(current, proposal(current, num_ctx=4096), ['num_ctx'])
    path = next((tmp_path / 'settings-history').glob('backup_*.json'))
    record = json.loads(path.read_text(encoding='utf-8'))
    record['status'] = 'pending'
    path.write_text(json.dumps(record), encoding='utf-8')
    history = SettingsHistory(tmp_path)
    assert history.list_history()[0]['status'] == 'applied'
    assert history.list_history()[0]['recovered_from_pending'] is True
    assert history.restore(updated) == current
    original_record = next(
        row for row in history.list_history() if row['backup_id'] == record['backup_id']
    )
    assert original_record['status'] == 'applied'
    assert json.loads(path.read_text(encoding='utf-8'))['status'] == 'applied'


def test_restore_explicit_id_and_invalid_path_ids_do_not_escape_history(tmp_path):
    current = Settings()
    saved(tmp_path, current)
    updated = SettingsHistory(tmp_path).apply(current, proposal(current, num_ctx=4096), ['num_ctx'])
    backup_id = SettingsHistory(tmp_path).list_history()[0]['backup_id']
    with pytest.raises(ValueError, match='이력이 없습니다'):
        SettingsHistory(tmp_path).restore(updated, '../settings')
    assert SettingsHistory(tmp_path).restore(updated, backup_id) == current


def test_empty_history_and_corrupt_records_have_clear_errors(tmp_path):
    history = SettingsHistory(tmp_path)
    history.directory.mkdir()
    (history.directory / 'backup_bad.json').write_text('{broken', encoding='utf-8')
    assert history.list_history() == []
    with pytest.raises(ValueError, match='이력이 없습니다'):
        history.restore(Settings())


def test_default_root_uses_models_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(module, 'data_root', lambda: tmp_path / 'Recheck')
    assert SettingsHistory().path == tmp_path / 'Recheck' / 'settings.json'
