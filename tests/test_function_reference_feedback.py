"""Function-reference correction feedback; synthetic identities, no inference."""

import copy
import json

import pytest

from recheck.engine import Engine


def fixture_record():
    refs = [
        {
            'target': 'original',
            'id': 'shared-entry',
            'name': 'orbit_pack',
            'evidence_id': 'ev-function-left',
        },
        {
            'target': 'reimplementation',
            'id': 'shared-entry',
            'name': 'ripple_fold',
            'evidence_id': 'ev-function-right',
        },
    ]
    evidence = [
        dict(
            evidence_id=ref['evidence_id'],
            target=ref['target'],
            kind='function',
            data={'id': ref['id'], 'name': ref['name']},
        )
        for ref in refs
    ]
    evidence += [dict(evidence_id='ev-observation', target='both', kind='behavior', data={})]
    record = dict(
        run_id='run_fixture',
        tests=[],
        static={
            target: {
                'functions': [
                    {'id': ref['id'], 'name': ref['name']}
                    for ref in refs
                    if ref['target'] == target
                ]
            }
            for target in ('original', 'reimplementation')
        },
        evidence=evidence,
        structure={},
        behavior={'tests': []},
        llm={'context': {'evidence_data': [{'function_records': refs}]}},
    )
    return record, refs


def answer():
    return dict(
        summary='등록된 근거의 확인 범위를 설명합니다.',
        findings=[
            dict(
                finding_id='toy-observation',
                claim='등록된 관찰의 범위에서 추가 확인이 필요합니다.',
                rationale='함수 구현의 동등성은 추가로 검증해야 합니다.',
                targets=[],
                function_ids=[],
                test_ids=[],
                evidence_ids=['ev-observation'],
                additional_checks=['orbit_pack 함수를 확인하세요.'],
                recommended_actions=['ripple_fold 함수도 비교해야 합니다.'],
            )
        ],
    )


def validate(record, value, allowed):
    return object.__new__(Engine)._validate_interpretation(
        record, json.dumps(value, ensure_ascii=False), allowed
    )


def test_missing_names_report_both_target_relations_even_at_the_same_address():
    record, refs = fixture_record()
    value = answer()
    before = copy.deepcopy((record, value))
    with pytest.raises(ValueError, match='언급한 함수') as failure:
        validate(record, value, {ev['evidence_id'] for ev in record['evidence']})
    message = str(failure.value)
    # A one-name-only error leaves the other relation unavailable to correction.
    assert 'ev-function-left' in message and 'ev-function-right' in message
    payload = json.JSONDecoder().raw_decode(message[message.index('{') :])[0]
    assert payload['declared_targets'] == []
    assert payload['allowed_function_references'] == refs
    assert 'target을 targets에' in message
    assert '정확한 id를 function_ids에' in message
    assert '같은 레코드의 evidence_id를 evidence_ids에' in message
    assert '추측해서 연결하지 마세요' in message
    assert '함수명 언급을 삭제하세요' in message
    assert (record, value) == before


def test_untransmitted_named_function_still_rejects_without_disclosing_its_record():
    record, refs = fixture_record()
    with pytest.raises(ValueError, match='언급한 함수') as failure:
        validate(record, answer(), {'ev-observation', 'ev-function-left'})
    message = str(failure.value)
    assert 'ev-function-right' not in message
    assert 'ev-function-left' in message


def test_nonmentioned_same_address_record_is_not_suggested_as_correction():
    record, refs = fixture_record()
    value = answer()
    value['findings'][0]['recommended_actions'] = []
    with pytest.raises(ValueError, match='언급한 함수') as failure:
        validate(record, value, {ev['evidence_id'] for ev in record['evidence']})
    message = str(failure.value)
    assert 'ev-function-left' in message
    assert 'ev-function-right' not in message


@pytest.mark.parametrize('failure_kind', ['ambiguous', 'untransmitted', 'cross-target'])
def test_invalid_function_relationship_remains_rejected(failure_kind):
    record, refs = fixture_record()
    value = answer()
    finding = value['findings'][0]
    finding.update(
        targets=['original'],
        function_ids=['shared-entry'],
        additional_checks=['orbit_pack 함수를 확인하세요.'],
        recommended_actions=[],
    )
    allowed = {'ev-observation'}
    if failure_kind == 'ambiguous':
        extra = dict(refs[0], evidence_id='ev-function-alternative')
        refs.append(extra)
        record['evidence'].append(
            dict(
                evidence_id=extra['evidence_id'],
                target='original',
                kind='function',
                data={'id': extra['id'], 'name': extra['name']},
            )
        )
        allowed.update(('ev-function-left', 'ev-function-alternative'))
    elif failure_kind == 'cross-target':
        finding['evidence_ids'].append('ev-function-right')
        allowed.add('ev-function-right')
    before = copy.deepcopy((record, value))
    with pytest.raises(ValueError, match='실제 함수 evidence_id'):
        validate(record, value, allowed)
    assert (record, value) == before


def test_complete_function_links_still_pass_without_rewriting_the_raw_answer():
    record, refs = fixture_record()
    value = answer()
    finding = value['findings'][0]
    finding.update(
        targets=['both'],
        function_ids=['shared-entry'],
        evidence_ids=['ev-observation', 'ev-function-left', 'ev-function-right'],
    )
    original = copy.deepcopy(value)
    result = validate(record, value, {ev['evidence_id'] for ev in record['evidence']})
    assert result['findings'][0]['function_ids'] == ['shared-entry']
    assert result['findings'][0]['verification_status'] == 'needs_verification'
    assert value == original


def test_function_name_prefixes_do_not_create_a_new_reference_requirement():
    record, refs = fixture_record()
    value = answer()
    value['findings'][0].update(
        additional_checks=['preorbit_pack 및 orbit_pack_tail 확인이 필요합니다.'],
        recommended_actions=['ripple_fold9 표기를 확인하세요.'],
    )
    result = validate(record, value, {ev['evidence_id'] for ev in record['evidence']})
    assert result['findings'][0]['function_ids'] == []
    assert result['findings'][0]['targets'] == []
