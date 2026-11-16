"""외부 인증·추론 없이 factory, 예산, 기존 API의 실제 전달 계약을 검증한다."""

import asyncio
import importlib
import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from ai_providers.contracts import (
    GenerationOptions,
    Message,
    ModelDescriptor,
    ProviderError,
    ProviderProfile,
    ProviderRequest,
    ProviderResponse,
    ToolCall,
)


def factory():
    module = importlib.import_module('ai_providers.factory')
    assert hasattr(module, 'create_provider'), 'create_provider 구현이 필요합니다'
    return module


def profile(**kwargs):
    values = dict(
        provider='compatible',
        auth_mode='none',
        endpoint='https://fixture.invalid/v1',
        model='chosen',
        enabled=True,
        external_consent=True,
        allowed_evidence=('runtime', 'static', 'code'),
    )
    values.update(kwargs)
    return ProviderProfile(**values)


class Native:
    def __init__(self, base_url, timeout=120):
        self.calls, self.closed = [], False

    async def probe(self, cancel=None):
        return {'connected': True}

    async def models(self, cancel=None):
        return [{'name': 'chosen', 'digest': 'digest'}]

    async def local_metadata(self, model, cancel=None):
        return {'details': {'format': 'gguf'}, 'digest': 'digest'}

    async def ensure_server(self, **kwargs):
        return {'connected': True}

    async def chat(self, model, messages, **kwargs):
        self.calls.append((model, messages, kwargs))
        return {
            'message': {'content': 'ok'},
            'done_reason': 'stop',
            '_execution': {'kind': 'local', 'digest': 'digest'},
        }

    async def close(self):
        self.closed = True


def answer(content='ok', finish='stop'):
    return httpx.Response(
        200,
        json={
            'id': 'request-fixture',
            'model': 'chosen',
            'choices': [
                {'message': {'role': 'assistant', 'content': content}, 'finish_reason': finish}
            ],
            'usage': {'prompt_tokens': 3, 'completion_tokens': 2},
        },
    )


@pytest.mark.asyncio
async def test_budget_failed_attempt_is_charged_once_without_retry():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, json={'error': {'message': 'slow down'}})

    p = factory().create_provider(profile(), transport=httpx.MockTransport(handler))
    request = ProviderRequest(
        messages=(Message(role='user', content='synthetic'),), purpose='probe'
    )
    with pytest.raises(ProviderError) as first:
        await p.chat(request)
    assert first.value.code == 'rate_limit'
    with pytest.raises(ProviderError) as second:
        await p.chat(request)
    assert second.value.code == 'request_budget' and len(calls) == 1 and p.requests_used == 1
    await p.close()


@pytest.mark.asyncio
async def test_local_factory_needs_no_credential_store_and_keeps_native(tmp_path):
    p = factory().create_provider(
        profile(provider='ollama', endpoint='http://127.0.0.1:11434'),
        root=tmp_path,
        local_factory=Native,
    )
    assert isinstance(p.native, Native) and not (tmp_path / 'credentials').exists()
    result = await p.chat(ProviderRequest(messages=(Message(role='user', content='fixture'),)))
    assert result.content == 'ok' and result.provenance['budget']['used'] == 1
    await p.close()
    assert p.native.closed


@pytest.mark.asyncio
async def test_explicit_environment_binding_snapshot_change_stops_before_wire(monkeypatch):
    monkeypatch.setenv('REBUILD_TEST_EXPLICIT_KEY', 'fixture-key-one')
    calls = []
    p = factory().create_provider(
        profile(
            auth_mode='api_key',
            credential_ref='env:REBUILD_TEST_EXPLICIT_KEY',
            credential_origin='https://fixture.invalid',
            credential_version=1,
            options=GenerationOptions(max_requests=2),
        ),
        transport=httpx.MockTransport(lambda r: calls.append(r) or answer()),
    )
    req = ProviderRequest(messages=(Message(role='user', content='fixture'),), purpose='probe')
    await p.chat(req)
    monkeypatch.setenv('REBUILD_TEST_EXPLICIT_KEY', 'fixture-key-two')
    with pytest.raises(ProviderError) as error:
        await p.chat(req)
    assert error.value.code == 'credentials_changed' and len(calls) == 1
    assert 'fixture-key' not in str(error.value) + json.dumps(error.value.details)
    await p.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'kind,version,code',
    [('oauth_google', 3, 'credential_kind'), ('api_key', 4, 'credentials_changed')],
)
async def test_vault_api_key_kind_and_version_checked_before_wire(kind, version, code):
    class Store:
        def get(self, ref, owner, origin):
            return SimpleNamespace(
                ref=ref,
                owner=owner,
                origin=origin,
                kind=kind,
                version=version,
                value=SecretStr('fixture-secret'),
            )

    calls = []
    p = factory().create_provider(
        profile(auth_mode='api_key', credential_ref='cred_' + 'a' * 32, credential_version=3),
        store=Store(),
        transport=httpx.MockTransport(lambda r: calls.append(r) or answer()),
    )
    with pytest.raises(ProviderError) as error:
        await p.chat(
            ProviderRequest(messages=(Message(role='user', content='fixture'),), purpose='probe')
        )
    assert error.value.code == code and not calls
    await p.close()


