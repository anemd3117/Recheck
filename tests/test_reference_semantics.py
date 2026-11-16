"""정확한 ID를 인용해도 실측 일치 판정을 반대로 서술하면 수락하지 않는다."""

import json

import pytest

from recheck.engine import Engine
from recheck.models import Settings


def observed_case(match):
    test = {'id': 'edge', 'args': ['17'], 'stdin': ''}
    evidence = []
    for target, output in [('original', '17\n'), ('reimplementation', '17\n' if match else '18\n')]:
        evidence.append(
            {
                'evidence_id': 'ev-' + target,
                'kind': 'execution',
                'target': target,
                'source': 'test:edge',
                'data': {
                    'test_id': 'edge',
                    'actual_input': test,
                    'stdout': output,
                    'stderr': '',
                    'exit_code': 0,
                    'status': 'success',
                },
            }
        )
    return {
        'tests': [test],
        'static': {},
        'evidence': evidence,
        'behavior': {
            'tests': [{'test_id': 'edge', 'match': match}],
            'denominator': 1,
            'matched': 1 if match else 0,
            'match_rate': 1.0 if match else 0.0,
        },
    }


def observation_answer():
    return {
        'summary': '등록 입력의 실제 관찰 결과입니다.',
        'findings': [
            {
                'finding_id': 'observed-edge',
                'claim': 'edge 테스트의 실행 결과를 비교했습니다.',
                'rationale': '양쪽 실행의 출력과 종료 상태를 관찰했습니다.',
                'targets': ['both'],
                'function_ids': [],
                'test_ids': ['edge'],
                'evidence_ids': ['ev-original', 'ev-reimplementation'],
            }
        ],
    }


def test_summary_cannot_claim_all_registered_inputs_match_when_a_mismatch_was_measured(tmp_path):
    # ID/인용 검증만으로는 실측과 반대인 요약을 차단하지 못한다.
    answer = observation_answer()
    answer['summary'] = '모든 등록 입력이 일치했습니다.'
    with pytest.raises(ValueError):
        Engine(Settings(), tmp_path)._validate_interpretation(
            observed_case(False), json.dumps(answer)
        )


def test_finding_cannot_claim_observed_output_mismatch_for_a_matching_test(tmp_path):
    # 정확한 test_id와 실행 근거를 인용하는 잘못된 관찰 주장도 거부해야 한다.
    answer = observation_answer()
    answer['findings'][0]['claim'] = 'edge 테스트에서 원본과 후보의 출력 불일치가 발생했습니다.'
    with pytest.raises(ValueError):
        Engine(Settings(), tmp_path)._validate_interpretation(
            observed_case(True), json.dumps(answer)
        )


def test_unobserved_input_hypothesis_is_distinct_from_an_observed_mismatch_claim(tmp_path):
    answer = observation_answer()
    answer['findings'][0][
        'claim'
    ] = 'edge 테스트는 일치했습니다. 관찰하지 않은 다른 입력에서는 불일치할 가능성이 있습니다.'
    result = Engine(Settings(), tmp_path)._validate_interpretation(
        observed_case(True), json.dumps(answer)
    )
    assert result['findings'][0]['verification_status'] == 'needs_verification'


def test_explicit_match_claim_cannot_reverse_a_measured_mismatch(tmp_path):
    answer = observation_answer()
    answer['findings'][0]['claim'] = 'edge 테스트의 출력이 일치했습니다.'
    with pytest.raises(ValueError):
        Engine(Settings(), tmp_path)._validate_interpretation(
            observed_case(False), json.dumps(answer)
        )


@pytest.mark.parametrize(
    'claim',
    [
        'edge 테스트에서 출력 불일치가 발생하지 않았습니다.',
        'edge 테스트에서 출력 불일치가 발생한 것은 아닙니다.',
        'edge 테스트의 일치 여부 외에 다른 입력의 불일치 가능성은 추가 확인이 필요합니다.',
    ],
)
def test_negation_or_future_uncertainty_is_not_mislabeled_as_observed_mismatch(tmp_path, claim):
    answer = observation_answer()
    answer['findings'][0]['claim'] = claim
    result = Engine(Settings(), tmp_path)._validate_interpretation(
        observed_case(True), json.dumps(answer)
    )
    assert result['findings'][0]['verification_status'] == 'needs_verification'


def test_missing_function_citation_feedback_lists_transmitted_target_id_and_evidence_choices(
    tmp_path,
):
    run = observed_case(True)
    references = []
    for target in ('original', 'reimplementation'):
        function = {'id': 'fn-shared', 'name': 'transform_value'}
        run['static'][target] = {'functions': [function]}
        run['evidence'].append(
            {
                'evidence_id': 'ev-function-' + target,
                'kind': 'function',
                'target': target,
                'data': function,
            }
        )
        references.append(
            {
                'target': target,
                'id': 'fn-shared',
                'name': 'transform_value',
                'evidence_id': 'ev-function-' + target,
            }
        )
    run['llm'] = {'context': {'evidence_data': [{'function_records': references}]}}
    answer = observation_answer()
    answer['findings'][0].update(targets=[], function_ids=['fn-shared'])
    with pytest.raises(ValueError) as caught:
        Engine(Settings(), tmp_path)._validate_interpretation(
            run, json.dumps(answer), [row['evidence_id'] for row in run['evidence']]
        )
    feedback = str(caught.value)
    assert 'fn-shared' in feedback
    assert 'ev-function-original' in feedback and 'ev-function-reimplementation' in feedback
    assert 'targets' in feedback
