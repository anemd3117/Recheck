"""부분 UTF-8·교차 도구 인자·완료/취소 경계의 HTTP 스트림 검사."""

import asyncio
import gzip
import json
import threading
import time

import httpx
import pytest

from ai_providers.contracts import GenerationOptions, ProviderError, ToolDefinition
from test_http_provider import credentials, profile, provider_class, request, simple_response


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks, delay=0, cancel=None):
        self.chunks, self.delay, self.cancel, self.closed = chunks, delay, cancel, False

    async def __aiter__(self):
        for index, part in enumerate(self.chunks):
            if index and self.cancel:
                self.cancel.set()
            if index and self.delay:
                await asyncio.sleep(self.delay)
            yield part

    async def aclose(self):
        self.closed = True


def sse(events):
    return ''.join(
        'data: '
        + (value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
        + '\n\n'
        for value in events
    ).encode('utf-8')


def tool_stream(kind):
    if kind == 'openai':
        calls = [
            {
                'type': 'function_call',
                'id': 'item-' + name,
                'call_id': name,
                'name': 'echo',
                'arguments': '{"value":' + str(number) + '}',
            }
            for name, number in [('first', 5), ('second', 6)]
        ]
        events = [
            {
                'type': 'response.output_item.added',
                'output_index': index,
                'item': {**call, 'arguments': ''},
            }
            for index, call in enumerate(calls)
        ]
        events += [
            {
                'type': 'response.function_call_arguments.delta',
                'output_index': 0,
                'item_id': 'item-first',
                'delta': '{"value":',
            },
            {
                'type': 'response.function_call_arguments.delta',
                'output_index': 1,
                'item_id': 'item-second',
                'delta': '{"value":6',
            },
            {
                'type': 'response.function_call_arguments.delta',
                'output_index': 0,
                'item_id': 'item-first',
                'delta': '5}',
            },
            {
                'type': 'response.function_call_arguments.delta',
                'output_index': 1,
                'item_id': 'item-second',
                'delta': '}',
            },
            {'type': 'response.completed', 'response': {**simple_response(kind), 'output': calls}},
        ]
    elif kind == 'anthropic':
        events = [
            {
                'type': 'message_start',
                'message': {
                    'id': 'msg-tools',
                    'model': 'actual-model',
                    'content': [],
                    'usage': {'input_tokens': 4},
                },
            }
        ]
        events += [
            {
                'type': 'content_block_start',
                'index': index,
                'content_block': {'type': 'tool_use', 'id': name, 'name': 'echo', 'input': {}},
            }
            for index, name in enumerate(('first', 'second'))
        ]
        events += [
            {
                'type': 'content_block_delta',
                'index': index,
                'delta': {'type': 'input_json_delta', 'partial_json': part},
            }
            for index, part in [(0, '{"value":'), (1, '{"value":6'), (0, '5}'), (1, '}')]
        ]
        events += [{'type': 'content_block_stop', 'index': index} for index in (0, 1)]
        events += [
            {
                'type': 'message_delta',
                'delta': {'stop_reason': 'tool_use'},
                'usage': {'output_tokens': 12},
            },
            {'type': 'message_stop'},
        ]
    elif kind == 'gemini':
        events = [
            {
                'event_type': 'interaction.created',
                'interaction': {'id': 'interaction-tools', 'model': 'actual-model'},
            }
        ]
        events += [
            {
                'event_type': 'step.start',
                'index': index,
                'step': {'type': 'function_call', 'id': name, 'name': 'echo', 'arguments': {}},
            }
            for index, name in enumerate(('first', 'second'))
        ]
        events += [
            {
                'event_type': 'step.delta',
                'index': index,
                'delta': {'type': 'arguments_delta', 'arguments': part},
            }
            for index, part in [(0, '{"value":'), (1, '{"value":6'), (0, '5}'), (1, '}')]
        ]
        events += [{'event_type': 'step.stop', 'index': index} for index in (0, 1)]
        events += [
            {
                'event_type': 'interaction.completed',
                'interaction': {
                    'id': 'interaction-tools',
                    'model': 'actual-model',
                    'status': 'requires_action',
                    'usage': {'total_input_tokens': 4, 'total_output_tokens': 12},
                },
            },
            '[DONE]',
        ]
    else:
        events = [
            {
                'id': 'chat-tools',
                'model': 'actual-model',
                'choices': [
                    {
                        'index': 0,
                        'delta': {
                            'tool_calls': [
                                {
                                    'index': index,
                                    'id': name,
                                    'type': 'function',
                                    'function': {'name': 'echo', 'arguments': ''},
                                }
                                for index, name in enumerate(('first', 'second'))
                            ]
                        },
                        'finish_reason': None,
                    }
                ],
            }
        ]
        events += [
            {
                'choices': [
                    {
                        'index': 0,
                        'delta': {
                            'tool_calls': [{'index': index, 'function': {'arguments': part}}]
                        },
                        'finish_reason': None,
                    }
                ]
            }
            for index, part in [(0, '{"value":'), (1, '{"value":6'), (0, '5}'), (1, '}')]
        ]
        events += [
            {
                'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls'}],
                'usage': {'prompt_tokens': 4, 'completion_tokens': 12},
            },
            '[DONE]',
        ]
    return sse(events)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['openai', 'anthropic', 'gemini', 'compatible'])
