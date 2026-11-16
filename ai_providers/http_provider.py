"""실행 권한이 없는 HTTP/SSE 어댑터. 원격 원문 조각은 이벤트에 내보내지 않는다."""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import json
import time
from typing import Any

import httpx
from pydantic import ValidationError

from .contracts import (
    CredentialMaterial,
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


def strict_schema_supported(schema):
    """보수적인 native strict 부분집합. 지원 여부만 판정하며 원본을 변경하지 않는다."""
    # 공식 structured-outputs의 root/object/required/format/size 제한. 지원 모델의
    # 모든 스키마를 보장하는 판정이 아니며, 불명확한 키워드는 무료 선제 거부한다.
    if not isinstance(schema, dict) or schema.get('type') != 'object' or 'anyOf' in schema:
        return False
    allowed = {
        'type',
        'properties',
        'required',
        'additionalProperties',
        'items',
        'enum',
        'const',
        'anyOf',
        '$defs',
        '$ref',
        'description',
        'title',
        'minimum',
        'maximum',
        'exclusiveMinimum',
        'exclusiveMaximum',
        'multipleOf',
        'minLength',
        'maxLength',
        'pattern',
        'format',
        'minItems',
        'maxItems',
    }
    counts = {'properties': 0, 'strings': 0, 'enums': 0}

    def text_size(value):
        return len(value) if isinstance(value, str) else 0

    def visit(node, depth=0):
        if not isinstance(node, dict) or any(key not in allowed for key in node):
            return False
        kinds = node.get('type', [])
        kinds = [kinds] if isinstance(kinds, str) else kinds
        if not isinstance(kinds, list) or any(
            x not in ('object', 'array', 'string', 'number', 'integer', 'boolean', 'null')
            for x in kinds
        ):
            return False
        if not kinds and not any(key in node for key in ('$ref', 'anyOf', 'enum', 'const')):
            return False
        if 'format' in node and node['format'] not in (
            'date-time',
            'time',
            'date',
            'duration',
            'email',
            'hostname',
            'ipv4',
            'ipv6',
            'uuid',
        ):
            return False
        if 'object' in kinds or 'properties' in node:
            props = node.get('properties', {})
            depth += 1
            if (
                depth > 10
                or not isinstance(props, dict)
                or node.get('additionalProperties') is not False
                or set(node.get('required', [])) != set(props)
            ):
                return False
            counts['properties'] += len(props)
            counts['strings'] += sum(map(len, props))
            if not all(visit(item, depth) for item in props.values()):
                return False
        if 'array' in kinds and 'items' not in node:
            return False
        if 'items' in node and not visit(node['items'], depth):
            return False
        if 'anyOf' in node and (
            not isinstance(node['anyOf'], list) or not all(visit(x, depth) for x in node['anyOf'])
        ):
            return False
        if '$defs' in node:
            if not isinstance(node['$defs'], dict):
                return False
            counts['strings'] += sum(map(len, node['$defs']))
            if not all(visit(x, 0) for x in node['$defs'].values()):
                return False
        if 'enum' in node:
            values = node['enum']
            size = sum(text_size(x) for x in values)
            if len(values) > 250 and size > 15000:
                return False
            counts['enums'] += len(values)
            counts['strings'] += size
        if 'const' in node:
            counts['strings'] += text_size(node['const'])
        return (
            counts['properties'] <= 5000 and counts['strings'] <= 120000 and counts['enums'] <= 1000
        )

    try:
        return visit(schema)
    except (RecursionError, TypeError, ValueError):
        return False


def native_schema_preflight(profile, request):
    if (
        request.output_schema is not None
        and profile.provider in ('openai', 'compatible')
        and profile.structured_mode == 'native_schema'
        and not strict_schema_supported(request.output_schema)
    ):
        raise ProviderError(
            'schema_unsupported',
            '선택 JSON Schema는 API strict 부분집합과 다릅니다. JSON 프롬프트 방식을 명시적으로 선택하세요',
            {'available_mode': 'json_prompt', 'automatic_fallback': False},
        )
    validate_tool_calls((), request.tools)
    if profile.provider in ('openai', 'compatible'):
        for tool in request.tools:
            if tool.strict and not strict_schema_supported(tool.parameters):
                raise ProviderError(
                    'schema_unsupported',
                    '도구 strict 스키마가 API 지원 범위와 다릅니다. 호출자가 strict:false 정책을 명시하세요',
                    {'tool': tool.name, 'automatic_fallback': False},
                )


def _json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('중복 JSON 키')
            result[key] = value
        return result

    def constant(value):
        raise ValueError('유한하지 않은 JSON 값')

    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, TypeError, RecursionError):
        raise ProviderError(
            'malformed_response', 'AI 서버가 올바른 JSON 응답을 반환하지 않았습니다'
        ) from None


