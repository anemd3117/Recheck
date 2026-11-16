"""검증 도구가 누락·중복·비정상 관찰과 AI 재사용을 성공으로 세지 않는지 검사."""

import base64
import copy
import importlib.util
import hashlib
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/verify_reference_stability.py'
SPEC = importlib.util.spec_from_file_location('reference_stability', SCRIPT)
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)


def material():
    test = {'id': 'edge', 'args': ['17'], 'stdin': '', 'env': {}, 'aux_files': []}
    sample = {
        'tests': [test],
        'oracle': {
            'edge': {
                'original': {'stdout': '한글\n', 'stderr': '', 'exit_code': 0},
                'reimplementation': {'stdout': '차이\n', 'stderr': '', 'exit_code': 0},
                'match': False,
            }
        },
    }
    row = {'test_id': 'edge', 'input': copy.deepcopy(test)}
    for target, text in [('original', '한글\n'), ('reimplementation', '차이\n')]:
        row[target] = {
            'status': 'success',
            'infrastructure_ok': True,
            'stdout_b64': base64.b64encode(text.encode()).decode(),
            'stderr_b64': '',
            'stdout': text,
            'stderr': '',
            'exit_code': 0,
            'actual_input': {'args': ['17'], 'stdin': '', 'env': {}, 'auxiliary_files': []},
            'target_exit_code': 0,
            'timed_out': False,
            'oom_killed': False,
            'truncated': False,
            'cancelled': False,
        }
    run = {
        'dynamic': [row],
        'behavior': {
            'tests': [{'test_id': 'edge', 'match': False}],
            'registered': 1,
            'denominator': 1,
            'matched': 0,
        },
    }
    return run, sample


def test_confined_rejects_parent_escape(tmp_path):
    with pytest.raises(ValueError):
        verifier.confined(tmp_path, '../outside')


def test_correct_intentional_difference_passes_without_changing_expected_value():
    run, sample = material()
    assert verifier.check_observations(run, sample)['passed'] is True
    assert sample['oracle']['edge']['match'] is False


@pytest.mark.parametrize(
    'mutation', ['missing', 'duplicate', 'bytes', 'exit', 'timeout', 'match', 'missing_behavior']
)
def test_observation_verification_rejects_missing_duplicate_wrong_or_abnormal_results(mutation):
    run, sample = material()
    if mutation == 'missing':
        run['dynamic'] = []
    elif mutation == 'duplicate':
        run['dynamic'].append(copy.deepcopy(run['dynamic'][0]))
    elif mutation == 'bytes':
        run['dynamic'][0]['original']['stdout_b64'] = base64.b64encode(b'wrong').decode()
    elif mutation == 'exit':
        run['dynamic'][0]['original']['target_exit_code'] = 2
    elif mutation == 'timeout':
        run['dynamic'][0]['original']['timed_out'] = True
    elif mutation == 'match':
        run['behavior']['tests'][0]['match'] = True
    else:
        run['behavior']['tests'] = []
    assert verifier.check_observations(run, sample)['passed'] is False


def test_extra_ai_has_new_identity_and_no_previous_llm_response(tmp_path):
    source = tmp_path / 'result.json'
    source.write_text('{"preserved":true}', encoding='utf-8')
    run = {
        'run_id': 'run_' + 'a' * 32,
        'attempt_id': 'attempt_before',
        'status': 'success',
        'created': 'before',
        'tests': [],
        'stages': {'llm': {'status': 'success'}},
        'static': {'original': {'functions': []}},
        'dynamic': [{'test_id': 'edge'}],
        'llm': {'responses': [{'message': {'content': 'previous answer'}}]},
        'evidence': [
            {'evidence_id': 'ev-observation', 'kind': 'execution'},
            {
                'evidence_id': 'ev-feedback',
                'kind': 'llm_failure',
                'data': {'error': 'previous correction'},
            },
        ],
        'attempts': [{'attempt_id': 'attempt_before'}],
        'events': [],
        'settings': {},
    }
    original = copy.deepcopy(run)
    fresh = verifier.independent_record(run, {'model': 'selected'}, source)
    assert fresh['run_id'] != original['run_id']
    assert fresh['attempt_id'] != original['attempt_id']
    assert fresh['llm'] == {}
    assert fresh['evidence'] == [{'evidence_id': 'ev-observation', 'kind': 'execution'}]
    assert fresh['dynamic'] == original['dynamic']
    assert fresh['analysis_reuse']['source_run_id'] == original['run_id']
    assert fresh['settings']['model'] == 'selected'
    assert run == original


