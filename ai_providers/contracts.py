"""제공자 독립 계약. 자격증명 값과 실행 권한은 프로필에 넣지 않는다."""

from __future__ import annotations

import hashlib
import inspect
import ipaddress
import json
import re
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit, urlunsplit
import uuid

from jsonschema import Draft202012Validator
from referencing import Registry
from referencing.exceptions import NoSuchResource
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

ProviderKind = Literal['ollama', 'openai', 'anthropic', 'gemini', 'compatible']
AuthMode = Literal['none', 'api_key', 'chatgpt', 'google_oauth']
EvidenceKind = Literal['runtime', 'static', 'code', 'source']
DEFAULT_ENDPOINTS = dict(
    ollama='http://127.0.0.1:11434',
    openai='https://api.openai.com/v1',
    anthropic='https://api.anthropic.com/v1',
    gemini='https://generativelanguage.googleapis.com/v1beta',
)


class ProviderError(RuntimeError):
    def __init__(
        self,
        code,
        message,
        details=None,
        *,
        status_code=None,
        retry_after=None,
        duplicate_charge_possible=False,
    ):
        self.code, self.details = code, details or {}
        self.status_code, self.retry_after = status_code, retry_after
        self.duplicate_charge_possible = duplicate_charge_possible
        super().__init__(message)


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)