@pytest.mark.asyncio
async def test_oauth_refresh_keeps_logical_binding_but_records_actual_revision():
    origin = 'https://api.openai.com'
    tokens = dict(
        schema=1,
        provider='chatgpt',
        client_id='registered',
        account={'issuer': 'https://auth.openai.com', 'subject': 'same-user'},
        access_token='fixture-oauth-token',
        scopes=['chatgpt.tokens.use.direct'],
        token_type='Bearer',
        expires_at=9999999999,
    )

    class Store:
        def get(self, ref, owner, target):
            assert target == origin
            return SimpleNamespace(
                ref=ref,
                owner=owner,
                origin=target,
                kind='oauth_chatgpt',
                version=7,
                value=SecretStr(json.dumps(tokens)),
            )

    class OAuth:
        async def refresh(self, provider, owner, ref, config, cancel=None):
            assert provider == 'chatgpt' and config.expected_subject == 'same-user'
            return self.account_status(ref, owner, origin)

        def account_status(self, ref, owner, origin):
            return dict(
                authenticated=True,
                provider='chatgpt',
                client_id='registered',
                credential_ref=ref,
                credential_version=7,
                account=tokens['account'],
                scopes=tokens['scopes'],
                inference_authorized=True,
                expired=False,
            )

    # Credential lookup is independently exercised; no ChatGPT request is sent.
    p = factory().create_provider(
        profile(
            provider='openai',
            endpoint='https://api.openai.com/v1',
            auth_mode='chatgpt',
            credential_ref='cred_' + 'a' * 32,
            credential_version=1,
            oauth_client_id='registered',
            oauth_subject='same-user',
        ),
        store=Store(),
        oauth_service=OAuth(),
    )
    material = await p.adapter.credential_getter(p.profile)
    assert (
        material.version == 1
        and material.token_revision == 7
        and material.value.get_secret_value() == tokens['access_token']
    )
    tokens['account']['subject'] = 'changed-user'
    with pytest.raises(ProviderError) as error:
        await p.adapter.credential_getter(p.profile)
    assert error.value.code == 'credentials_changed'
    await p.close()


def facade(p):
    return factory().LegacyClientFacade(p, role='analysis')


@pytest.mark.asyncio
async def test_facade_evidence_scope_required_and_accumulated_and_options_frozen():
    seen = []
    p = factory().create_provider(
        profile(options=GenerationOptions(max_requests=3)),
        transport=httpx.MockTransport(lambda r: seen.append(json.loads(r.content)) or answer()),
    )
    client = facade(p)
    with pytest.raises(ProviderError) as no_scope:
        await client.chat('chosen', [{'role': 'user', 'content': 'fixture'}])
    assert no_scope.value.code == 'evidence_scope_required' and p.requests_used == 0
    with pytest.raises(ProviderError) as options:
        await client.chat(
            'chosen', [], options={'num_predict': 4096, '_evidence_kinds': ['runtime']}
        )
    assert options.value.code == 'profile_changed' and p.requests_used == 0
    result = await client.chat(
        'chosen', [{'role': 'user', 'content': 'fixture'}], options={'_evidence_kinds': ['runtime']}
    )
    assert (
        result['_provider']['request_id'] == 'request-fixture' and result['done_reason'] == 'stop'
    )
    client.profile.authorize(('static',), purpose='analysis')
    await client.chat('chosen', [], options={'_evidence_kinds': ['static']})
    assert client.evidence_kinds == ('runtime', 'static') and '_evidence_kinds' not in json.dumps(
        seen
    )
    await client.close()


@pytest.mark.asyncio
async def test_facade_unsupported_openai_native_schema_is_free_and_never_falls_back():
    calls = []
    p = factory().create_provider(
        profile(provider='openai', endpoint='https://api.openai.com/v1', auth_mode='api_key'),
        transport=httpx.MockTransport(lambda r: calls.append(r) or answer()),
    )
    client = facade(p)
    with pytest.raises(ProviderError) as error:
        await client.chat(
            'chosen',
            [],
            format={'type': 'object', 'properties': {'summary': {'type': 'string'}}},
            options={'_evidence_kinds': []},
        )
    assert error.value.code == 'schema_unsupported' and p.requests_used == 0 and not calls
    assert error.value.details['available_mode'] == 'json_prompt'
    await client.close()


