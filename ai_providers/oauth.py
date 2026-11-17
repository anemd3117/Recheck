"""공식 ChatGPT·Google Desktop OAuth. 토큰은 DPAPI 저장소에만 기록한다.

공식 계약 확인: 2026-11-14. 실제 계정 로그인 성공 여부는 별도 실검증 상태다.
"""

from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
from dataclasses import dataclass
import hashlib
import hmac
import inspect
import json
import os
from pathlib import Path
import re
import secrets
import socket
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit
import uuid
import weakref
import webbrowser

import httpx
import jwt
from pydantic import SecretStr

from .contracts import emit
from .credentials import AuthError, canonical_origin

_PROVIDERS = {
    'chatgpt': dict(
        issuer='https://auth.openai.com',
        discovery='https://auth.openai.com/.well-known/openid-configuration',
        authorization_endpoint='https://auth.openai.com/api/accounts/authorize',
        token_endpoint='https://auth.openai.com/api/accounts/oauth/token',
        jwks_uri='https://auth.openai.com/.well-known/jwks.json',
        origin='https://api.openai.com',
        resource='https://api.openai.com/v1',
        scopes=(
            'openid',
            'profile',
            'email',
            'offline_access',
            'resource.invoke',
            'chatgpt.tokens.use.direct',
        ),
    ),
    'google': dict(
        issuer='https://accounts.google.com',
        discovery='https://accounts.google.com/.well-known/openid-configuration',
        authorization_endpoint='https://accounts.google.com/o/oauth2/v2/auth',
        token_endpoint='https://oauth2.googleapis.com/token',
        jwks_uri='https://www.googleapis.com/oauth2/v3/certs',
        origin='https://generativelanguage.googleapis.com',
        scopes=('openid', 'profile', 'email', 'https://www.googleapis.com/auth/cloud-platform'),
    ),
}
_LOOP_LOCKS = weakref.WeakKeyDictionary()
_LOCK_GUARD = threading.Lock()


@dataclass(frozen=True)
class OAuthConfig:
    client_id: str = ''
    client_secret: SecretStr | None = None
    scopes: tuple[str, ...] = ()
    api_origin: str = ''
    expected_subject: str = ''
    credential_ref: str = ''
    host_id: str = ''
    timeout: float = 180
    http_timeout: float = 20

    def __post_init__(self):
        if self.client_secret is not None and not isinstance(self.client_secret, SecretStr):
            try:
                object.__setattr__(self, 'client_secret', SecretStr(self.client_secret))
            except (TypeError, ValueError):
                raise AuthError(
                    'oauth_configuration', 'Desktop client secret 형식을 확인하세요.'
                ) from None
        object.__setattr__(self, 'scopes', tuple(self.scopes))
        if (
            not isinstance(self.timeout, (int, float))
            or isinstance(self.timeout, bool)
            or not 0.01 <= self.timeout <= 900
            or not isinstance(self.http_timeout, (int, float))
            or isinstance(self.http_timeout, bool)
            or not 0.01 <= self.http_timeout <= 120
        ):
            raise AuthError('oauth_configuration', '로그인 또는 통신 제한 시간을 확인하세요.')
        for name in ('client_id', 'expected_subject', 'credential_ref', 'host_id', 'api_origin'):
            value = getattr(self, name)
            if not isinstance(value, str) or len(value) > 2048 or any(c in value for c in '\r\n\0'):
                raise AuthError('oauth_configuration', 'OAuth 등록 정보 형식을 확인하세요.')
        if any(
            not isinstance(scope, str)
            or not scope
            or len(scope) > 1024
            or any(c.isspace() for c in scope)
            for scope in self.scopes
        ):
            raise AuthError(
                'oauth_configuration', '요청 권한은 비어 있지 않은 scope 목록이어야 합니다.'
            )


def _cancelled(cancel):
    return bool(cancel is not None and cancel.is_set())


