"""Controlled evidence/linking fixtures; no external tools or target execution."""

import asyncio
import base64
import copy
import json
from html.parser import HTMLParser

import pytest

from recheck.engine import Engine
from recheck.models import Settings
from recheck.report import render_report


@pytest.fixture
def engine(tmp_path):
    return Engine(Settings(model='gemma4:fixture', num_ctx=8192, num_predict=2048), tmp_path)


@pytest.fixture
def record():
    functions = {
        'original': {'id': 'fn-left', 'name': 'transform_value', 'evidence_id': 'ev-left'},
        'reimplementation': {
            'id': 'fn-right',
            'name': 'transform_value',
            'evidence_id': 'ev-right',
        },
    }
    test = {'id': 'boundary', 'args': ['17'], 'stdin': ''}
    outputs = {
        target: dict(
            test_id=test['id'],
            actual_input=test,
            status='success',
            stdout=str(value),
            stdout_b64=base64.b64encode(str(value).encode()).decode(),
            stderr='',
            stderr_b64='',
            exit_code=0,
            evidence_id='ev-' + target,
        )
        for target, value in [('original', 17), ('reimplementation', 18)]
    }
    evidence = [
        dict(
            evidence_id=fn['evidence_id'],
            kind='function',
            target=target,
            data=dict(fn, decompile='x' * 20000),
        )
        for target, fn in functions.items()
    ]
    evidence.extend(
        dict(
            evidence_id=out['evidence_id'],
            kind='execution',
            target=target,
            source='test:boundary',
            data=out,
        )
        for target, out in outputs.items()
    )
    structure = {
        'decompile_differences': [
            dict(
                original='fn-left',
                reimplementation='fn-right',
                original_name='transform_value',
                reimplementation_name='transform_value',
                evidence_ids=['ev-left', 'ev-right'],
                diff='- return value;\n+ return value + 1;',
            )
        ]
    }
    evidence.append(
        dict(evidence_id='ev-structure', kind='structure', target='both', data=structure)
    )
    return dict(
        run_id='run_' + 'b' * 32,
        attempt_id='attempt_fixture',
        status='success',
        files={},
        settings=dict(normalize_newlines=False, trim_trailing_whitespace=False),
        tests=[test],
        static={target: {'functions': [fn]} for target, fn in functions.items()},
        structure=structure,
        dynamic=[dict(test_id=test['id'], input=test, **outputs)],
        behavior={
            'tests': [{'test_id': 'boundary', 'match': False}],
            'matched': 0,
            'denominator': 1,
            'match_rate': 0,
        },
        evidence=evidence,
        diagnostics=[],
        events=[],
        llm={},
    )


def answer():
    return dict(
        summary='등록 입력에서 출력 차이를 관찰했습니다.',
        findings=[
            dict(
                finding_id='difference',
                claim='boundary 테스트에서 출력이 다릅니다.',
                rationale='동일한 등록 입력의 양쪽 실행을 비교했습니다.',
                targets=['both'],
                test_ids=['boundary'],
                function_ids=[],
                evidence_ids=['ev-original', 'ev-reimplementation'],
                additional_checks=['transform_value 함수의 반환 조건을 추가 확인하세요.'],
            )
        ],
    )


def linked_answer():
    value = answer()
    value['findings'][0].update(
        function_ids=['fn-left', 'fn-right'],
        evidence_ids=['ev-original', 'ev-reimplementation', 'ev-left', 'ev-right'],
    )
    return value


def test_compact_function_identity_is_supplied_when_full_record_is_omitted(engine, record):
    context = engine._context(record)
    structure = next(item for item in context['evidence_data'] if item['kind'] == 'structure')
    assert structure['function_records'] == [
        {
            'target': 'original',
            'id': 'fn-left',
            'name': 'transform_value',
            'evidence_id': 'ev-left',
        },
        {
            'target': 'reimplementation',
            'id': 'fn-right',
            'name': 'transform_value',
            'evidence_id': 'ev-right',
        },
    ]
    assert 'return value' in structure['data_excerpt']


