"""Explicit function-EID completion fixtures; no model or target is executed."""

import copy
import json

import pytest

from recheck.engine import Engine


def material():
    run_id = 'run_' + 'e' * 32
    refs = [
        dict(
            target='original', id='shared-entry', name='apricot_step', evidence_id='ev_left_fixture'
        ),
        dict(
            target='reimplementation',
            id='shared-entry',
            name='violet_join',
            evidence_id='ev_right_fixture',
        ),
    ]
    evidence = [
        dict(
            evidence_id=ref['evidence_id'],
            run_id=run_id,
            kind='function',
            target=ref['target'],
            data={'id': ref['id'], 'name': ref['name']},
        )
        for ref in refs
    ]
    evidence.append(
        dict(
            evidence_id='ev_metric_fixture', run_id=run_id, kind='behavior', target='both', data={}
        )
    )
    data = dict(
        run_id=run_id,
        tests=[],
        dynamic=[],
        structure={},
        static={
            target: {
                'functions': [
                    dict(id=ref['id'], name=ref['name']) for ref in refs if ref['target'] == target
                ]
            }
            for target in ('original', 'reimplementation')
        },
        evidence=evidence,
        behavior={'tests': []},
        llm={'context': {'evidence_data': [{'function_records': refs}]}},
    )
    return object.__new__(Engine), data, {ev['evidence_id'] for ev in evidence}


def answer():
    return dict(
        summary='등록 관찰과 함수 근거의 확인 범위를 설명합니다.',
        findings=[
            dict(
                finding_id='reference',
                claim='정적 근거의 추가 검토가 필요합니다.',
                rationale='실행 결과만으로 원인을 확정하지 않았습니다.',
                targets=['both'],
                function_ids=[],
                test_ids=[],
                evidence_ids=['ev_metric_fixture'],
                additional_checks=[
                    '정적 함수 근거(ev_left_fixture, ev_right_fixture)를 확인하세요.'
                ],
            )
        ],
    )


def complete(engine, data, allowed, value):
    raw = json.dumps(value, ensure_ascii=False)
    return raw, engine._complete_citations(data, raw, allowed)


@pytest.mark.parametrize('source', ['prose', 'structured'])
def test_explicit_function_eids_complete_only_their_unique_typed_relationships(source):
    engine, data, allowed = material()
    value = answer()
    if source == 'structured':
        value['findings'][0].update(
            evidence_ids=['ev_metric_fixture', 'ev_left_fixture', 'ev_right_fixture'],
            additional_checks=['선택한 정적 근거를 추가 검토하세요.'],
        )
    original = copy.deepcopy((data, value))
    with pytest.raises(ValueError):
        engine._validate_interpretation(data, json.dumps(value, ensure_ascii=False), allowed)
    raw, result = complete(engine, data, allowed, value)
    assert result['status'] == 'completed'
    finding = json.loads(result['normalized_text'])['findings'][0]
    assert finding['function_ids'] == ['shared-entry']
    assert set(finding['evidence_ids']) == {
        'ev_metric_fixture',
        'ev_left_fixture',
        'ev_right_fixture',
    }
    assert finding['targets'] == ['both']
    assert finding['additional_checks'] == value['findings'][0]['additional_checks']
    assert result['additions'] and all(
        a['relation'] == 'explicit_function_evidence_id' for a in result['additions']
    )
    assert all(a['evidence_reference_source'] == source for a in result['additions'])
    assert result == engine._complete_citations(data, raw, allowed)
    accepted = engine._validate_interpretation(data, result['normalized_text'], allowed)
    assert accepted['findings'][0]['verification_status'] == 'needs_verification'
    assert (data, value) == original


@pytest.mark.parametrize(
    'failure',
    [
        'untransmitted',
        'missing_typed_record',
        'duplicate_actual',
        'cross_run',
        'wrong_target',
        'empty_scope',
        'unknown_selected',
    ],
)
def test_explicit_function_evidence_cannot_bypass_authority_or_existing_bad_choices(failure):
    engine, data, allowed = material()
    value = answer()
    if failure == 'untransmitted':
        allowed.remove('ev_left_fixture')
    elif failure == 'missing_typed_record':
        data['llm']['context']['evidence_data'][0]['function_records'].pop(0)
    elif failure == 'duplicate_actual':
        data['evidence'].append(copy.deepcopy(data['evidence'][0]))
    elif failure == 'cross_run':
        data['evidence'][0]['run_id'] = 'run_' + 'f' * 32
    elif failure == 'wrong_target':
        value['findings'][0]['targets'] = ['original']
    elif failure == 'empty_scope':
        value['findings'][0]['targets'] = []
    else:
        value['findings'][0]['evidence_ids'].append('ev_unknown_fixture')
    before = copy.deepcopy((data, value))
    raw, result = complete(engine, data, allowed, value)
    assert result['status'] == 'blocked'
    assert result['normalized_text'] == raw and not result['additions'] and result['refusals']
    assert (data, value) == before


def test_evidence_prefix_or_dotted_extension_never_selects_a_real_function():
    engine, data, allowed = material()
    value = answer()
    value['findings'][0]['additional_checks'] = [
        'prefixev_left_fixture ev_left_fixture.extra ev_right_fixture-more 표기를 확인하세요.'
    ]
    raw, result = complete(engine, data, allowed, value)
    assert result['normalized_text'] == raw and not result['additions']
    assert json.loads(result['normalized_text'])['findings'][0]['function_ids'] == []
    with pytest.raises(ValueError):
        engine._validate_interpretation(data, result['normalized_text'], allowed)


