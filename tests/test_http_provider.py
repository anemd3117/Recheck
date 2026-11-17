"""실제 HTTP 전송 경계의 payload·인증·완료 판정. 유료 API는 호출하지 않는다."""

import importlib
import asyncio
import json

import httpx
import pytest

from ai_providers.contracts import (
    CredentialMaterial,
    GenerationOptions,
    Message,
    ProviderError,
    ProviderProfile,
    ProviderRequest,
    ToolCall,
    ToolDefinition,
)


def provider_class():
    result = getattr(importlib.import_module('ai_providers.http_provider'), 'HTTPProvider', None)
    assert result is not None, 'HTTPProvider 전송 구현이 필요합니다'
    return result


def profile(provider='openai', auth_mode='api_key', **values):
    return ProviderProfile(
        provider=provider,
        auth_mode=auth_mode,
        model='chosen-model',
        enabled=True,
        external_consent=True,
        allowed_evidence=('runtime',),
        credential_version=1,
        **values,
    )


def request(**values):
    return ProviderRequest(
        messages=(
            Message(role='system', content='한국어로 답하세요.'),
            Message(role='user', content='선택한 관찰을 설명하세요.'),
        ),
        evidence_kinds=('runtime',),
        **values,
    )


def credentials(value, cancel=None):
    return CredentialMaterial(value='private-test-key', origin=value.origin, version=1)


@pytest.mark.asyncio
async def test_direct_http_schema_preflight_is_free_and_json_prompt_is_explicit():
    seen = []
    p = provider_class()(
        profile(),
        credential_getter=credentials,
        transport=httpx.MockTransport(
            lambda r: seen.append(r) or httpx.Response(200, json=simple_response('openai'))
        ),
    )
    try:
        with pytest.raises(ProviderError) as error:
            await p.chat(request(output_schema={'type': 'object'}))
        assert error.value.code == 'schema_unsupported' and not seen
    finally:
        await p.close()


@pytest.mark.parametrize(
    'schema',
    [
        {'anyOf': [{'type': 'string'}, {'type': 'number'}]},
        {'type': ['object', 'null']},
        {
            'type': 'object',
            'properties': {'v': {'type': ['object', 'null']}},
            'required': ['v'],
            'additionalProperties': False,
        },
        {
            'type': 'object',
            'properties': {'v': {'type': 'string', 'format': 'unknown-format'}},
            'required': ['v'],
            'additionalProperties': False,
        },
        {
            'type': 'object',
            'properties': {'v': {'type': 'string', 'enum': [str(i) for i in range(1001)]}},
            'required': ['v'],
            'additionalProperties': False,
        },
    ],
)
def test_native_strict_subset_rejects_root_union_open_nullable_object_format_and_size(schema):
    from ai_providers.http_provider import strict_schema_supported

    assert strict_schema_supported(schema) is False


@pytest.mark.asyncio
async def test_direct_strict_tool_schema_requires_explicit_supported_contract_before_wire():
    seen = []
    p = provider_class()(
        profile(),
        credential_getter=credentials,
        transport=httpx.MockTransport(
            lambda r: seen.append(r) or httpx.Response(200, json=simple_response('openai'))
        ),
    )
    try:
        with pytest.raises(ProviderError) as error:
            await p.chat(
                request(tools=(ToolDefinition(name='free_object', parameters={'type': 'object'}),))
            )
        assert error.value.code == 'schema_unsupported' and not seen
    finally:
        await p.close()


