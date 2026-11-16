"""Fixture checks for preserving failures and validating linked function candidates."""

from scripts.run_benchmark_revision import execution_record, function_localization


def test_failed_interpretation_keeps_verified_local_execution():
    failed = dict(
        status='failed',
        responses=[dict(_execution=dict(kind='local', digest='abc', model='local:model'))],
    )
    record = execution_record(failed)
    assert record['status'] == 'VERIFIED' and record['source'] == 'responses[0]._execution'
    assert record['data']['digest'] == 'abc'
    assert '해석 JSON 검증 성공을 의미하지 않음' in record['limitation']


def test_missing_execution_is_explicitly_unverified():
    assert execution_record(dict(status='failed'))['status'] == 'NOT VERIFIED'


def test_function_localization_requires_matching_ids_and_cited_evidence():
    pair = dict(
        candidate_functions=['calculate_fee'], ground_truth=[dict(test_id='threshold', match=False)]
    )
    result = dict(
        evidence=[
            dict(
                evidence_id='f',
                kind='function',
                target='original',
                data=dict(id='id1', name='calculate_fee'),
            )
        ],
        llm=dict(
            interpretation=dict(
                findings=[dict(function_ids=['id1'], evidence_ids=['f'], test_ids=['threshold'])]
            )
        ),
    )
    metric = function_localization(result, pair)
    assert metric['precision'] == dict(numerator=1, denominator=1)
    assert metric['mismatch_test_localization']['numerator'] == 1
    result['llm']['interpretation']['findings'][0]['function_ids'] = []
    metric = function_localization(result, pair)
    assert (
        metric['cited_candidate_count'] == 0
        and metric['mismatch_test_localization']['numerator'] == 0
    )