def normalize_endpoint(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError('연결 주소를 확인하세요')
    parsed = urlsplit(value.strip())
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError('연결 주소의 포트를 확인하세요') from exc
    if (
        parsed.scheme not in ('http', 'https')
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(part == '..' for part in parsed.path.split('/'))
    ):
        raise ValueError('연결 주소에는 HTTP(S) 서버만 지정하고 계정·키·쿼리를 넣지 마세요')
    host = parsed.hostname.lower()
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == 'localhost'
    if parsed.scheme == 'http' and not loopback:
        raise ValueError('원격 AI 서버는 HTTPS 연결이 필요합니다')
    formatted = '[' + host + ']' if ':' in host else host
    if port is not None and port != (443 if parsed.scheme == 'https' else 80):
        formatted += ':' + str(port)
    return urlunsplit((parsed.scheme, formatted, parsed.path.rstrip('/'), '', ''))


def origin(value):
    parsed = urlsplit(normalize_endpoint(value))
    return urlunsplit((parsed.scheme, parsed.netloc, '', '', ''))


class GenerationOptions(FrozenModel):
    context_budget: int = Field(default=8192, ge=1024, le=131072)
    max_output_tokens: int = Field(default=2048, ge=16, le=16384)
    timeout_seconds: float = Field(default=300, ge=1, le=900)
    max_requests: int = Field(default=1, ge=1, le=5)
    temperature: float = Field(default=0.1, ge=0, le=2)
    max_response_bytes: int = Field(default=2 * 1024 * 1024, ge=1024, le=16 * 1024 * 1024)


class ProviderProfile(FrozenModel):
    profile_id: str = Field(
        default_factory=lambda: 'profile_' + uuid.uuid4().hex, pattern=r'^[A-Za-z0-9_.-]{1,80}$'
    )
    name: str = Field(default='새 AI 연결', min_length=1, max_length=100)
    provider: ProviderKind = 'ollama'
    auth_mode: AuthMode = 'none'
    endpoint: str = ''
    model: str = Field(default='', max_length=512)
    credential_ref: str = Field(default='', max_length=200)
    credential_origin: str = Field(default='', max_length=2048)
    credential_version: int = Field(default=0, ge=0)
    enabled: bool = False
    experimental: bool = True
    external_consent: bool = False
    allowed_evidence: tuple[EvidenceKind, ...] = ()
    options: GenerationOptions = Field(default_factory=GenerationOptions)
    oauth_client_id: str = Field(default='', max_length=512)
    oauth_subject: str = Field(default='', max_length=512)
    oauth_scopes: tuple[str, ...] = ()
    oauth_client_secret_ref: str = Field(default='', max_length=200)
    google_project_id: str = Field(default='', max_length=200)
    workspace_id: str = Field(default='', max_length=200)
    api_version: str = Field(default='', max_length=100)
    structured_mode: Literal['native_schema', 'json_prompt'] = 'native_schema'

    @model_validator(mode='before')
    @classmethod
    def defaults(cls, value):
        if isinstance(value, dict):
            value = dict(value)
            if not value.get('endpoint'):
                value['endpoint'] = DEFAULT_ENDPOINTS.get(value.get('provider', 'ollama'), '')
        return value

    @field_validator('endpoint')
    @classmethod
    def endpoint_valid(cls, value):
        return normalize_endpoint(value)

    @field_validator('credential_origin')
    @classmethod
    def credential_origin_valid(cls, value):
        return origin(value) if value else ''

    @field_validator('credential_ref', 'oauth_client_secret_ref')
    @classmethod
    def credential_reference_valid(cls, value, info):
        if not value or re.fullmatch(r'cred_[a-f0-9]{32}', value):
            return value
        if info.field_name == 'credential_ref' and re.fullmatch(
            r'env:[A-Za-z_][A-Za-z0-9_]{0,127}', value
        ):
            return value
        raise ValueError('인증 값 대신 앱 보안 저장소 참조를 사용하세요')

    @field_validator(
        'model',
        'name',
        'oauth_client_id',
        'oauth_subject',
        'google_project_id',
        'workspace_id',
        'api_version',
    )
    @classmethod
    def text_valid(cls, value):
        if any(ord(char) < 32 for char in value):
            raise ValueError('설정 값에 제어 문자를 넣지 마세요')
        return value.strip()

    @model_validator(mode='after')
    def compatible_auth(self):
        allowed = dict(
            ollama={'none'},
            openai={'api_key', 'chatgpt'},
            anthropic={'api_key'},
            gemini={'api_key', 'google_oauth'},
            compatible={'none', 'api_key'},
        )
        if self.auth_mode not in allowed[self.provider]:
            raise ValueError('선택 제공자가 지원하는 인증 방식을 선택하세요')
        if self.credential_ref.startswith('env:') and not self.credential_origin:
            raise ValueError('환경변수 인증에는 명시적인 인증 대상 origin이 필요합니다')
        if self.provider == 'ollama':
            if origin(self.endpoint) not in ('http://127.0.0.1:11434', 'http://localhost:11434'):
                host = urlsplit(self.endpoint).hostname
                try:
                    local = ipaddress.ip_address(host).is_loopback
                except ValueError:
                    local = host == 'localhost'
                if not local:
                    raise ValueError('Ollama 프로필은 로컬 루프백 서버만 지원합니다')
        elif (
            self.provider in DEFAULT_ENDPOINTS and self.endpoint != DEFAULT_ENDPOINTS[self.provider]
        ):
            raise ValueError(
                '공식 제공자의 고정 API 주소를 사용하세요. 다른 서버는 호환 서버로 등록하세요'
            )
        return self

    @property
    def origin(self):
        return origin(self.endpoint)

    @property
    def remote(self):
        if self.provider == 'ollama':
            return False
        host = urlsplit(self.endpoint).hostname
        try:
            return not ipaddress.ip_address(host).is_loopback
        except ValueError:
            return host != 'localhost'

    def fingerprint(self):
        payload = json.dumps(
            self.model_dump(mode='json'), ensure_ascii=False, sort_keys=True, separators=(',', ':')
        )
        return hashlib.sha256(payload.encode('utf-8')).hexdigest()

    def authorize(self, evidence_kinds=(), *, purpose='analysis'):
        if purpose == 'probe':
            if evidence_kinds:
                raise ProviderError(
                    'external_consent', '기능 검사에는 대상 분석 자료를 전송할 수 없습니다'
                )
            return
        if not self.enabled:
            raise ProviderError('profile_disabled', '선택 AI 연결은 비활성 상태입니다')
        if not self.model:
            raise ProviderError('model_required', '사용할 모델 ID를 선택하거나 입력하세요')
        if self.provider != 'ollama' and (
            not self.external_consent or not set(evidence_kinds) <= set(self.allowed_evidence)
        ):
            raise ProviderError(
                'external_consent', '선택한 AI 서버에 전송할 근거 종류를 먼저 허용하세요'
            )


class CredentialMaterial(FrozenModel):
    value: SecretStr = Field(repr=False)
    origin: str
    version: int = Field(ge=0)
    token_revision: int = Field(default=0, ge=0)
    extra_headers: dict[str, SecretStr] = Field(default_factory=dict, repr=False)

    def require_origin(self, expected):
        if origin(self.origin) != origin(expected):
            raise ProviderError(
                'credential_origin',
                '저장한 인증의 서버와 현재 연결 주소가 다릅니다. 다시 연결하세요',
            )


def validate_schema(schema):
    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ('$ref', '$dynamicRef') and (
                    not isinstance(item, str) or not item.startswith('#')
                ):
                    raise ValueError('외부 JSON Schema 참조는 허용하지 않습니다')
                visit(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item)

    visit(schema)
    Draft202012Validator.check_schema(schema)
    return schema


class ToolDefinition(FrozenModel):
    name: str = Field(pattern=r'^[A-Za-z0-9_-]{1,100}$')
    description: str = Field(default='', max_length=4000)
    parameters: dict[str, Any] = Field(default_factory=lambda: {'type': 'object'})
    strict: bool = True
    _schema_valid = field_validator('parameters')(
        classmethod(lambda cls, value: validate_schema(value))
    )


class ToolCall(FrozenModel):
    id: str = Field(min_length=1, max_length=300)
    name: str = Field(pattern=r'^[A-Za-z0-9_-]{1,100}$')
    arguments: dict[str, Any]

    @field_validator('arguments')
    @classmethod
    def arguments_limit(cls, value):
        if len(json.dumps(value, ensure_ascii=False).encode('utf-8')) > 65536:
            raise ValueError('도구 인자가 허용 크기를 초과했습니다')
        return value


class Message(FrozenModel):
    role: Literal['system', 'developer', 'user', 'assistant', 'tool']
    content: str = ''
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str = ''
    name: str = ''

    @model_validator(mode='after')
    def tool_result_identity(self):
        if self.role == 'tool' and not self.tool_call_id:
            raise ValueError('도구 결과에는 원래 호출 ID가 필요합니다')
        if self.tool_calls and self.role != 'assistant':
            raise ValueError('도구 호출은 assistant 메시지에만 넣을 수 있습니다')
        return self


class ProviderRequest(FrozenModel):
    messages: tuple[Message, ...]
    tools: tuple[ToolDefinition, ...] = ()
    output_schema: dict[str, Any] | None = None
    purpose: Literal['analysis', 'generation', 'review', 'probe'] = 'analysis'
    evidence_kinds: tuple[EvidenceKind, ...] = ()
    stream: bool = True

    @field_validator('output_schema')
    @classmethod
    def output_schema_valid(cls, value):
        return validate_schema(value) if value is not None else None


class ProviderResponse(FrozenModel):
    content: str = ''
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: Literal[
        'stop',
        'tool_calls',
        'length',
        'refusal',
        'safety',
        'empty',
        'error',
        'cancelled',
        'incomplete',
    ] = 'stop'
    raw_finish_reason: str = ''
    usage: dict[str, Any] = Field(default_factory=dict)
    request_id: str = ''
    model: str = ''
    provider: ProviderKind
    auth_mode: AuthMode
    raw: dict[str, Any] = Field(default_factory=dict, repr=False)
    provenance: dict[str, Any] = Field(default_factory=dict)


class ModelDescriptor(FrozenModel):
    id: str
    display_name: str = ''
    digest: str = ''
    metadata: dict[str, Any] = Field(default_factory=dict)


def validate_tool_calls(calls, tools):
    ids = [call.id for call in calls]
    by_name = {tool.name: tool for tool in tools}
    if len(ids) > 32 or len(set(ids)) != len(ids) or len(by_name) != len(tools):
        raise ProviderError(
            'tool_call_identity', '도구 호출 ID·이름 중복 또는 호출 수 상한을 확인하세요'
        )

    def no_retrieve(uri):
        raise NoSuchResource(ref=uri)

    for call in calls:
        if call.name not in by_name:
            raise ProviderError('tool_not_allowed', '등록되지 않은 도구 요청을 거부했습니다')
        validator = Draft202012Validator(
            by_name[call.name].parameters, registry=Registry(retrieve=no_retrieve)
        )
        try:
            validator.validate(call.arguments)
        except Exception as exc:
            raise ProviderError(
                'tool_arguments', '도구 인자가 등록한 스키마와 다릅니다', {'tool': call.name}
            ) from exc
    return tuple(calls)


def redact(value, secrets=(), _depth=0):
    if _depth > 32:
        return '<DEPTH OMITTED>'
    if isinstance(value, SecretStr):
        return '<MASKED>'
    if isinstance(value, dict):
        sensitive = r'(?i)(api[_-]?key|authorization|access[_-]?token|refresh[_-]?token|id[_-]?token|client[_-]?secret|code_verifier|password)'
        return {
            str(key): (
                '<MASKED>' if re.search(sensitive, str(key)) else redact(item, secrets, _depth + 1)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item, secrets, _depth + 1) for item in value]
    if isinstance(value, str):
        for secret in sorted(
            (s for s in secrets if isinstance(s, str) and s), key=len, reverse=True
        ):
            value = value.replace(secret, '<MASKED>')
        return re.sub(r'(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+', 'Bearer <MASKED>', value)
    return value


async def emit(callback, value):
    if callback:
        result = callback(value)
        if inspect.isawaitable(result):
            await result


class Provider(Protocol):
    profile: ProviderProfile

    async def probe(self, cancel=None) -> dict: ...
    async def list_models(self, cancel=None) -> list[ModelDescriptor]: ...
    async def chat(
        self, request: ProviderRequest, cancel=None, on_event=None
    ) -> ProviderResponse: ...
    async def close(self): ...