@pytest.mark.asyncio
async def test_facade_finish_failure_is_typed_and_preserves_redacted_response():
    p = factory().create_provider(
        profile(), transport=httpx.MockTransport(lambda r: answer('{"partial":', 'length'))
    )
    client = facade(p)
    with pytest.raises(ProviderError) as error:
        await client.chat('chosen', [], options={'_evidence_kinds': []})
    assert (
        error.value.code == 'length'
        and error.value.details['provider_response']['raw_finish_reason'] == 'length'
    )
    assert client.last_execution['provider'] == 'compatible'
    await client.close()


@pytest.mark.asyncio
async def test_facade_exact_tool_call_id_survives_round_trip_and_nonstrict_policy():
    seen = []
    p = factory().create_provider(
        profile(options=GenerationOptions(max_requests=2)),
        transport=httpx.MockTransport(lambda r: seen.append(json.loads(r.content)) or answer()),
    )
    client = facade(p)
    await client.chat(
        'chosen',
        [
            {
                'role': 'assistant',
                'content': '',
                'tool_calls': [
                    {
                        'id': 'original-id',
                        'type': 'function',
                        'function': {'name': 'status', 'arguments': {'value': 1}},
                    }
                ],
            },
            {
                'role': 'tool',
                'tool_call_id': 'original-id',
                'tool_name': 'status',
                'content': '{"ready":true}',
            },
        ],
        tools=[
            {
                'type': 'function',
                'function': {
                    'name': 'status',
                    'parameters': {'type': 'object', 'properties': {'value': {'type': 'integer'}}},
                },
            }
        ],
        options={'_evidence_kinds': ['runtime']},
    )
    assert seen[0]['messages'][1]['tool_call_id'] == 'original-id'
    assert seen[0]['tools'][0]['function']['strict'] is False
    await client.close()


@pytest.mark.asyncio
async def test_capability_probe_separate_budget_nonce_identity_and_no_complex_schema_claim():
    class Probe:
        profile = profile(options=GenerationOptions(max_requests=4, max_output_tokens=256))
        requests_used = 0

        async def probe(self, cancel=None):
            return {'connected': True, 'authenticated': False, 'inference_verified': False}

        async def chat(self, request, **kwargs):
            self.requests_used += 1
            text = request.messages[-1].content
            nonce = text.split('NONCE=')[-1].split()[0]
            if request.tools:
                return ProviderResponse(
                    provider='compatible',
                    auth_mode='none',
                    finish_reason='tool_calls',
                    tool_calls=(
                        ToolCall(
                            id='probe-exact-id',
                            name=request.tools[0].name,
                            arguments={'nonce': nonce},
                        ),
                    ),
                )
            if any(m.role == 'tool' for m in request.messages):
                assert request.messages[-2].tool_call_id == 'probe-exact-id'
            content = (
                json.dumps({'ready': True, 'nonce': nonce}) if request.output_schema else nonce
            )
            return ProviderResponse(provider='compatible', auth_mode='none', content=content)

    p = Probe()
    result = await factory().probe_capabilities(p)
    assert (
        result['basic_chat']
        and result['json_schema']
        and result['tool_calls']
        and result['tool_roundtrip']
    )
    assert result['interpretation_schema_supported'] is False and p.requests_used == 4
    assert result['budget']['max_requests'] == 4 and result['budget']['max_output_tokens'] <= 256
    assert result['basic'] and result['tools']
    assert result['structured_output']['schema_scope'] == 'probe_ready_nonce_only'


@pytest.mark.asyncio
async def test_capability_missing_model_is_free():
    calls = []
    p = factory().create_provider(
        profile(model=''), transport=httpx.MockTransport(lambda r: calls.append(r) or answer())
    )
    with pytest.raises(ProviderError) as error:
        await factory().probe_capabilities(p)
    assert error.value.code == 'model_required' and not calls and p.requests_used == 0
    await p.close()