async def _wait(awaitable, cancel, seconds):
    task = asyncio.ensure_future(awaitable)
    deadline = time.monotonic() + seconds
    try:
        while True:
            if _cancelled(cancel):
                raise AuthError('cancelled', '계정 연결을 취소했습니다.')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AuthError(
                    'oauth_timeout', '계정 연결 제한 시간이 지났습니다. 다시 시작하세요.'
                )
            done, _ = await asyncio.wait({task}, timeout=min(0.04, remaining))
            if done:
                return task.result()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class OAuthService:
    def __init__(self, store, transport=None):
        self.store, self.transport = store, transport

    @staticmethod
    def _configuration(provider, config):
        provider = 'google' if provider == 'gemini' else provider
        if provider not in _PROVIDERS:
            raise AuthError(
                'oauth_unsupported', '이 제공자의 자체 계정 로그인은 지원하지 않습니다.'
            )
        try:
            config = OAuthConfig(**config) if isinstance(config, dict) else config
        except (TypeError, ValueError):
            raise AuthError('oauth_configuration', 'OAuth 등록 정보 형식을 확인하세요.') from None
        if not isinstance(config, OAuthConfig):
            raise AuthError('oauth_configuration', 'OAuth 설정이 필요합니다.')
        specification = _PROVIDERS[provider]
        origin = canonical_origin(config.api_origin or specification['origin'])
        if origin != specification['origin']:
            raise AuthError('oauth_origin', '공식 계정 토큰을 다른 인증 대상에 연결할 수 없습니다.')
        if provider == 'google' and not config.client_id:
            raise AuthError(
                'oauth_registration_required',
                'Google Cloud에서 등록한 Desktop OAuth client ID가 필요합니다.',
            )
        if config.client_id == 'dynamic_agent_client':
            raise AuthError(
                'oauth_configuration',
                '동적 등록 시작 ID를 저장된 발급 client ID로 사용할 수 없습니다.',
            )
        scopes = config.scopes or specification['scopes']
        if not {'openid', 'profile', 'email'} <= set(scopes):
            raise AuthError(
                'oauth_configuration', '계정 검증을 위해 openid, profile, email 권한이 필요합니다.'
            )
        if provider == 'chatgpt' and not set(scopes) <= set(specification['scopes']):
            raise AuthError(
                'oauth_configuration', 'ChatGPT 공개 클라이언트의 공식 scope 범위를 확인하세요.'
            )
        return provider, config, specification, origin, tuple(scopes)

    def _host_id(self, configured):
        with self.store.transaction(None, 'oauth-host'):
            path = self.store.root / 'oauth-host.json'
            self.store._reparse(path)
            try:
                if path.exists():
                    value = json.loads(path.read_text(encoding='utf-8'))['host_id']
                    if configured and configured != value:
                        raise AuthError(
                            'oauth_host', '이 설치의 기존 host ID와 다른 값으로 바꿀 수 없습니다.'
                        )
                else:
                    value = configured or 'urn:uuid:' + str(uuid.uuid4())
                if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9:._-]{1,200}', value):
                    raise ValueError()
                if not path.exists():
                    temporary = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
                    try:
                        with temporary.open('x', encoding='utf-8') as stream:
                            json.dump(dict(schema=1, host_id=value), stream)
                            stream.flush()
                            os.fsync(stream.fileno())
                        os.replace(temporary, path)
                    finally:
                        temporary.unlink(missing_ok=True)
                return value
            except AuthError:
                raise
            except (OSError, ValueError, KeyError, TypeError):
                raise AuthError(
                    'oauth_host', '이 설치의 OAuth host ID를 안전하게 저장하거나 읽지 못했습니다.'
                ) from None

    def _client(self, config):
        return httpx.AsyncClient(
            transport=self.transport,
            trust_env=False,
            follow_redirects=False,
            timeout=config.http_timeout,
        )

    async def _request(self, client, method, url, config, cancel=None, data=None):
        async def fetch():
            async with client.stream(
                method, url, data=data, headers={'Accept': 'application/json'}
            ) as response:
                content = bytearray()
                async for part in response.aiter_bytes():
                    content.extend(part)
                    if len(content) > 2 * 1024 * 1024:
                        raise AuthError(
                            'oauth_response', '인증 서버 응답이 허용 크기를 초과했습니다.'
                        )
                if 300 <= response.status_code < 400:
                    raise AuthError(
                        'oauth_redirect', '인증 요청의 다른 주소 리디렉션을 거부했습니다.'
                    )
                if response.status_code >= 400:
                    code = 'oauth_http'
                    try:
                        reported = json.loads(content).get('error')
                        if reported in (
                            'invalid_grant',
                            'invalid_client',
                            'invalid_scope',
                            'access_denied',
                            'temporarily_unavailable',
                        ):
                            code = reported
                    except (ValueError, AttributeError):
                        pass
                    raise AuthError(
                        code,
                        '인증 서버가 요청을 거부했습니다. 등록·권한·다시 로그인 여부를 확인하세요.',
                    )
                try:
                    value = json.loads(content) if content else {}
                except ValueError:
                    raise AuthError(
                        'oauth_response', '인증 서버가 올바른 JSON을 반환하지 않았습니다.'
                    ) from None
                if not isinstance(value, dict):
                    raise AuthError('oauth_response', '인증 서버 응답 형식이 잘못되었습니다.')
                return value

        try:
            return await _wait(fetch(), cancel, config.http_timeout)
        except AuthError:
            raise
        except Exception:
            raise AuthError(
                'oauth_network',
                '인증 서버와 통신하지 못했습니다. 자격증명은 응답이 완전히 검증된 후에만 교체합니다.',
            ) from None

    async def _discovery(self, client, specification, config, cancel):
        document = await self._request(client, 'GET', specification['discovery'], config, cancel)
        if document.get('issuer') != specification['issuer']:
            raise AuthError('oauth_discovery', '공식 인증 서버의 issuer가 일치하지 않습니다.')
        for name in ('authorization_endpoint', 'token_endpoint', 'jwks_uri'):
            if document.get(name) != specification[name]:
                raise AuthError(
                    'oauth_discovery', '인증 서버의 공식 endpoint가 예상된 주소와 다릅니다.'
                )
        revocation = document.get('revocation_endpoint')
        if revocation:
            permitted = (
                'https://auth.openai.com'
                if specification is _PROVIDERS['chatgpt']
                else 'https://oauth2.googleapis.com'
            )
            try:
                parsed = urlsplit(revocation)
                if canonical_origin(revocation) != permitted or not parsed.path:
                    raise ValueError()
            except (AuthError, ValueError):
                raise AuthError(
                    'oauth_discovery', '토큰 폐기 endpoint가 공식 인증 범위를 벗어났습니다.'
                ) from None
        algorithms = document.get('id_token_signing_alg_values_supported', ['RS256'])
        document['_allowed_algorithms'] = [
            name for name in algorithms if name in ('RS256', 'ES256')
        ]
        if not document['_allowed_algorithms']:
            raise AuthError('oauth_discovery', '지원하는 ID 토큰 서명 알고리즘이 없습니다.')
        return document

    async def _identity(
        self,
        client,
        document,
        token,
        client_id,
        nonce,
        config,
        cancel,
        *,
        refresh=False,
        access_token=None,
    ):
        try:
            if not isinstance(token, str) or not 1 <= len(token) <= 128 * 1024:
                raise ValueError()
            header = jwt.get_unverified_header(token)
            if (
                header.get('alg') not in document['_allowed_algorithms']
                or not isinstance(header.get('kid'), str)
                or not header['kid']
                or len(header['kid']) > 256
                or header.get('crit')
                or any(key in header for key in ('jku', 'x5u'))
            ):
                raise ValueError()
            jwks = await self._request(client, 'GET', document['jwks_uri'], config, cancel)
            candidates = [
                key
                for key in jwks.get('keys', [])
                if isinstance(key, dict)
                and key.get('kid') == header['kid']
                and key.get('use', 'sig') == 'sig'
                and key.get('alg', header['alg']) == header['alg']
            ]
            if len(candidates) != 1:
                raise ValueError()
            key = jwt.PyJWK.from_dict(candidates[0], algorithm=header['alg']).key
            issuers = [document['issuer']]
            if document['issuer'] == 'https://accounts.google.com':
                issuers.append('accounts.google.com')
            claims = jwt.decode(
                token,
                key,
                algorithms=[header['alg']],
                audience=client_id,
                issuer=issuers,
                leeway=30,
                options={'require': ['iss', 'sub', 'aud', 'iat', 'exp']},
            )
            if (
                not isinstance(claims['sub'], str)
                or not 1 <= len(claims['sub']) <= 255
                or claims.get('azp', client_id) != client_id
                or isinstance(claims['aud'], list)
                and len(claims['aud']) > 1
                and claims.get('azp') != client_id
            ):
                raise ValueError()
            if not refresh or 'nonce' in claims:
                if not isinstance(claims.get('nonce'), str) or not hmac.compare_digest(
                    claims['nonce'], nonce
                ):
                    raise ValueError()
            if 'at_hash' in claims:
                if not isinstance(access_token, str) or not isinstance(claims['at_hash'], str):
                    raise ValueError()
                digest = jwt.algorithms.get_default_algorithms()[header['alg']].compute_hash_digest(
                    access_token.encode('ascii')
                )
                expected_hash = (
                    base64.urlsafe_b64encode(digest[: len(digest) // 2]).decode('ascii').rstrip('=')
                )
                if not hmac.compare_digest(claims['at_hash'], expected_hash):
                    raise ValueError()
            if config.expected_subject and not hmac.compare_digest(
                claims['sub'], config.expected_subject
            ):
                raise AuthError(
                    'oauth_account_mismatch',
                    '선택한 등록 계정과 로그인한 계정이 다릅니다. 다른 계정은 별도 프로필에 연결하세요.',
                )
            verified = claims.get('email_verified') in (True, 'true')
            if verified and (
                not isinstance(claims.get('email', ''), str)
                or len(claims.get('email', '')) > 320
                or any(c in claims.get('email', '') for c in '\r\n\0')
            ):
                raise ValueError()
            return dict(
                issuer=document['issuer'],
                subject=claims['sub'],
                email=claims.get('email', '') if verified else '',
                email_verified=verified,
            )
        except AuthError:
            raise
        except (jwt.PyJWTError, ValueError, TypeError, KeyError, AttributeError):
            raise AuthError(
                'oauth_id_token', 'ID 토큰의 서명·계정·대상·기간·nonce를 검증하지 못했습니다.'
            ) from None

    @asynccontextmanager
    async def _session_lock(self, ref, owner, config, cancel):
        loop = asyncio.get_running_loop()
        key = (str(self.store.root), ref)
        with _LOCK_GUARD:
            lock = _LOOP_LOCKS.setdefault(loop, {}).setdefault(key, asyncio.Lock())
        acquisition = asyncio.create_task(lock.acquire())
        try:
            await _wait(acquisition, cancel, config.http_timeout)
        except BaseException:
            if (
                acquisition.done()
                and not acquisition.cancelled()
                and acquisition.exception() is None
                and acquisition.result()
            ):
                lock.release()
            elif not acquisition.done():
                acquisition.cancel()
                await asyncio.gather(acquisition, return_exceptions=True)
            raise
        manager = None
        deadline = time.monotonic() + config.http_timeout
        try:
            while True:
                if _cancelled(cancel):
                    raise AuthError('cancelled', '계정 연결을 취소했습니다.')
                candidate = self.store.transaction(ref, owner, timeout=0)
                try:
                    candidate.__enter__()
                    manager = candidate
                    break
                except AuthError as exc:
                    if exc.code != 'credential_busy':
                        raise
                    if time.monotonic() >= deadline:
                        raise
                    await asyncio.sleep(0.04)
            yield
        finally:
            if manager is not None:
                manager.__exit__(None, None, None)
            lock.release()

    @staticmethod
    def _tokens(
        response, provider, client_id, account, nonce, host_id, scopes, config, previous=None
    ):
        previous = previous or {}
        try:
            access = response.get('access_token')
            if (
                not isinstance(access, str)
                or not 1 <= len(access) <= 128 * 1024
                or any(c in access for c in '\r\n\0')
            ):
                raise ValueError()
            if str(response.get('token_type', '')).lower() != 'bearer':
                raise ValueError()
            expires = response['expires_in']
            if (
                isinstance(expires, bool)
                or not isinstance(expires, (int, float))
                or not 0 < expires <= 366 * 86400
            ):
                raise ValueError()
            returned_scope = response.get('scope')
            if returned_scope is None:
                granted = previous.get('scopes', []) or (
                    [] if provider == 'chatgpt' else list(scopes)
                )
                scope_source = (
                    'previous_grant'
                    if previous
                    else 'not_returned' if provider == 'chatgpt' else 'request_per_oauth_contract'
                )
            else:
                if not isinstance(returned_scope, str) or len(returned_scope) > 32768:
                    raise ValueError()
                granted = sorted(set(returned_scope.split()))
                scope_source = 'token_response'
            refresh = response.get('refresh_token', previous.get('refresh_token', ''))
            if (
                not isinstance(refresh, str)
                or len(refresh) > 128 * 1024
                or any(c in refresh for c in '\r\n\0')
            ):
                raise ValueError()
            identity_token = response.get('id_token', previous.get('id_token', ''))
            secret = (
                config.client_secret.get_secret_value()
                if config.client_secret is not None
                else previous.get('client_secret', '')
            )
            return dict(
                schema=1,
                provider=provider,
                client_id=client_id,
                account=account,
                nonce=nonce,
                host_id=host_id,
                access_token=access,
                refresh_token=refresh,
                id_token=identity_token,
                token_type='Bearer',
                expires_at=time.time() + float(expires),
                scopes=list(granted),
                scope_source=scope_source,
                client_secret=secret,
            )
        except (ValueError, TypeError, KeyError):
            raise AuthError(
                'oauth_token_response', '토큰 응답의 형식·기간·권한 정보를 확인하지 못했습니다.'
            ) from None

    @staticmethod
    def _result(saved, tokens):
        plan = tokens['provider'] == 'chatgpt' and 'chatgpt.tokens.use.direct' in tokens['scopes']
        google_grant = 'https://www.googleapis.com/auth/cloud-platform' in tokens['scopes']
        return dict(
            credential_ref=saved['ref'],
            credential_version=saved['version'],
            account=tokens['account'],
            client_id=tokens['client_id'],
            host_id=tokens.get('host_id', ''),
            scopes=tokens['scopes'],
            plan_usage_enabled=plan,
            inference_authorized=plan if tokens['provider'] == 'chatgpt' else google_grant,
            actual_inference_verified=False,
            inference_verified=False,
        )

    async def login(self, provider, profile_id, config, cancel=None, on_url=None, *, on_event=None):
        provider, config, _, _, _ = self._configuration(provider, config)
        try:
            return await _wait(
                self._login(provider, profile_id, config, cancel, on_url, on_event=on_event),
                cancel,
                config.timeout,
            )
        except AuthError:
            raise
        except Exception:
            raise AuthError(
                'oauth_login', '계정 연결을 완료하지 못했습니다. 인증 정보는 교체하지 않았습니다.'
            ) from None

    async def _login(self, provider, profile_id, config, cancel=None, on_url=None, *, on_event=None):
        provider, config, _, _, _ = self._configuration(provider, config)
        pending = {}
        try:
            for round_number in range(2):
                pending.clear()
                try:
                    return await self._login_once(
                        provider, profile_id, config, cancel, on_url,
                        on_event=on_event, pending=pending,
                    )
                except AuthError as error:
                    if (
                        provider != 'chatgpt'
                        or round_number != 0
                        or error.code != 'invalid_grant'
                        or not pending.get('code_exchange_rejected')
                        or not pending.get('client_id')
                    ):
                        raise
                # The round has closed its listener and discarded its code. Only
                # its validated issued registration survives, in memory, until a
                # fresh authorization proves the account identity and permission.
                config = OAuthConfig(**{**config.__dict__, 'client_id': pending['client_id']})
                if _cancelled(cancel):
                    raise AuthError('cancelled', '계정 연결을 취소했습니다.')
                await emit(
                    on_event,
                    dict(
                        kind='oauth_login', provider=provider, profile_id=profile_id,
                        status='authorization_restart',
                    ),
                )
                if _cancelled(cancel):
                    raise AuthError('cancelled', '계정 연결을 취소했습니다.')
        finally:
            pending.clear()

    async def _login_once(
        self, provider, profile_id, config, cancel=None, on_url=None, *, on_event=None, pending,
    ):
        provider, config, specification, origin, scopes = self._configuration(provider, config)

        async def progress(status):
            if _cancelled(cancel):
                raise AuthError('cancelled', '계정 연결을 취소했습니다.')
            await emit(
                on_event,
                dict(kind='oauth_login', provider=provider, profile_id=profile_id, status=status),
            )
            if _cancelled(cancel):
                raise AuthError('cancelled', '계정 연결을 취소했습니다.')

        if _cancelled(cancel):
            raise AuthError('cancelled', '계정 연결을 취소했습니다.')
        previous = None
        if config.credential_ref:
            record = self.store.get(config.credential_ref, profile_id, origin)
            if record.kind != 'oauth_' + provider:
                raise AuthError('oauth_binding', '선택한 자격증명의 제공자가 다릅니다.')
            previous = json.loads(record.value.get_secret_value())
            if config.client_id and config.client_id != previous.get('client_id'):
                raise AuthError('oauth_binding', '선택한 계정의 발급 client ID가 다릅니다.')
            if config.expected_subject and config.expected_subject != previous.get(
                'account', {}
            ).get('subject'):
                raise AuthError(
                    'oauth_binding', '선택한 등록 계정의 subject를 다른 계정으로 바꿀 수 없습니다.'
                )
        client_id = config.client_id or (previous or {}).get('client_id', '')
        expected = config.expected_subject or (previous or {}).get('account', {}).get('subject', '')
        if expected != config.expected_subject:
            config = OAuthConfig(**{**config.__dict__, 'expected_subject': expected})
        host_id = self._host_id(config.host_id) if provider == 'chatgpt' else ''
        state, nonce, verifier = (
            secrets.token_urlsafe(32),
            secrets.token_urlsafe(32),
            secrets.token_urlsafe(64),
        )
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode('ascii')).digest())
            .decode('ascii')
            .rstrip('=')
        )
        future = asyncio.get_running_loop().create_future()
        connections = set()
        handlers = set()
        server = None
        redirect_uri = ''
        port = 0

        async def callback(reader, writer):
            task = asyncio.current_task()
            handlers.add(task)
            connections.add(writer)
            status = 400
            try:
                header = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), timeout=2)
                if len(header) > 8192:
                    raise ValueError()
                lines = header.decode('ascii').split('\r\n')
                request = lines[0].split(' ')
                if (
                    len(request) != 3
                    or request[0] != 'GET'
                    or request[2] not in ('HTTP/1.1', 'HTTP/1.0')
                ):
                    raise ValueError()
                headers = {}
                for line in lines[1:]:
                    if not line:
                        continue
                    key, value = line.split(':', 1)
                    key = key.lower()
                    if key in headers:
                        raise ValueError()
                    headers[key] = value.strip()
                if headers.get('host') != '127.0.0.1:' + str(port):
                    raise ValueError()
                parsed = urlsplit(request[1])
                if (
                    parsed.scheme
                    or parsed.netloc
                    or parsed.fragment
                    or parsed.path != '/auth/callback'
                ):
                    raise ValueError()
                query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=24)
                if any(
                    len(query.get(key, [])) > 1
                    for key in ('state', 'code', 'error', 'client_id', 'scope', 'iss')
                ):
                    raise ValueError()
                incoming = query.get('state', [''])[0]
                if not hmac.compare_digest(incoming, state) or future.done():
                    raise ValueError()
                if 'iss' in query and query['iss'][0] != specification['issuer']:
                    raise ValueError()
                if 'error' in query:
                    if 'code' in query:
                        raise ValueError()
                    code = (
                        'access_denied'
                        if query['error'][0] == 'access_denied'
                        else 'oauth_authorization'
                    )
                    future.set_exception(AuthError(code, '계정 권한 요청이 승인되지 않았습니다.'))
                    status = 200
                else:
                    code = query.get('code', [''])[0]
                    issued = query.get('client_id', [client_id])[0]
                    if not code or len(code) > 4096 or any(c in code for c in '\r\n\0'):
                        raise ValueError()
                    if provider == 'chatgpt' and (
                        not issued
                        or issued == 'dynamic_agent_client'
                        or client_id
                        and issued != client_id
                    ):
                        raise ValueError()
                    if provider == 'google' and 'client_id' in query and issued != client_id:
                        raise ValueError()
                    if (
                        not isinstance(issued, str)
                        or len(issued) > 2048
                        or any(c in issued for c in '\r\n\0')
                    ):
                        raise ValueError()
                    future.set_result(dict(code=code, client_id=issued))
                    status = 200
            except (
                ValueError,
                TypeError,
                UnicodeError,
                asyncio.TimeoutError,
                asyncio.IncompleteReadError,
                asyncio.LimitOverrunError,
            ):
                pass
            finally:
                body = (
                    '로그인 요청을 확인했습니다. 앱으로 돌아가세요.'
                    if status == 200
                    else '유효한 로그인 콜백이 아닙니다. 앱에서 다시 확인하세요.'
                ).encode('utf-8')
                try:
                    writer.write(
                        (
                            'HTTP/1.1 '
                            + str(status)
                            + ' '
                            + ('OK' if status == 200 else 'Bad Request')
                            + '\r\nContent-Type: text/plain; charset=utf-8\r\nCache-Control: no-store\r\nContent-Security-Policy: default-src \'none\'\r\nConnection: close\r\nContent-Length: '
                            + str(len(body))
                            + '\r\n\r\n'
                        ).encode('ascii')
                        + body
                    )
                    await writer.drain()
                except (OSError, ConnectionError):
                    pass
                writer.close()
                try:
                    await writer.wait_closed()
                except (OSError, ConnectionError):
                    pass
                connections.discard(writer)
                handlers.discard(task)

        try:
            async with self._client(config) as client:
                document = await self._discovery(client, specification, config, cancel)
                server = await asyncio.start_server(
                    callback, '127.0.0.1', 0, family=socket.AF_INET, limit=8192
                )
                port = server.sockets[0].getsockname()[1]
                redirect_uri = 'http://127.0.0.1:' + str(port) + '/auth/callback'
                parameters = dict(
                    client_id=client_id or 'dynamic_agent_client',
                    response_type='code',
                    redirect_uri=redirect_uri,
                    scope=' '.join(scopes),
                    state=state,
                    nonce=nonce,
                    code_challenge_method='S256',
                    code_challenge=challenge,
                )
                if provider == 'chatgpt':
                    parameters.update(ext_agent_host_id=host_id, resource=specification['resource'])
                    if not client_id:
                        parameters['agent_name_hint'] = 'Recheck'
                    if previous and previous.get('id_token'):
                        parameters['id_token_hint'] = previous['id_token']
                    if previous and previous.get('account', {}).get('email'):
                        parameters['login_hint'] = previous['account']['email']
                else:
                    parameters.update(
                        access_type='offline',
                        prompt='consent' if not previous else 'select_account',
                    )
                url = document['authorization_endpoint'] + '?' + urlencode(parameters)
                if on_url is None:
                    opened = await _wait(
                        asyncio.to_thread(webbrowser.open, url, new=2), cancel, config.timeout
                    )
                    if not opened:
                        raise AuthError('oauth_browser', '시스템 브라우저를 열지 못했습니다.')
                else:
                    outcome = on_url(url)
                    if inspect.isawaitable(outcome):
                        await _wait(outcome, cancel, config.timeout)
                returned = await _wait(asyncio.shield(future), cancel, config.timeout)
                await progress('callback_validated')
                client_id = returned['client_id']
                pending['client_id'] = client_id
                form = dict(
                    grant_type='authorization_code',
                    client_id=client_id,
                    code=returned['code'],
                    code_verifier=verifier,
                    redirect_uri=redirect_uri,
                )
                if provider == 'chatgpt':
                    form['resource'] = specification['resource']
                elif config.client_secret is not None:
                    form['client_secret'] = config.client_secret.get_secret_value()
                await progress('token_exchange')
                try:
                    response = await self._request(
                        client, 'POST', document['token_endpoint'], config, cancel, form
                    )
                except AuthError as error:
                    pending['code_exchange_rejected'] = error.code == 'invalid_grant'
                    raise
                account = await self._identity(
                    client,
                    document,
                    response.get('id_token'),
                    client_id,
                    nonce,
                    config,
                    cancel,
                    access_token=response.get('access_token'),
                )
                await progress('identity_verified')
                tokens = self._tokens(
                    response, provider, client_id, account, nonce, host_id, scopes, config
                )
                saved = self.store.put(profile_id, origin, 'oauth_' + provider, tokens)
                return self._result(saved, tokens)
        except AuthError:
            raise
        except (OSError, ValueError, TypeError, KeyError):
            raise AuthError(
                'oauth_login', '계정 연결을 완료하지 못했습니다. 인증 정보는 교체하지 않았습니다.'
            ) from None
        finally:
            if server is not None:
                server.close()
                await server.wait_closed()
            for writer in list(connections):
                writer.close()
            if handlers:
                await asyncio.gather(*list(handlers), return_exceptions=True)
            if future.done() and not future.cancelled():
                future.exception()
            elif not future.done():
                future.cancel()

    def _stored(self, provider, profile_id, ref, origin, config, record=None):
        record = record or self.store.get(ref, profile_id, origin)
        try:
            tokens = json.loads(record.value.get_secret_value())
            if (
                record.kind != 'oauth_' + provider
                or tokens['provider'] != provider
                or tokens.get('schema') != 1
                or not isinstance(tokens['account']['subject'], str)
                or not tokens['account']['subject']
                or tokens['account']['issuer'] != _PROVIDERS[provider]['issuer']
                or not isinstance(tokens.get('client_id'), str)
                or not tokens['client_id']
                or tokens['client_id'] == 'dynamic_agent_client'
                or not isinstance(tokens.get('scopes'), list)
                or any(not isinstance(scope, str) for scope in tokens['scopes'])
                or not isinstance(tokens.get('expires_at'), (float, int))
                or isinstance(tokens.get('expires_at'), bool)
            ):
                raise ValueError()
            if config.client_id and tokens['client_id'] != config.client_id:
                raise ValueError()
            if config.expected_subject and tokens['account']['subject'] != config.expected_subject:
                raise ValueError()
            return record, tokens
        except (ValueError, KeyError, TypeError):
            raise AuthError(
                'oauth_binding', '선택한 프로필의 제공자·계정·client 등록이 저장된 인증과 다릅니다.'
            ) from None

    def account_status(self, ref, owner, origin):
        origin = canonical_origin(origin)
        record = self.store.get(ref, owner, origin)
        provider = record.kind.removeprefix('oauth_')
        if provider not in _PROVIDERS or origin != _PROVIDERS[provider]['origin']:
            raise AuthError('oauth_binding', '공식 OAuth 계정의 인증 대상과 저장 기록이 다릅니다.')
        _, tokens = self._stored(provider, owner, ref, origin, OAuthConfig(), record=record)
        result = self._result(dict(ref=ref, version=record.version), tokens)
        result.update(
            authenticated=True,
            provider=provider,
            expired=tokens['expires_at'] <= time.time(),
            expires_at=tokens['expires_at'],
        )
        return result

    async def refresh(self, provider, profile_id, ref, config, cancel=None, force=False):
        provider, config, specification, origin, scopes = self._configuration(provider, config)
        async with self._session_lock(ref, profile_id, config, cancel):
            record, tokens = self._stored(provider, profile_id, ref, origin, config)
            if not force and tokens.get('expires_at', 0) > time.time() + 60:
                return self._result(dict(ref=ref, version=record.version), tokens)
            if not tokens.get('refresh_token'):
                raise AuthError(
                    'oauth_reauthentication',
                    '갱신 토큰이 없습니다. 브라우저에서 다시 로그인하세요.',
                )
            async with self._client(config) as client:
                document = await self._discovery(client, specification, config, cancel)
                form = dict(
                    grant_type='refresh_token',
                    client_id=tokens['client_id'],
                    refresh_token=tokens['refresh_token'],
                )
                if provider == 'chatgpt':
                    form['resource'] = specification['resource']
                elif tokens.get('client_secret'):
                    form['client_secret'] = tokens['client_secret']
                response = await self._request(
                    client, 'POST', document['token_endpoint'], config, cancel, form
                )
                account = tokens['account']
                if response.get('id_token'):
                    bound = OAuthConfig(
                        **{**config.__dict__, 'expected_subject': account['subject']}
                    )
                    account = await self._identity(
                        client,
                        document,
                        response['id_token'],
                        tokens['client_id'],
                        tokens['nonce'],
                        bound,
                        cancel,
                        refresh=True,
                        access_token=response.get('access_token'),
                    )
                replacement = self._tokens(
                    response,
                    provider,
                    tokens['client_id'],
                    account,
                    tokens['nonce'],
                    tokens.get('host_id', ''),
                    scopes,
                    config,
                    tokens,
                )
                saved = self.store.put(
                    profile_id,
                    origin,
                    'oauth_' + provider,
                    replacement,
                    ref=ref,
                    expected_version=record.version,
                )
                return self._result(saved, replacement)

    async def logout(self, provider, profile_id, ref, config, cancel=None):
        provider, config, specification, origin, scopes = self._configuration(provider, config)
        async with self._session_lock(ref, profile_id, config, cancel):
            try:
                record, tokens = self._stored(provider, profile_id, ref, origin, config)
            except AuthError as exc:
                if exc.code == 'credential_missing':
                    return dict(
                        local_deleted=True,
                        remote_revocation='not_supported',
                        client_id=config.client_id,
                        account=None,
                    )
                raise
            remote = 'not_supported'
            try:
                async with self._client(config) as client:
                    document = await self._discovery(client, specification, config, cancel)
                    endpoint = document.get('revocation_endpoint')
                    token = tokens.get('refresh_token') or tokens.get('access_token')
                    if endpoint and token:
                        remote = 'not_confirmed'
                        form = dict(token=token)
                        if provider == 'chatgpt':
                            form.update(
                                token_type_hint=(
                                    'refresh_token'
                                    if tokens.get('refresh_token')
                                    else 'access_token'
                                ),
                                client_id=tokens['client_id'],
                            )
                        for attempt in range(2):
                            try:
                                await self._request(client, 'POST', endpoint, config, cancel, form)
                                remote = 'confirmed'
                                break
                            except AuthError:
                                if attempt == 0 and not _cancelled(cancel):
                                    await asyncio.sleep(0.15)
            except AuthError:
                remote = 'not_confirmed'
            finally:
                self.store.delete(ref, profile_id)
            return dict(
                local_deleted=True,
                remote_revocation=remote,
                client_id=tokens['client_id'],
                account=tokens['account'],
                host_id=tokens.get('host_id', ''),
            )
