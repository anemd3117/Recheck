"""Benchmark fairness and score accounting; these are controlled fixture checks."""

from scripts.run_benchmark import (
    behavior_decisions,
    check_outputs,
    evaluate_decisions,
    function_metric,
    neutral_tests,
    sample_materials,
    static_context,
)
import base64
import pytest


def test_blind_context_removes_hidden_answers():
    cases = [
        dict(
            id='boundary',
            args=['100'],
            stdin='',
            description='secret expected total=90',
            source='intentional bug',
            expected='hidden',
            timeout_seconds=3,
        )
    ]
    assert neutral_tests(cases) == [dict(id='boundary', args=['100'], stdin='', timeout_seconds=3)]
    functions = {
        'original': dict(
            functions=[
                dict(
                    id='f1', name='calculate', decompile='return x;', blocks=[], edges=[], calls=[]
                )
            ]
        ),
        'reimplementation': dict(functions=[]),
    }
    context = static_context(functions, cases)
    assert context['registered_inputs'] == neutral_tests(cases)
    assert context['binary_derived_functions']['original'][0]['function_id'] == 'original:f1'
    assert 'expected' not in str(context) and 'intentional' not in str(context)


def test_metrics_keep_inconclusive_separate_from_misses():
    truth = [
        dict(test_id='a', match=False),
        dict(test_id='b', match=False),
        dict(test_id='c', match=True),
        dict(test_id='d', match=True),
    ]
    decisions = [
        dict(test_id='a', decision='mismatch'),
        dict(test_id='b', decision='inconclusive'),
        dict(test_id='c', decision='mismatch'),
        dict(test_id='d', decision='match'),
    ]
    metrics = evaluate_decisions(decisions, truth)
    assert metrics['true_positive'] == 1 and metrics['false_negative'] == 0
    assert metrics['inconclusive'] == 1 and metrics['false_positive'] == 1
    assert metrics['missed_mismatches'] == 1 and metrics['inconclusive_mismatches'] == 1
    assert metrics['detection_rate'] == dict(numerator=1, denominator=2)
    assert metrics['false_positive_rate'] == dict(numerator=1, denominator=2)


def test_invalid_function_prediction_is_not_counted_as_correct():
    prediction = dict(predictions=[dict(candidate_function_ids=['original:f1', 'invented'])])
    static = dict(
        original=dict(functions=[dict(id='f1', name='calculate_fee')]),
        reimplementation=dict(functions=[]),
    )
    result = function_metric(prediction, static, dict(candidate_functions=['calculate_fee']))
    assert result['invalid_ids'] == ['invented'] and result['correct_candidate_count'] == 1


def test_new_pairs_have_distinct_registered_truth():
    samples = sample_materials()
    assert all(row['match'] for row in samples['equivalent_structure']['ground_truth'])
    changed = [row for row in samples['similar_structure']['ground_truth'] if not row['match']]
    assert len(changed) == 1 and changed[0]['test_id'] == 'boundary-50'
    assert changed[0]['original']['stdout'] == 'units=50\nfee=100\n'
    assert changed[0]['reimplementation']['stdout'] == 'units=50\nfee=150\n'


def test_ground_truth_checks_exact_raw_output_and_exit():
    encode = lambda value: base64.b64encode(value.encode()).decode()
    observed = dict(stdout_b64=encode('x\n'), stderr_b64='', exit_code=2)
    rows = [dict(test_id='a', original=observed, reimplementation=observed)]
    expected = dict(stdout='x\n', stderr='', exit_code=2)
    truth = [dict(test_id='a', original=expected, reimplementation=expected)]
    assert check_outputs(rows, truth)['status'] == 'VERIFIED'
    truth[0]['original'] = dict(expected, stdout='x\r\n')
    assert check_outputs(rows, truth)['status'] == 'FAILED'


def test_unmeasured_observations_remain_inconclusive():
    assert behavior_decisions(dict(tests=[dict(test_id='a', match=None)])) == [
        dict(test_id='a', decision='inconclusive')
    ]


@pytest.mark.parametrize('observed_ids,oracle_ids', [
    (['a', 'a'], ['a', 'b']),
    (['a', 'unknown'], ['a', 'b']),
    (['a', 'b'], ['a', 'a']),
    (['a', 'a'], ['a', 'a']),
])
def test_ground_truth_requires_exact_unique_input_identities(observed_ids, oracle_ids):
    observation = dict(stdout_b64='b2sK', stderr_b64='', exit_code=0)
    expected = dict(stdout='ok\n', stderr='', exit_code=0)
    rows = [dict(test_id=tid, original=observation, reimplementation=observation)
            for tid in observed_ids]
    truth = [dict(test_id=tid, original=expected, reimplementation=expected)
             for tid in oracle_ids]

    result = check_outputs(rows, truth)

    assert result['status'] == 'FAILED'
    assert result['errors']