@pytest.mark.asyncio
async def test_build_client_role_snapshot_and_local_legacy_apis(tmp_path):
    from recheck.models import Settings

    chosen = profile(
        profile_id='generation-choice',
        provider='ollama',
        endpoint='http://127.0.0.1:11434',
        options=GenerationOptions(context_budget=16384, max_output_tokens=3072, temperature=0.4),
    )
    settings = Settings(
        model='legacy',
        ai_profiles=[chosen],
        ai_role_profiles={'generation': chosen.profile_id, 'review': 'legacy-local'},
    )
    client = factory().build_client(
        settings, role='generation', root=tmp_path, local_factory=Native
    )
    settings.ai_profiles = []
    assert client.profile.profile_id == chosen.profile_id and client.role == 'generation'
    assert (await client.models())[0]['digest'] == 'digest'
    assert (await client.local_metadata('chosen'))['details']['format'] == 'gguf'
    assert (await client.ensure_server())['connected']
    result = await client.chat(
        'chosen',
        [{'role': 'user', 'content': 'fixture'}],
        options={'num_ctx': 16384, 'num_predict': 3072, 'temperature': 0.4},
    )
    assert (
        result['_provider']['provenance']['role'] == 'generation'
        and result['_execution']['kind'] == 'local'
    )
    assert client.native.calls[0][2]['options']['num_predict'] == 3072
    assert not (tmp_path / 'credentials').exists()
    await client.close()


@pytest.mark.asyncio
async def test_capability_clone_does_not_spend_job_budget_and_uses_256_output_ceiling():
    seen = []

    def handler(request):
        if request.url.path.endswith('/models'):
            return httpx.Response(200, json={'data': [{'id': 'chosen'}]})
        wire = json.loads(request.content)
        seen.append(wire)
        nonce = wire['messages'][-1]['content'].split('NONCE=')[-1].split()[0]
        if wire.get('tools'):
            return httpx.Response(
                200,
                json={
                    'choices': [
                        {
                            'finish_reason': 'tool_calls',
                            'message': {
                                'content': None,
                                'tool_calls': [
                                    {
                                        'id': 'wire-original-id',
                                        'type': 'function',
                                        'function': {
                                            'name': 'rebuild_probe_status',
                                            'arguments': json.dumps({'nonce': nonce}),
                                        },
                                    }
                                ],
                            },
                        }
                    ]
                },
            )
        return answer(
            json.dumps({'ready': True, 'nonce': nonce}) if wire.get('response_format') else nonce
        )

    p = factory().create_provider(
        profile(options=GenerationOptions(max_requests=1, max_output_tokens=4096)),
        transport=httpx.MockTransport(handler),
    )
    result = await factory().probe_capabilities(p)
    assert (
        result['basic'] and result['json_schema'] and result['tools'] and result['tool_roundtrip']
    )
    assert p.requests_used == 0 and len(seen) == 4
    assert all(wire['max_tokens'] == 256 for wire in seen)
    assert seen[-1]['messages'][-2]['tool_call_id'] == 'wire-original-id'
    await p.chat(
        ProviderRequest(
            messages=(Message(role='user', content='NONCE=after_probe'),), purpose='probe'
        )
    )
    assert p.requests_used == 1
    await p.close()


@pytest.mark.asyncio
async def test_capability_wrong_nonce_and_tool_id_are_not_verified():
    class Wrong:
        profile = profile(options=GenerationOptions(max_requests=4, max_output_tokens=256))

        async def probe(self, cancel=None):
            return {'connected': True}

        async def chat(self, request, **kwargs):
            if request.tools:
                return ProviderResponse(
                    provider='compatible',
                    auth_mode='none',
                    finish_reason='tool_calls',
                    tool_calls=(
                        ToolCall(
                            id='some-id', name='rebuild_probe_status', arguments={'nonce': 'wrong'}
                        ),
                    ),
                )
            return ProviderResponse(
                provider='compatible', auth_mode='none', content='{"ready":true,"nonce":"wrong"}'
            )

    result = await factory().probe_capabilities(Wrong())
    assert not any(
        result[key]
        for key in ('basic', 'json_schema', 'tools', 'tool_roundtrip', 'inference_verified')
    )
    assert result['budget']['used'] == 3 and len(result['errors']) == 3


@pytest.mark.asyncio
async def test_capability_probe_rejects_uuid_when_identifier_prefix_is_missing():
    class PrefixMissing:
        profile = profile(options=GenerationOptions(max_requests=4, max_output_tokens=256))

        async def probe(self, cancel=None):
            return {'connected': True}

        async def chat(self, request, **kwargs):
            nonce = request.messages[-1].content.split('NONCE=')[-1].split()[0]
            return ProviderResponse(
                provider='compatible', auth_mode='none', content=nonce.removeprefix('rebuild_')
            )

    result = await factory().probe_capabilities(PrefixMissing())
    assert not result['basic'] and not result['actual_inference_verified']