def test_function_mentioned_only_in_recommended_check_requires_ids(engine, record):
    with pytest.raises(ValueError, match='언급한 함수'):
        engine._validate_interpretation(record, json.dumps(answer(), ensure_ascii=False))


def test_valid_function_links_preserve_hypothesis_status(engine, record):
    value = engine._validate_interpretation(record, json.dumps(linked_answer(), ensure_ascii=False))
    finding = value['findings'][0]
    assert finding['function_ids'] == ['fn-left', 'fn-right']
    assert finding['verification_status'] == 'needs_verification'
    assert record['behavior']['match_rate'] == 0


def test_known_function_without_cited_or_transmitted_identity_record_is_rejected(engine, record):
    value = linked_answer()
    value['findings'][0]['evidence_ids'].remove('ev-left')
    with pytest.raises(ValueError, match='실제 함수 evidence_id'):
        engine._validate_interpretation(record, json.dumps(value, ensure_ascii=False))


def test_target_mismatch_is_rejected_even_when_function_id_exists(engine, record):
    value = linked_answer()
    value['findings'][0].update(targets=['reimplementation'], function_ids=['fn-left'])
    with pytest.raises(ValueError, match='function_ids|실제 함수 evidence_id'):
        engine._validate_interpretation(record, json.dumps(value, ensure_ascii=False))


def test_same_address_on_both_targets_does_not_authorize_wrong_side(engine, record):
    record['static']['reimplementation']['functions'][0]['id'] = 'fn-left'
    record['evidence'][1]['data']['id'] = 'fn-left'
    value = linked_answer()
    value['findings'][0].update(targets=['reimplementation'], function_ids=['fn-left'])
    value['findings'][0]['evidence_ids'].remove('ev-right')
    with pytest.raises(ValueError, match='실제 함수 evidence_id'):
        engine._validate_interpretation(record, json.dumps(value, ensure_ascii=False))


def test_function_candidates_need_both_real_function_records(engine, record):
    value = linked_answer()
    value['function_candidates'] = [
        {
            'original': 'fn-left',
            'reimplementation': 'fn-right',
            'evidence_ids': ['ev-original'],
            'rationale': '대응 후보입니다.',
        }
    ]
    with pytest.raises(ValueError, match='양쪽 실제 함수 evidence_id'):
        engine._validate_interpretation(record, json.dumps(value, ensure_ascii=False))
    value['function_candidates'][0]['evidence_ids'] = ['ev-left', 'ev-right']
    accepted = engine._validate_interpretation(record, json.dumps(value, ensure_ascii=False))
    assert accepted['function_candidates'][0]['verification_status'] == 'needs_verification'


@pytest.mark.parametrize('direct_citation', [False, True])
def test_diff_citation_without_symbol_name_still_requires_function_link(
    engine, record, direct_citation
):
    value = answer()
    finding = value['findings'][0]
    finding['additional_checks'] = [
        '디컴파일 차이(ev-structure)의 반환 조건 변경이 불일치의 원인일 수 있습니다.'
    ]
    if direct_citation:
        finding['evidence_ids'].append('ev-structure')
        finding['additional_checks'] = ['인용한 디컴파일 차이의 반환 조건을 확인하세요.']
    with pytest.raises(ValueError, match='인용한 정적 함수'):
        engine._validate_interpretation(record, json.dumps(value, ensure_ascii=False))
    finding['function_ids'] = ['fn-left']
    finding['evidence_ids'].append('ev-left')
    accepted = engine._validate_interpretation(record, json.dumps(value, ensure_ascii=False))
    assert accepted['findings'][0]['function_ids'] == ['fn-left']


def test_diff_can_explicitly_leave_localization_unknown(engine, record):
    value = answer()
    value['findings'][0]['additional_checks'] = [
        '디컴파일 차이(ev-structure)는 관찰되었지만 관련 함수를 특정할 수 없습니다. 추가 검증이 필요합니다.'
    ]
    accepted = engine._validate_interpretation(record, json.dumps(value, ensure_ascii=False))
    assert accepted['findings'][0]['function_ids'] == []
    assert accepted['findings'][0]['verification_status'] == 'needs_verification'