async def test_interleaved_arguments_are_assembled_by_call_index_before_batch_validation(kind):
    body = tool_stream(kind)
    stream = Chunks([body[index : index + 7] for index in range(0, len(body), 7)])
    values = {'endpoint': 'https://compatible.example/v1'} if kind == 'compatible' else {}
    p = provider_class()(
        profile(kind, **values),
        credential_getter=credentials,
        transport=httpx.MockTransport(
            lambda outgoing: httpx.Response(
                200, stream=stream, headers={'content-type': 'text/event-stream'}
            )
        ),
    )
    tool = ToolDefinition(
        name='echo',
        parameters={
            'type': 'object',
            'properties': {'value': {'type': 'integer'}},
            'required': ['value'],
            'additionalProperties': False,
        },
    )
    try:
        result = await p.chat(request(tools=(tool,)))
    finally:
        await p.close()
    assert result.finish_reason == 'tool_calls'
    assert [(call.id, call.name, call.arguments) for call in result.tool_calls] == [
        ('first', 'echo', {'value': 5}),
        ('second', 'echo', {'value': 6}),
    ]
    assert stream.closed


@pytest.mark.asyncio
async def test_utf8_split_secret_deltas_are_never_emitted_or_preserved_as_fragments():
    final = simple_response('openai')
    final['output'][0]['content'][0]['text'] = '한글 private-test-key 완료'
    body = sse(
        [
            {'type': 'response.output_text.delta', 'delta': '한글 private-'},
            {'type': 'response.output_text.delta', 'delta': 'test-key 완료'},
            {'type': 'response.completed', 'response': final},
        ]
    )
    stream = Chunks([body[index : index + 1] for index in range(len(body))])
    events = []
    p = provider_class()(
        profile(),
        credential_getter=credentials,
        transport=httpx.MockTransport(
            lambda outgoing: httpx.Response(
                200, stream=stream, headers={'content-type': 'text/event-stream'}
            )
        ),
    )
    try:
        result = await p.chat(request(), on_event=events.append)
    finally:
        await p.close()
    assert result.content == '한글 <MASKED> 완료'
    text = result.model_dump_json() + json.dumps(events)
    assert 'private-test-key' not in text and 'private-' not in text and 'test-key' not in text
    assert events and all('delta' not in event and 'content' not in event for event in events)


@pytest.mark.asyncio
async def test_stream_without_provider_completion_is_rejected_even_if_json_text_is_complete():
    body = sse([{'type': 'response.output_text.delta', 'delta': '{"ok":true}'}])
    p = provider_class()(
        profile(),
        credential_getter=credentials,
        transport=httpx.MockTransport(
            lambda outgoing: httpx.Response(
                200, content=body, headers={'content-type': 'text/event-stream'}
            )
        ),
    )
    try:
        with pytest.raises(ProviderError) as caught:
            await p.chat(request())
    finally:
        await p.close()
    assert caught.value.code == 'incomplete_stream'