@pytest.mark.asyncio
async def test_no_evidence_scope_shrinking_or_unknown_category_can_bypass_consent():
    calls = []
    p = factory().create_provider(
        profile(allowed_evidence=('runtime',), options=GenerationOptions(max_requests=3)),
        transport=httpx.MockTransport(lambda r: calls.append(r) or answer()),
    )
    client = facade(p)
    await client.chat('chosen', [], options={'_evidence_kinds': ['runtime']})
    with pytest.raises(ProviderError) as unsupported:
        await client.chat('chosen', [], options={'_evidence_kinds': ['code']})
    assert unsupported.value.code == 'external_consent'
    with pytest.raises(ProviderError) as unknown:
        await client.chat('chosen', [], options={'_evidence_kinds': ['made-up']})
    assert unknown.value.code == 'evidence_scope' and len(calls) == 1
    assert client.evidence_kinds == ('runtime',)
    await client.close()


@pytest.mark.asyncio
async def test_shared_budget_joins_multiple_facades_without_joining_manual_probe():
    selected = profile(options=GenerationOptions(max_requests=2))
    ledger = factory().SharedRequestBudget(selected)
    transport = httpx.MockTransport(lambda r: answer())
    first = facade(factory().create_provider(selected, budget=ledger, transport=transport))
    second = facade(factory().create_provider(selected, budget=ledger, transport=transport))
    await first.chat('chosen', [], options={'_evidence_kinds': []})
    await second.chat('chosen', [], options={'_evidence_kinds': []})
    with pytest.raises(ProviderError) as error:
        await first.chat('chosen', [], options={'_evidence_kinds': []})
    assert (
        error.value.code == 'request_budget'
        and first.provider.requests_used == second.provider.requests_used == 2
    )
    separate_probe = first.provider.clone_for_probe()
    assert separate_probe.requests_used == 0 and separate_probe.profile.options.max_requests == 4
    with pytest.raises(ProviderError) as changed:
        factory().create_provider(
            selected.model_copy(update={'model': 'other'}), budget=ledger, transport=transport
        )
    assert changed.value.code == 'profile_changed'
    await first.close()
    await second.close()
    await separate_probe.close()


@pytest.mark.asyncio
async def test_direct_broker_schema_preflight_does_not_charge_or_authenticate():
    p = factory().create_provider(
        profile(provider='openai', endpoint='https://api.openai.com/v1', auth_mode='api_key')
    )
    with pytest.raises(ProviderError) as error:
        await p.chat(
            ProviderRequest(messages=(), purpose='probe', output_schema={'type': 'object'})
        )
    assert error.value.code == 'schema_unsupported' and p.requests_used == 0
    await p.close()


@pytest.mark.asyncio
async def test_plan_probe_does_not_claim_server_output_limit():
    class Plan:
        profile = profile(
            provider='openai',
            endpoint='https://api.openai.com/v1',
            auth_mode='chatgpt',
            options=GenerationOptions(max_requests=4, max_output_tokens=256),
        )

        async def probe(self, cancel=None):
            return {'connected': True}

        async def chat(self, request, **kwargs):
            return ProviderResponse(provider='openai', auth_mode='chatgpt', content='wrong nonce')

    result = await factory().probe_capabilities(Plan())
    assert result['budget']['requested_soft_output_tokens'] == 256
    assert result['budget']['max_output_tokens'] is None
    assert result['budget']['server_output_token_limit_enforced'] is False
    assert result['budget']['max_requests'] == 4


@pytest.mark.asyncio
async def test_probe_clone_retains_original_explicit_environment_authentication_binding(
    monkeypatch,
):
    name = 'REBUILD_TEST_FROZEN_CLONE_KEY'
    monkeypatch.setenv(name, 'fixture-original-key')
    seen = []
    p = factory().create_provider(
        profile(
            auth_mode='api_key',
            credential_ref='env:' + name,
            credential_origin='https://fixture.invalid',
            credential_version=1,
        ),
        transport=httpx.MockTransport(
            lambda r: seen.append(r.headers.get('authorization')) or answer()
        ),
    )
    request = ProviderRequest(messages=(Message(role='user', content='fixture'),), purpose='probe')
    first = p.clone_for_probe()
    try:
        assert (await first.chat(request)).content == 'ok'
    finally:
        await first.close()
    monkeypatch.setenv(name, 'fixture-replaced-key')
    second = p.clone_for_probe()
    try:
        with pytest.raises(ProviderError) as error:
            await second.chat(request)
        assert error.value.code == 'credentials_changed'
        assert seen == ['Bearer fixture-original-key'] and p.requests_used == 0
    finally:
        await second.close()
        await p.close()