class Client:
    def __init__(self, values):
        self.values = values
        self.calls = []
        self.last_execution = {'kind': 'local'}

    async def probe(self, **kwargs):
        return {'connected': True, 'version': 'fixture'}

    async def local_metadata(self, *args, **kwargs):
        return {'details': {'format': 'gguf'}}

    async def chat(self, model, messages, **kwargs):
        self.calls.append(copy.deepcopy(messages))
        return {
            'message': {
                'content': json.dumps(
                    self.values[min(len(self.calls) - 1, len(self.values) - 1)], ensure_ascii=False
                )
            },
            'done': True,
        }

    async def close(self):
        pass


def test_explicit_function_names_are_linked_once_from_transmitted_identities(
    engine, record, monkeypatch
):
    client = Client([answer(), linked_answer()])
    monkeypatch.setattr(engine, '_client', lambda: client)
    result = asyncio.run(engine._interpret(record, None))
    assert result['status'] == 'success' and len(client.calls) == 1
    assert 'ev-left' in result['evidence_ids'] and 'ev-right' in result['evidence_ids']
    assert result['interpretation']['findings'][0]['function_ids'] == ['fn-left', 'fn-right']
    assert json.loads(result['responses'][0]['message']['content']) == answer()
    completion = result['citation_completions'][0]
    assert completion['status'] == 'completed'
    assert {
        (row['target'], row['value'])
        for row in completion['additions']
        if row['field'] == 'function_ids'
    } == {('original', 'fn-left'), ('reimplementation', 'fn-right')}
    assert {row['value'] for row in completion['additions'] if row['field'] == 'evidence_ids'} == {
        'ev-left',
        'ev-right',
    }
    assert (
        engine._complete_citations(
            record, result['responses'][0]['message']['content'], result['evidence_ids']
        )['normalized_text']
        == result['validated_text']
    )
    assert 'untrusted analysis DATA' in client.calls[0][0]['content']


def test_id_inside_untrusted_stdout_is_not_supplied_evidence(engine, record, monkeypatch):
    hidden = {
        'evidence_id': 'ev-hidden',
        'kind': 'function',
        'target': 'original',
        'data': {'id': 'fn-hidden', 'name': 'hidden_function'},
    }
    record['evidence'].append(hidden)
    record['static']['original']['functions'].append(hidden['data'])
    record['dynamic'][0]['original']['stdout'] = 'This is an arbitrary string: ev-hidden'
    value = linked_answer()
    value['findings'][0]['evidence_ids'].append('ev-hidden')
    client = Client([value])
    monkeypatch.setattr(engine, '_client', lambda: client)
    result = asyncio.run(engine._interpret(record, None))
    assert result['status'] == 'failed' and len(client.calls) == 3
    assert all('evidence_ids' in error['error'] for error in result['validation_errors'])
    assert record['behavior']['denominator'] == 1


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if 'id' in attrs:
            self.ids.add(attrs['id'])
        if attrs.get('href', '').startswith('#'):
            self.hrefs.append(attrs['href'][1:])


def test_report_links_mismatch_and_finding_directly_to_real_functions(engine, record, tmp_path):
    record['llm'] = {
        'interpretation': engine._validate_interpretation(
            record, json.dumps(linked_answer(), ensure_ascii=False)
        )
    }
    text = render_report(record, tmp_path / 'report.html').read_text(encoding='utf8')
    overview = text.split('로컬 LLM 해석')[0]
    assert '원본 · transform_value [fn-left]' in overview
    assert '비교할 프로그램 · transform_value [fn-right]' in overview
    assert '원인 미확정 가설' in overview
    parser = Links()
    parser.feed(text)
    assert parser.hrefs and set(parser.hrefs) <= parser.ids