def simple_response(kind):
    if kind == 'openai':
        return {
            'id': 'resp-1',
            'status': 'completed',
            'model': 'actual-model',
            'output': [
                {'type': 'message', 'content': [{'type': 'output_text', 'text': '관찰 완료'}]}
            ],
            'usage': {'input_tokens': 4, 'output_tokens': 3},
        }
    if kind == 'anthropic':
        return {
            'id': 'msg-1',
            'model': 'actual-model',
            'content': [{'type': 'text', 'text': '관찰 완료'}],
            'stop_reason': 'end_turn',
            'usage': {'input_tokens': 4, 'output_tokens': 3},
        }
    if kind == 'gemini':
        return {
            'id': 'interaction-1',
            'model': 'actual-model',
            'status': 'completed',
            'steps': [{'type': 'model_output', 'content': [{'type': 'text', 'text': '관찰 완료'}]}],
            'usage': {'total_input_tokens': 4, 'total_output_tokens': 3},
        }
    return {
        'id': 'chat-1',
        'model': 'actual-model',
        'choices': [
            {
                'index': 0,
                'message': {'role': 'assistant', 'content': '관찰 완료'},
                'finish_reason': 'stop',
            }
        ],
        'usage': {'prompt_tokens': 4, 'completion_tokens': 3},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'kind,path',
    [
        ('openai', '/v1/responses'),
        ('anthropic', '/v1/messages'),
        ('gemini', '/v1beta/interactions'),
        ('compatible', '/v1/chat/completions'),
    ],
)
async def test_native_payloads_and_actual_response_metadata(kind, path):
    seen = []

    def handler(outgoing):
        seen.append(outgoing)
        return httpx.Response(
            200, json=simple_response(kind), headers={'x-request-id': 'wire-request'}
        )

    values = {'endpoint': 'https://compatible.example/v1'} if kind == 'compatible' else {}
    selected = profile(kind, **values)
    provider = provider_class()(
        selected, credential_getter=credentials, transport=httpx.MockTransport(handler)
    )
    schema = {
        'type': 'object',
        'properties': {'ok': {'type': 'boolean'}},
        'required': ['ok'],
        'additionalProperties': False,
    }
    try:
        result = await provider.chat(request(output_schema=schema, stream=False))
    finally:
        await provider.close()
    wire = json.loads(seen[0].content)
    assert seen[0].url.path == path
    assert (
        result.content == '관찰 완료'
        and result.model == 'actual-model'
        and result.finish_reason == 'stop'
    )
    assert result.request_id == 'wire-request' and result.provider == kind
    assert 'num_ctx' not in wire and 'num_predict' not in wire
    if kind == 'openai':
        assert wire['max_output_tokens'] == 2048 and wire['store'] is False
        assert wire['text']['format']['schema'] == schema
        assert wire['instructions'] == '한국어로 답하세요.'
    elif kind == 'anthropic':
        assert seen[0].headers['anthropic-version'] == '2026-10-21'
        assert wire['max_tokens'] == 2048 and wire['system'] == '한국어로 답하세요.'
        assert wire['output_config']['format']['schema'] == schema
    elif kind == 'gemini':
        assert wire['generation_config']['max_output_tokens'] == 2048 and wire['store'] is False
        assert wire['system_instruction'] == '한국어로 답하세요.'
        assert wire['input'][0]['type'] == 'user_input'
        assert wire['response_format']['schema'] == schema
        assert seen[0].headers['x-goog-api-key'] == 'private-test-key'
    else:
        assert wire['messages'][0]['role'] == 'system'
        assert wire['response_format']['json_schema']['schema'] == schema