@pytest.mark.asyncio
async def test_late_stream_error_does_not_return_previously_assembled_tool_calls():
    body = tool_stream('openai')
    body = body[: body.index(b'data: {"type": "response.completed"')] + sse(
        [{'type': 'error', 'code': 'rate_limit_exceeded', 'message': 'private-test-key failed'}]
    )
    p = provider_class()(
        profile(),
        credential_getter=credentials,
        transport=httpx.MockTransport(
            lambda outgoing: httpx.Response(
                200, content=body, headers={'content-type': 'text/event-stream'}
            )
        ),
    )
    try:
        with pytest.raises(ProviderError) as caught:
            await p.chat(request(tools=(ToolDefinition(name='echo', strict=False),)))
    finally:
        await p.close()
    assert caught.value.code == 'rate_limit' and 'private-test-key' not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize('event', [
    {'type': 'error', 'code': 'subscription_sharing_user_not_eligible', 'message': 'private-test-key'},
    {'type': 'error', 'error': {'code': 'subscription_sharing_user_not_eligible', 'message': 'private-test-key'}},
    {'type': 'response.failed', 'response': {
        'error': {'code': 'subscription_sharing_user_not_eligible', 'message': 'private-test-key'},
    }},
])
async def test_structured_stream_plan_ineligibility_is_terminal_and_redacted(event):
    calls = []

    def handler(outgoing):
        calls.append(outgoing)
        return httpx.Response(200, content=sse([event]), headers={'content-type': 'text/event-stream'})

    selected = profile()
    p = provider_class()(selected, credential_getter=credentials, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ProviderError) as caught:
            await p.chat(request(stream=True))
    finally:
        await p.close()
    assert caught.value.code == 'subscription_sharing_user_not_eligible'
    assert len(calls) == 1
    assert p.profile == selected
    assert 'private-test-key' not in str(caught.value)
    assert 'private-test-key' not in json.dumps(caught.value.details)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'mutation,expected', [('length', 'length'), ('refusal', 'refusal'), ('empty', 'empty')]
)
async def test_model_completion_categories_preserve_failure_without_executable_calls(
    mutation, expected
):
    final = simple_response('openai')
    if mutation == 'length':
        final.update(status='incomplete', incomplete_details={'reason': 'max_output_tokens'})
    elif mutation == 'refusal':
        final['output'] = [
            {'type': 'message', 'content': [{'type': 'refusal', 'refusal': '거부했습니다.'}]}
        ]
    else:
        final['output'] = []
    p = provider_class()(
        profile(),
        credential_getter=credentials,
        transport=httpx.MockTransport(lambda outgoing: httpx.Response(200, json=final)),
    )
    try:
        result = await p.chat(request())
    finally:
        await p.close()
    assert result.finish_reason == expected and result.tool_calls == ()


@pytest.mark.asyncio
async def test_response_byte_limit_closes_stream_before_parsing_oversized_body():
    stream = Chunks([b'x' * 700, b'y' * 700])
    selected = profile().model_copy(update={'options': GenerationOptions(max_response_bytes=1024)})
    p = provider_class()(
        selected,
        credential_getter=credentials,
        transport=httpx.MockTransport(lambda outgoing: httpx.Response(200, stream=stream)),
    )
    try:
        with pytest.raises(ProviderError) as caught:
            await p.chat(request())
    finally:
        await p.close()
    assert caught.value.code == 'response_limit' and stream.closed


@pytest.mark.asyncio
async def test_cancellation_interrupts_a_stalled_chunk_and_closes_response():
    cancel = threading.Event()
    stream = Chunks([b'data: {}\n\n', b'data: {}\n\n'], delay=10, cancel=cancel)
    p = provider_class()(
        profile(),
        credential_getter=credentials,
        transport=httpx.MockTransport(
            lambda outgoing: httpx.Response(
                200, stream=stream, headers={'content-type': 'text/event-stream'}
            )
        ),
    )
    start = time.monotonic()
    try:
        with pytest.raises(ProviderError) as caught:
            await p.chat(request(), cancel=cancel)
    finally:
        await p.close()
    assert caught.value.code == 'cancelled' and time.monotonic() - start < 2 and stream.closed


