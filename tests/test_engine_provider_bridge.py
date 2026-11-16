"""Provider selection and strict Engine validation without network requests."""

import asyncio
import pytest
import copy
import json
import sys
from types import SimpleNamespace

from ai_providers.contracts import GenerationOptions, ProviderError, ProviderProfile
from recheck.engine import Engine
from recheck.models import Settings


def run(value):
    return asyncio.run(value)


def profile(**changes):
    return ProviderProfile(
        profile_id='remote',
        name='원격 연결',
        provider='openai',
        auth_mode='api_key',
        model='remote-model',
        enabled=True,
        external_consent=True,
        allowed_evidence=('runtime', 'static', 'code'),
        options=GenerationOptions(
            context_budget=16384, max_output_tokens=3072, max_requests=1, temperature=0.4
        ),
        **changes,
    )


def settings(selected=None):
    return Settings(
        model='preserved-local',
        num_ctx=4096,
        num_predict=1024,
        ai_profiles=[selected or profile()],
        active_ai_profile='remote',
    )


def record():
    return dict(
        run_id='run_' + 'a' * 32,
        attempt_id='attempt_fixture',
        settings={},
        files={},
        tests=[],
        static={},
        structure={},
        dynamic=[],
        behavior={},
        evidence=[],
        diagnostics=[],
        events=[],
        llm={},
        agent={},
        stages={},
    )


class Facade:
    def __init__(self, selected, response=None, failure=None):
        self.profile, self.calls, self.closed = selected, [], False
        self.response, self.failure = response, failure
        self.last_execution = None
        self.capability_calls = []

    async def probe(self, **kwargs):
        return {'connected': True, 'provider': 'openai', 'local': False}

    async def local_metadata(self, model, **kwargs):
        return {'model': model, 'local': False}

    async def capabilities(self, model, **kwargs):
        self.capability_calls.append(model)
        return {'basic': True, 'tools': True}

    async def chat(self, model, messages, **kwargs):
        self.calls.append((model, copy.deepcopy(kwargs)))
        if self.failure:
            raise self.failure
        return self.response or {
            'message': {
                'role': 'assistant',
                'content': '{"summary":"관찰된 자료를 설명합니다.","findings":[]}',
            },
            '_provider': {
                'provider': 'openai',
                'auth_mode': 'api_key',
                'model': 'remote-model',
                'role': 'analysis',
                'usage': {'output_tokens': 12},
            },
        }

    async def close(self):
        self.closed = True


def test_engine_keeps_an_independent_settings_snapshot(tmp_path):
    caller = settings()
    engine = Engine(caller, tmp_path)
    caller.model = 'changed-after-engine-creation'
    caller.ai_profiles.clear()
    assert engine.settings.model == 'preserved-local'
    assert len(engine.settings.ai_profiles) == 1


def test_selected_remote_options_model_and_provenance_are_used_without_rewriting_local_settings(
    tmp_path, monkeypatch
):
    selected = profile()
    engine, data = Engine(settings(selected), tmp_path), record()
    client = Facade(selected)
    monkeypatch.setattr(engine, '_client', lambda: client)
    result = run(engine._interpret(data, None))
    assert result['status'] == 'success'
    model, options = client.calls[0]
    assert model == 'remote-model'
    assert options['options'] == {
        'num_ctx': 16384,
        'num_predict': 3072,
        'temperature': 0.4,
        '_evidence_kinds': (),
    }
    assert result['context']['token_budget_heuristic']['num_ctx'] == 16384
    assert result['provider'] == 'openai' and result['usage']['output_tokens'] == 12
    assert result['provider_response']['provider'] == 'openai'
    assert (
        result['auth_mode'] == 'api_key'
        and result['profile_id'] == 'remote'
        and result['role'] == 'analysis'
    )
    assert result['profile_snapshot']['profile_id'] == 'remote'
    assert result['producer'] != 'Ollama local LLM'
    assert engine.settings.model == 'preserved-local' and engine.settings.num_ctx == 4096
    assert client.closed


def test_profile_request_budget_limits_invalid_json_repair(tmp_path, monkeypatch):
    engine, data = Engine(settings(), tmp_path), record()
    client = Facade(profile(), response={'message': {'role': 'assistant', 'content': '{broken'}})
    monkeypatch.setattr(engine, '_client', lambda: client)
    result = run(engine._interpret(data, None))
    assert result['status'] == 'failed' and len(client.calls) == 1
    assert len(result['validation_errors']) == 1