@pytest.mark.asyncio
async def test_chatgpt_plan_uses_streaming_namespace_tools_and_omits_api_key_only_fields():
    seen = []
    final = simple_response('openai')

    def handler(outgoing):
        seen.append(json.loads(outgoing.content))
        return httpx.Response(
            200,
            text='event: response.completed\ndata: '
            + json.dumps({'type': 'response.completed', 'response': final})
            + '\n\n',
            headers={'content-type': 'text/event-stream'},
        )

    p = provider_class()(
        profile(auth_mode='chatgpt'),
        credential_getter=credentials,
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await p.chat(
            request(tools=(ToolDefinition(name='get_status', strict=False),), stream=False)
        )
    finally:
        await p.close()
    wire = seen[0]
    assert result.finish_reason == 'stop' and wire['stream'] is True and wire['store'] is False
    assert isinstance(wire['input'], list) and not any(
        item.get('role') == 'system' for item in wire['input']
    )
    assert (
        wire['tools'][0]['type'] == 'namespace'
        and wire['tools'][0]['tools'][0]['name'] == 'get_status'
    )
    assert not set(wire) & {
        'max_output_tokens',
        'max_tool_calls',
        'temperature',
        'top_p',
        'metadata',
        'previous_response_id',
    }


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['openai', 'anthropic', 'gemini', 'compatible'])
async def test_tool_results_preserve_original_call_identity(kind):
    wires = []

    def handler(outgoing):
        wires.append(json.loads(outgoing.content))
        return httpx.Response(200, json=simple_response(kind))

    values = {'endpoint': 'https://compatible.example/v1'} if kind == 'compatible' else {}
    p = provider_class()(
        profile(kind, **values),
        credential_getter=credentials,
        transport=httpx.MockTransport(handler),
    )
    previous = ToolCall(id='call-17', name='get_status', arguments={})
    history = ProviderRequest(
        messages=(
            Message(role='user', content='상태'),
            Message(role='assistant', tool_calls=(previous,)),
            Message(role='tool', tool_call_id='call-17', content='{"ready":true}'),
        ),
        evidence_kinds=('runtime',),
        tools=(ToolDefinition(name='get_status', strict=False),),
        stream=False,
    )
    try:
        await p.chat(history)
    finally:
        await p.close()
    text = json.dumps(wires[0])
    assert 'call-17' in text and 'get_status' in text
    if kind == 'openai':
        assert wires[0]['input'][-1]['call_id'] == 'call-17'
    elif kind == 'anthropic':
        assert wires[0]['messages'][-1]['content'][0]['tool_use_id'] == 'call-17'
    elif kind == 'gemini':
        assert wires[0]['input'][-1]['call_id'] == 'call-17'
    else:
        assert wires[0]['messages'][-1]['tool_call_id'] == 'call-17'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'status,code,body',
    [
        (401, 'authentication', {'error': {'message': 'bad private-test-key'}}),
        (403, 'permission', {'error': {'message': 'forbidden private-test-key'}}),
        (
            403,
            'subscription_sharing_user_not_eligible',
            {'error': {'code': 'subscription_sharing_user_not_eligible', 'message': 'private-test-key'}},
        ),
        (429, 'rate_limit', {'error': {'type': 'rate_limit_error'}}),
        (429, 'quota', {'error': {'code': 'insufficient_quota'}}),
        (400, 'context', {'error': {'code': 'context_length_exceeded'}}),
        (503, 'service', {'error': {'message': 'unavailable'}}),
    ],
)
async def test_http_failures_are_classified_redacted_and_never_silently_retried(status, code, body):
    calls = []

    def handler(outgoing):
        calls.append(outgoing)
        return httpx.Response(status, json=body, headers={'retry-after': '7'})

    p = provider_class()(
        profile(), credential_getter=credentials, transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(ProviderError) as caught:
            await p.chat(request())
    finally:
        await p.close()
    assert caught.value.code == code and caught.value.status_code == status
    assert len(calls) == 1 and 'private-test-key' not in str(caught.value)
    assert 'private-test-key' not in json.dumps(caught.value.details)
    if status == 429:
        assert caught.value.retry_after == 7


@pytest.mark.parametrize('status,body,expected', [
    (403, {'error': {'code': 'subscription_sharing_user_not_eligible'}},
     'subscription_sharing_user_not_eligible'),
    (None, {'code': 'subscription_sharing_user_not_eligible'},
     'subscription_sharing_user_not_eligible'),
    (403, {'detail': 'subscription_sharing_user_not_eligible'}, 'permission'),
    (None, {'detail': 'subscription_sharing_user_not_eligible'}, 'api_error'),
    (403, {'error': 'subscription_sharing_user_not_eligible'}, 'permission'),
    (403, {'error': {'message': 'subscription_sharing_user_not_eligible'}}, 'permission'),
    (403, {'error': {'code': 'subscription_sharing_user_not_eligible_suffix'}}, 'permission'),
    (403, {'error': {'code': 'SUBSCRIPTION_SHARING_USER_NOT_ELIGIBLE'}}, 'permission'),
    (403, {'metadata': {'error': {'code': 'subscription_sharing_user_not_eligible'}}}, 'permission'),
    (403, {'error': {'code': 'invalid_grant'}}, 'permission'),
    (400, {'error': {'code': 'invalid_grant'}}, 'api_error'),
    (None, {'code': 'invalid_grant'}, 'api_error'),
])
def test_plan_eligibility_classification_requires_the_exact_structured_error_code(status, body, expected):
    from ai_providers.http_provider import error_code

    assert error_code(status, body) == expected


@pytest.mark.asyncio
async def test_credential_origin_mismatch_is_blocked_before_any_http_request():
    sent = []

    def getter(value, cancel=None):
        return CredentialMaterial(
            value='private-test-key', origin='https://another.example', version=1
        )

    p = provider_class()(
        profile(),
        credential_getter=getter,
        transport=httpx.MockTransport(
            lambda request: sent.append(request) or httpx.Response(200, json={})
        ),
    )
    try:
        with pytest.raises(ProviderError, match='인증'):
            await p.chat(request())
    finally:
        await p.close()
    assert sent == []


@pytest.mark.asyncio
async def test_redirect_does_not_forward_credentials_to_another_service():
    seen = []

    def handler(outgoing):
        seen.append(str(outgoing.url))
        return httpx.Response(307, headers={'location': 'https://other.example/collect'})

    p = provider_class()(
        profile(), credential_getter=credentials, transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(ProviderError) as caught:
            await p.chat(request())
    finally:
        await p.close()
    assert caught.value.code == 'redirect' and seen == ['https://api.openai.com/v1/responses']


@pytest.mark.asyncio
async def test_disabled_profile_can_be_probed_without_inference_or_activation():
    paths = []

    def handler(outgoing):
        paths.append(outgoing.url.path)
        return httpx.Response(200, json={'data': [{'id': 'model-a'}]})

    selected = profile().model_copy(update={'enabled': False})
    p = provider_class()(
        selected, credential_getter=credentials, transport=httpx.MockTransport(handler)
    )
    try:
        probe = await p.probe()
        models = await p.list_models()
        with pytest.raises(ProviderError):
            await p.chat(request())
    finally:
        await p.close()
    assert (
        probe['connected'] is True
        and probe['authenticated'] is True
        and probe['inference_verified'] is False
    )
    assert [item.id for item in models] == ['model-a'] and paths == ['/v1/models', '/v1/models']


@pytest.mark.asyncio
async def test_complete_tool_batch_rejects_a_later_invalid_call_without_partial_success():
    body = simple_response('openai')
    body['output'] = [
        {'type': 'function_call', 'call_id': 'one', 'name': 'echo', 'arguments': '{"value":1}'},
        {'type': 'function_call', 'call_id': 'two', 'name': 'echo', 'arguments': '{"value":-1}'},
    ]
    tool = ToolDefinition(
        name='echo',
        parameters={
            'type': 'object',
            'properties': {'value': {'type': 'integer', 'minimum': 0}},
            'required': ['value'],
            'additionalProperties': False,
        },
    )
    p = provider_class()(
        profile(),
        credential_getter=credentials,
        transport=httpx.MockTransport(lambda outgoing: httpx.Response(200, json=body)),
    )
    try:
        with pytest.raises(ProviderError) as caught:
            await p.chat(request(tools=(tool,)))
    finally:
        await p.close()
    assert caught.value.code == 'tool_arguments'


@pytest.mark.asyncio
async def test_api_response_reflecting_credentials_is_redacted_in_content_raw_and_events():
    body = simple_response('openai')
    body['output'][0]['content'][0]['text'] = 'reflected private-test-key'
    events = []
    p = provider_class()(
        profile(),
        credential_getter=credentials,
        transport=httpx.MockTransport(lambda outgoing: httpx.Response(200, json=body)),
    )
    try:
        result = await p.chat(request(), on_event=events.append)
    finally:
        await p.close()
    assert (
        'private-test-key' not in result.model_dump_json()
        and 'private-test-key' not in json.dumps(events)
    )


@pytest.mark.asyncio
async def test_concurrent_token_refresh_does_not_use_another_requests_redaction_values():
    count = 0

    def getter(value, cancel=None):
        nonlocal count
        count += 1
        return CredentialMaterial(
            value='private-key-' + str(count), origin=value.origin, version=1, token_revision=count
        )

    async def handler(outgoing):
        secret = outgoing.headers['authorization'].removeprefix('Bearer ')
        if secret.endswith('1'):
            await asyncio.sleep(0.05)
        value = simple_response('openai')
        value['output'][0]['content'][0]['text'] = secret
        return httpx.Response(200, json=value)

    p = provider_class()(
        profile(), credential_getter=getter, transport=httpx.MockTransport(handler)
    )
    try:
        first, second = await asyncio.gather(p.chat(request()), p.chat(request()))
    finally:
        await p.close()
    assert first.content == '<MASKED>' and second.content == '<MASKED>'
    assert first.provenance['token_revision'] == 1 and second.provenance['token_revision'] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'broken', [{'output': [5]}, {'output': None}, {'output': [{'type': 'message', 'content': 5}]}]
)
async def test_malformed_provider_shapes_have_a_controlled_error_without_raw_python_exceptions(
    broken,
):
    value = {**simple_response('openai'), **broken}
    p = provider_class()(
        profile(),
        credential_getter=credentials,
        transport=httpx.MockTransport(lambda outgoing: httpx.Response(200, json=value)),
    )
    try:
        with pytest.raises(ProviderError) as caught:
            await p.chat(request())
    finally:
        await p.close()
    assert caught.value.code == 'malformed_response'


@pytest.mark.asyncio
async def test_non_ascii_authentication_header_has_a_controlled_error_before_http():
    sent = []

    def getter(value, cancel=None):
        return CredentialMaterial(value='잘못된_키', origin=value.origin, version=1)

    p = provider_class()(
        profile(),
        credential_getter=getter,
        transport=httpx.MockTransport(
            lambda outgoing: sent.append(outgoing) or httpx.Response(200, json={})
        ),
    )
    try:
        with pytest.raises(ProviderError) as caught:
            await p.chat(request())
    finally:
        await p.close()
    assert caught.value.code == 'credential' and sent == []


@pytest.mark.asyncio
async def test_reflected_secret_is_removed_from_raw_finish_reason_too():
    body = {**simple_response('openai'), 'status': 'private-test-key'}
    p = provider_class()(
        profile(),
        credential_getter=credentials,
        transport=httpx.MockTransport(lambda outgoing: httpx.Response(200, json=body)),
    )
    try:
        result = await p.chat(request())
    finally:
        await p.close()
    assert 'private-test-key' not in result.model_dump_json()


@pytest.mark.asyncio
async def test_reflected_authentication_secret_cannot_be_persisted_as_a_tool_call_id():
    body = simple_response('openai')
    body['output'] = [
        {
            'type': 'function_call',
            'call_id': 'private-test-key',
            'name': 'get_status',
            'arguments': '{}',
        }
    ]
    p = provider_class()(
        profile(),
        credential_getter=credentials,
        transport=httpx.MockTransport(lambda outgoing: httpx.Response(200, json=body)),
    )
    try:
        with pytest.raises(ProviderError) as caught:
            await p.chat(request(tools=(ToolDefinition(name='get_status', strict=False),)))
    finally:
        await p.close()
    assert caught.value.code == 'credential_reflection' and 'private-test-key' not in str(
        caught.value
    )