def test_usage_keeps_rejected_attempts_and_token_counts():
    result = verifier.llm_usage(
        {
            'status': 'success',
            'settings': {'num_predict': 2048},
            'validation_errors': [{'attempt': 1, 'error': '잘못된 참조'}],
            'responses': [
                {'eval_count': 5, 'prompt_eval_count': 12, 'done_reason': 'stop'},
                {'eval_count': 7, 'prompt_eval_count': 19, 'done_reason': 'stop'},
            ],
        }
    )
    assert result['response_count'] == 2
    assert result['rejected_response_count'] == 1
    assert result['accepted_response_count'] == 1
    assert result['output_tokens'] == 12
    assert result['input_tokens'] == 31
    assert result['cost'] is None


def test_extra_ai_report_stage_is_freshly_completed(tmp_path):
    from recheck.engine import Engine
    from recheck.models import Settings
    from recheck.storage import new_id

    engine = Engine(Settings(model='fixture'), tmp_path)
    run = {
        'run_id': new_id('run'),
        'attempt_id': new_id('attempt'),
        'status': 'success',
        'settings': {},
        'stages': {'report': {'status': 'pending'}},
        'files': {},
        'tests': [],
        'evidence': [],
        'dynamic': [],
        'llm': {},
        'agent': {},
        'events': [],
        'diagnostics': [],
    }
    verifier.finish_extra_report(engine, run)
    assert run['stages']['report']['status'] == 'success'
    assert (engine.store.run_dir(run['run_id']) / 'report.html').is_file()


def test_observation_verification_rejects_different_actual_inputs_despite_expected_outputs():
    run, sample = material()
    run['dynamic'][0]['original']['actual_input']['args'] = ['999']
    run['dynamic'][0]['reimplementation']['actual_input']['args'] = ['888']
    assert verifier.check_observations(run, sample)['passed'] is False


def test_observation_verification_rejects_display_text_that_differs_from_raw_bytes():
    run, sample = material()
    run['dynamic'][0]['original']['stdout'] = '표시만 다른 잘못된 결과'
    assert verifier.check_observations(run, sample)['passed'] is False