def test_report_does_not_link_missing_evidence_or_execute_user_html(record, tmp_path):
    value = linked_answer()
    value['findings'][0]['evidence_ids'].append('missing-record')
    value['summary'] = '<img src=x onerror=alert(1)>'
    record['llm'] = {'interpretation': value}
    text = render_report(record, tmp_path / 'report.html').read_text(encoding='utf8')
    assert '<img src=x' not in text and '&lt;img src=x' in text
    assert 'href="#missing-record"' not in text
    parser = Links()
    parser.feed(text)
    assert set(parser.hrefs) <= parser.ids


def test_structure_payload_is_valid_json_and_transmitted_as_native_object(
    engine, record, monkeypatch
):
    record['structure']['decompile_differences'][0]['diff'] = (
        '- return value;\n+ return value + 1;\n' * 200
    )
    context = engine._context(record)
    structure = next(item for item in context['evidence_data'] if item['kind'] == 'structure')
    assert json.loads(structure['data_excerpt']) == structure['data']
    assert structure['data']['decompile_differences'][0]['truncated'] is True
    client = Client([linked_answer()])
    monkeypatch.setattr(engine, '_client', lambda: client)
    result = asyncio.run(engine._interpret(record, None))
    assert result['status'] == 'success'
    supplied = json.loads(client.calls[0][1]['content'].splitlines()[1])
    transmitted = next(item for item in supplied['evidence_data'] if item['kind'] == 'structure')
    assert isinstance(transmitted['data'], dict) and 'data_excerpt' not in transmitted
    assert transmitted['function_records'][0]['id'] == 'fn-left'


def candidate_link_answer():
    """Model selects real functions, but cites their records only in its candidate."""
    value = answer()
    value['findings'][0].update(
        function_ids=['fn-left', 'fn-right'], verification_status='needs_verification'
    )
    value['function_candidates'] = [
        {
            'original': 'fn-left',
            'reimplementation': 'fn-right',
            'evidence_ids': ['ev-left', 'ev-right'],
            'rationale': '양쪽 함수의 정적 대응을 검토할 수 있습니다.',
        }
    ]
    return value


@pytest.mark.parametrize(
    'targets,function_ids,expected',
    [
        (
            ['both'],
            ['fn-left', 'fn-right'],
            {('original', 'fn-left', 'ev-left'), ('reimplementation', 'fn-right', 'ev-right')},
        ),
        (['original'], ['fn-left'], {('original', 'fn-left', 'ev-left')}),
        (['reimplementation'], ['fn-right'], {('reimplementation', 'fn-right', 'ev-right')}),
    ],
)
def test_validated_candidate_completes_only_selected_function_references(
    engine, record, targets, function_ids, expected
):
    value = candidate_link_answer()
    value['findings'][0].update(targets=targets, function_ids=function_ids)
    original = copy.deepcopy(value)
    metrics = copy.deepcopy(record['behavior'])
    raw = json.dumps(value, ensure_ascii=False)
    result = engine._validate_interpretation(
        record, raw, {ev['evidence_id'] for ev in record['evidence']}
    )
    finding = result['findings'][0]
    assert set(finding['evidence_ids']) == set(original['findings'][0]['evidence_ids']) | {
        eid for _, _, eid in expected
    }
    for field in (
        'claim',
        'rationale',
        'additional_checks',
        'recommended_actions',
        'targets',
        'test_ids',
        'function_ids',
        'verification_status',
    ):
        assert finding.get(field, []) == original['findings'][0].get(field, [])
    normalizations = result['reference_normalizations']
    assert {
        (row['target'], row['function_id'], row['evidence_id']) for row in normalizations
    } == expected
    assert all(
        row['finding_id'] == 'difference' and row['source'] == 'validated_function_candidate'
        for row in normalizations
    )
    assert (
        raw == json.dumps(original, ensure_ascii=False)
        and value == original
        and record['behavior'] == metrics
    )