async def deadline(awaitable, cancel, seconds):
    """인증 준비부터 응답 회수까지 한 요청 전체를 제한하고 대기 작업을 회수한다."""
    task = asyncio.ensure_future(awaitable)
    expires = time.monotonic() + seconds
    try:
        while not task.done():
            if cancel is not None and cancel.is_set():
                raise ProviderError('cancelled', 'AI 요청을 취소했습니다')
            remaining = expires - time.monotonic()
            if remaining <= 0:
                raise ProviderError(
                    'timeout',
                    'AI 요청 전체 제한 시간을 초과했습니다',
                    duplicate_charge_possible=True,
                )
            await asyncio.wait({task}, timeout=min(0.05, remaining))
        if cancel is not None and cancel.is_set():
            raise ProviderError('cancelled', 'AI 요청을 취소했습니다')
        if time.monotonic() > expires:
            raise ProviderError(
                'timeout', 'AI 요청 전체 제한 시간을 초과했습니다', duplicate_charge_possible=True
            )
        return await task
    except asyncio.CancelledError:
        raise ProviderError('cancelled', 'AI 요청을 취소했습니다') from None
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def error_code(status, body):
    text = json.dumps(body, ensure_ascii=False).lower()
    if status == 401:
        return 'authentication'
    if status == 403:
        return 'permission'
    if (
        any(
            term in text
            for term in (
                'insufficient_quota',
                'billing',
                'credit balance',
                'payment required',
                'quota_exceeded',
            )
        )
        or status == 402
    ):
        return 'quota'
    if any(
        term in text
        for term in (
            'context_length',
            'context window',
            'context_exceeded',
            'too many tokens',
            'input too long',
        )
    ):
        return 'context'
    if status == 429 or 'rate_limit' in text or 'rate limit' in text:
        return 'rate_limit'
    if any(term in text for term in ('safety', 'content_filter', 'blocked_by_policy')):
        return 'safety'
    if 'timeout' in text or 'deadline' in text:
        return 'timeout'
    return 'service' if status and status >= 500 else 'api_error'


def _sse(body):
    try:
        text = body.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise ProviderError(
            'malformed_response', 'AI 응답의 UTF-8 인코딩이 잘못되었습니다'
        ) from None
    events, data, event_name = [], [], ''

    def finish():
        nonlocal data, event_name
        if data:
            payload = '\n'.join(data)
            if payload.strip() == '[DONE]':
                events.append({'_done': True})
            else:
                value = _json(payload)
                if not isinstance(value, dict):
                    raise ProviderError('malformed_response', 'SSE 데이터는 JSON 객체여야 합니다')
                if event_name and 'type' not in value and 'event_type' not in value:
                    value = {**value, 'type': event_name}
                events.append(value)
        data, event_name = [], ''

    for line in text.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        if not line:
            finish()
        elif line.startswith('data:'):
            data.append(line[5:].removeprefix(' '))
        elif line.startswith('event:'):
            event_name = line[6:].strip()
        # Comments, event IDs and retry hints cannot execute or expand tools.
    finish()
    return events


def _history(request):
    calls = {}
    for message in request.messages:
        for call in message.tool_calls:
            if call.id in calls:
                raise ProviderError('tool_call_identity', '대화 이력의 도구 호출 ID가 중복됩니다')
            calls[call.id] = call
        if message.role == 'tool':
            call = calls.get(message.tool_call_id)
            if call is None or message.name and message.name != call.name:
                raise ProviderError(
                    'tool_call_identity', '도구 결과에 대응하는 원래 호출이 없습니다'
                )
    return calls


