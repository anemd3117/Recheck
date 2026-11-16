"""근거 전달·참조 계약 회귀 검사. 외부 도구나 사용자 바이너리를 실행하지 않는다."""

import asyncio
import copy
import json

import pytest

from recheck.engine import Engine
from recheck.models import Settings


def record(ids=('case-1', 'case-10'), mismatch=False):
    value = dict(
        run_id='run_' + 'c' * 32,
        attempt_id='attempt_fixture',
        tests=[],
        static={},
        structure={},
        dynamic=[],
        behavior=dict(tests=[], matched=0, denominator=len(ids), match_rate=0 if mismatch else 1),
        evidence=[],
        diagnostics=[],
        events=[],
        llm={},
        files={},
    )
    for tid in ids:
        test = dict(id=tid, args=['17'], stdin='')
        value['tests'].append(test)
        row = dict(test_id=tid, input=test)
        for target in ('original', 'reimplementation'):
            eid = 'ev-' + tid + '-' + target
            output = '18\n' if mismatch and target == 'reimplementation' else '17\n'
            row[target] = dict(
                test_id=tid,
                actual_input=test,
                stdout=output,
                stderr='',
                exit_code=0,
                status='success',
                evidence_id=eid,
            )
            value['evidence'].append(
                dict(
                    evidence_id=eid,
                    kind='execution',
                    target=target,
                    source='test:' + tid,
                    data=copy.deepcopy(row[target]),
                )
            )
        value['dynamic'].append(row)
        value['behavior']['tests'].append(dict(test_id=tid, match=not mismatch))
    return value


def finding(tid='case-10'):
    return dict(
        summary='등록된 입력의 실제 관찰을 확인했습니다.',
        findings=[
            dict(
                finding_id='observation',
                claim=tid + ' 테스트 결과를 관찰했습니다.',
                rationale='양쪽 실행 결과를 비교했습니다.',
                targets=['both'],
                function_ids=[],
                test_ids=[tid],
                evidence_ids=['ev-' + tid + '-original', 'ev-' + tid + '-reimplementation'],
            )
        ],
    )


def function_context(value):
    records = []
    for target, fid in [('original', 'fn-left'), ('reimplementation', 'fn-right')]:
        fn = dict(
            id=fid, name='transform_value', decompile='int transform_value(int x) { return x; }'
        )
        value['static'][target] = dict(functions=[fn])
        eid = 'ev-' + fid
        value['evidence'].append(dict(evidence_id=eid, kind='function', target=target, data=fn))
        records.append(dict(target=target, id=fid, name='transform_value', evidence_id=eid))
    value['llm']['context'] = dict(evidence_data=[dict(function_records=records)])
    return records


def test_exact_test_id_does_not_require_its_registered_prefix(tmp_path):
    # A substring scan wrongly treats case-1 as a claim about case-10.
    value = record()
    result = Engine(Settings(), tmp_path)._validate_interpretation(
        value, json.dumps(finding(), ensure_ascii=False)
    )
    assert result['findings'][0]['test_ids'] == ['case-10']


def test_test_reference_in_recommended_check_cannot_bypass_citation_validation(tmp_path):
    value = finding()
    value['findings'][0]['recommended_actions'] = ['case-1 테스트의 출력 차이를 확인하세요.']
    with pytest.raises(ValueError, match='case-1'):
        Engine(Settings(), tmp_path)._validate_interpretation(
            record(), json.dumps(value, ensure_ascii=False)
        )


def test_sentence_final_period_does_not_hide_an_uncited_test_reference(tmp_path):
    value = finding()
    value['findings'][0]['rationale'] = '추가로 관찰한 입력은 case-1.'
    with pytest.raises(ValueError, match='case-1'):
        Engine(Settings(), tmp_path)._validate_interpretation(
            record(), json.dumps(value, ensure_ascii=False)
        )