def test_typed_provider_failure_preserves_safe_response_and_is_not_repaired(tmp_path, monkeypatch):
    response = {
        'provider': 'openai',
        'auth_mode': 'api_key',
        'finish_reason': 'safety',
        'usage': {'input_tokens': 8},
    }
    engine, data = Engine(settings(), tmp_path), record()
    client = Facade(
        profile(),
        failure=ProviderError(
            'safety', '안전 정책으로 응답이 제한되었습니다.', {'provider_response': response}
        ),
    )
    monkeypatch.setattr(engine, '_client', lambda: client)
    result = run(engine._interpret(data, None))
    assert result['status'] == 'failed' and result['error_code'] == 'safety'
    assert len(client.calls) == 1 and not result.get('corrections')
    assert result['provider_response'] == response and result['usage'] == {'input_tokens': 8}


def test_profile_creation_failure_is_an_llm_result_and_does_not_escape(tmp_path, monkeypatch):
    engine, data = Engine(settings(), tmp_path), record()

    def invalid():
        raise ProviderError('profile_disabled', '선택 AI 연결은 비활성 상태입니다')

    monkeypatch.setattr(engine, '_client', invalid)
    result = run(engine._interpret(data, None))
    assert result['status'] == 'failed' and result['error_code'] == 'profile_disabled'
    assert data['evidence'][-1]['kind'] == 'llm_failure'


def test_client_factory_uses_application_credential_root_and_injected_local_client(
    tmp_path, monkeypatch
):
    import recheck.engine as module

    received = {}
    sentinel = object()

    def build(configuration, **kwargs):
        received.update(kwargs)
        return sentinel

    monkeypatch.setitem(sys.modules, 'ai_providers.factory', SimpleNamespace(build_client=build))
    monkeypatch.setattr(module, 'OllamaClient', object)
    monkeypatch.setenv('RECHECK_DATA_DIR', str(tmp_path / 'application-data'))
    engine = Engine(Settings(), tmp_path / 'result-only')
    assert engine._client() is sentinel
    assert received['root'] == tmp_path / 'application-data'
    assert received['local_factory'] is object and received['role'] == 'analysis'


def test_invalid_profile_does_not_prevent_independent_environment_tools(tmp_path, monkeypatch):
    engine = Engine(settings(), tmp_path)

    def invalid():
        raise ProviderError('profile_configuration', '선택 연결을 확인하세요')

    class Tool:
        async def probe(self):
            return {'ready': True}

        async def leftovers(self):
            return []

    monkeypatch.setattr(engine, '_client', invalid)
    monkeypatch.setattr(engine, '_docker', Tool)
    monkeypatch.setattr(engine, '_ghidra', Tool)
    result = run(engine.environment())
    assert result['ollama']['code'] == 'profile_configuration'
    assert result['docker']['ready'] and result['ghidra']['ready']


def test_remote_selection_requires_llm_even_with_no_legacy_model(tmp_path):
    selected = settings()
    selected.model = ''
    engine = Engine(selected, tmp_path)
    assert 'llm' in engine._required_stages({'mode': 'automatic'})


def test_transmitted_decompile_and_runtime_have_separate_consent_categories(tmp_path, monkeypatch):
    engine, data = Engine(settings(), tmp_path), record()
    data['evidence'] = [
        dict(
            evidence_id='ev-code',
            kind='structure',
            target='both',
            data={
                'decompile_differences': [
                    {'original': 'one', 'reimplementation': 'two', 'diff': '- old\n+ new'}
                ]
            },
        ),
        dict(evidence_id='ev-run', kind='execution', target='original', data={'stdout': 'data'}),
    ]
    client = Facade(profile())
    monkeypatch.setattr(engine, '_client', lambda: client)
    result = run(engine._interpret(data, None))
    assert result['status'] == 'success'
    assert client.calls[0][1]['options']['_evidence_kinds'] == ('runtime', 'static', 'code')


def test_remote_response_cannot_bypass_strict_citation_validation(tmp_path, monkeypatch):
    engine, data = Engine(settings(), tmp_path), record()
    response = {
        'message': {
            'role': 'assistant',
            'content': json.dumps(
                {
                    'summary': '관찰된 자료를 설명합니다.',
                    'findings': [
                        {
                            'finding_id': 'bad',
                            'claim': '아직 검증할 가설입니다.',
                            'targets': [],
                            'function_ids': [],
                            'test_ids': [],
                            'evidence_ids': ['unknown-evidence'],
                            'rationale': '검증할 근거가 필요합니다.',
                        }
                    ],
                },
                ensure_ascii=False,
            ),
        }
    }
    client = Facade(profile(), response=response)
    monkeypatch.setattr(engine, '_client', lambda: client)
    result = run(engine._interpret(data, None))
    assert result['status'] == 'failed' and len(client.calls) == 1
    assert 'interpretation' not in result and result['responses'][0] == response


