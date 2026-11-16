"""기존 로컬 OllamaClient를 보존하는 공통 제공자 어댑터."""

from __future__ import annotations

import inspect
import json
import uuid

import httpx
from pydantic import ValidationError

from recheck.ollama import OllamaClient, OllamaError
from .contracts import (
    ModelDescriptor,
    ProviderError,
    ProviderProfile,
    ProviderRequest,
    ProviderResponse,
    ToolCall,
    emit,
    redact,
    validate_tool_calls,
)
from .http_provider import _history, _json, deadline


class _LimitedStream(httpx.AsyncByteStream):
    def __init__(self, source, limit):
        self.source, self.limit = source, limit

    async def __aiter__(self):
        total = 0
        async for part in self.source:
            total += len(part)
            if total > self.limit:
                raise ProviderError('response_limit', '로컬 AI 응답이 허용된 크기를 초과했습니다')
            yield part

    async def aclose(self):
        await self.source.aclose()


class _LimitedTransport(httpx.AsyncBaseTransport):
    def __init__(self, limit):
        self.inner = httpx.AsyncHTTPTransport(trust_env=False)
        self.limit, self.closed = limit, False

    async def handle_async_request(self, request):
        request.headers['accept-encoding'] = 'identity'
        response = await self.inner.handle_async_request(request)
        if response.headers.get('content-encoding', 'identity').lower() not in ('', 'identity'):
            await response.aclose()
            raise ProviderError(
                'response_encoding', '로컬 AI의 압축 응답은 응답 상한 검사에서 허용하지 않습니다'
            )
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            stream=_LimitedStream(response.stream, self.limit),
            extensions=response.extensions,
        )

    async def aclose(self):
        if not self.closed:
            self.closed = True
            await self.inner.aclose()


