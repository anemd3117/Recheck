"""모델 원문과 엄격 검증 사이의 결정적 인용 보완·전달 경계를 검사한다."""

import asyncio
import copy
import json

import pytest

from recheck.engine import Engine
from recheck.models import Settings


@pytest.fixture
def material(tmp_path):
    engine = Engine(Settings(model='fixture', num_ctx=16384, num_predict=4096), tmp_path)
    test = dict(id='edge', args=['7'], stdin='')
    value = dict(
        run_id='run_' + 'd' * 32,
        attempt_id='attempt_fixture',
        tests=[test],
        static={},
        structure={},
        dynamic=[],
        behavior=dict(
            tests=[dict(test_id='edge', match=False)], matched=0, denominator=1, match_rate=0
        ),
        evidence=[],
        diagnostics=[],
        events=[],
        llm={},
        files={},
    )
    row = dict(test_id='edge', input=test)
    for target, output in [('original', '7'), ('reimplementation', '8')]:
        result = dict(
            test_id='edge',
            actual_input=test,
            stdout=output,
            stderr='',
            exit_code=0,
            status='success',
            evidence_id='ev-' + target,
        )
        row[target] = result
        value['evidence'].append(
            dict(
                evidence_id='ev-' + target,
                kind='execution',
                target=target,
                data=copy.deepcopy(result),
            )
        )
        functions = [
            dict(
                id='caller',
                name='main',
                calls=['printf-plt'],
                decompile='int main(void) { printf("x"); }',
            ),
            dict(
                id='printf-plt',
                name='printf',
                calls=['second-hop'],
                decompile='CALLEE_BODY_MUST_STAY_PRIVATE',
            ),
            dict(id='printf-import', name='printf', calls=[], decompile='UNSELECTED_IMPORT'),
            dict(
                id='second-hop',
                name='second_hop',
                calls=[],
                decompile='SECOND_HOP_MUST_STAY_PRIVATE',
            ),
        ]
        for fn in functions:
            fn['evidence_id'] = 'ev-' + target + '-' + fn['id']
            value['evidence'].append(
                dict(
                    evidence_id=fn['evidence_id'],
                    kind='function',
                    target=target,
                    data=copy.deepcopy(fn),
                )
            )
        value['static'][target] = dict(functions=functions)
    value['dynamic'] = [row]
    difference = dict(
        original='caller',
        reimplementation='caller',
        evidence_ids=['ev-original-caller', 'ev-reimplementation-caller'],
        diff='- printf("x");\n+ printf("y");',
    )
    value['structure'] = dict(decompile_differences=[difference])
    value['evidence'].append(
        dict(
            evidence_id='ev-structure',
            kind='structure',
            target='both',
            data=copy.deepcopy(value['structure']),
        )
    )
    return engine, value


def transmitted(engine, value):
    context = engine._context(value)
    value['llm']['context'] = context
    allowed = {eid for row in context['observations'] for eid in row['evidence_ids']}
    for item in context['evidence_data']:
        allowed.add(item['evidence_id'])
        allowed.update(ref['evidence_id'] for ref in item.get('function_records', []))
    return allowed


def answer():
    return dict(
        summary='등록된 경계 입력에서 출력 차이를 관찰했습니다.',
        findings=[
            dict(
                finding_id='difference',
                claim='edge 테스트에서 양쪽 출력이 불일치했습니다.',
                rationale='main의 동작 차이는 추가 검증이 필요합니다.',
                targets=['both'],
                function_ids=['caller'],
                test_ids=['edge'],
                evidence_ids=['ev-original'],
            )
        ],
        function_candidates=[],
        suggested_tests=[],
    )


def test_selected_call_graph_supplies_only_one_hop_typed_identities_without_bodies(material):
    engine, value = material
    context = engine._context(value)
    refs = [ref for item in context['evidence_data'] for ref in item.get('function_records', [])]
    assert {(ref['target'], ref['id']) for ref in refs} == {
        ('original', 'caller'),
        ('reimplementation', 'caller'),
        ('original', 'printf-plt'),
        ('reimplementation', 'printf-plt'),
    }
    text = json.dumps(context, ensure_ascii=False)
    assert (
        'CALLEE_BODY_MUST_STAY_PRIVATE' not in text and 'SECOND_HOP_MUST_STAY_PRIVATE' not in text
    )
    assert all(set(ref) >= {'target', 'id', 'name', 'evidence_id'} for ref in refs)