def test_candidate_reference_normalization_preserves_original_ollama_response(
    engine, record, monkeypatch
):
    value = candidate_link_answer()
    raw = json.dumps(value, ensure_ascii=False)
    client = Client([value])
    monkeypatch.setattr(engine, '_client', lambda: client)
    metrics = copy.deepcopy(record['behavior'])
    result = asyncio.run(engine._interpret(record, None))
    assert result['status'] == 'success' and len(client.calls) == 1
    assert result['responses'][0]['message']['content'] == raw
    assert (
        result['interpretation']['findings'][0]['function_ids']
        == value['findings'][0]['function_ids']
    )
    completion = result['citation_completions'][0]
    assert {(row['target'], row['value']) for row in completion['additions']} == {
        ('original', 'ev-left'),
        ('reimplementation', 'ev-right'),
    }
    assert all(row['relation'] == 'explicit_target_function_id' for row in completion['additions'])
    assert set(json.loads(result['validated_text'])['findings'][0]['evidence_ids']) == {
        'ev-original',
        'ev-reimplementation',
        'ev-left',
        'ev-right',
    }
    assert record['behavior'] == metrics


def test_same_function_address_candidates_keep_both_target_evidence_distinct(engine, record):
    record['static']['reimplementation']['functions'][0]['id'] = 'fn-left'
    record['evidence'][1]['data']['id'] = 'fn-left'
    value = candidate_link_answer()
    value['findings'][0]['function_ids'] = ['fn-left']
    value['function_candidates'][0]['reimplementation'] = 'fn-left'
    result = engine._validate_interpretation(record, json.dumps(value, ensure_ascii=False))
    assert result['findings'][0]['function_ids'] == ['fn-left']
    assert {(row['target'], row['evidence_id']) for row in result['reference_normalizations']} == {
        ('original', 'ev-left'),
        ('reimplementation', 'ev-right'),
    }


@pytest.mark.parametrize(
    'failure', ['wrong_target_id', 'wrong_kind', 'unknown_evidence', 'nontransmitted_evidence']
)
def test_invalid_candidates_cannot_authorize_reference_normalization(engine, record, failure):
    # The finding itself has valid original-side evidence; candidate validation
    # must still reject every bad candidate instead of silently ignoring it.
    value = candidate_link_answer()
    value['findings'][0].update(targets=['original'], function_ids=['fn-left'])
    value['findings'][0]['evidence_ids'].append('ev-left')
    candidate = value['function_candidates'][0]
    allowed = {ev['evidence_id'] for ev in record['evidence']}
    if failure == 'wrong_target_id':
        candidate['original'] = 'fn-right'
    elif failure == 'wrong_kind':
        candidate['evidence_ids'] = ['ev-original', 'ev-reimplementation']
    elif failure == 'unknown_evidence':
        candidate['evidence_ids'].append('ev-invented')
    elif failure == 'nontransmitted_evidence':
        allowed.remove('ev-right')
    original = copy.deepcopy(value)
    with pytest.raises(ValueError):
        engine._validate_interpretation(record, json.dumps(value, ensure_ascii=False), allowed)
    assert value == original


@pytest.mark.parametrize(
    'targets,function_ids',
    [
        (['original'], ['fn-right']),
        (['reimplementation'], ['fn-left']),
        (['both'], []),
        ([], ['fn-left']),
    ],
)
def test_candidate_cannot_invent_function_selection_or_target_scope(
    engine, record, targets, function_ids
):
    value = candidate_link_answer()
    value['findings'][0].update(targets=targets, function_ids=function_ids)
    original = copy.deepcopy(value)
    with pytest.raises(ValueError):
        engine._validate_interpretation(record, json.dumps(value, ensure_ascii=False))
    assert value == original


def test_validated_candidate_does_not_silently_remove_unknown_finding_evidence(engine, record):
    value = candidate_link_answer()
    value['findings'][0]['evidence_ids'].append('ev-invented')
    with pytest.raises(ValueError):
        engine._validate_interpretation(record, json.dumps(value, ensure_ascii=False))