@pytest.mark.parametrize('static_status', ['success', 'partial', 'failed', 'blocked'])
def test_engine_agent_uses_selected_model_snapshot_instead_of_legacy_model(
    tmp_path, monkeypatch, static_status
):
    engine, data = Engine(settings(), tmp_path), record()
    data['status'] = 'running'
    data['stages'] = {
        key: {'status': 'success'}
        for key in ('static_original', 'static_reimplementation', 'structure', 'image', 'dynamic')
    }
    for target in ('original', 'reimplementation'):
        data['stages']['static_' + target]['status'] = static_status
    client = Facade(
        profile(),
        response={'message': {'role': 'assistant', 'content': '필수 단계를 확인했습니다.'}},
    )
    engine.settings.capabilities = {
        'profile_fingerprint': client.profile.fingerprint(),
        'model': 'remote-model',
        'basic': True,
        'tools': True,
        'actual_inference_verified': True,
    }
    monkeypatch.setattr(engine, '_client', lambda: client)
    result = run(engine._agent(data, None))
    assert result['status'] == ('success' if static_status in ('success', 'partial') else 'partial')
    assert data['stages']['static_original']['status'] == static_status
    assert client.calls[0][0] == 'remote-model'
    assert result['profile_snapshot']['profile_id'] == 'remote'
    assert result['limits']['max_tokens'] == 3072
    assert client.capability_calls == []


def test_engine_agent_profile_failure_becomes_an_explicit_result(tmp_path, monkeypatch):
    engine, data = Engine(settings(), tmp_path), record()
    engine.settings.capabilities = {
        'profile_fingerprint': profile().fingerprint(),
        'model': 'remote-model',
        'basic': True,
        'tools': True,
        'actual_inference_verified': True,
    }

    def invalid():
        raise ProviderError('profile_disabled', '선택 연결이 비활성 상태입니다')

    monkeypatch.setattr(engine, '_client', invalid)
    result = run(engine._agent(data, None))
    assert result['status'] == 'failed' and result['error_code'] == 'profile_disabled'
    assert data['agent'] is result


def test_one_public_workflow_shares_request_budget_across_nested_interpretations(
    tmp_path, monkeypatch
):
    import ai_providers.factory as factory
    from ai_providers.contracts import ProviderResponse

    sent = []

    class Adapter:
        def __init__(self, selected, **kwargs):
            self.profile = selected

        async def probe(self, **kwargs):
            return {'connected': True}

        async def chat(self, request, **kwargs):
            sent.append(request)
            return ProviderResponse(
                provider='openai',
                auth_mode='api_key',
                model='remote-model',
                content='{"summary":"관찰된 자료를 설명합니다.","findings":[]}',
            )

        async def close(self):
            pass

    selected = profile(structured_mode='json_prompt')
    engine = Engine(settings(selected), tmp_path)
    monkeypatch.setattr(factory, 'HTTPProvider', Adapter)

    async def pipeline(data, *args):
        first = await engine._interpret(data, None)
        second = await engine._interpret(data, None)
        data['first_status'], data['second_status'] = first['status'], second['status']
        data['second_error'] = second.get('error_code')

    monkeypatch.setattr(engine, '_pipeline', pipeline)
    result = run(engine.analyze('unused-original', 'unused-rebuilt', [{'id': 'one'}]))
    assert result['first_status'] == 'success' and result['second_status'] == 'failed'
    assert result['second_error'] == 'request_budget' and len(sent) == 1
    result2 = run(engine.analyze('unused-original', 'unused-rebuilt', [{'id': 'two'}]))
    assert result2['first_status'] == 'success' and len(sent) == 2


def test_remote_agent_missing_or_stale_capability_cache_is_blocked_without_paid_probe(
    tmp_path, monkeypatch
):
    engine, data = Engine(settings(), tmp_path), record()
    client = Facade(profile())
    monkeypatch.setattr(engine, '_client', lambda: client)
    result = run(engine._agent(data, None))
    assert result['status'] == 'blocked' and client.capability_calls == [] and client.calls == []
    engine.settings.capabilities = {
        'profile_fingerprint': 'other-profile',
        'model': 'remote-model',
        'basic': True,
        'tools': True,
        'actual_inference_verified': True,
    }
    result = run(engine._agent(data, None))
    assert result['status'] == 'blocked' and client.capability_calls == [] and client.calls == []


def test_recovery_profile_failure_still_reaches_registered_analysis_pipeline(tmp_path, monkeypatch):
    engine, data = Engine(settings(), tmp_path), record()
    data.update(
        attempts=[],
        status='partial',
        files={'original': {'path': 'original'}, 'reimplementation': {'path': 'rebuilt'}},
    )
    engine.store.save(data)

    def invalid():
        raise ProviderError('profile_disabled', '선택 연결이 비활성 상태입니다')

    class Docker:
        async def probe(self):
            return {'ready': True}

    async def pipeline(data, *args):
        data['pipeline_reached'] = True

    monkeypatch.setattr(engine, '_client', invalid)
    monkeypatch.setattr(engine, '_docker', Docker)
    monkeypatch.setattr(engine, '_pipeline', pipeline)
    result = run(engine.recover(data['run_id']))
    assert result['pipeline_reached'] is True
