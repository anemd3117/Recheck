"""Request-only scope contract. All provider responses here are synthetic."""

import asyncio
import copy
import json

import pytest
from jsonschema import Draft202012Validator

from recheck.engine import Engine
from recheck.models import Interpretation, Settings


def record():
    test = {'id': 'toy-input', 'args': ['7'], 'stdin': ''}
    row = {'test_id': test['id'], 'input': test}
    evidence = []
    for target in ('original', 'reimplementation'):
        value = dict(
            test_id=test['id'],
            actual_input=test,
            status='success',
            stdout='14\n',
            stderr='',
            exit_code=0,
            evidence_id='ev-' + target,
        )
        row[target] = value
        evidence.append(
            dict(
                evidence_id=value['evidence_id'],
                target=target,
                kind='execution',
                source='test:toy-input',
                data=copy.deepcopy(value),
            )
        )
    return dict(
        run_id='run_' + '8' * 32,
        attempt_id='attempt_fixture',
        status='running',
        settings={},
        files={},
        tests=[test],
        static={},
        structure={},
        dynamic=[row],
        behavior={
            'tests': [{'test_id': test['id'], 'match': True}],
            'matched': 1,
            'denominator': 1,
            'match_rate': 1,
        },
        evidence=evidence,
        diagnostics=[],
        events=[],
        llm={
            'context': {
                'observations': [
                    {'test_id': test['id'], 'evidence_ids': ['ev-original', 'ev-reimplementation']}
                ],
                'evidence_data': [],
            }
        },
        agent={},
        stages={},
    )


def answer(targets):
    return dict(
        summary='등록 입력의 실제 관찰 결과를 설명합니다.',
        findings=[
            dict(
                finding_id='runtime-observation',
                claim='등록 입력의 양쪽 관찰 결과가 일치했습니다.',
                rationale='측정된 실행 결과만 비교했으며 함수의 원인은 특정하지 않았습니다.',
                targets=copy.deepcopy(targets),
                function_ids=[],
                test_ids=['toy-input'],
                evidence_ids=['ev-original', 'ev-reimplementation'],
            )
        ],
    )


def test_request_schema_requires_explicit_nonempty_targets():
    schema = Engine._interpretation_request_schema()
    target = schema['$defs']['Finding']['properties']['targets']
    assert target['minItems'] == 1 and target['default'] == ['both']
    assert 'targets' in schema['$defs']['Finding']['required']
    validator = Draft202012Validator(schema)
    assert list(validator.iter_errors(answer([])))
    missing = answer(['both'])
    del missing['findings'][0]['targets']
    assert list(validator.iter_errors(missing))
    assert list(validator.iter_errors(answer(['both']))) == []
    assert (
        list(validator.iter_errors({'summary': '추가 가설을 만들지 않았습니다.', 'findings': []}))
        == []
    )


def test_request_schema_never_mutates_persistent_schema_or_the_next_request():
    before = Interpretation.model_json_schema()
    first = Engine._interpretation_request_schema()
    first['$defs']['Finding']['properties']['targets']['default'].append('fixture-mutation')
    second = Engine._interpretation_request_schema()
    assert second['$defs']['Finding']['properties']['targets']['default'] == ['both']
    assert Interpretation.model_json_schema() == before
    assert not before['$defs']['Finding']['properties']['targets'].get('minItems')


class Client:
    def __init__(self, values):
        self.values, self.calls, self.closed = copy.deepcopy(values), [], False

    async def probe(self, **kwargs):
        return {'connected': True}

    async def local_metadata(self, model, **kwargs):
        return {'details': {'format': 'fixture'}}

    async def chat(self, model, messages, **kwargs):
        self.calls.append(
            {'model': model, 'messages': copy.deepcopy(messages), **copy.deepcopy(kwargs)}
        )
        value = self.values[min(len(self.calls) - 1, len(self.values) - 1)]
        return {'message': {'role': 'assistant', 'content': json.dumps(value, ensure_ascii=False)}}

    async def close(self):
        self.closed = True


def test_new_request_rejects_empty_scope_until_provider_explicitly_supplies_both(
    tmp_path, monkeypatch
):
    raw_empty, raw_both = answer([]), answer(['both'])
    client = Client([raw_empty, raw_both])
    engine = Engine(Settings(model='fixture-model', num_ctx=16384), tmp_path)
    monkeypatch.setattr(engine, '_client', lambda: client)
    result = asyncio.run(engine._interpret(record(), None))
    assert result['status'] == 'success'
    assert len(client.calls) == 2 and len(result['validation_errors']) == 1
    assert json.loads(result['responses'][0]['message']['content'])['findings'][0]['targets'] == []
    assert result['interpretation']['findings'][0]['targets'] == ['both']
    assert result['interpretation']['findings'][0]['function_ids'] == []
    assert client.calls[0]['format']['$defs']['Finding']['properties']['targets']['minItems'] == 1
    assert '"targets":["both"]' in client.calls[0]['messages'][0]['content']
    assert result['prompt_version'] == 'recheck-interpret-1.8'
    assert raw_empty['findings'][0]['targets'] == [] and client.closed


def test_new_request_never_inserts_scope_when_the_provider_repeats_empty_targets(
    tmp_path, monkeypatch
):
    client = Client([answer([])])
    engine = Engine(Settings(model='fixture-model', num_ctx=16384), tmp_path)
    monkeypatch.setattr(engine, '_client', lambda: client)
    result = asyncio.run(engine._interpret(record(), None))
    assert result['status'] == 'failed' and len(client.calls) == 3
    assert all(
        json.loads(r['message']['content'])['findings'][0]['targets'] == []
        for r in result['responses']
    )
    assert not result.get('interpretation') and client.closed


def test_legacy_empty_runtime_scope_remains_valid_without_normalizer_insertion():
    data, value = record(), answer([])
    raw = json.dumps(value, ensure_ascii=False)
    engine = object.__new__(Engine)
    allowed = [ev['evidence_id'] for ev in data['evidence']]
    completion = engine._complete_citations(data, raw, allowed)
    assert completion['status'] == 'completed'
    assert json.loads(completion['normalized_text'])['findings'][0]['targets'] == []
    interpreted = engine._validate_interpretation(data, raw, allowed)
    assert interpreted['findings'][0]['targets'] == []
    assert value['findings'][0]['targets'] == []


@pytest.mark.parametrize('target', [[], None, 'both'])
def test_request_scope_preflight_rejects_non_array_or_empty_values_without_changes(target):
    value = answer(target)
    before = copy.deepcopy(value)
    with pytest.raises(ValueError, match='targets'):
        Engine._require_interpretation_request_scope(json.dumps(value, ensure_ascii=False))
    assert value == before
