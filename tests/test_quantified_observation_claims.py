"""Count-limited aggregate clauses must not become individual match claims."""

import copy
import json

import pytest

from recheck.engine import Engine


def material():
    tests = [dict(id='trial-' + str(index), args=[], stdin='') for index in range(1, 8)]
    evidence = [
        dict(
            evidence_id='ev-' + test['id'] + '-' + target,
            kind='execution',
            target=target,
            data={'test_id': test['id']},
        )
        for test in tests
        for target in ('original', 'reimplementation')
    ]
    rows = [dict(test_id=test['id'], match=index < 4) for index, test in enumerate(tests)]
    run = dict(
        tests=tests,
        static={},
        evidence=evidence,
        behavior=dict(tests=rows, registered=7, denominator=7, matched=4),
    )
    value = dict(
        summary='등록 입력의 실제 관찰 범위를 설명합니다.',
        findings=[
            dict(
                finding_id='observations',
                claim='등록 입력의 실제 관찰 결과를 비교했습니다.',
                rationale='원본과 후보의 출력 및 종료 상태를 근거로 확인했습니다.',
                targets=['both'],
                function_ids=[],
                test_ids=[test['id'] for test in tests],
                evidence_ids=[row['evidence_id'] for row in evidence],
            )
        ],
    )
    return object.__new__(Engine), run, value


def validate(engine, run, value):
    return engine._validate_interpretation(
        run, json.dumps(value, ensure_ascii=False), [row['evidence_id'] for row in run['evidence']]
    )


@pytest.mark.parametrize('prefix', ['전체 ', '총 ', ''])
@pytest.mark.parametrize('unit', ['테스트', '입력', '사례'])
@pytest.mark.parametrize('relation,count', [('일치', 4), ('불일치', 3)])
def test_verified_aggregate_is_not_a_universal_or_individual_claim(prefix, unit, relation, count):
    engine, run, value = material()
    value['summary'] = (
        f'{prefix}7개 {unit} 중 {count}개가 {relation}했지만, '
        'trial-5 입력에서는 출력 불일치가 관찰되었습니다.'
    )
    before = copy.deepcopy((run, value))
    accepted = validate(engine, run, value)
    assert accepted['summary'] == value['summary']
    assert (run, value) == before


def split_findings(value, run):
    template = value['findings'][0]
    value['findings'] = []
    for index in range(5, 8):
        tid = 'trial-' + str(index)
        finding = copy.deepcopy(template)
        finding.update(
            finding_id=tid,
            test_ids=[tid],
            evidence_ids=[
                row['evidence_id'] for row in run['evidence'] if row['data']['test_id'] == tid
            ],
        )
        value['findings'].append(finding)
    return value['findings'][0]


@pytest.mark.parametrize('field', ['claim', 'rationale'])
def test_global_aggregate_does_not_bind_to_a_single_finding_fallback(field):
    engine, run, value = material()
    finding = split_findings(value, run)
    finding[field] = '총 7개 입력 중 4개가 일치했지만, trial-5 입력의 출력 불일치가 관찰되었습니다.'
    accepted = validate(engine, run, value)
    assert accepted['findings'][0][field] == finding[field]


@pytest.mark.parametrize(
    'summary',
    [
        '전체 8개 테스트 중 4개가 일치했습니다.',
        '총 7개 테스트 중 3개가 일치했습니다.',
        '전체 7개 사례 중 2개가 불일치했습니다.',
        '7개 입력 중 8개가 일치했습니다.',
        '모든 등록 입력이 일치했습니다.',
        'trial-5 입력은 일치했습니다.',
        '총 7개 테스트 중 4개가 일치했지만, trial-5 입력은 일치했습니다.',
        '전체 7개 테스트 중 4개가 일치했습니다. trial-5 입력은 일치했습니다.',
        '총 7개 테스트 중 4개가 일치했지만, 모든 등록 입력이 일치했습니다.',
        '총 7개 테스트 중 4개가 일치했습니다. 모든 테스트가 일치했습니다.',
        '총 7개 입력 중 4개가 일치했지만, trial-1 입력에서 불일치가 발생했습니다.',
        '총 7개 입력 중 4개가 일치했으며, 총 7개 입력 중 2개가 불일치했습니다.',
    ],
)
def test_wrong_counts_or_remaining_false_claims_are_still_rejected(summary):
    engine, run, value = material()
    value['summary'] = summary
    with pytest.raises(ValueError):
        validate(engine, run, value)


@pytest.mark.parametrize(
    'claim',
    [
        '총 7개 입력 중 3개가 일치했습니다.',
        '총 7개 입력 중 4개가 일치했지만, 이 입력은 일치했습니다.',
        '총 7개 입력 중 4개가 일치했습니다. trial-5 입력은 일치했습니다.',
    ],
)
def test_aggregate_mask_cannot_hide_false_finding_claim_or_single_case_fallback(claim):
    engine, run, value = material()
    finding = split_findings(value, run)
    finding['claim'] = claim
    with pytest.raises(ValueError):
        validate(engine, run, value)