def test_explicit_test_id_completes_only_its_transmitted_pair_and_preserves_raw(material):
    engine, value = material
    allowed = transmitted(engine, value)
    payload = answer()
    payload['findings'][0].update(
        function_ids=[], rationale='등록 입력의 실제 출력을 비교했습니다.'
    )
    raw = json.dumps(payload, ensure_ascii=False)
    completion = engine._complete_citations(value, raw, allowed)
    normalized = json.loads(completion['normalized_text'])
    assert normalized['findings'][0]['evidence_ids'] == ['ev-original', 'ev-reimplementation']
    assert (
        json.loads(raw) == payload
        and completion['raw_text_sha256'] != completion['normalized_text_sha256']
    )
    assert len(completion['additions']) == 1
    assert completion == engine._complete_citations(value, raw, allowed)
    assert engine._validate_interpretation(value, completion['normalized_text'], allowed)[
        'findings'
    ][0]['test_ids'] == ['edge']


def test_same_function_address_is_completed_per_explicit_target(material):
    engine, value = material
    allowed = transmitted(engine, value)
    completion = engine._complete_citations(
        value, json.dumps(answer(), ensure_ascii=False), allowed
    )
    normalized = json.loads(completion['normalized_text'])['findings'][0]
    assert set(normalized['evidence_ids']) == {
        'ev-original',
        'ev-reimplementation',
        'ev-original-caller',
        'ev-reimplementation-caller',
    }
    assert {
        (a['target'], a['value'])
        for a in completion['additions']
        if a['field'] == 'evidence_ids' and a.get('function_id') == 'caller'
    } == {('original', 'ev-original-caller'), ('reimplementation', 'ev-reimplementation-caller')}
    engine._validate_interpretation(value, completion['normalized_text'], allowed)


def test_explicit_symbol_in_code_quote_uses_only_actual_transmitted_call_target(material):
    engine, value = material
    allowed = transmitted(engine, value)
    payload = answer()
    payload['findings'][0]['rationale'] = 'main의 printf("x"); 호출이 있는 디컴파일을 관찰했습니다.'
    completion = engine._complete_citations(value, json.dumps(payload, ensure_ascii=False), allowed)
    finding = json.loads(completion['normalized_text'])['findings'][0]
    assert finding['function_ids'] == ['caller', 'printf-plt']
    assert {'ev-original-printf-plt', 'ev-reimplementation-printf-plt'} <= set(
        finding['evidence_ids']
    )
    assert not any('printf-import' in eid for eid in finding['evidence_ids'])
    engine._validate_interpretation(value, completion['normalized_text'], allowed)


@pytest.mark.parametrize(
    'mutation',
    [
        'unsupplied',
        'duplicate_observation',
        'conflicting_execution',
        'unknown_test',
        'wrong_target_execution',
    ],
)
def test_execution_completion_never_guesses_or_uses_untransmitted_records(material, mutation):
    engine, value = material
    allowed = transmitted(engine, value)
    payload = answer()
    payload['findings'][0].update(
        function_ids=[], rationale='등록 입력의 실제 출력을 비교했습니다.'
    )
    payload['findings'][0]['evidence_ids'] = []
    if mutation == 'unsupplied':
        allowed.remove('ev-reimplementation')
    elif mutation == 'duplicate_observation':
        value['llm']['context']['observations'].append(
            copy.deepcopy(value['llm']['context']['observations'][0])
        )
    elif mutation == 'conflicting_execution':
        duplicate = copy.deepcopy(value['dynamic'][0])
        duplicate['original']['evidence_id'] = 'ev-other'
        value['dynamic'].append(duplicate)
    elif mutation == 'unknown_test':
        payload['findings'][0]['test_ids'] = ['invented']
    else:
        value['evidence'][0]['target'] = 'reimplementation'
    completion = engine._complete_citations(value, json.dumps(payload, ensure_ascii=False), allowed)
    assert json.loads(completion['normalized_text'])['findings'][0]['evidence_ids'] == []
    with pytest.raises(ValueError):
        engine._validate_interpretation(value, completion['normalized_text'], allowed)