def test_longer_dotted_identifier_does_not_count_as_its_registered_prefix(tmp_path):
    value = finding()
    value['findings'][0]['rationale'] = '별도 이름 case-1.extra에 관한 가설입니다.'
    result = Engine(Settings(), tmp_path)._validate_interpretation(
        record(), json.dumps(value, ensure_ascii=False)
    )
    assert result['findings'][0]['test_ids'] == ['case-10']


def test_reference_error_identifies_missing_and_cited_test_ids(tmp_path):
    value = finding()
    value['findings'][0]['claim'] = 'case-1 및 case-10 테스트의 관찰입니다.'
    with pytest.raises(ValueError, match='case-1'):
        Engine(Settings(), tmp_path)._validate_interpretation(
            record(), json.dumps(value, ensure_ascii=False)
        )


def test_all_mismatches_are_transmitted_or_context_capacity_is_explicitly_rejected(tmp_path):
    value = record(tuple('mismatch-' + str(i) for i in range(6)), mismatch=True)
    engine = Engine(Settings(num_ctx=8192, num_predict=4096), tmp_path)
    with pytest.raises(ValueError, match='문맥 예산'):
        engine._context(value)


def test_missing_required_execution_cannot_be_replaced_with_omitted_id(tmp_path):
    value = record(('difference',), mismatch=True)
    value['dynamic'].clear()
    with pytest.raises(ValueError, match='불일치.*관찰'):
        Engine(Settings(), tmp_path)._context(value)


def test_single_oversized_mismatch_is_not_sent_over_the_context_budget(tmp_path):
    value = record(('difference',), mismatch=True)
    value['dynamic'][0]['input']['args'] = ['x' * 128 for _ in range(16)]
    with pytest.raises(ValueError, match='문맥 예산'):
        Engine(Settings(num_ctx=4096, num_predict=2048), tmp_path)._context(value)


def test_larger_context_contains_every_mismatch_and_exact_execution_pair(tmp_path):
    value = record(tuple('mismatch-' + str(i) for i in range(5)), mismatch=True)
    context = Engine(Settings(num_ctx=16384, num_predict=4096), tmp_path)._context(value)
    assert [row['test_id'] for row in context['observations']] == [
        'mismatch-0',
        'mismatch-1',
        'mismatch-2',
        'mismatch-3',
        'mismatch-4',
    ]
    assert all(
        len(row['evidence_ids']) == 2 and row['match'] is False for row in context['observations']
    )
    assert context['omitted_test_ids'] == []


def test_direct_function_excerpt_includes_complete_structured_identity(tmp_path):
    value = record(('only',))
    expected = function_context(value)
    value['evidence'] = value['evidence'][2:]
    value['dynamic'] = []
    value['structure'] = {
        'decompile_differences': [dict(evidence_ids=['ev-fn-left', 'ev-fn-right'])]
    }
    context = Engine(Settings(), tmp_path)._context(value)
    actual = [ref for item in context['evidence_data'] for ref in item.get('function_records', [])]
    assert actual == expected


def test_unique_transmitted_candidate_name_is_normalized_with_audit_record(tmp_path):
    value = record()
    function_context(value)
    answer = finding()
    answer['function_candidates'] = [
        dict(
            original='transform_value',
            reimplementation='transform_value',
            evidence_ids=['ev-fn-left', 'ev-fn-right'],
            rationale='실제로 전달된 함수의 대응 후보입니다.',
        )
    ]
    raw = json.dumps(answer, ensure_ascii=False)
    result = Engine(Settings(), tmp_path)._validate_interpretation(
        value, raw, {ev['evidence_id'] for ev in value['evidence']}
    )
    assert result['function_candidates'][0]['original'] == 'fn-left'
    assert result['function_candidates'][0]['reimplementation'] == 'fn-right'
    audit = result['reference_normalizations']
    assert [(row['target'], row['original_value'], row['function_id']) for row in audit] == [
        ('original', 'transform_value', 'fn-left'),
        ('reimplementation', 'transform_value', 'fn-right'),
    ]
    assert json.loads(raw) == answer