def assessment_material(tmp_path):
    """실제 Store·참조 검증·보고서 렌더러를 사용하는 검증 스크립트 입력."""
    from recheck.engine import Engine
    from recheck.models import Settings
    from recheck.storage import new_id

    engine = Engine(Settings(), tmp_path / 'data')
    run, sample = material()
    run.update(
        run_id=new_id('run'),
        attempt_id=new_id('attempt'),
        status='success',
        mode='automatic',
        settings={},
        static={},
        tests=copy.deepcopy(sample['tests']),
        events=[],
        diagnostics=[],
    )
    run['behavior']['match_rate'] = 0.0
    run['stages'] = {
        name: {'status': 'success'}
        for name in (
            'metadata',
            'static_original',
            'static_reimplementation',
            'structure',
            'image',
            'dynamic',
            'llm',
            'report',
        )
    }
    run['files'], sample['manifest'] = {}, {}
    for target in ('original', 'reimplementation'):
        binary = tmp_path / (target + '.elf')
        binary.write_bytes((target + '-fixture-bytes').encode())
        sha = hashlib.sha256(binary.read_bytes()).hexdigest()
        run['files'][target] = {
            'name': binary.name,
            'path': str(binary),
            'snapshot': str(binary),
            'sha256': sha,
        }
        run['dynamic'][0][target]['binary_sha256'] = sha
        sample['manifest'][target] = {
            'path': str(binary),
            'sha256': sha,
            'size': binary.stat().st_size,
        }
    run['evidence'] = [
        {
            'evidence_id': 'ev-' + target,
            'kind': 'execution',
            'target': target,
            'source': 'test:edge',
            'data': {'test_id': 'edge', **run['dynamic'][0][target]},
        }
        for target in ('original', 'reimplementation')
    ]
    answer = {
        'summary': '등록 입력의 실제 관찰 결과입니다.',
        'findings': [
            {
                'finding_id': 'observed-edge',
                'claim': 'edge 테스트의 양쪽 출력 차이를 관찰했습니다.',
                'rationale': '실제로 실행한 결과를 비교했습니다.',
                'targets': ['both'],
                'function_ids': [],
                'test_ids': ['edge'],
                'evidence_ids': ['ev-original', 'ev-reimplementation'],
            }
        ],
    }
    raw = json.dumps(answer, ensure_ascii=False)
    run['llm'] = {
        'status': 'success',
        'responses': [{'message': {'content': raw}}],
        'evidence_ids': ['ev-original', 'ev-reimplementation'],
    }
    run['llm']['interpretation'] = engine._validate_interpretation(
        run, raw, run['llm']['evidence_ids']
    )
    sample['sample_id'] = 'fixture'
    engine.store.save(run)
    engine.report(run['run_id'])
    return engine, run, sample


def test_complete_assessment_fixture_has_valid_observations_references_and_report(tmp_path):
    engine, run, sample = assessment_material(tmp_path)
    assert verifier.assess(engine, run, sample, 'fixture')['status'] == 'VERIFIED'


def test_assessment_rejects_validated_text_that_cannot_be_reconstructed_from_raw_response(tmp_path):
    engine, run, sample = assessment_material(tmp_path)
    run['llm']['validated_text'] = '{"forged":"different response"}'
    engine.store.save(run)
    assert verifier.assess(engine, run, sample, 'fixture')['status'] == 'FAILED'


def test_assessment_rejects_display_interpretation_that_differs_from_validated_response(tmp_path):
    engine, run, sample = assessment_material(tmp_path)
    run['llm']['interpretation']['summary'] = '저장된 표시 설명이 변조되었습니다.'
    engine.store.save(run)
    assert verifier.assess(engine, run, sample, 'fixture')['status'] == 'FAILED'


def test_assessment_rejects_a_run_from_a_different_binary_than_the_sample_manifest(tmp_path):
    engine, run, sample = assessment_material(tmp_path)
    run['files']['original']['sha256'] = '0' * 64
    engine.store.save(run)
    assert verifier.assess(engine, run, sample, 'fixture')['status'] == 'FAILED'


@pytest.mark.parametrize('failure', ['partial_run', 'structure', 'image'])
def test_assessment_rejects_incomplete_run_or_failed_required_pipeline_stage(tmp_path, failure):
    engine, run, sample = assessment_material(tmp_path)
    if failure == 'partial_run':
        run['status'] = 'partial'
    else:
        run['stages'][failure]['status'] = 'failed'
    engine.store.save(run)
    assert verifier.assess(engine, run, sample, 'fixture')['status'] == 'FAILED'


@pytest.mark.parametrize('failure', ['run_id', 'observed_output'])
def test_assessment_rejects_report_from_another_run_or_missing_measured_output(tmp_path, failure):
    engine, run, sample = assessment_material(tmp_path)
    report = engine.store.run_dir(run['run_id']) / 'report.html'
    text = report.read_text(encoding='utf-8')
    if failure == 'run_id':
        text = text.replace(run['run_id'], 'run_' + 'f' * 32)
    else:
        text = text.replace('한글', '누락된_관찰')
    report.write_text(text, encoding='utf-8')
    assert verifier.assess(engine, run, sample, 'fixture')['status'] == 'FAILED'
