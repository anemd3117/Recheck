"""고정 프로필의 업무 예산·인증 연결과 기존 클라이언트 호환 계층."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
from pathlib import Path
import threading
import time
import uuid

from pydantic import SecretStr, ValidationError

from recheck.models import data_root
from recheck.ollama import OllamaClient
from .contracts import (
    CredentialMaterial,
    Message,
    ProviderError,
    ProviderProfile,
    ProviderRequest,
    ToolCall,
    ToolDefinition,
    emit,
    redact,
    validate_schema,
    validate_tool_calls,
)
from .credentials import AuthError, CredentialStore
from .http_provider import (
    HTTPProvider,
    native_schema_preflight,
    strict_schema_supported as _strict_schema,
)
from .oauth import OAuthConfig, OAuthService
from .ollama_provider import OllamaProvider
from .profiles import resolve_profile


def _auth_error(exc):
    # 외부 예외의 텍스트에는 토큰이 들어 있을 수 있어 원문을 재출력하지 않는다.
    return ProviderError(
        getattr(exc, 'code', 'credential'), '선택한 인증 정보를 확인하거나 다시 연결하세요'
    )


class _Credentials:
    """명시적으로 선택된 한 ref만 해석하며, 토큰을 profile/로그에 붙이지 않는다."""

    def __init__(self, profile, root, store, oauth_service):
        self.profile, self.root, self.store, self.oauth = profile, root, store, oauth_service
        self.env_value = None
        if profile.credential_ref.startswith('env:'):
            if profile.credential_origin != profile.origin:
                raise ProviderError(
                    'credential_origin', '환경변수 인증의 대상 서버가 현재 연결과 다릅니다'
                )
            name = profile.credential_ref[4:]
            value = os.environ.get(name)
            if not value:
                raise ProviderError('credential_required', '명시한 환경변수에 인증 값이 없습니다')
            self.env_value = SecretStr(value)

    def fork(self, profile):
        # 수동 probe의 별도 출력/횟수 예산은 인증 선택을 새로 하는 행위가 아니다.
        # 환경 변수를 다시 읽어 새 값을 채택하지 않고 원래 SecretStr 바인딩을 유지한다.
        if (
            profile.model_copy(update={'options': self.profile.options}).fingerprint()
            != self.profile.fingerprint()
        ):
            raise ProviderError(
                'credentials_changed', '인증 복제 중 선택 프로필의 바인딩이 달라졌습니다'
            )
        copied = object.__new__(type(self))
        copied.profile, copied.root = profile, self.root
        copied.store, copied.oauth, copied.env_value = self.store, self.oauth, self.env_value
        return copied

    def _store(self):
        if self.store is None:
            self.store = CredentialStore(Path(self.root or data_root()) / 'credentials')
        return self.store

    def _record(self, ref, kind):
        p = self.profile
        record = self._store().get(ref, p.profile_id, p.origin)
        if record.ref != ref or record.owner != p.profile_id or record.origin != p.origin:
            raise ProviderError(
                'credential_origin', '인증 소유자·참조·서버 바인딩이 일치하지 않습니다'
            )
        if record.kind != kind:
            raise ProviderError(
                'credential_kind', '선택 인증의 저장 종류가 현재 연결 방식과 다릅니다'
            )
        return record

    def _status(self, status, provider):
        p = self.profile
        account = status.get('account') or {}
        if (
            not status.get('authenticated')
            or status.get('provider') != provider
            or status.get('credential_ref') != p.credential_ref
            or status.get('client_id') != p.oauth_client_id
            or account.get('subject') != p.oauth_subject
            or not p.oauth_client_id
            or not p.oauth_subject
        ):
            raise ProviderError(
                'credentials_changed', '계정·클라이언트 바인딩이 변경되었습니다. 다시 연결하세요'
            )
        if not status.get('inference_authorized'):
            raise ProviderError('permission', '선택한 로그인에는 이 API 추론 권한이 없습니다')

    async def __call__(self, profile, cancel=None):
        if profile.fingerprint() != self.profile.fingerprint():
            raise ProviderError('credentials_changed', '선택 인증과 프로필 스냅샷이 다릅니다')
        p = self.profile
        try:
            if p.auth_mode == 'none':
                return None
            if not p.credential_ref:
                raise ProviderError(
                    'credential_required', '이 연결에 사용할 인증을 먼저 저장하세요'
                )
            if p.credential_origin and p.credential_origin != p.origin:
                raise ProviderError('credential_origin', '저장한 인증의 대상 서버가 다릅니다')
            if self.env_value is not None:
                if p.auth_mode != 'api_key':
                    raise ProviderError(
                        'credential_kind', '환경변수 참조는 API 키에서만 사용할 수 있습니다'
                    )
                original = self.env_value.get_secret_value()
                if not hmac.compare_digest(
                    os.environ.get(p.credential_ref[4:], '').encode(), original.encode()
                ):
                    raise ProviderError(
                        'credentials_changed',
                        '선택한 환경변수 인증이 변경되었습니다. 연결을 새로 선택하세요',
                    )
                return CredentialMaterial(
                    value=self.env_value,
                    origin=p.origin,
                    version=p.credential_version,
                    token_revision=p.credential_version,
                )
            if p.auth_mode == 'api_key':
                record = await asyncio.to_thread(self._record, p.credential_ref, 'api_key')
                if record.version != p.credential_version:
                    raise ProviderError(
                        'credentials_changed',
                        '선택한 API 키의 버전이 변경되었습니다. 다시 연결하세요',
                    )
                return CredentialMaterial(
                    value=record.value,
                    origin=p.origin,
                    version=record.version,
                    token_revision=record.version,
                )
            provider = 'chatgpt' if p.auth_mode == 'chatgpt' else 'google'
            if self.oauth is None:
                self.oauth = OAuthService(self._store())
            before = await asyncio.to_thread(
                self.oauth.account_status, p.credential_ref, p.profile_id, p.origin
            )
            self._status(before, provider)
            secret = None
            if p.oauth_client_secret_ref:
                secret = (
                    await asyncio.to_thread(
                        self._record, p.oauth_client_secret_ref, 'oauth_client_secret'
                    )
                ).value
            config = OAuthConfig(
                client_id=p.oauth_client_id,
                client_secret=secret,
                scopes=p.oauth_scopes,
                api_origin=p.origin,
                expected_subject=p.oauth_subject,
                credential_ref=p.credential_ref,
                host_id=before.get('host_id', ''),
                timeout=p.options.timeout_seconds,
            )
            await self.oauth.refresh(
                provider, p.profile_id, p.credential_ref, config, cancel=cancel
            )
            # status + token을 동일 보호 레코드 버전에서 읽었는지 확인한다. refresh 이후
            # 다른 프로세스가 다시 갱신하면 잘못된 세션을 추정하지 않고 이번 요청을 중단한다.
            record = await asyncio.to_thread(self._record, p.credential_ref, 'oauth_' + provider)
            after = await asyncio.to_thread(
                self.oauth.account_status, p.credential_ref, p.profile_id, p.origin
            )
            self._status(after, provider)
            if after.get('credential_version') != record.version or after.get('expired'):
                raise ProviderError(
                    'credentials_changed', '인증 레코드가 요청 준비 중 변경되었거나 만료되었습니다'
                )
            tokens = json.loads(record.value.get_secret_value())
            account = tokens.get('account') or {}
            expected_issuer = (
                'https://auth.openai.com'
                if provider == 'chatgpt'
                else 'https://accounts.google.com'
            )
            required_scope = (
                'chatgpt.tokens.use.direct'
                if provider == 'chatgpt'
                else 'https://www.googleapis.com/auth/cloud-platform'
            )
            if (
                tokens.get('schema') != 1
                or tokens.get('provider') != provider
                or tokens.get('client_id') != p.oauth_client_id
                or account.get('subject') != p.oauth_subject
                or account.get('issuer') != expected_issuer
                or account != after.get('account')
                or tokens.get('token_type', '').lower() != 'bearer'
                or not isinstance(tokens.get('scopes'), list)
                or required_scope not in tokens['scopes']
                or tokens.get('expires_at', 0) <= time.time()
                or not isinstance(tokens.get('access_token'), str)
                or not tokens['access_token']
            ):
                raise ProviderError(
                    'credentials_changed', '보호 인증 레코드의 계정·발급자·권한이 선택과 다릅니다'
                )
            return CredentialMaterial(
                value=SecretStr(tokens['access_token']),
                origin=p.origin,
                version=p.credential_version,
                token_revision=record.version,
            )
        except AuthError as exc:
            raise _auth_error(exc) from None
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ProviderError(
                'credential', '선택한 보호 인증 레코드의 형식이 올바르지 않습니다'
            ) from None


class SharedRequestBudget:
    """중첩 분석·Agent·재해석이 공유하는 하나의 업무 장부."""

    def __init__(self, profile):
        self.fingerprint = profile.fingerprint()
        self.max_requests = profile.options.max_requests
        self.requests_used, self.attempts, self._lock = 0, [], threading.Lock()

    def require_profile(self, profile):
        if profile.fingerprint() != self.fingerprint:
            raise ProviderError('profile_changed', '같은 업무의 AI 프로필 스냅샷이 변경되었습니다')

    def charge(self, profile):
        self.require_profile(profile)
        with self._lock:
            if self.requests_used >= self.max_requests:
                raise ProviderError(
                    'request_budget',
                    '이 업무의 AI 요청 예산을 모두 사용했습니다',
                    {'used': self.requests_used, 'max_requests': self.max_requests},
                )
            self.requests_used += 1
            attempt = {
                'number': self.requests_used,
                'status': 'attempted',
                'charge_possible': profile.provider != 'ollama',
            }
            self.attempts.append(attempt)
            return attempt

    def snapshot(self):
        with self._lock:
            return {
                'used': self.requests_used,
                'max_requests': self.max_requests,
                'attempts': [dict(item) for item in self.attempts],
            }


class BudgetProvider:
    """한 업무의 추론 시도 예산. 실패 시도도 사용하며 자동 재시도하지 않는다."""

    def __init__(self, adapter, clone, budget):
        self.adapter, self.profile, self._clone, self.ledger = (
            adapter,
            adapter.profile,
            clone,
            budget,
        )

    @property
    def requests_used(self):
        return self.ledger.requests_used

    @property
    def attempts(self):
        return self.ledger.snapshot()['attempts']

    @property
    def native(self):
        return getattr(self.adapter, 'native', None)

    async def probe(self, cancel=None):
        return await self.adapter.probe(cancel=cancel)

    async def list_models(self, cancel=None):
        return await self.adapter.list_models(cancel=cancel)

    async def chat(self, request, cancel=None, on_event=None):
        self.profile.authorize(request.evidence_kinds, purpose=request.purpose)
        if not self.profile.model:
            raise ProviderError('model_required', '검사·추론에 사용할 모델 ID를 먼저 선택하세요')
        if cancel is not None and cancel.is_set():
            raise ProviderError('cancelled', 'AI 요청을 취소했습니다')
        native_schema_preflight(self.profile, request)
        validate_tool_calls((), request.tools)
        attempt = self.ledger.charge(self.profile)
        try:
            result = await self.adapter.chat(request, cancel=cancel, on_event=on_event)
        except ProviderError as exc:
            attempt.update(
                status='failed',
                code=exc.code,
                duplicate_charge_possible=exc.duplicate_charge_possible,
            )
            exc.details = {**exc.details, 'budget': self.budget()}
            raise
        attempt.update(status='completed', finish_reason=result.finish_reason)
        return result.model_copy(
            update={'provenance': {**result.provenance, 'budget': self.budget()}}
        )

    def budget(self):
        return self.ledger.snapshot()

    def clone_for_probe(self):
        options = self.profile.options.model_copy(
            update={
                'max_requests': 4,
                'max_output_tokens': min(256, self.profile.options.max_output_tokens),
            }
        )
        return self._clone(self.profile.model_copy(update={'options': options}))

    async def close(self):
        await self.adapter.close()


def create_provider(
    profile,
    root=None,
    local_factory=None,
    store=None,
    oauth_service=None,
    transport=None,
    budget=None,
):
    profile = ProviderProfile.model_validate(profile).model_copy(deep=True)
    budget = budget if budget is not None else SharedRequestBudget(profile)
    budget.require_profile(profile)
    if profile.provider == 'ollama':
        adapter = OllamaProvider(profile, client_factory=local_factory or OllamaClient)

        def clone(snapshot):
            return create_provider(
                snapshot,
                root=root,
                local_factory=local_factory,
                store=store,
                oauth_service=oauth_service,
                transport=transport,
            )

    else:
        getter = (
            _Credentials(profile, root, store, oauth_service)
            if profile.auth_mode != 'none'
            else None
        )
        adapter = HTTPProvider(profile, credential_getter=getter, transport=transport)

        def clone(snapshot):
            child = HTTPProvider(
                snapshot,
                credential_getter=getter.fork(snapshot) if getter else None,
                transport=transport,
            )
            return BudgetProvider(child, clone, SharedRequestBudget(snapshot))

    return BudgetProvider(adapter, clone, budget)


class LegacyClientFacade:
    """기존 Engine API를 공통 typed 요청으로 변환한다. 선택 프로필은 업무 동안 고정된다."""

    def __init__(self, provider, role='analysis'):
        if role not in ('analysis', 'generation', 'review'):
            raise ProviderError('purpose', '알 수 없는 AI 업무 역할입니다')
        self.provider, self.profile, self.role = provider, provider.profile, role
        self.last_execution, self.evidence_kinds, self._scope_declared = {}, (), False

    @property
    def native(self):
        return getattr(self.provider, 'native', None)

    def authorize_context(self, evidence_kinds, purpose=None):
        if purpose is not None and purpose != self.role:
            raise ProviderError('purpose', '선택 프로필의 업무 역할을 요청 중 바꿀 수 없습니다')
        if not isinstance(evidence_kinds, (list, tuple, set)) or any(
            x not in ('runtime', 'static', 'code', 'source') for x in evidence_kinds
        ):
            raise ProviderError('evidence_scope', '전송할 근거 종류를 명시적인 목록으로 지정하세요')
        scope = tuple(dict.fromkeys((*self.evidence_kinds, *evidence_kinds)))
        self.profile.authorize(scope, purpose=self.role)
        self.evidence_kinds, self._scope_declared = scope, True
        return scope

    async def probe(self, cancel=None):
        return await self.provider.probe(cancel=cancel)

    async def models(self, cancel=None):
        values = await self.provider.list_models(cancel=cancel)
        return [
            {**row.metadata, 'name': row.id, 'model': row.id, 'digest': row.digest}
            for row in values
        ]

    async def local_metadata(self, model, cancel=None):
        self._model(model)
        if self.native is not None:
            return await self.native.local_metadata(model, cancel=cancel)
        return {
            'name': model,
            'provider': self.profile.provider,
            'auth_mode': self.profile.auth_mode,
            'details': {'format': 'remote_api'},
            'inference_verified': False,
            'execution_location': (
                '호환 서버의 추론 위치 미확인'
                if self.profile.provider == 'compatible'
                else '원격 API'
            ),
            'local_settings_applied': False,
        }

    async def ensure_server(self, cancel=None, **kwargs):
        if self.native is not None:
            return await self.native.ensure_server(cancel=cancel, **kwargs)
        return await self.probe(cancel=cancel)

    async def pull(self, model, cancel=None, on_event=None):
        if self.native is None:
            raise ProviderError(
                'unsupported', '원격 API 연결에서는 로컬 모델 다운로드를 지원하지 않습니다'
            )
        return await self.native.pull(model, cancel=cancel, on_event=on_event)

    def _model(self, model):
        if not self.profile.model:
            raise ProviderError('model_required', '사용할 모델 ID를 먼저 선택하세요')
        if model != self.profile.model:
            raise ProviderError('profile_changed', '선택한 프로필 모델과 요청 모델이 다릅니다')

    def _options(self, options):
        options = dict(options or {})
        if '_evidence_kinds' in options:
            self.authorize_context(options.pop('_evidence_kinds'))
        if self.profile.provider != 'ollama' and not self._scope_declared:
            raise ProviderError(
                'evidence_scope_required', '이 요청에 실제 전송할 근거 종류를 먼저 지정하세요'
            )
        expected = dict(
            num_ctx=self.profile.options.context_budget,
            num_predict=self.profile.options.max_output_tokens,
            temperature=self.profile.options.temperature,
        )
        if any(key not in expected or value != expected[key] for key, value in options.items()):
            raise ProviderError(
                'profile_changed', '요청 옵션이 선택한 프로필의 고정 예산과 다릅니다'
            )

    def _messages(self, messages):
        result, pending = [], {}
        try:
            for item in messages:
                if isinstance(item, Message):
                    typed = item
                else:
                    calls = []
                    for call in item.get('tool_calls', ()):
                        function = call.get('function', call)
                        args = function.get('arguments', {})
                        if isinstance(args, str):
                            args = json.loads(args)
                        call_id = call.get('id', '')
                        if not call_id:
                            raise ProviderError(
                                'tool_call_identity', '이전 도구 호출에는 원래 호출 ID가 필요합니다'
                            )
                        calls.append(ToolCall(id=call_id, name=function['name'], arguments=args))
                    call_id = item.get('tool_call_id', '')
                    name = item.get('name', item.get('tool_name', ''))
                    if (
                        item.get('role') == 'tool'
                        and not call_id
                        and self.profile.provider == 'ollama'
                    ):
                        matches = [key for key, value in pending.items() if value == name]
                        if len(matches) == 1:
                            call_id = matches[0]
                    typed = Message(
                        role=item['role'],
                        content=item.get('content') or '',
                        tool_calls=tuple(calls),
                        tool_call_id=call_id,
                        name=name,
                    )
                for call in typed.tool_calls:
                    if call.id in pending:
                        raise ProviderError(
                            'tool_call_identity', '이전 도구 호출 ID가 중복되었습니다'
                        )
                    pending[call.id] = call.name
                if typed.role == 'tool':
                    if typed.tool_call_id not in pending or (
                        typed.name and pending[typed.tool_call_id] != typed.name
                    ):
                        raise ProviderError(
                            'tool_call_identity',
                            '도구 결과 ID·이름과 이전 호출이 일치하지 않습니다',
                        )
                    pending.pop(typed.tool_call_id)
                result.append(typed)
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ProviderError(
                'request_format', '요청 메시지·도구 결과의 형식이 올바르지 않습니다'
            ) from None
        return tuple(result)

    async def chat(
        self, model, messages, format=None, tools=None, cancel=None, options=None, on_event=None
    ):
        self._model(model)
        self._options(options)
        schema = format if isinstance(format, dict) else None
        if format is not None and not isinstance(format, dict):
            if format != 'json':
                raise ProviderError(
                    'schema_unsupported', 'JSON Schema 객체 또는 명시적인 json 형식을 사용하세요'
                )
            schema = {'type': 'object'}
        if schema is not None:
            try:
                validate_schema(schema)
            except Exception:
                raise ProviderError(
                    'schema_unsupported', '출력 JSON Schema 형식을 확인하세요'
                ) from None
            if (
                self.profile.provider in ('openai', 'compatible')
                and self.profile.structured_mode == 'native_schema'
                and not _strict_schema(schema)
            ):
                raise ProviderError(
                    'schema_unsupported',
                    '선택한 스키마는 이 API의 strict 형식에 맞지 않습니다. 설정에서 JSON 프롬프트 방식을 명시적으로 선택하세요',
                    {'available_mode': 'json_prompt', 'automatic_fallback': False},
                )
        definitions, policies = [], []
        try:
            for tool in tools or ():
                if isinstance(tool, ToolDefinition):
                    definition = tool
                else:
                    function = tool.get('function', tool)
                    definition = ToolDefinition(
                        name=function['name'],
                        description=function.get('description', ''),
                        parameters=function.get('parameters', {'type': 'object'}),
                        strict=function.get('strict', True),
                    )
                if (
                    self.profile.provider in ('openai', 'compatible')
                    and definition.strict
                    and not _strict_schema(definition.parameters)
                ):
                    definition = definition.model_copy(update={'strict': False})
                    policies.append(
                        {
                            'tool': definition.name,
                            'strict': False,
                            'reason': 'API strict 부분집합 외의 스키마; 로컬 전체 JSON Schema 검증 유지',
                        }
                    )
                definitions.append(definition)
            request = ProviderRequest(
                messages=self._messages(messages),
                tools=tuple(definitions),
                output_schema=schema,
                purpose=self.role,
                evidence_kinds=self.evidence_kinds,
            )
        except (ValidationError, TypeError, KeyError, ValueError):
            raise ProviderError(
                'request_format', '요청 메시지·도구 정의의 형식이 올바르지 않습니다'
            ) from None
        response = await self.provider.chat(request, cancel=cancel, on_event=on_event)
        provenance = {**response.provenance, 'role': self.role, 'tool_schema_policy': policies}
        response = response.model_copy(update={'provenance': provenance})
        execution = response.provenance.get('execution') or {
            'kind': 'remote_api' if self.profile.provider != 'ollama' else 'unknown',
            'provider': self.profile.provider,
            'auth_mode': self.profile.auth_mode,
            'model': response.model,
            'endpoint': self.profile.endpoint,
            'request_id': response.request_id,
            'execution_location': provenance.get('execution_location', '원격 API'),
        }
        self.last_execution = redact(execution)
        common = response.model_dump(mode='json')
        if response.finish_reason not in ('stop', 'tool_calls'):
            raise ProviderError(
                response.finish_reason,
                'AI 응답이 정상 완료되지 않았습니다: ' + response.finish_reason,
                {'provider_response': common, 'execution': self.last_execution},
                duplicate_charge_possible=self.profile.provider != 'ollama',
            )
        message = {'role': 'assistant', 'content': response.content}
        if response.tool_calls:
            message['tool_calls'] = [
                {
                    'id': call.id,
                    'type': 'function',
                    'function': {'name': call.name, 'arguments': call.arguments},
                }
                for call in response.tool_calls
            ]
        native_usage = response.usage.get('native', {})
        return {
            **response.raw,
            **native_usage,
            'model': response.model,
            'message': message,
            'done': True,
            'done_reason': response.raw_finish_reason or response.finish_reason,
            'prompt_eval_count': response.usage.get(
                'input_tokens', response.usage.get('prompt_tokens')
            ),
            'eval_count': response.usage.get(
                'output_tokens', response.usage.get('completion_tokens')
            ),
            '_provider': common,
            '_execution': self.last_execution,
        }

    async def capabilities(self, model=None, cancel=None, on_event=None):
        if model is not None:
            self._model(model)
        return await probe_capabilities(self.provider, cancel=cancel, on_event=on_event)

    async def close(self):
        await self.provider.close()


def build_client(settings, role='analysis', root=None, local_factory=OllamaClient, budget=None):
    profile = resolve_profile(settings, role).model_copy(deep=True)
    return LegacyClientFacade(
        create_provider(profile, root=root, local_factory=local_factory, budget=budget), role=role
    )


async def probe_capabilities(provider, cancel=None, on_event=None):
    """수동 기능 검사: 대상 근거 없이 별도 최대 네 번, 실행 권한 없는 fixture 도구."""
    if not provider.profile.model:
        raise ProviderError('model_required', '기능 검사에 사용할 모델 ID를 먼저 선택하세요')
    own = hasattr(provider, 'clone_for_probe')
    probe = provider.clone_for_probe() if own else provider
    if probe.profile.options.max_output_tokens > 256 or probe.profile.options.max_requests > 4:
        if own:
            await probe.close()
        raise ProviderError(
            'probe_budget', '기능 검사는 출력 256 토큰·요청 네 번 이하의 별도 예산이 필요합니다'
        )
    result = dict(
        basic_chat=False,
        json_schema=False,
        tool_calls=False,
        tool_roundtrip=False,
        interpretation_schema_supported=False,
        structured_mode=probe.profile.structured_mode,
        available_modes=[],
        errors=[],
        model=probe.profile.model,
        provider=probe.profile.provider,
        profile_fingerprint=provider.profile.fingerprint(),
        actual_inference_verified=False,
    )
    nonce = 'rebuild_' + uuid.uuid4().hex
    schema = {
        'type': 'object',
        'properties': {
            'ready': {'type': 'boolean', 'const': True},
            'nonce': {'type': 'string', 'const': nonce},
        },
        'required': ['ready', 'nonce'],
        'additionalProperties': False,
    }
    calls = 0
    terminal_plan_error = False

    async def ask(label, messages, tools=(), output_schema=None):
        nonlocal calls
        if cancel is not None and cancel.is_set():
            raise ProviderError('cancelled', '기능 검사를 취소했습니다')
        calls += 1
        response = await probe.chat(
            ProviderRequest(
                messages=tuple(messages),
                tools=tuple(tools),
                output_schema=output_schema,
                purpose='probe',
            ),
            cancel=cancel,
            on_event=on_event,
        )
        if response.finish_reason not in ('stop', 'tool_calls'):
            raise ProviderError(response.finish_reason, '기능 검사 응답이 정상 완료되지 않았습니다')
        result[label + '_response'] = response.model_dump(mode='json')
        return response

    async def stage(label, action):
        nonlocal terminal_plan_error
        if terminal_plan_error:
            return
        try:
            await action()
        except ProviderError as exc:
            if exc.code == 'cancelled':
                raise
            result['errors'].append(
                {
                    'stage': label,
                    'code': exc.code,
                    'message': '해당 기능 검사를 통과하지 못했습니다',
                }
            )
            if exc.code == 'subscription_sharing_user_not_eligible':
                terminal_plan_error = True
        except (ValueError, KeyError, TypeError, AssertionError):
            result['errors'].append(
                {
                    'stage': label,
                    'code': 'probe_mismatch',
                    'message': '검사 nonce·형식·호출 ID가 일치하지 않습니다',
                }
            )

    try:
        try:
            result['connection'] = await probe.probe(cancel=cancel)
        except ProviderError as exc:
            if exc.code == 'cancelled':
                raise
            result['connection'] = {
                'connected': False,
                'authenticated': False,
                'inference_verified': False,
                'code': exc.code,
            }

        if (isinstance(result['connection'], dict)
                and result['connection'].get('code') == 'subscription_sharing_user_not_eligible'):
            terminal_plan_error = True
            result['errors'].append({
                'stage': 'connection',
                'code': 'subscription_sharing_user_not_eligible',
                'message': '해당 기능 검사를 통과하지 못했습니다',
            })

        async def basic():
            response = await ask(
                'basic',
                [
                    Message(
                        role='user',
                        content='Return the entire value after NONCE= exactly, including the literal '
                        'rebuild_ prefix and every character after it. Do not output NONCE=, '
                        'quotes, or an explanation.\nNONCE=' + nonce,
                    )
                ],
            )
            if response.tool_calls or response.content.strip() != nonce:
                raise ValueError('nonce')
            result['basic_chat'] = True

        await stage('basic', basic)

        async def structured():
            response = await ask(
                'json_schema',
                [
                    Message(
                        role='user',
                        content='ready=true, nonce를 담은 JSON 객체로 답하세요. NONCE=' + nonce,
                    )
                ],
                output_schema=schema,
            )
            if response.tool_calls or json.loads(response.content) != {
                'ready': True,
                'nonce': nonce,
            }:
                raise ValueError('nonce')
            result['json_schema'] = True

        await stage('json_schema', structured)

        async def tool_roundtrip():
            tool = ToolDefinition(
                name='rebuild_probe_status',
                description='검사용 nonce를 반환하는 모의 도구. 외부 작업을 실행하지 않음',
                parameters={
                    'type': 'object',
                    'properties': {'nonce': {'type': 'string', 'const': nonce}},
                    'required': ['nonce'],
                    'additionalProperties': False,
                },
            )
            history = [
                Message(
                    role='user',
                    content='반드시 rebuild_probe_status 도구를 nonce 인자로 한 번 호출하세요. NONCE='
                    + nonce,
                )
            ]
            first = await ask('tools', history, tools=(tool,))
            validate_tool_calls(first.tool_calls, (tool,))
            if (
                first.finish_reason != 'tool_calls'
                or len(first.tool_calls) != 1
                or first.tool_calls[0].arguments != {'nonce': nonce}
            ):
                raise ValueError('call')
            result['tool_calls'] = True
            call = first.tool_calls[0]
            history += [
                Message(role='assistant', content=first.content, tool_calls=first.tool_calls),
                Message(
                    role='tool',
                    tool_call_id=call.id,
                    name=call.name,
                    content=json.dumps({'ready': True, 'nonce': nonce}),
                ),
                Message(
                    role='user',
                    content='직전 도구 결과의 ready와 nonce를 JSON으로 그대로 답하세요. NONCE='
                    + nonce,
                ),
            ]
            final = await ask('tool_roundtrip', history, output_schema=schema)
            if final.tool_calls or json.loads(final.content) != {'ready': True, 'nonce': nonce}:
                raise ValueError('nonce')
            result['tool_roundtrip'] = True

        await stage('tools', tool_roundtrip)
        if terminal_plan_error:
            for key in ('basic_chat', 'json_schema', 'tool_calls', 'tool_roundtrip'):
                result[key] = False
        if result['basic_chat'] and result['json_schema']:
            result['available_modes'].append('automatic')
        if result['basic_chat'] and result['tool_calls'] and result['tool_roundtrip']:
            result['available_modes'].append('agent')
        result['actual_inference_verified'] = bool(result['basic_chat'])
        result['inference_verified'] = bool(result['basic_chat'])
        result['basic'] = result['basic_chat']
        result['tools'] = result['tool_calls'] and result['tool_roundtrip']
        result['structured_output'] = {
            'mode': probe.profile.structured_mode,
            'verified': result['json_schema'],
            'native_schema': result['json_schema']
            and probe.profile.structured_mode == 'native_schema',
            'schema_scope': 'probe_ready_nonce_only',
            'interpretation_schema_supported': False,
        }
        plan = probe.profile.auth_mode == 'chatgpt'
        result['budget'] = {
            'used': calls,
            'max_requests': 4,
            'max_output_tokens': None if plan else probe.profile.options.max_output_tokens,
            'requested_soft_output_tokens': probe.profile.options.max_output_tokens,
            'server_output_token_limit_enforced': not plan,
            'timeout_seconds': probe.profile.options.timeout_seconds,
            'max_response_bytes': min(probe.profile.options.max_response_bytes, 2 * 1024 * 1024),
            'independent_job': True,
            'automatic_retry': False,
        }
        return result
    finally:
        if own:
            await probe.close()