class OllamaProvider:
    def __init__(self, profile: ProviderProfile, client_factory=OllamaClient):
        if profile.provider != 'ollama' or profile.auth_mode != 'none':
            raise ValueError('Ollama 로컬 프로필이 필요합니다')
        self.profile = profile.model_copy(deep=True)
        self._limit = min(profile.options.max_response_bytes, 2 * 1024 * 1024)
        self._transport = None
        kwargs = dict(timeout=profile.options.timeout_seconds)
        parameters = inspect.signature(client_factory).parameters
        if 'transport' in parameters or any(
            value.kind == inspect.Parameter.VAR_KEYWORD for value in parameters.values()
        ):
            self._transport = _LimitedTransport(self._limit)
            kwargs['transport'] = self._transport
        try:
            self.native = client_factory(profile.endpoint, **kwargs)
        except OllamaError as exc:
            raise ProviderError(exc.code, str(exc), redact(exc.details)) from None
        self._closed = False

    async def close(self):
        self._closed = True
        try:
            await self.native.close()
        finally:
            if self._transport:
                await self._transport.aclose()

    def _bound(self, value):
        if len(json.dumps(value, ensure_ascii=False, default=str).encode('utf-8')) > self._limit:
            raise ProviderError('response_limit', '로컬 AI 응답이 허용된 크기를 초과했습니다')
        return value

    async def _run(self, operation, cancel):
        if self._closed:
            raise ProviderError('closed', '이미 닫힌 로컬 AI 연결입니다')
        try:
            return await deadline(operation(), cancel, self.profile.options.timeout_seconds)
        except OllamaError as exc:
            code = {
                'response_timeout': 'timeout',
                'connection_failed': 'network',
                'stream_transport': 'network',
                'output_limit': 'response_limit',
            }.get(exc.code, exc.code)
            raise ProviderError(code, redact(str(exc)), redact(exc.details)) from None
        except (TypeError, AttributeError, KeyError, IndexError, RecursionError, ValidationError):
            raise ProviderError(
                'malformed_response', '로컬 AI 응답의 메시지·도구 형식이 잘못되었습니다'
            ) from None

    async def probe(self, cancel=None):
        self.profile.authorize((), purpose='probe')

        async def operation():
            value = self._bound(await self.native.probe(cancel=cancel))
            return {
                **redact(value),
                'authenticated': False,
                'inference_verified': False,
                'provider': 'ollama',
                'auth_mode': 'none',
            }

        return await self._run(operation, cancel)

    async def list_models(self, cancel=None):
        self.profile.authorize((), purpose='probe')

        async def operation():
            rows = self._bound(await self.native.models(cancel=cancel))
            return [
                ModelDescriptor(
                    id=row['name'],
                    display_name=row['name'],
                    digest=row.get('digest', ''),
                    metadata=redact(row),
                )
                for row in rows
            ]

        return await self._run(operation, cancel)

    async def chat(self, request: ProviderRequest, cancel=None, on_event=None):
        request = request.model_copy(deep=True)
        self.profile.authorize(request.evidence_kinds, purpose=request.purpose)

        async def operation():
            previous = _history(request)
            messages = []
            for message in request.messages:
                item = dict(
                    role='system' if message.role == 'developer' else message.role,
                    content=message.content,
                )
                if message.role == 'tool':
                    item['tool_name'] = previous[message.tool_call_id].name
                if message.tool_calls:
                    item['tool_calls'] = [
                        dict(function=dict(name=call.name, arguments=call.arguments))
                        for call in message.tool_calls
                    ]
                messages.append(item)
            schema = (
                request.output_schema if self.profile.structured_mode == 'native_schema' else None
            )
            if request.output_schema is not None and schema is None:
                messages.insert(
                    0,
                    dict(
                        role='system',
                        content='다음 JSON Schema에 맞는 JSON만 출력하세요: '
                        + json.dumps(request.output_schema, ensure_ascii=False),
                    ),
                )
            tools = [
                dict(
                    type='function',
                    function=tool.model_dump(include={'name', 'description', 'parameters'}),
                )
                for tool in request.tools
            ]
            chunks, observed_bytes = 0, 0

            async def progress(value):
                nonlocal chunks, observed_bytes
                chunks += 1
                observed_bytes += len(
                    json.dumps(value, ensure_ascii=False, default=str).encode('utf-8')
                )
                if observed_bytes > self._limit:
                    raise ProviderError(
                        'response_limit', '로컬 AI 스트림이 허용된 크기를 초과했습니다'
                    )
                if chunks == 1 or chunks % 32 == 0:
                    await emit(
                        on_event,
                        dict(
                            kind='provider_progress',
                            provider='ollama',
                            bytes_received=observed_bytes,
                            chunks=chunks,
                        ),
                    )

            result = self._bound(
                await self.native.chat(
                    self.profile.model,
                    messages,
                    format=schema,
                    tools=tools or None,
                    cancel=cancel,
                    options=dict(
                        num_ctx=self.profile.options.context_budget,
                        num_predict=self.profile.options.max_output_tokens,
                        temperature=self.profile.options.temperature,
                    ),
                    on_event=progress,
                )
            )
            message = result.get('message', {})
            if not isinstance(message, dict) or not isinstance(message.get('content', ''), str):
                raise ProviderError(
                    'malformed_response', '로컬 AI 응답 메시지 형식이 잘못되었습니다'
                )
            raw_finish = str(result.get('done_reason', 'stop'))
            finish = (
                'length'
                if raw_finish == 'length'
                else 'stop' if raw_finish == 'stop' else 'incomplete'
            )
            calls, generated = [], []
            if finish == 'stop':
                for index, item in enumerate(message.get('tool_calls', [])):
                    if not isinstance(item, dict) or not isinstance(item.get('function'), dict):
                        raise ProviderError(
                            'tool_call_identity', '로컬 AI 도구 호출 형식이 잘못되었습니다'
                        )
                    function = item['function']
                    arguments = function.get('arguments')
                    if isinstance(arguments, str):
                        try:
                            arguments = _json(arguments)
                        except ProviderError:
                            raise ProviderError(
                                'tool_arguments', '로컬 AI 도구 인자가 완성된 JSON 객체가 아닙니다'
                            ) from None
                    call_id = item.get('id')
                    if not call_id:
                        call_id = 'ollama_' + uuid.uuid4().hex
                        generated.append(
                            dict(
                                index=index,
                                id=call_id,
                                reason='native 응답에 호출 ID가 없어 엔진이 새 식별자를 발급함',
                            )
                        )
                    try:
                        calls.append(
                            ToolCall(id=call_id, name=function.get('name'), arguments=arguments)
                        )
                    except ValidationError:
                        raise ProviderError(
                            'tool_call_identity', '로컬 AI 도구 호출 ID·이름·인자가 잘못되었습니다'
                        ) from None
                validate_tool_calls(calls, request.tools)
                finish = (
                    'tool_calls'
                    if calls
                    else 'stop' if message.get('content', '').strip() else 'empty'
                )
            usage = dict(
                input_tokens=result.get('prompt_eval_count'),
                output_tokens=result.get('eval_count'),
                native={
                    key: result[key]
                    for key in (
                        'prompt_eval_count',
                        'eval_count',
                        'prompt_eval_duration',
                        'eval_duration',
                        'load_duration',
                        'total_duration',
                    )
                    if key in result
                },
            )
            return ProviderResponse(
                content=redact(message.get('content', '')),
                tool_calls=tuple(calls),
                finish_reason=finish,
                raw_finish_reason=raw_finish,
                usage=usage,
                model=result.get('model', self.profile.model),
                provider='ollama',
                auth_mode='none',
                raw=redact(result),
                provenance=dict(
                    endpoint=self.profile.endpoint,
                    execution=redact(result.get('_execution', {})),
                    profile_fingerprint=self.profile.fingerprint(),
                    synthetic_tool_ids=generated,
                    schema_mode=self.profile.structured_mode,
                ),
            )

        return await self._run(operation, cancel)