def test_already_linked_finding_is_not_recorded_as_reference_normalization(engine, record):
    value = candidate_link_answer()
    value['findings'][0]['evidence_ids'].extend(['ev-left', 'ev-right'])
    result = engine._validate_interpretation(record, json.dumps(value, ensure_ascii=False))
    assert result['reference_normalizations'] == []


def transmitted_records(engine, record):
    """Mirror the trusted identity envelopes and allowlist passed by _interpret."""
    context = engine._context(record)
    record['llm']['context'] = context
    allowed = {
        eid
        for observation in context['observations']
        for eid in observation.get('evidence_ids', [])
    }
    for item in context['evidence_data']:
        allowed.add(item['evidence_id'])
        allowed.update(ref['evidence_id'] for ref in item.get('function_records', []))
    return allowed


def selected_function_answer():
    value = answer()
    value['findings'][0]['function_ids'] = ['fn-left', 'fn-right']
    value['function_candidates'] = []
    return value


@pytest.mark.parametrize(
    'targets,function_ids,expected',
    [
        (
            ['both'],
            ['fn-left', 'fn-right'],
            {('original', 'fn-left', 'ev-left'), ('reimplementation', 'fn-right', 'ev-right')},
        ),
        (['original'], ['fn-left'], {('original', 'fn-left', 'ev-left')}),
        (['reimplementation'], ['fn-right'], {('reimplementation', 'fn-right', 'ev-right')}),
    ],
)
def test_selected_transmitted_identity_completes_only_unique_exact_reference(
    engine, record, targets, function_ids, expected
):
    allowed = transmitted_records(engine, record)
    value = selected_function_answer()
    value['findings'][0].update(targets=targets, function_ids=function_ids)
    before = copy.deepcopy(value)
    metrics = copy.deepcopy(record['behavior'])
    evidence = copy.deepcopy(record['evidence'])
    raw = json.dumps(value, ensure_ascii=False)
    result = engine._validate_interpretation(record, raw, allowed)
    finding = result['findings'][0]
    assert set(finding['evidence_ids']) == set(before['findings'][0]['evidence_ids']) | {
        eid for _, _, eid in expected
    }
    assert {
        (row['target'], row['function_id'], row['evidence_id'])
        for row in result['reference_normalizations']
    } == expected
    assert all(
        row['finding_id'] == 'difference' and row['source'] == 'selected_function_record'
        for row in result['reference_normalizations']
    )
    for field in (
        'claim',
        'rationale',
        'additional_checks',
        'recommended_actions',
        'targets',
        'test_ids',
        'function_ids',
    ):
        assert finding.get(field, []) == before['findings'][0].get(field, [])
    assert finding['verification_status'] == 'needs_verification'
    assert result['function_candidates'] == []
    assert raw == json.dumps(before, ensure_ascii=False) and value == before
    assert record['behavior'] == metrics and record['evidence'] == evidence


def test_selected_identity_normalization_retains_raw_ollama_response_without_retry(
    engine, record, monkeypatch
):
    value = selected_function_answer()
    raw = json.dumps(value, ensure_ascii=False)
    client = Client([value])
    monkeypatch.setattr(engine, '_client', lambda: client)
    result = asyncio.run(engine._interpret(record, None))
    assert result['status'] == 'success' and len(client.calls) == 1
    assert result['responses'][0]['message']['content'] == raw
    completion = result['citation_completions'][0]
    assert {(row['target'], row['value']) for row in completion['additions']} == {
        ('original', 'ev-left'),
        ('reimplementation', 'ev-right'),
    }
    assert all(row['relation'] == 'explicit_target_function_id' for row in completion['additions'])
    assert (
        engine._complete_citations(record, raw, result['evidence_ids'])['normalized_text']
        == result['validated_text']
    )
    assert result['interpretation']['function_candidates'] == []


