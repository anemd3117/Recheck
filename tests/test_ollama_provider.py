"""로컬 native client 보존·공통 도구/취소 계약. 로컬 서버를 실행하지 않는다."""

import asyncio
import importlib
import json
import threading
import httpx

import pytest

from ai_providers.contracts import (
    GenerationOptions,
    Message,
    ProviderError,
    ProviderProfile,
    ProviderRequest,
    ToolDefinition,
)


def provider_class():
    result = getattr(
        importlib.import_module('ai_providers.ollama_provider'), 'OllamaProvider', None
    )
    assert result is not None, 'OllamaProvider 구현이 필요합니다'
    return result


class Native:
    def __init__(self, base_url, timeout=120):
        self.base_url, self.timeout, self.calls, self.closed = base_url, timeout, [], False
        self.answer = {
            'message': {'content': '로컬 관찰 완료'},
            'done_reason': 'stop',
            'prompt_eval_count': 11,
            'eval_count': 5,
            '_execution': {'kind': 'local', 'digest': 'real-local-digest'},
        }

    async def probe(self, cancel=None):
        return {'connected': True, 'version': 'fixture', 'managed': False}

    async def models(self, cancel=None):
        return [
            {'name': 'chosen-local', 'digest': 'real-local-digest', 'details': {'format': 'gguf'}}
        ]

    async def local_metadata(self, model, cancel=None):
        return {'details': {'format': 'gguf'}}

    async def ensure_server(self, **kwargs):
        return {'connected': True}

    async def chat(self, model, messages, **kwargs):
        self.calls.append({'model': model, 'messages': messages, **kwargs})
        if kwargs.get('on_event'):
            value = kwargs['on_event'](
                {'message': {'content': '비밀일 수 있는 조각'}, 'done': False}
            )
            if asyncio.iscoroutine(value):
                await value
        return self.answer

    async def close(self):
        self.closed = True


def selected(**values):
    return ProviderProfile(
        provider='ollama', auth_mode='none', model='chosen-local', enabled=True, **values
    )


def prompt(**values):
    return ProviderRequest(
        messages=(
            Message(role='developer', content='한국어'),
            Message(role='user', content='관찰 설명'),
        ),
        **values,
    )


@pytest.mark.asyncio
async def test_native_local_client_selection_metadata_and_options_are_preserved():
    p = provider_class()(
        selected(options=GenerationOptions(context_budget=16384, max_output_tokens=4096)),
        client_factory=Native,
    )
    events = []
    try:
        result = await p.chat(prompt(), on_event=events.append)
        models = await p.list_models()
        probe = await p.probe()
        assert (await p.native.local_metadata('chosen-local'))['details']['format'] == 'gguf'
    finally:
        await p.close()
    native_request = p.native.calls[0]
    assert (
        result.content == '로컬 관찰 완료'
        and result.provider == 'ollama'
        and result.auth_mode == 'none'
    )
    assert result.provenance['execution']['digest'] == 'real-local-digest'
    assert result.usage['input_tokens'] == 11 and result.usage['output_tokens'] == 5
    assert native_request['model'] == 'chosen-local'
    assert (
        native_request['options']['num_ctx'] == 16384
        and native_request['options']['num_predict'] == 4096
    )
    assert native_request['messages'][0]['role'] == 'system'
    assert models[0].id == 'chosen-local' and probe['inference_verified'] is False
    assert '비밀일 수 있는 조각' not in json.dumps(events, ensure_ascii=False)
    assert p.native.closed


@pytest.mark.asyncio
async def test_native_tool_id_is_generated_once_and_result_maps_back_to_native_tool_name():
    p = provider_class()(selected(), client_factory=Native)
    p.native.answer = {
        'message': {
            'content': '',
            'tool_calls': [{'function': {'name': 'get_status', 'arguments': {}}}],
        },
        'done_reason': 'stop',
    }
    tool = ToolDefinition(name='get_status')
    try:
        first = await p.chat(prompt(tools=(tool,)))
        assert (
            first.finish_reason == 'tool_calls'
            and len(first.tool_calls) == 1
            and first.tool_calls[0].id
        )
        p.native.answer = {'message': {'content': '완료'}, 'done_reason': 'stop'}
        history = ProviderRequest(
            messages=(
                Message(role='assistant', tool_calls=first.tool_calls),
                Message(role='tool', tool_call_id=first.tool_calls[0].id, content='{"ready":true}'),
            ),
            tools=(tool,),
        )
        await p.chat(history)
    finally:
        await p.close()
    assert p.native.calls[-1]['messages'][-1]['tool_name'] == 'get_status'
    assert p.native.calls[-1]['messages'][-1]['content'] == '{"ready":true}'