@pytest.mark.asyncio
async def test_total_deadline_covers_credential_resolution_and_http_work():
    async def slow_getter(value, cancel=None):
        await asyncio.sleep(10)
        return credentials(value)

    seen = []
    selected = profile().model_copy(update={'options': GenerationOptions(timeout_seconds=1)})
    p = provider_class()(
        selected,
        credential_getter=slow_getter,
        transport=httpx.MockTransport(
            lambda outgoing: seen.append(outgoing)
            or httpx.Response(200, json=simple_response('openai'))
        ),
    )
    start = time.monotonic()
    try:
        with pytest.raises(ProviderError) as caught:
            await p.chat(request())
    finally:
        await p.close()
    assert caught.value.code == 'timeout' and time.monotonic() - start < 2 and seen == []


@pytest.mark.asyncio
async def test_compressed_response_is_rejected_before_decompression_can_bypass_byte_limit():
    stream = Chunks([gzip.compress(b'x' * 100000)])
    p = provider_class()(
        profile(),
        credential_getter=credentials,
        transport=httpx.MockTransport(
            lambda outgoing: httpx.Response(
                200,
                stream=stream,
                headers={'content-type': 'application/json', 'content-encoding': 'gzip'},
            )
        ),
    )
    try:
        with pytest.raises(ProviderError) as caught:
            await p.chat(request())
    finally:
        await p.close()
    assert caught.value.code == 'response_encoding' and stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize('position', ['after_done', 'after_finish'])
async def test_compatible_terminal_markers_cannot_be_followed_by_new_model_content(position):
    events = [
        {'choices': [{'index': 0, 'delta': {'content': '정상'}, 'finish_reason': None}]},
        {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]},
    ]
    injection = {
        'choices': [{'index': 0, 'delta': {'content': '잘못된 추가 출력'}, 'finish_reason': None}]
    }
    events += ['[DONE]', injection] if position == 'after_done' else [injection, '[DONE]']
    p = provider_class()(
        profile('compatible', endpoint='https://compatible.example/v1'),
        credential_getter=credentials,
        transport=httpx.MockTransport(
            lambda outgoing: httpx.Response(
                200, content=sse(events), headers={'content-type': 'text/event-stream'}
            )
        ),
    )
    try:
        with pytest.raises(ProviderError) as caught:
            await p.chat(request())
    finally:
        await p.close()
    assert caught.value.code == 'stream_protocol'


@pytest.mark.asyncio
async def test_responses_complete_arguments_event_is_sufficient_when_no_delta_was_emitted():
    call = {
        'type': 'function_call',
        'id': 'item',
        'call_id': 'call',
        'name': 'get_status',
        'arguments': '{}',
    }
    events = [
        {
            'type': 'response.output_item.added',
            'output_index': 0,
            'item': {**call, 'arguments': ''},
        },
        {
            'type': 'response.function_call_arguments.done',
            'output_index': 0,
            'item_id': 'item',
            'arguments': '{}',
        },
        {'type': 'response.completed', 'response': {**simple_response('openai'), 'output': [call]}},
    ]
    p = provider_class()(
        profile(),
        credential_getter=credentials,
        transport=httpx.MockTransport(
            lambda outgoing: httpx.Response(
                200, content=sse(events), headers={'content-type': 'text/event-stream'}
            )
        ),
    )
    try:
        result = await p.chat(request(tools=(ToolDefinition(name='get_status', strict=False),)))
    finally:
        await p.close()
    assert result.tool_calls[0].arguments == {} and result.tool_calls[0].id == 'call'


@pytest.mark.asyncio
async def test_sync_credential_getter_cannot_block_the_event_loop_past_request_deadline():
    def slow_getter(value, cancel=None):
        time.sleep(2)
        return credentials(value)

    selected = profile().model_copy(update={'options': GenerationOptions(timeout_seconds=1)})
    p = provider_class()(
        selected,
        credential_getter=slow_getter,
        transport=httpx.MockTransport(
            lambda outgoing: httpx.Response(200, json=simple_response('openai'))
        ),
    )
    start = time.monotonic()
    try:
        with pytest.raises(ProviderError) as caught:
            await p.chat(request())
    finally:
        await p.close()
    assert caught.value.code == 'timeout' and time.monotonic() - start < 1.5