class HTTPProvider:
    def __init__(self, profile: ProviderProfile, credential_getter=None, transport=None):
        if profile.provider == 'ollama':
            raise ValueError('Ollama는 로컬 전용 어댑터를 사용하세요')
        self.profile = profile.model_copy(deep=True)
        self.credential_getter = credential_getter
        self.http = httpx.AsyncClient(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(
                profile.options.timeout_seconds, connect=min(10, profile.options.timeout_seconds)
            ),
        )
        # OAuth refresh may supply a new token to a concurrent request. Redaction
        # and audit identity must remain bound to that request's actual token.
        self._authentication = contextvars.ContextVar('provider_authentication', default=((), 0))
        self._closed = False

    @property
    def _secrets(self):
        return self._authentication.get()[0]

    @property
    def _credential_revision(self):
        return self._authentication.get()[1]

    async def close(self):
        self._closed = True
        await self.http.aclose()

    async def _headers(self, cancel):
        if self._closed:
            raise ProviderError('closed', '이미 닫힌 AI 연결입니다')
        headers = {
            'accept': 'application/json, text/event-stream',
            'content-type': 'application/json',
            'accept-encoding': 'identity',
        }
        if self.profile.auth_mode != 'none':
            if self.credential_getter is None:
                raise ProviderError(
                    'credential_required',
                    '이 연결의 인증을 보안 입력 또는 공식 로그인으로 연결하세요',
                )
            try:
                if inspect.iscoroutinefunction(self.credential_getter):
                    material = self.credential_getter(self.profile, cancel=cancel)
                else:
                    material = await asyncio.to_thread(
                        self.credential_getter, self.profile, cancel=cancel
                    )
                if inspect.isawaitable(material):
                    material = await material
            except ProviderError:
                raise
            except Exception:
                raise ProviderError(
                    'credential', '보안 저장소에서 AI 인증을 읽지 못했습니다'
                ) from None
            if not isinstance(material, CredentialMaterial):
                raise ProviderError('credential', 'AI 인증 자료의 형식이 잘못되었습니다')
            material.require_origin(self.profile.origin)
            if material.version != self.profile.credential_version:
                raise ProviderError(
                    'credential_version', '선택한 인증이 변경되었습니다. 연결을 다시 선택하세요'
                )
            secret = material.value.get_secret_value()
            if not secret or not secret.isascii() or any(ord(char) < 32 for char in secret):
                raise ProviderError('credential', 'AI 인증 값이 비어 있거나 잘못되었습니다')
            permitted = {
                'openai-organization',
                'openai-project',
                'chatgpt-account-id',
                'anthropic-workspace-id',
                'x-goog-user-project',
            }
            extras = {
                name.lower(): value.get_secret_value()
                for name, value in material.extra_headers.items()
            }
            if not set(extras) <= permitted or any(
                '\r' in value or '\n' in value for value in extras.values()
            ):
                raise ProviderError('credential_headers', '허용되지 않은 인증 부가 헤더입니다')
            self._authentication.set(
                ((secret, *extras.values()), getattr(material, 'token_revision', 0))
            )
            if self.profile.provider == 'gemini' and self.profile.auth_mode == 'api_key':
                headers['x-goog-api-key'] = secret
            else:
                headers['authorization'] = 'Bearer ' + secret
            headers.update(extras)
        if self.profile.provider == 'anthropic':
            headers['anthropic-version'] = self.profile.api_version or '2026-10-21'
            if self.profile.workspace_id:
                headers['anthropic-workspace-id'] = self.profile.workspace_id
        if self.profile.provider == 'gemini':
            headers['Api-Revision'] = self.profile.api_version or '2026-05-20'
            if self.profile.google_project_id:
                headers['x-goog-user-project'] = self.profile.google_project_id
        if any(
            not value.isascii() or any(ord(char) < 32 for char in value)
            for value in headers.values()
        ):
            raise ProviderError(
                'credential_headers', 'AI 인증 또는 버전 헤더의 형식이 잘못되었습니다'
            )
        return headers

    def _payload(self, request):
        calls = _history(request)
        profile = self.profile
        kind = profile.provider
        systems = [
            message.content
            for message in request.messages
            if message.role in ('system', 'developer') and message.content
        ]
        ordinary = [
            message for message in request.messages if message.role not in ('system', 'developer')
        ]
        tools = [tool.model_dump() for tool in request.tools]
        body = {'model': profile.model, 'stream': request.stream}
        if kind == 'openai':
            body.update(store=False, input=[])
            if systems:
                body['instructions'] = '\n\n'.join(systems)
            for message in ordinary:
                if message.role == 'tool':
                    body['input'].append(
                        dict(
                            type='function_call_output',
                            call_id=message.tool_call_id,
                            output=message.content,
                        )
                    )
                else:
                    if message.content:
                        body['input'].append(dict(role=message.role, content=message.content))
                    for call in message.tool_calls:
                        item = dict(
                            type='function_call',
                            call_id=call.id,
                            name=call.name,
                            arguments=json.dumps(call.arguments, ensure_ascii=False),
                        )
                        if profile.auth_mode == 'chatgpt':
                            item['namespace'] = 'rebuild'
                        body['input'].append(item)
            if tools:
                declared = [dict(type='function', **tool) for tool in tools]
                body['tools'] = (
                    [
                        dict(
                            type='namespace',
                            name='rebuild',
                            description='허용된 Rebuild 분석 도구',
                            tools=declared,
                        )
                    ]
                    if profile.auth_mode == 'chatgpt'
                    else declared
                )
            if request.output_schema is not None and profile.structured_mode == 'native_schema':
                body['text'] = {
                    'format': {
                        'type': 'json_schema',
                        'name': 'rebuild_result',
                        'strict': True,
                        'schema': request.output_schema,
                    }
                }
            if profile.auth_mode == 'chatgpt':
                body['stream'] = True
            else:
                body['max_output_tokens'] = profile.options.max_output_tokens
        elif kind == 'anthropic':
            body.update(max_tokens=profile.options.max_output_tokens, messages=[])
            if systems:
                body['system'] = '\n\n'.join(systems)
            for message in ordinary:
                if message.role == 'tool':
                    content = [
                        dict(
                            type='tool_result',
                            tool_use_id=message.tool_call_id,
                            content=message.content,
                        )
                    ]
                    role = 'user'
                else:
                    role = message.role
                    content = [dict(type='text', text=message.content)] if message.content else []
                    content.extend(
                        dict(type='tool_use', id=call.id, name=call.name, input=call.arguments)
                        for call in message.tool_calls
                    )
                body['messages'].append(dict(role=role, content=content))
            if tools:
                body['tools'] = [
                    dict(
                        name=tool['name'],
                        description=tool['description'],
                        input_schema=tool['parameters'],
                        strict=tool['strict'],
                    )
                    for tool in tools
                ]
            if request.output_schema is not None and profile.structured_mode == 'native_schema':
                body['output_config'] = {
                    'format': {'type': 'json_schema', 'schema': request.output_schema}
                }
        elif kind == 'gemini':
            body.update(
                store=False,
                input=[],
                generation_config={'max_output_tokens': profile.options.max_output_tokens},
            )
            if systems:
                body['system_instruction'] = '\n\n'.join(systems)
            for message in ordinary:
                if message.role == 'tool':
                    body['input'].append(
                        dict(
                            type='function_result',
                            name=calls[message.tool_call_id].name,
                            call_id=message.tool_call_id,
                            result=[dict(type='text', text=message.content)],
                        )
                    )
                else:
                    if message.content:
                        body['input'].append(
                            dict(
                                type=(
                                    'model_output' if message.role == 'assistant' else 'user_input'
                                ),
                                content=[dict(type='text', text=message.content)],
                            )
                        )
                    body['input'].extend(
                        dict(
                            type='function_call',
                            id=call.id,
                            name=call.name,
                            arguments=call.arguments,
                        )
                        for call in message.tool_calls
                    )
            if tools:
                body['tools'] = [
                    dict(
                        type='function',
                        name=tool['name'],
                        description=tool['description'],
                        parameters=tool['parameters'],
                    )
                    for tool in tools
                ]
            if request.output_schema is not None and profile.structured_mode == 'native_schema':
                body['response_format'] = dict(
                    type='text', mime_type='application/json', schema=request.output_schema
                )
        else:
            body.update(max_tokens=profile.options.max_output_tokens, messages=[])
            for message in request.messages:
                item = dict(role=message.role, content=message.content)
                if message.role == 'tool':
                    item['tool_call_id'] = message.tool_call_id
                if message.tool_calls:
                    item['tool_calls'] = [
                        dict(
                            id=call.id,
                            type='function',
                            function=dict(
                                name=call.name,
                                arguments=json.dumps(call.arguments, ensure_ascii=False),
                            ),
                        )
                        for call in message.tool_calls
                    ]
                body['messages'].append(item)
            if tools:
                body['tools'] = [dict(type='function', function=tool) for tool in tools]
            if request.output_schema is not None and profile.structured_mode == 'native_schema':
                body['response_format'] = {
                    'type': 'json_schema',
                    'json_schema': {
                        'name': 'rebuild_result',
                        'strict': True,
                        'schema': request.output_schema,
                    },
                }
        if request.output_schema is not None and profile.structured_mode == 'json_prompt':
            instruction = '\n반드시 다음 JSON Schema에 맞는 JSON만 출력하세요: ' + json.dumps(
                request.output_schema, ensure_ascii=False
            )
            if kind == 'openai':
                body['instructions'] = body.get('instructions', '') + instruction
            elif kind == 'anthropic':
                body['system'] = body.get('system', '') + instruction
            elif kind == 'gemini':
                body['system_instruction'] = body.get('system_instruction', '') + instruction
            else:
                body['messages'].insert(0, dict(role='system', content=instruction))
        return body

    async def _wire(self, method, suffix, body=None, cancel=None, on_event=None):
        headers = await self._headers(cancel)
        url = self.profile.endpoint.rstrip('/') + '/' + suffix.lstrip('/')
        limit = min(self.profile.options.max_response_bytes, 2 * 1024 * 1024)
        try:
            async with self.http.stream(method, url, headers=headers, json=body) as response:
                if response.headers.get('content-encoding', 'identity').lower() not in (
                    '',
                    'identity',
                ):
                    raise ProviderError(
                        'response_encoding',
                        '응답 크기 상한을 보장하기 위해 압축 AI 응답을 거부했습니다',
                    )
                chunks, total, count = [], 0, 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    count += 1
                    if total > limit:
                        raise ProviderError(
                            'response_limit',
                            'AI 서버 응답이 허용된 크기를 초과했습니다',
                            duplicate_charge_possible=method == 'POST',
                        )
                    chunks.append(chunk)
                    if count == 1 or count % 32 == 0:
                        await emit(
                            on_event,
                            dict(
                                kind='provider_progress',
                                provider=self.profile.provider,
                                bytes_received=total,
                                chunks=count,
                            ),
                        )
                raw = b''.join(chunks)
                if 300 <= response.status_code < 400:
                    raise ProviderError(
                        'redirect',
                        'AI 서버의 다른 주소로 이동하는 응답을 거부했습니다',
                        status_code=response.status_code,
                    )
                if response.status_code >= 400:
                    try:
                        value = _json(raw)
                    except ProviderError:
                        value = {'message': raw.decode('utf-8', 'replace')[:2000]}
                    retry = response.headers.get('retry-after')
                    try:
                        retry = min(3600, max(0, float(retry))) if retry is not None else None
                    except ValueError:
                        retry = None
                    raise ProviderError(
                        error_code(response.status_code, value),
                        'AI 서버가 요청을 거부했습니다 (HTTP ' + str(response.status_code) + ')',
                        redact(value, self._secrets),
                        status_code=response.status_code,
                        retry_after=retry,
                        duplicate_charge_possible=method == 'POST' and response.status_code >= 500,
                    )
                await emit(
                    on_event,
                    dict(
                        kind='provider_received',
                        provider=self.profile.provider,
                        bytes_received=total,
                        chunks=count,
                    ),
                )
                return (
                    raw,
                    response.headers.get('content-type', ''),
                    response.headers.get('x-request-id', response.headers.get('request-id', '')),
                )
        except httpx.TimeoutException:
            raise ProviderError(
                'timeout',
                'AI 서버 응답 시간이 초과되었습니다',
                duplicate_charge_possible=method == 'POST',
            ) from None
        except httpx.ConnectError:
            raise ProviderError('network', 'AI 서버에 연결하지 못했습니다') from None
        except httpx.HTTPError:
            raise ProviderError(
                'network',
                'AI 서버와 통신하는 중 연결이 끊겼습니다',
                duplicate_charge_possible=method == 'POST',
            ) from None

    async def list_models(self, cancel=None):
        self.profile.authorize((), purpose='probe')

        async def collect():
            data, _, _ = await self._wire('GET', 'models', cancel=cancel)
            value = _json(data)
            if not isinstance(value, dict):
                raise ProviderError('malformed_response', 'AI 모델 목록의 형식이 잘못되었습니다')
            account_catalog = self.profile.auth_mode == 'chatgpt' and 'models' in value
            rows = value.get(
                'models' if self.profile.provider == 'gemini' or account_catalog else 'data'
            )
            if not isinstance(rows, list) or len(rows) > 10000:
                raise ProviderError(
                    'malformed_response', 'AI 모델 목록이 없거나 상한을 초과했습니다'
                )
            models = []
            identifiers = set()
            for row in rows:
                if not isinstance(row, dict):
                    raise ProviderError('malformed_response', 'AI 모델 식별자가 잘못되었습니다')
                if account_catalog and row.get('visibility') != 'list':
                    continue
                identifier = row.get('slug') if account_catalog else row.get('id', row.get('name'))
                if (
                    not isinstance(identifier, str)
                    or not identifier.strip()
                    or len(identifier) > 512
                    or any(ord(char) < 32 for char in identifier)
                ):
                    raise ProviderError('malformed_response', 'AI 모델 식별자가 잘못되었습니다')
                identifier = identifier.removeprefix('models/')
                if identifier in identifiers:
                    raise ProviderError('malformed_response', 'AI 모델 식별자가 중복되었습니다')
                identifiers.add(identifier)
                models.append(
                    ModelDescriptor(
                        id=redact(identifier, self._secrets),
                        display_name=redact(
                            row.get('display_name', row.get('displayName', identifier)),
                            self._secrets,
                        ),
                        metadata=redact(row, self._secrets),
                    )
                )
            return models

        return await deadline(collect(), cancel, self.profile.options.timeout_seconds)

    async def probe(self, cancel=None):
        try:
            models = await self.list_models(cancel=cancel)
            return dict(
                connected=True,
                authenticated=self.profile.auth_mode != 'none',
                inference_verified=False,
                model_count=len(models),
                provider=self.profile.provider,
                auth_mode=self.profile.auth_mode,
            )
        except ProviderError as exc:
            if exc.code == 'cancelled':
                raise
            return dict(
                connected=exc.status_code is not None,
                authenticated=False,
                inference_verified=False,
                provider=self.profile.provider,
                code=exc.code,
                error=redact(str(exc), self._secrets),
            )

    async def chat(self, request: ProviderRequest, cancel=None, on_event=None):
        request = request.model_copy(deep=True)
        self.profile.authorize(request.evidence_kinds, purpose=request.purpose)
        native_schema_preflight(self.profile, request)

        async def collect():
            suffix = dict(
                openai='responses',
                anthropic='messages',
                gemini='interactions',
                compatible='chat/completions',
            )[self.profile.provider]
            wire, content_type, request_id = await self._wire(
                'POST', suffix, self._payload(request), cancel, on_event
            )
            is_stream = 'text/event-stream' in content_type or wire.lstrip().startswith(
                (b'data:', b'event:', b':')
            )
            try:
                native = self._assemble(_sse(wire)) if is_stream else _json(wire)
                if not isinstance(native, dict):
                    raise ProviderError('malformed_response', 'AI 완료 응답은 JSON 객체여야 합니다')
                return self._response(native, request, request_id, is_stream)
            except (
                TypeError,
                AttributeError,
                KeyError,
                IndexError,
                RecursionError,
                ValidationError,
            ):
                raise ProviderError(
                    'malformed_response', 'AI 응답의 메시지·도구·사용량 형식이 잘못되었습니다'
                ) from None

        return await deadline(collect(), cancel, self.profile.options.timeout_seconds)

    def _stream_error(self, event):
        value = event.get('error', event)
        raise ProviderError(
            error_code(None, value),
            'AI 스트림 처리 중 서버 오류가 발생했습니다',
            redact(value, self._secrets),
            duplicate_charge_possible=True,
        )

    def _assemble(self, events):
        kind = self.profile.provider
        final, metadata, blocks, stopped, finish, done = None, {}, {}, set(), '', False
        for event in events:
            if done:
                raise ProviderError(
                    'stream_protocol', 'AI 스트림의 종료 표식 뒤에 추가 데이터가 도착했습니다'
                )
            if event.get('_done'):
                done = True
                continue
            typ = event.get('event_type', event.get('type', ''))
            if typ in ('error', 'response.failed'):
                self._stream_error(event.get('response', event))
            if final is not None:
                if typ not in ('ping', 'interaction.status_update'):
                    raise ProviderError(
                        'stream_protocol', 'AI 완료 표식 뒤에 추가 응답이 도착했습니다'
                    )
                continue
            if kind == 'openai':
                if typ in ('response.completed', 'response.incomplete'):
                    final = event.get('response')
                elif typ == 'response.output_item.added':
                    index, item = event.get('output_index'), event.get('item')
                    if not isinstance(index, int) or not isinstance(item, dict) or index in blocks:
                        raise ProviderError(
                            'stream_protocol', '도구 출력 인덱스가 잘못되거나 중복됩니다'
                        )
                    blocks[index] = dict(item)
                elif typ == 'response.function_call_arguments.delta':
                    block = blocks.get(event.get('output_index'))
                    if (
                        block is None
                        or block.get('type') != 'function_call'
                        or event.get('item_id', block.get('id')) != block.get('id')
                    ):
                        raise ProviderError(
                            'stream_protocol', '도구 인자 조각의 호출 식별자가 다릅니다'
                        )
                    if not isinstance(event.get('delta'), str):
                        raise ProviderError('stream_protocol', '도구 인자 조각이 문자열이 아닙니다')
                    block['arguments'] = block.get('arguments', '') + event['delta']
                elif typ in ('response.function_call_arguments.done', 'response.output_item.done'):
                    index = event.get('output_index')
                    block = blocks.get(index)
                    if block is None:
                        raise ProviderError(
                            'stream_protocol', '시작하지 않은 도구 출력의 완료 표식입니다'
                        )
                    if typ == 'response.output_item.done':
                        item = event.get('item', {})
                        if (
                            block.get('type') == 'function_call'
                            and block.get('arguments')
                            and block['arguments'] != item.get('arguments')
                        ):
                            raise ProviderError(
                                'stream_protocol', '도구 인자 조각과 최종 인자가 다릅니다'
                            )
                        blocks[index] = item
                    elif block.get('arguments') and block['arguments'] != event.get('arguments'):
                        raise ProviderError(
                            'stream_protocol', '도구 인자 조각과 최종 인자가 다릅니다'
                        )
                    else:
                        block['arguments'] = event.get('arguments')
            elif kind in ('anthropic', 'gemini'):
                start = 'content_block_start' if kind == 'anthropic' else 'step.start'
                delta = 'content_block_delta' if kind == 'anthropic' else 'step.delta'
                stop = 'content_block_stop' if kind == 'anthropic' else 'step.stop'
                if typ in ('message_start', 'interaction.created'):
                    metadata.update(
                        event.get('message' if kind == 'anthropic' else 'interaction', {})
                    )
                elif typ == start:
                    index, block = event.get('index'), event.get(
                        'content_block' if kind == 'anthropic' else 'step'
                    )
                    if not isinstance(index, int) or index in blocks or not isinstance(block, dict):
                        raise ProviderError(
                            'stream_protocol', '응답 블록의 인덱스가 잘못되거나 중복됩니다'
                        )
                    blocks[index] = {**block, '_arguments': ''}
                elif typ == delta:
                    block, change = blocks.get(event.get('index')), event.get('delta', {})
                    if block is None or event.get('index') in stopped:
                        raise ProviderError(
                            'stream_protocol', '시작하지 않았거나 끝난 블록의 추가 조각입니다'
                        )
                    if change.get('type') in ('text_delta', 'text'):
                        block['text'] = block.get('text', '') + change.get('text', '')
                    elif change.get('type') in ('input_json_delta', 'arguments_delta'):
                        part = change.get('partial_json', change.get('arguments'))
                        if not isinstance(part, str):
                            raise ProviderError(
                                'stream_protocol', '도구 인자 조각이 문자열이 아닙니다'
                            )
                        block['_arguments'] += part
                elif typ == stop:
                    if event.get('index') not in blocks:
                        raise ProviderError(
                            'stream_protocol', '시작하지 않은 블록의 완료 표식입니다'
                        )
                    stopped.add(event['index'])
                elif typ == 'message_delta':
                    finish = event.get('delta', {}).get('stop_reason', finish)
                    metadata['usage'] = {**metadata.get('usage', {}), **event.get('usage', {})}
                elif typ in ('message_stop', 'interaction.completed'):
                    if set(blocks) != stopped:
                        raise ProviderError(
                            'incomplete_stream', '완료되지 않은 응답 블록이 있습니다'
                        )
                    assembled = []
                    for index in sorted(blocks):
                        block = dict(blocks[index])
                        arguments = block.pop('_arguments')
                        if arguments:
                            key = 'input' if kind == 'anthropic' else 'arguments'
                            if block.get(key) not in (None, {}):
                                raise ProviderError(
                                    'stream_protocol', '도구의 초기 인자와 분할 인자가 충돌합니다'
                                )
                            block[key] = _json(arguments)
                        if kind == 'gemini' and block.get('type') == 'model_output':
                            # Gemini may include the first output in step.start;
                            # later text deltas contain only the continuation.
                            content = list(block.get('content', []))
                            if block.get('text'):
                                content.append({'type': 'text', 'text': block['text']})
                            block = {
                                'type': 'model_output',
                                'content': content,
                            }
                        assembled.append(block)
                    final = {**metadata, **event.get('interaction', {})}
                    if kind == 'anthropic':
                        final.update(content=assembled, stop_reason=finish)
                    else:
                        final['steps'] = assembled
            else:
                if event.get('error'):
                    self._stream_error(event)
                if event.get('id'):
                    metadata['id'] = event['id']
                if event.get('model'):
                    metadata['model'] = event['model']
                if event.get('usage'):
                    metadata['usage'] = event['usage']
                choices = event.get('choices', [])
                if len(choices) > 1 or any(choice.get('index', 0) != 0 for choice in choices):
                    raise ProviderError(
                        'stream_protocol', '한 요청에 여러 응답 후보를 수신했습니다'
                    )
                for choice in choices:
                    change = choice.get('delta', {})
                    if finish and (change or choice.get('finish_reason') not in (None, finish)):
                        raise ProviderError(
                            'stream_protocol', 'AI 완료 판정 뒤에 추가 출력이 도착했습니다'
                        )
                    metadata['text'] = metadata.get('text', '') + (change.get('content') or '')
                    if change.get('refusal'):
                        metadata['refusal'] = metadata.get('refusal', '') + change['refusal']
                    for call in change.get('tool_calls', []):
                        index = call.get('index')
                        if not isinstance(index, int) or index < 0 or index > 31:
                            raise ProviderError(
                                'stream_protocol', '도구 호출 인덱스가 잘못되었습니다'
                            )
                        block = blocks.setdefault(index, {'id': '', 'name': '', 'arguments': ''})
                        for key in ('id',):
                            if call.get(key):
                                if block[key] and block[key] != call[key]:
                                    raise ProviderError(
                                        'stream_protocol', '도구 호출 ID가 중간에 변경되었습니다'
                                    )
                                block[key] = call[key]
                        fn = call.get('function', {})
                        block['name'] += fn.get('name', '')
                        block['arguments'] += fn.get('arguments', '')
                    if choice.get('finish_reason') is not None:
                        finish = choice['finish_reason']
        if kind == 'compatible':
            if not done or not finish:
                raise ProviderError(
                    'incomplete_stream', '완료 표식 없이 AI 스트림이 종료되었습니다'
                )
            final = {
                **metadata,
                'choices': [
                    {
                        'message': {
                            'content': metadata.get('text', ''),
                            'refusal': metadata.get('refusal'),
                            'tool_calls': [
                                dict(
                                    id=block['id'],
                                    function=dict(name=block['name'], arguments=block['arguments']),
                                )
                                for _, block in sorted(blocks.items())
                            ],
                        },
                        'finish_reason': finish,
                    }
                ],
            }
        if final is None:
            raise ProviderError('incomplete_stream', '완료 표식 없이 AI 스트림이 종료되었습니다')
        if kind == 'openai':
            streamed = [block for block in blocks.values() if block.get('type') == 'function_call']
            final_calls = [
                block for block in final.get('output', []) if block.get('type') == 'function_call'
            ]
            if streamed and [
                (item.get('call_id'), item.get('name'), item.get('arguments')) for item in streamed
            ] != [
                (item.get('call_id'), item.get('name'), item.get('arguments'))
                for item in final_calls
            ]:
                raise ProviderError('stream_protocol', '분할 도구 호출과 최종 응답이 다릅니다')
        # Preserve the provider's completed envelope, not credential-bearing raw
        # fragments that could disclose a secret when concatenated later.
        return final

    def _response(self, native, request, request_id, streamed):
        kind, content, calls, finish = self.profile.provider, [], [], 'stop'
        if native.get('error'):
            self._stream_error(native)
        if kind == 'openai':
            status = native.get('status')
            incomplete = native.get('incomplete_details')
            if incomplete is None:
                incomplete = {}
            if not isinstance(incomplete, dict):
                raise ProviderError(
                    'malformed_response', 'AI 응답의 완료 상태 형식이 잘못되었습니다'
                )
            if status == 'incomplete':
                finish = (
                    'length' if incomplete.get('reason') == 'max_output_tokens' else 'incomplete'
                )
            elif status != 'completed':
                finish = 'cancelled' if status == 'cancelled' else 'error'
            raw_finish = incomplete.get('reason') or str(status or '')
            for item in native.get('output', []):
                if item.get('type') == 'message':
                    for part in item.get('content', []):
                        if part.get('type') == 'refusal':
                            finish = 'refusal'
                        elif part.get('type') == 'output_text':
                            content.append(part.get('text', ''))
                elif item.get('type') == 'function_call':
                    calls.append((item.get('call_id'), item.get('name'), item.get('arguments')))
        elif kind == 'anthropic':
            raw_finish = native.get('stop_reason', '')
            finish = {
                'end_turn': 'stop',
                'stop_sequence': 'stop',
                'tool_use': 'tool_calls',
                'max_tokens': 'length',
                'refusal': 'refusal',
                'pause_turn': 'incomplete',
                'model_context_window_exceeded': 'length',
            }.get(raw_finish, 'incomplete')
            for item in native.get('content', []):
                if item.get('type') == 'text':
                    content.append(item.get('text', ''))
                elif item.get('type') == 'tool_use':
                    calls.append((item.get('id'), item.get('name'), item.get('input')))
                elif item.get('type') == 'refusal':
                    finish = 'refusal'
        elif kind == 'gemini':
            raw_finish = native.get('status', '')
            finish = {
                'completed': 'stop',
                'requires_action': 'tool_calls',
                'incomplete': 'length',
                'cancelled': 'cancelled',
                'requires_continuation': 'incomplete',
            }.get(raw_finish, 'error')
            for step in native.get('steps', []):
                if step.get('type') == 'model_output':
                    for item in step.get('content', []):
                        if item.get('type') == 'text':
                            content.append(item.get('text', ''))
                        elif item.get('type') in ('refusal', 'safety'):
                            finish = 'safety'
                elif step.get('type') == 'function_call':
                    calls.append((step.get('id'), step.get('name'), step.get('arguments')))
        else:
            choices = native.get('choices', [])
            if len(choices) != 1:
                raise ProviderError('malformed_response', 'AI 응답 후보는 정확히 하나여야 합니다')
            message = choices[0].get('message', {})
            raw_finish = choices[0].get('finish_reason', '')
            finish = {
                'stop': 'stop',
                'tool_calls': 'tool_calls',
                'length': 'length',
                'content_filter': 'safety',
            }.get(raw_finish, 'incomplete')
            if message.get('refusal'):
                finish = 'refusal'
            content.append(message.get('content') or '')
            calls = [
                (
                    item.get('id'),
                    item.get('function', {}).get('name'),
                    item.get('function', {}).get('arguments'),
                )
                for item in message.get('tool_calls', [])
            ]
        if not all(isinstance(text, str) for text in content):
            raise ProviderError('malformed_response', 'AI 응답 텍스트의 형식이 잘못되었습니다')
        parsed_calls = []
        if finish in ('stop', 'tool_calls'):
            for call_id, name, arguments in calls:
                if isinstance(arguments, str):
                    try:
                        arguments = _json(arguments)
                    except ProviderError:
                        raise ProviderError(
                            'tool_arguments', '도구 인자가 완성된 JSON 객체가 아닙니다'
                        ) from None
                try:
                    parsed_calls.append(ToolCall(id=call_id, name=name, arguments=arguments))
                except ValidationError:
                    raise ProviderError(
                        'tool_call_identity', '도구 호출 ID·이름·인자 형식이 잘못되었습니다'
                    ) from None
            validate_tool_calls(parsed_calls, request.tools)
            if parsed_calls:
                finish = 'tool_calls'
            elif finish == 'tool_calls':
                raise ProviderError(
                    'tool_call_identity', '도구 호출 종료를 수신했지만 완성된 호출이 없습니다'
                )
            elif not ''.join(content).strip():
                finish = 'empty'
        text = redact(''.join(content), self._secrets)
        safe_calls = []
        for call in parsed_calls:
            safe_arguments = redact(call.arguments, self._secrets)
            if (
                safe_arguments != call.arguments
                or redact(call.id, self._secrets) != call.id
                or redact(call.name, self._secrets) != call.name
            ):
                raise ProviderError(
                    'credential_reflection',
                    '도구 인자에 인증 비밀값이 반영되어 요청을 거부했습니다',
                )
            safe_calls.append(call)
        provenance = dict(
            endpoint=self.profile.endpoint,
            profile_fingerprint=self.profile.fingerprint(),
            token_revision=self._credential_revision,
            streamed=streamed,
            execution_location=(
                '호환 서버의 실제 추론 위치 미확인' if kind == 'compatible' else '외부 제공자 서버'
            ),
            schema_mode=self.profile.structured_mode,
            actual_authentication_verified=False,
            effective_options=dict(
                timeout_seconds=self.profile.options.timeout_seconds,
                max_response_bytes=min(self.profile.options.max_response_bytes, 2 * 1024 * 1024),
                max_output_tokens=(
                    None
                    if self.profile.auth_mode == 'chatgpt'
                    else self.profile.options.max_output_tokens
                ),
                temperature_sent=False,
            ),
            api_version=(
                (self.profile.api_version or '2026-10-21')
                if kind == 'anthropic'
                else (self.profile.api_version or '2026-05-20') if kind == 'gemini' else ''
            ),
        )
        return ProviderResponse(
            content=text,
            tool_calls=tuple(safe_calls),
            finish_reason=finish,
            raw_finish_reason=redact(str(raw_finish), self._secrets),
            usage=redact(native.get('usage', {}), self._secrets),
            request_id=redact(request_id or native.get('id', ''), self._secrets),
            model=redact(native.get('model', self.profile.model), self._secrets),
            provider=kind,
            auth_mode=self.profile.auth_mode,
            raw=redact(native, self._secrets),
            provenance=provenance,
        )