@pytest.mark.parametrize('failure', ['not_transmitted', 'wrong_evidence', 'ambiguous'])
def test_candidate_name_normalization_never_guesses_or_uses_unsupplied_identity(tmp_path, failure):
    value = record()
    refs = function_context(value)
    answer = finding()
    answer['function_candidates'] = [
        dict(
            original='transform_value',
            reimplementation='transform_value',
            evidence_ids=['ev-fn-left', 'ev-fn-right'],
            rationale='검증해야 할 함수 대응 후보입니다.',
        )
    ]
    if failure == 'not_transmitted':
        value['llm']['context'] = {}
    elif failure == 'wrong_evidence':
        answer['function_candidates'][0]['evidence_ids'] = [
            'ev-case-10-original',
            'ev-case-10-reimplementation',
        ]
    else:
        fn = dict(id='fn-second', name='transform_value')
        value['static']['original']['functions'].append(fn)
        value['evidence'].append(
            dict(evidence_id='ev-second', kind='function', target='original', data=fn)
        )
        refs.append(
            dict(target='original', id='fn-second', name='transform_value', evidence_id='ev-second')
        )
        answer['function_candidates'][0]['evidence_ids'].append('ev-second')
    with pytest.raises(ValueError, match='함수 후보'):
        Engine(Settings(), tmp_path)._validate_interpretation(
            value,
            json.dumps(answer, ensure_ascii=False),
            {ev['evidence_id'] for ev in value['evidence']},
        )


class LocalClient:
    """Ollama 경계만 대체하며 근거 선택·응답 검증은 실제 엔진을 사용한다."""

    def __init__(self):
        self.calls = []
        self.closed = False

    async def probe(self, **kwargs):
        return dict(connected=True)

    async def local_metadata(self, *args, **kwargs):
        return dict(details={'format': 'gguf'})

    async def chat(self, model, messages, **kwargs):
        self.calls.append(copy.deepcopy(messages))
        return dict(
            message=dict(
                content=json.dumps(
                    dict(summary='등록 입력은 모두 동작이 일치했습니다.', findings=[]),
                    ensure_ascii=False,
                )
            )
        )

    async def close(self):
        self.closed = True


def test_context_capacity_failure_is_saved_without_model_request_or_resource_leak(
    tmp_path, monkeypatch
):
    value = record(tuple('mismatch-' + str(i) for i in range(6)), mismatch=True)
    engine = Engine(Settings(model='fixture', num_ctx=8192, num_predict=4096), tmp_path)
    client = LocalClient()
    monkeypatch.setattr(engine, '_client', lambda: client)
    result = asyncio.run(engine._interpret(value, None))
    assert result['status'] == 'failed' and '문맥 예산' in result['error']
    assert result['responses'] == [] and client.calls == []


def test_all_matching_inputs_allow_empty_findings_without_invented_failure(tmp_path, monkeypatch):
    value = record()
    engine = Engine(Settings(model='fixture'), tmp_path)
    client = LocalClient()
    monkeypatch.setattr(engine, '_client', lambda: client)
    result = asyncio.run(engine._interpret(value, None))
    assert result['status'] == 'success' and result['interpretation']['findings'] == []
    assert client.closed


def test_length_stop_is_distinct_and_does_not_repeat_the_same_request_budget(tmp_path, monkeypatch):
    class LengthClient(LocalClient):
        async def chat(self, model, messages, **kwargs):
            self.calls.append(copy.deepcopy(messages))
            return dict(message=dict(content='{"summary":'), done_reason='length')

    value = record()
    engine = Engine(Settings(model='fixture'), tmp_path)
    client = LengthClient()
    monkeypatch.setattr(engine, '_client', lambda: client)
    result = asyncio.run(engine._interpret(value, None))
    assert result['status'] == 'failed' and result['error_code'] == 'output_length'
    assert len(result['responses']) == 1 and client.closed