@pytest.mark.parametrize(
    'mutation',
    [
        'empty_targets',
        'wrong_target',
        'unsupplied',
        'ambiguous_name',
        'unknown_function',
        'unknown_evidence',
    ],
)
def test_function_completion_preserves_rejection_boundaries(material, mutation):
    engine, value = material
    allowed = transmitted(engine, value)
    payload = answer()
    finding = payload['findings'][0]
    if mutation == 'empty_targets':
        finding['targets'] = []
    elif mutation == 'wrong_target':
        finding.update(targets=['original'], function_ids=['right-only'])
        value['static']['reimplementation']['functions'].append(
            dict(id='right-only', name='right_only')
        )
    elif mutation == 'unsupplied':
        allowed.remove('ev-original-caller')
    elif mutation == 'ambiguous_name':
        finding.update(function_ids=[], rationale='printf 함수를 추가 검토해야 합니다.')
        item = value['llm']['context']['evidence_data'][0]
        item.setdefault('function_records', []).append(
            dict(
                target='original',
                id='printf-import',
                name='printf',
                evidence_id='ev-original-printf-import',
            )
        )
        allowed.add('ev-original-printf-import')
    elif mutation == 'unknown_function':
        finding['function_ids'].append('invented-function')
    else:
        finding['evidence_ids'].append('invented-evidence')
    raw = json.dumps(payload, ensure_ascii=False)
    completion = engine._complete_citations(value, raw, allowed)
    normalized = json.loads(completion['normalized_text'])['findings'][0]
    assert set(finding['function_ids']) <= set(normalized['function_ids'])
    assert set(finding['evidence_ids']) <= set(normalized['evidence_ids'])
    assert completion['status'] == 'blocked' and completion['normalized_text'] == raw
    assert completion['refusals'] and not completion['additions']


def test_one_cited_side_does_not_disambiguate_an_unsupplied_other_side_of_same_address(material):
    engine, value = material
    allowed = transmitted(engine, value)
    payload = answer()
    payload['findings'][0]['evidence_ids'].append('ev-original-caller')
    allowed.remove('ev-reimplementation-caller')
    completion = engine._complete_citations(value, json.dumps(payload, ensure_ascii=False), allowed)
    assert completion['status'] == 'blocked' and not completion['additions']
    assert (
        'ev-reimplementation-caller'
        not in json.loads(completion['normalized_text'])['findings'][0]['evidence_ids']
    )


def test_no_name_reference_means_no_implicit_callee_selection(material):
    engine, value = material
    allowed = transmitted(engine, value)
    completion = engine._complete_citations(
        value, json.dumps(answer(), ensure_ascii=False), allowed
    )
    assert json.loads(completion['normalized_text'])['findings'][0]['function_ids'] == ['caller']


def test_call_metadata_respects_context_budget_instead_of_silently_overfilling(material):
    engine, value = material
    for target in ('original', 'reimplementation'):
        caller = value['static'][target]['functions'][0]
        caller['calls'] = []
        evidence = next(
            ev for ev in value['evidence'] if ev['evidence_id'] == 'ev-' + target + '-caller'
        )
        for index in range(80):
            fid = 'long-callee-' + str(index)
            caller['calls'].append(fid)
            fn = dict(
                id=fid,
                name='callee_' + str(index),
                calls=[],
                evidence_id='ev-' + target + '-' + fid,
            )
            value['static'][target]['functions'].append(fn)
            value['evidence'].append(
                dict(
                    evidence_id=fn['evidence_id'],
                    kind='function',
                    target=target,
                    data=copy.deepcopy(fn),
                )
            )
        evidence['data']['calls'] = caller['calls']
    with pytest.raises(ValueError, match='문맥 예산'):
        engine._context(value)


def test_interpret_stores_reproducible_completion_beside_untouched_provider_response(
    material, monkeypatch
):
    engine, value = material
    raw = json.dumps(answer(), ensure_ascii=False)

    class Client:
        async def probe(self, **kwargs):
            return dict(connected=True)

        async def local_metadata(self, *args, **kwargs):
            return dict(details={'format': 'gguf'})

        async def chat(self, *args, **kwargs):
            return dict(message=dict(content=raw), done=True)

        async def close(self):
            pass

    monkeypatch.setattr(engine, '_client', lambda: Client())
    result = asyncio.run(engine._interpret(value, None))
    assert result['status'] == 'success' and len(result['responses']) == 1
    assert result['responses'][0]['message']['content'] == raw
    assert result['validated_text'] != raw and len(result['citation_completions']) == 1
    replay = engine._complete_citations(value, raw, result['evidence_ids'])
    assert replay['normalized_text'] == result['validated_text']
    assert replay == {
        key: item for key, item in result['citation_completions'][0].items() if key != 'attempt'
    }


def add_observation(value, test_id):
    row = copy.deepcopy(value['dynamic'][0])
    row['test_id'] = test_id
    row['input']['id'] = test_id
    value['tests'].append(copy.deepcopy(row['input']))
    value['behavior']['tests'].append(dict(test_id=test_id, match=False))
    for target in ('original', 'reimplementation'):
        result = row[target]
        result.update(test_id=test_id, evidence_id='ev-' + test_id + '-' + target)
        result['actual_input']['id'] = test_id
        value['evidence'].append(
            dict(
                evidence_id=result['evidence_id'],
                kind='execution',
                target=target,
                data=copy.deepcopy(result),
            )
        )
    value['dynamic'].append(row)