def test_evidence_ids_inside_untrusted_output_do_not_authorize_function_selection():
    engine, data, allowed = material()
    data['llm']['context']['evidence_data'] = [
        dict(
            evidence_id='ev_metric_fixture',
            kind='behavior',
            data_excerpt='stdout contains ev_left_fixture and ev_right_fixture',
        )
    ]
    allowed = {'ev_metric_fixture'}
    value = answer()
    value['findings'][0]['additional_checks'] = ['등록 관찰의 범위만 추가 확인하세요.']
    raw, result = complete(engine, data, allowed, value)
    assert result['status'] == 'completed' and result['normalized_text'] == raw
    assert not result['additions']
    value['findings'][0]['additional_checks'] = ['ev_left_fixture 근거를 사용하겠습니다.']
    raw, result = complete(engine, data, allowed, value)
    assert result['status'] == 'blocked' and result['normalized_text'] == raw
    assert not result['additions'] and result['refusals']


def test_bare_shared_function_address_is_not_an_evidence_reference():
    engine, data, allowed = material()
    value = answer()
    value['findings'][0]['additional_checks'] = ['함수 주소 shared-entry 부분을 확인하세요.']
    raw, result = complete(engine, data, allowed, value)
    assert result['normalized_text'] == raw and not result['additions']


def test_one_explicit_eid_never_authorizes_the_other_targets_same_address():
    engine, data, allowed = material()
    value = answer()
    value['findings'][0]['additional_checks'] = ['ev_left_fixture 근거를 확인하세요.']
    _, result = complete(engine, data, allowed, value)
    assert result['status'] == 'completed'
    finding = json.loads(result['normalized_text'])['findings'][0]
    assert finding['function_ids'] == ['shared-entry']
    assert finding['evidence_ids'] == ['ev_metric_fixture', 'ev_left_fixture']
    assert all(a['target'] == 'original' for a in result['additions'])
    engine._validate_interpretation(data, result['normalized_text'], allowed)


def test_new_eid_completion_requires_an_explicit_same_run_record():
    engine, data, allowed = material()
    value = answer()
    data['evidence'][0].pop('run_id')
    raw, result = complete(engine, data, allowed, value)
    assert result['status'] == 'blocked' and result['normalized_text'] == raw
    assert not result['additions'] and result['refusals']


def test_one_wrong_explicit_eid_rolls_back_all_other_proposed_additions():
    engine, data, allowed = material()
    value = answer()
    other = copy.deepcopy(value['findings'][0])
    other.update(
        finding_id='wrong-side',
        targets=['original'],
        additional_checks=['ev_right_fixture 근거를 확인하세요.'],
    )
    value['findings'].append(other)
    raw, result = complete(engine, data, allowed, value)
    assert result['status'] == 'blocked' and result['normalized_text'] == raw
    assert not result['additions'] and result['refusals']
    assert result['rejected_additions']


@pytest.mark.parametrize('direct_selection', [False, True])
@pytest.mark.parametrize('targets', [['both'], ['original']])
def test_same_id_other_target_cannot_substitute_for_an_untransmitted_named_function(
    direct_selection, targets
):
    engine, data, allowed = material()
    data['llm']['context']['evidence_data'][0]['function_records'].pop(1)
    allowed.remove('ev_right_fixture')
    value = answer()
    value['findings'][0]['targets'] = targets
    value['findings'][0]['additional_checks'] = [
        'ev_left_fixture 근거와 violet_join 함수를 추가로 확인하세요.'
    ]
    if direct_selection:
        value['findings'][0].update(
            function_ids=['shared-entry'], evidence_ids=['ev_metric_fixture', 'ev_left_fixture']
        )
        text = json.dumps(value, ensure_ascii=False)
    else:
        _, completion = complete(engine, data, allowed, value)
        assert completion['status'] == 'completed'
        assert json.loads(completion['normalized_text'])['findings'][0]['function_ids'] == [
            'shared-entry'
        ]
        text = completion['normalized_text']
    with pytest.raises(ValueError, match='언급한 함수') as rejection:
        engine._validate_interpretation(data, text, allowed)
    assert 'ev_right_fixture' not in str(rejection.value)


@pytest.mark.parametrize('targets', [['both'], ['original']])
def test_same_named_function_needs_one_exact_tuple_within_declared_program_scope(targets):
    engine, data, allowed = material()
    for ref in data['llm']['context']['evidence_data'][0]['function_records']:
        ref['name'] = 'shared_symbol'
    for evidence in data['evidence'][:2]:
        evidence['data']['name'] = 'shared_symbol'
    for state in data['static'].values():
        state['functions'][0]['name'] = 'shared_symbol'
    data['llm']['context']['evidence_data'][0]['function_records'].pop(1)
    allowed.remove('ev_right_fixture')
    value = answer()
    value['findings'][0].update(
        targets=targets,
        function_ids=['shared-entry'],
        evidence_ids=['ev_metric_fixture', 'ev_left_fixture'],
        additional_checks=['shared_symbol 함수의 근거를 추가 확인하세요.'],
    )
    accepted = engine._validate_interpretation(data, json.dumps(value, ensure_ascii=False), allowed)
    assert accepted['findings'][0]['targets'] == targets
    assert accepted['findings'][0]['evidence_ids'] == ['ev_metric_fixture', 'ev_left_fixture']