@pytest.mark.parametrize(
    'failure',
    [
        'nontransmitted_evidence',
        'missing_compact_identity',
        'wrong_target',
        'unknown_function',
        'wrong_kind',
        'forged_name',
        'actual_record_mismatch',
        'empty_target',
        'unknown_finding_evidence',
    ],
)
def test_selected_identity_cannot_cross_reference_authority_boundaries(engine, record, failure):
    allowed = transmitted_records(engine, record)
    value = selected_function_answer()
    refs = next(
        item['function_records']
        for item in record['llm']['context']['evidence_data']
        if item.get('function_records')
    )
    if failure == 'nontransmitted_evidence':
        allowed.remove('ev-left')
    elif failure == 'missing_compact_identity':
        refs[:] = [ref for ref in refs if ref['evidence_id'] != 'ev-left']
    elif failure == 'wrong_target':
        value['findings'][0].update(targets=['reimplementation'], function_ids=['fn-left'])
    elif failure == 'unknown_function':
        value['findings'][0]['function_ids'].append('fn-invented')
    elif failure == 'wrong_kind':
        refs[0]['evidence_id'] = 'ev-original'
    elif failure == 'forged_name':
        refs[0]['name'] = 'invented_function_name'
    elif failure == 'actual_record_mismatch':
        record['evidence'][0]['data']['id'] = 'fn-not-in-static-output'
    elif failure == 'empty_target':
        value['findings'][0]['targets'] = []
    elif failure == 'unknown_finding_evidence':
        value['findings'][0]['evidence_ids'].append('ev-invented')
    before = copy.deepcopy(value)
    with pytest.raises(ValueError):
        engine._validate_interpretation(record, json.dumps(value, ensure_ascii=False), allowed)
    assert value == before


def test_ambiguous_same_address_across_both_targets_is_not_guessed(engine, record):
    record['static']['reimplementation']['functions'][0]['id'] = 'fn-left'
    record['evidence'][1]['data']['id'] = 'fn-left'
    record['structure']['decompile_differences'][0]['reimplementation'] = 'fn-left'
    allowed = transmitted_records(engine, record)
    value = selected_function_answer()
    value['findings'][0]['function_ids'] = ['fn-left']
    with pytest.raises(ValueError, match='실제 함수 evidence_id'):
        engine._validate_interpretation(record, json.dumps(value, ensure_ascii=False), allowed)
    # An explicit target removes address ambiguity without changing selection.
    value['findings'][0]['targets'] = ['reimplementation']
    result = engine._validate_interpretation(record, json.dumps(value, ensure_ascii=False), allowed)
    assert 'ev-right' in result['findings'][0]['evidence_ids']
    assert 'ev-left' not in result['findings'][0]['evidence_ids']


def test_competing_transmitted_records_for_same_target_and_function_stay_ambiguous(engine, record):
    duplicate = copy.deepcopy(record['evidence'][0])
    duplicate['evidence_id'] = 'ev-left-other'
    record['evidence'].append(duplicate)
    allowed = transmitted_records(engine, record)
    allowed.add('ev-left-other')
    refs = next(
        item['function_records']
        for item in record['llm']['context']['evidence_data']
        if item.get('function_records')
    )
    refs.append(
        dict(target='original', id='fn-left', name='transform_value', evidence_id='ev-left-other')
    )
    value = selected_function_answer()
    with pytest.raises(ValueError, match='실제 함수 evidence_id'):
        engine._validate_interpretation(record, json.dumps(value, ensure_ascii=False), allowed)


def test_identity_records_do_not_select_a_missing_function_or_fix_an_unrelated_claim(
    engine, record
):
    allowed = transmitted_records(engine, record)
    with pytest.raises(ValueError, match='언급한 함수'):
        engine._validate_interpretation(record, json.dumps(answer(), ensure_ascii=False), allowed)
    value = selected_function_answer()
    value['findings'][0]['claim'] = 'boundary 테스트의 입력 999에서 출력이 다릅니다.'
    with pytest.raises(ValueError, match='입력 주장이 실제 등록 입력과 다릅니다'):
        engine._validate_interpretation(record, json.dumps(value, ensure_ascii=False), allowed)