@pytest.mark.parametrize(
    'field', ['claim', 'rationale', 'additional_checks', 'recommended_actions']
)
def test_exact_test_id_in_any_finding_text_is_completed_only_from_its_unique_observation(
    material, field
):
    engine, value = material
    add_observation(value, 'other')
    allowed = transmitted(engine, value)
    payload = answer()
    finding = payload['findings'][0]
    finding[field] = (
        ['other.']
        if field in ('additional_checks', 'recommended_actions')
        else finding[field] + ' 추가 관찰은 other.'
    )
    raw = json.dumps(payload, ensure_ascii=False)
    completion = engine._complete_citations(value, raw, allowed)
    normalized = json.loads(completion['normalized_text'])['findings'][0]
    assert completion['status'] == 'completed' and normalized['test_ids'] == ['edge', 'other']
    assert {'ev-other-original', 'ev-other-reimplementation'} <= set(normalized['evidence_ids'])
    assert [
        row['value']
        for row in completion['additions']
        if row['relation'] == 'explicit_mentioned_test_id'
    ] == ['other']
    assert json.loads(raw) == payload
    engine._validate_interpretation(value, completion['normalized_text'], allowed)


def test_exact_test_token_does_not_select_its_prefix_or_dotted_extension(material):
    engine, value = material
    add_observation(value, 'edge-10')
    add_observation(value, 'edge-10.extra')
    allowed = transmitted(engine, value)
    payload = answer()
    finding = payload['findings'][0]
    finding.update(
        claim='등록 결과에서 차이를 관찰했습니다.',
        test_ids=[],
        evidence_ids=[],
        additional_checks=['edge-10.'],
    )
    completion = engine._complete_citations(value, json.dumps(payload, ensure_ascii=False), allowed)
    assert json.loads(completion['normalized_text'])['findings'][0]['test_ids'] == ['edge-10']
    assert not {
        'ev-original',
        'ev-reimplementation',
        'ev-edge-10.extra-original',
        'ev-edge-10.extra-reimplementation',
    } & set(json.loads(completion['normalized_text'])['findings'][0]['evidence_ids'])


@pytest.mark.parametrize(
    'mutation', ['not_transmitted', 'ambiguous_observation', 'missing_execution']
)
def test_mentioned_test_cannot_authorize_missing_or_ambiguous_execution_relation(
    material, mutation
):
    engine, value = material
    add_observation(value, 'other')
    allowed = transmitted(engine, value)
    payload = answer()
    payload['findings'][0]['additional_checks'] = ['other']
    if mutation == 'not_transmitted':
        value['llm']['context']['observations'] = [
            row for row in value['llm']['context']['observations'] if row['test_id'] != 'other'
        ]
    elif mutation == 'ambiguous_observation':
        value['llm']['context']['observations'].append(
            copy.deepcopy(value['llm']['context']['observations'][1])
        )
    else:
        allowed.remove('ev-other-reimplementation')
    raw = json.dumps(payload, ensure_ascii=False)
    completion = engine._complete_citations(value, raw, allowed)
    assert (
        completion['status'] == 'blocked'
        and completion['normalized_text'] == raw
        and not completion['additions']
    )


def test_unrelated_execution_citation_never_infers_a_new_test_selection(material):
    engine, value = material
    add_observation(value, 'other')
    allowed = transmitted(engine, value)
    payload = answer()
    payload['findings'][0]['evidence_ids'].extend(
        ['ev-other-original', 'ev-other-reimplementation']
    )
    completion = engine._complete_citations(value, json.dumps(payload, ensure_ascii=False), allowed)
    assert json.loads(completion['normalized_text'])['findings'][0]['test_ids'] == ['edge']
    with pytest.raises(ValueError, match='other'):
        engine._validate_interpretation(value, completion['normalized_text'], allowed)


def test_known_prose_test_does_not_remove_an_existing_invalid_selection(material):
    engine, value = material
    allowed = transmitted(engine, value)
    payload = answer()
    payload['findings'][0]['test_ids'] = ['invented']
    raw = json.dumps(payload, ensure_ascii=False)
    completion = engine._complete_citations(value, raw, allowed)
    assert completion['status'] == 'blocked' and completion['normalized_text'] == raw