@pytest.mark.asyncio
async def test_native_length_termination_never_returns_a_tool_for_execution():
    p = provider_class()(selected(), client_factory=Native)
    p.native.answer = {
        'message': {
            'content': '{"partial":',
            'tool_calls': [{'function': {'name': 'get_status', 'arguments': {}}}],
        },
        'done_reason': 'length',
    }
    try:
        result = await p.chat(prompt(tools=(ToolDefinition(name='get_status'),)))
    finally:
        await p.close()
    assert result.finish_reason == 'length' and result.tool_calls == ()


@pytest.mark.asyncio
async def test_native_later_invalid_tool_prevents_return_of_a_partial_valid_batch():
    p = provider_class()(selected(), client_factory=Native)
    p.native.answer = {
        'message': {
            'content': '',
            'tool_calls': [
                {'function': {'name': 'get_status', 'arguments': {}}},
                {'function': {'name': 'forbidden', 'arguments': {}}},
            ],
        }
    }
    try:
        with pytest.raises(ProviderError) as caught:
            await p.chat(prompt(tools=(ToolDefinition(name='get_status'),)))
    finally:
        await p.close()
    assert caught.value.code == 'tool_not_allowed'


@pytest.mark.asyncio
async def test_native_request_cancellation_closes_waiting_operation_without_changing_profile():
    class Slow(Native):
        async def chat(self, *args, **kwargs):
            await asyncio.sleep(10)

    cancel = threading.Event()
    p = provider_class()(selected(), client_factory=Slow)

    async def set_cancel():
        await asyncio.sleep(0.05)
        cancel.set()

    setter = asyncio.create_task(set_cancel())
    try:
        with pytest.raises(ProviderError) as caught:
            await p.chat(prompt(), cancel=cancel)
    finally:
        await setter
        await p.close()
    assert (
        caught.value.code == 'cancelled' and p.profile.model == 'chosen-local' and p.native.closed
    )


@pytest.mark.asyncio
async def test_native_raw_response_limit_is_applied_before_persisting_result():
    p = provider_class()(
        selected(options=GenerationOptions(max_response_bytes=1024)), client_factory=Native
    )
    p.native.answer['message']['content'] = 'x' * 2048
    try:
        with pytest.raises(ProviderError) as caught:
            await p.chat(prompt())
    finally:
        await p.close()
    assert caught.value.code == 'response_limit'


@pytest.mark.asyncio
async def test_native_tool_wire_preserves_the_existing_ollama_supported_definition_fields():
    p = provider_class()(selected(), client_factory=Native)
    try:
        await p.chat(prompt(tools=(ToolDefinition(name='get_status'),)))
    finally:
        await p.close()
    assert set(p.native.calls[0]['tools'][0]['function']) == {'name', 'description', 'parameters'}


@pytest.mark.asyncio
async def test_malformed_native_call_list_is_a_controlled_provider_error():
    p = provider_class()(selected(), client_factory=Native)
    p.native.answer['message']['tool_calls'] = 42
    try:
        with pytest.raises(ProviderError) as caught:
            await p.chat(prompt())
    finally:
        await p.close()
    assert caught.value.code == 'malformed_response'


@pytest.mark.asyncio
async def test_existing_native_client_round_trip_uses_bounded_transport_without_live_server():
    from recheck.ollama import OllamaClient

    paths = []

    def handler(outgoing):
        paths.append(outgoing.url.path)
        if outgoing.url.path == '/api/show':
            return httpx.Response(200, json={'details': {'format': 'gguf'}})
        if outgoing.url.path == '/api/chat':
            return httpx.Response(
                200,
                text=json.dumps(
                    {
                        'message': {'role': 'assistant', 'content': '완료'},
                        'done': True,
                        'done_reason': 'stop',
                        'prompt_eval_count': 3,
                        'eval_count': 2,
                    }
                )
                + '\n',
            )
        if outgoing.url.path == '/api/ps':
            return httpx.Response(
                200,
                json={'models': [{'name': 'chosen-local', 'size': 1, 'digest': 'verified-local'}]},
            )
        raise AssertionError('예상하지 않은 HTTP 요청')

    def factory(base_url, timeout, transport):
        transport.inner = httpx.MockTransport(handler)
        return OllamaClient(base_url, timeout=timeout, transport=transport)

    p = provider_class()(selected(), client_factory=factory)
    try:
        result = await p.chat(prompt())
    finally:
        await p.close()
    assert result.content == '완료' and result.provenance['execution']['digest'] == 'verified-local'
    assert paths == ['/api/show', '/api/chat', '/api/ps']
