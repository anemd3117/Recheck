"""공식 URL을 모의 HTTP로 대체하고 실제 loopback에서 OAuth 경계를 검사한다. 실제 계정은 사용하지 않는다."""

import asyncio
import base64
import importlib
import json
import threading
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
import pytest

CHATGPT = dict(
    issuer='https://auth.openai.com',
    authorization_endpoint='https://auth.openai.com/api/accounts/authorize',
    token_endpoint='https://auth.openai.com/api/accounts/oauth/token',
    jwks_uri='https://auth.openai.com/.well-known/jwks.json',
    revocation_endpoint='https://auth.openai.com/oauth/revoke',
)
GOOGLE = dict(
    issuer='https://accounts.google.com',
    authorization_endpoint='https://accounts.google.com/o/oauth2/v2/auth',
    token_endpoint='https://oauth2.googleapis.com/token',
    jwks_uri='https://www.googleapis.com/oauth2/v3/certs',
    revocation_endpoint='https://oauth2.googleapis.com/revoke',
)


def module():
    return importlib.import_module('ai_providers.oauth')


def b64integer(value):
    return (
        base64.urlsafe_b64encode(value.to_bytes((value.bit_length() + 7) // 8, 'big'))
        .decode()
        .rstrip('=')
    )


class ProviderFixture:
    def __init__(self, provider='chatgpt', mutation=None):
        self.provider = provider
        self.endpoints = CHATGPT if provider == 'chatgpt' else GOOGLE
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public = self.key.public_key().public_numbers()
        self.jwk = dict(
            kty='RSA',
            kid='fixture-signing-key',
            use='sig',
            alg='RS256',
            n=b64integer(public.n),
            e=b64integer(public.e),
        )
        self.auth = {}
        self.requests = []
        self.token_calls = 0
        self.refresh_calls = 0
        self.mutation = mutation
        self.subject = 'fixture-account-a'

    def token(self, audience, *, refresh=False):
        now = int(time.time())
        claims = dict(
            iss=self.endpoints['issuer'],
            sub=self.subject,
            aud=audience,
            iat=now,
            exp=now + 600,
            nonce=self.auth['nonce'][0],
            email='fixture@example.invalid',
            email_verified=True,
        )
        if self.mutation == 'nonce':
            claims['nonce'] = 'wrong-nonce'
        elif self.mutation == 'issuer':
            claims['iss'] = 'https://attacker.invalid'
        elif self.mutation == 'audience':
            claims['aud'] = 'wrong-client'
        elif self.mutation == 'expired':
            claims['exp'] = now - 120
        elif self.mutation == 'missing_sub':
            claims.pop('sub')
        elif self.mutation == 'azp':
            claims['azp'] = 'wrong-client'
        elif self.mutation == 'at_hash':
            claims['at_hash'] = 'wrong-access-token-hash'
        signing = (
            rsa.generate_private_key(public_exponent=65537, key_size=2048)
            if self.mutation == 'signature'
            else self.key
        )
        token = jwt.encode(
            claims, signing, algorithm='RS256', headers={'kid': 'fixture-signing-key'}
        )
        scopes = (
            'openid profile email offline_access resource.invoke chatgpt.tokens.use.direct'
            if self.provider == 'chatgpt'
            else 'openid profile email https://www.googleapis.com/auth/cloud-platform'
        )
        if self.mutation == 'identity_only':
            scopes = 'openid profile email'
        elif self.mutation == 'unrelated_google_scope':
            scopes = 'openid profile email https://www.googleapis.com/auth/drive.readonly'
        return dict(
            access_token='fixture-access-rotated' if refresh else 'fixture-access-initial',
            refresh_token='fixture-refresh-rotated' if refresh else 'fixture-refresh-initial',
            id_token=token,
            scope=scopes,
            token_type='Bearer',
            expires_in=3600,
        )

    async def handler(self, request):
        url = str(request.url)
        data = (
            {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
            if request.content
            else {}
        )
        self.requests.append((url, data))
        if url.endswith('/.well-known/openid-configuration'):
            discovery = dict(self.endpoints, id_token_signing_alg_values_supported=['RS256'])
            if self.mutation == 'discovery_redirect':
                discovery['token_endpoint'] = 'https://attacker.invalid/token'
            return httpx.Response(200, json=discovery)
        if url == self.endpoints['jwks_uri']:
            return httpx.Response(200, json={'keys': [self.jwk]})
        if url == self.endpoints['token_endpoint']:
            self.token_calls += 1
            if self.mutation == 'token_error':
                return httpx.Response(
                    400,
                    json={'error': 'invalid_grant', 'error_description': 'fixture-access-initial'},
                )
            if self.mutation == 'http_redirect':
                return httpx.Response(302, headers={'Location': 'https://attacker.invalid/collect'})
            refresh = data.get('grant_type') == 'refresh_token'
            if refresh:
                self.refresh_calls += 1
                await asyncio.sleep(0.04)
                assert data['refresh_token'] == 'fixture-refresh-initial'
            else:
                assert data['code'] == 'fixture-code'
                challenge = (
                    base64.urlsafe_b64encode(
                        __import__('hashlib').sha256(data['code_verifier'].encode()).digest()
                    )
                    .decode()
                    .rstrip('=')
                )
                assert (
                    challenge == self.auth['code_challenge'][0]
                    and data['redirect_uri'] == self.auth['redirect_uri'][0]
                )
            return httpx.Response(200, json=self.token(data['client_id'], refresh=refresh))
        if url == self.endpoints['revocation_endpoint']:
            return httpx.Response(500 if self.mutation == 'revocation_failure' else 200)
        raise AssertionError('unexpected mock endpoint')

    async def on_url(self, url, *, client_id=None):
        self.auth = parse_qs(urlsplit(url).query)
        parsed = urlsplit(self.auth['redirect_uri'][0])
        assert (
            parsed.scheme == 'http'
            and parsed.hostname == '127.0.0.1'
            and parsed.path == '/auth/callback'
        )
        params = dict(code='fixture-code', state=self.auth['state'][0])
        if self.provider == 'chatgpt':
            params['client_id'] = client_id or 'oaiapp_fixture-issued'
        async with httpx.AsyncClient(trust_env=False) as client:
            result = await client.get(self.auth['redirect_uri'][0], params=params)
        assert result.status_code == 200


def service(tmp_path, provider):
    credentials = importlib.import_module('ai_providers.credentials')
    store = credentials.CredentialStore(tmp_path)
    return module().OAuthService(store, transport=httpx.MockTransport(provider.handler)), store


@pytest.mark.parametrize('name', ['chatgpt', 'google'])
def test_real_loopback_code_pkce_signed_identity_and_dpapi_storage(tmp_path, name):
    async def scenario():
        fixture = ProviderFixture(name)
        oauth, store = service(tmp_path, fixture)
        cfg = module().OAuthConfig(
            client_id='' if name == 'chatgpt' else 'fixture.apps.googleusercontent.com'
        )
        result = await oauth.login(name, 'profile-a', cfg, on_url=fixture.on_url)
        assert result['account']['subject'] == 'fixture-account-a'
        assert result['credential_version'] == 1 and result['scopes']
        assert 'fixture-access-initial' not in repr(result)
        if name == 'chatgpt':
            assert (
                fixture.auth['client_id'] == ['dynamic_agent_client']
                and result['client_id'] == 'oaiapp_fixture-issued'
            )
            assert (
                fixture.auth['agent_name_hint'] == ['Recheck']
                and result['plan_usage_enabled'] is True
            )
        else:
            assert fixture.auth['code_challenge_method'] == ['S256']
        origin = (
            'https://api.openai.com'
            if name == 'chatgpt'
            else 'https://generativelanguage.googleapis.com'
        )
        protected = store.get(result['credential_ref'], 'profile-a', origin)
        assert (
            json.loads(protected.value.get_secret_value())['access_token']
            == 'fixture-access-initial'
        )
        assert all(
            b'fixture-access-initial' not in path.read_bytes()
            for path in tmp_path.rglob('*')
            if path.is_file()
        )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    'mutation', ['nonce', 'issuer', 'audience', 'signature', 'expired', 'missing_sub', 'azp']
)
def test_jwt_forgery_or_wrong_identity_claim_never_becomes_active(tmp_path, mutation):
    async def scenario():
        fixture = ProviderFixture(mutation=mutation)
        oauth, _ = service(tmp_path, fixture)
        with pytest.raises(module().AuthError):
            await oauth.login('chatgpt', 'profile-a', module().OAuthConfig(), on_url=fixture.on_url)
        assert not list(tmp_path.glob('cred_*.json'))

    asyncio.run(scenario())


def test_wrong_callback_host_path_state_and_duplicate_query_are_ignored_before_valid_callback(
    tmp_path,
):
    async def scenario():
        fixture = ProviderFixture()
        oauth, _ = service(tmp_path, fixture)

        async def callback(url):
            fixture.auth = parse_qs(urlsplit(url).query)
            base = fixture.auth['redirect_uri'][0]
            async with httpx.AsyncClient(trust_env=False) as client:
                wrong_state = await client.get(
                    base,
                    params={
                        'code': 'fixture-code',
                        'state': 'wrong',
                        'client_id': 'oaiapp_fixture-issued',
                    },
                )
                wrong_path = await client.get(
                    base.replace('/auth/callback', '/wrong'),
                    params={'state': fixture.auth['state'][0]},
                )
                wrong_host = await client.get(
                    base,
                    params={'state': fixture.auth['state'][0]},
                    headers={'Host': 'localhost:' + str(urlsplit(base).port)},
                )
                duplicate = await client.get(
                    base
                    + '?state='
                    + fixture.auth['state'][0]
                    + '&state=second&code=fixture-code&client_id=oaiapp_fixture-issued'
                )
            assert [
                wrong_state.status_code,
                wrong_path.status_code,
                wrong_host.status_code,
                duplicate.status_code,
            ] == [400, 400, 400, 400]
            assert fixture.token_calls == 0
            await fixture.on_url(url)

        result = await oauth.login('chatgpt', 'profile-a', module().OAuthConfig(), on_url=callback)
        assert result['account']['subject'] == 'fixture-account-a' and fixture.token_calls == 1

    asyncio.run(scenario())


@pytest.mark.parametrize('action', ['cancel', 'timeout', 'access_denied', 'missing_dynamic_id'])
def test_cancel_timeout_denial_and_incomplete_registration_never_exchange_code(tmp_path, action):
    async def scenario():
        fixture = ProviderFixture()
        oauth, _ = service(tmp_path, fixture)
        cancel = threading.Event()
        callback_uri = []

        async def callback(url):
            fixture.auth = parse_qs(urlsplit(url).query)
            base = fixture.auth['redirect_uri'][0]
            callback_uri.append(base)
            if action == 'cancel':
                cancel.set()
            elif action in ('access_denied', 'missing_dynamic_id'):
                params = {'state': fixture.auth['state'][0]}
                if action == 'access_denied':
                    params['error'] = 'access_denied'
                else:
                    params['code'] = 'fixture-code'
                async with httpx.AsyncClient(trust_env=False) as client:
                    await client.get(base, params=params)
                if action == 'missing_dynamic_id':
                    cancel.set()

        with pytest.raises(module().AuthError) as raised:
            await oauth.login(
                'chatgpt',
                'profile-a',
                module().OAuthConfig(timeout=0.12),
                cancel=cancel,
                on_url=callback,
            )
        assert (
            raised.value.code in ('cancelled', 'oauth_timeout', 'access_denied')
            and fixture.token_calls == 0
        )
        assert not list(tmp_path.glob('cred_*.json'))
        async with httpx.AsyncClient(trust_env=False, timeout=0.1) as client:
            with pytest.raises((httpx.ConnectError, httpx.ConnectTimeout)):
                await client.get(callback_uri[0])

    asyncio.run(scenario())


def test_returning_client_account_and_host_are_kept_separate_and_wrong_account_preserves_old_tokens(
    tmp_path,
):
    async def scenario():
        fixture = ProviderFixture()
        oauth, store = service(tmp_path, fixture)
        first = await oauth.login(
            'chatgpt', 'profile-a', module().OAuthConfig(), on_url=fixture.on_url
        )
        host = first['host_id']
        fixture.subject = 'fixture-account-b'
        config = module().OAuthConfig(
            client_id=first['client_id'],
            expected_subject='fixture-account-a',
            credential_ref=first['credential_ref'],
        )
        with pytest.raises(module().AuthError):
            await oauth.login('chatgpt', 'profile-a', config, on_url=fixture.on_url)
        assert (
            fixture.auth['client_id'] == ['oaiapp_fixture-issued']
            and 'agent_name_hint' not in fixture.auth
        )
        assert fixture.auth['ext_agent_host_id'] == [host]
        assert (
            store.get(first['credential_ref'], 'profile-a', 'https://api.openai.com').version == 1
        )

    asyncio.run(scenario())


def test_missing_plan_scope_is_distinct_from_verified_identity_login(tmp_path):
    async def scenario():
        fixture = ProviderFixture(mutation='identity_only')
        oauth, _ = service(tmp_path, fixture)
        result = await oauth.login(
            'chatgpt', 'profile-a', module().OAuthConfig(), on_url=fixture.on_url
        )
        assert (
            result['account']['subject'] == 'fixture-account-a'
            and result['plan_usage_enabled'] is False
        )
        assert result['inference_authorized'] is False

    asyncio.run(scenario())


def test_refresh_is_serialized_across_service_instances_and_retains_rotated_token(tmp_path):
    async def scenario():
        fixture = ProviderFixture()
        oauth, store = service(tmp_path, fixture)
        result = await oauth.login(
            'chatgpt', 'profile-a', module().OAuthConfig(), on_url=fixture.on_url
        )
        record = store.get(result['credential_ref'], 'profile-a', 'https://api.openai.com')
        tokens = json.loads(record.value.get_secret_value())
        tokens['expires_at'] = time.time() - 1
        store.put(
            'profile-a',
            'https://api.openai.com',
            'oauth_chatgpt',
            tokens,
            ref=record.ref,
            expected_version=record.version,
        )
        another = module().OAuthService(store, transport=httpx.MockTransport(fixture.handler))
        config = module().OAuthConfig(client_id=result['client_id'])
        refreshed = await asyncio.gather(
            oauth.refresh('chatgpt', 'profile-a', record.ref, config),
            another.refresh('chatgpt', 'profile-a', record.ref, config),
        )
        assert (
            fixture.refresh_calls == 1
            and refreshed[0]['credential_version'] == refreshed[1]['credential_version']
        )
        current = json.loads(
            store.get(record.ref, 'profile-a', 'https://api.openai.com').value.get_secret_value()
        )
        assert (
            current['refresh_token'] == 'fixture-refresh-rotated'
            and current['access_token'] == 'fixture-access-rotated'
        )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    'mutation,expected', [(None, 'confirmed'), ('revocation_failure', 'not_confirmed')]
)
def test_logout_distinguishes_local_deletion_from_remote_revocation(tmp_path, mutation, expected):
    async def scenario():
        fixture = ProviderFixture()
        oauth, store = service(tmp_path, fixture)
        result = await oauth.login(
            'chatgpt', 'profile-a', module().OAuthConfig(), on_url=fixture.on_url
        )
        fixture.mutation = mutation
        logged_out = await oauth.logout(
            'chatgpt',
            'profile-a',
            result['credential_ref'],
            module().OAuthConfig(client_id=result['client_id']),
        )
        assert logged_out['local_deleted'] is True and logged_out['remote_revocation'] == expected
        with pytest.raises(module().AuthError):
            store.get(result['credential_ref'], 'profile-a', 'https://api.openai.com')
        assert (
            logged_out['client_id'] == result['client_id']
            and logged_out['account']['subject'] == 'fixture-account-a'
        )

    asyncio.run(scenario())


@pytest.mark.parametrize('mutation', ['discovery_redirect', 'http_redirect', 'token_error'])
def test_endpoint_redirect_or_error_body_cannot_exfiltrate_tokens_or_secret_exception(
    tmp_path, mutation
):
    async def scenario():
        fixture = ProviderFixture(mutation=mutation)
        oauth, _ = service(tmp_path, fixture)
        with pytest.raises(module().AuthError) as raised:
            await oauth.login('chatgpt', 'profile-a', module().OAuthConfig(), on_url=fixture.on_url)
        assert 'fixture-access-initial' not in str(raised.value)
        assert not any('attacker.invalid' in url for url, data in fixture.requests)

    asyncio.run(scenario())


def test_account_status_exposes_grants_and_expiry_without_token_values(tmp_path):
    async def scenario():
        fixture = ProviderFixture()
        oauth, store = service(tmp_path, fixture)
        result = await oauth.login(
            'chatgpt', 'profile-a', module().OAuthConfig(), on_url=fixture.on_url
        )
        state = oauth.account_status(
            result['credential_ref'], 'profile-a', 'https://api.openai.com'
        )
        assert (
            state['authenticated'] is True
            and state['inference_authorized'] is True
            and state['expired'] is False
        )
        assert state['inference_verified'] is False and state['actual_inference_verified'] is False
        assert 'fixture-access-initial' not in json.dumps(state) and 'refresh_token' not in state
        record = store.get(result['credential_ref'], 'profile-a', 'https://api.openai.com')
        tokens = json.loads(record.value.get_secret_value())
        tokens['expires_at'] = time.time() - 5
        store.put('profile-a', 'https://api.openai.com', 'oauth_chatgpt', tokens, ref=record.ref)
        expired = oauth.account_status(record.ref, 'profile-a', 'https://api.openai.com')
        assert expired['expired'] is True and expired['inference_authorized'] is True
        with pytest.raises(module().AuthError):
            oauth.account_status(record.ref, 'profile-b', 'https://api.openai.com')

    asyncio.run(scenario())


def test_signed_access_token_hash_mismatch_is_rejected(tmp_path):
    async def scenario():
        fixture = ProviderFixture(mutation='at_hash')
        oauth, _ = service(tmp_path, fixture)
        with pytest.raises(module().AuthError):
            await oauth.login('chatgpt', 'profile-a', module().OAuthConfig(), on_url=fixture.on_url)
        assert not list(tmp_path.glob('cred_*.json'))

    asyncio.run(scenario())


def test_login_deadline_covers_token_exchange_after_callback(tmp_path):
    async def scenario():
        fixture = ProviderFixture()
        original = fixture.handler

        async def delayed(request):
            if str(request.url) == fixture.endpoints['token_endpoint']:
                await asyncio.sleep(0.2)
            return await original(request)

        oauth, _ = service(tmp_path, fixture)
        oauth.transport = httpx.MockTransport(delayed)
        with pytest.raises(module().AuthError) as raised:
            await oauth.login(
                'chatgpt',
                'profile-a',
                module().OAuthConfig(timeout=0.08, http_timeout=2),
                on_url=fixture.on_url,
            )
        assert raised.value.code == 'oauth_timeout' and not list(tmp_path.glob('cred_*.json'))

    asyncio.run(scenario())


def test_browser_callback_failure_does_not_escape_with_sensitive_exception_text(tmp_path):
    async def scenario():
        fixture = ProviderFixture()
        oauth, _ = service(tmp_path, fixture)

        def failed(url):
            raise RuntimeError('fixture-sensitive-callback')

        with pytest.raises(module().AuthError) as raised:
            await oauth.login('chatgpt', 'profile-a', module().OAuthConfig(), on_url=failed)
        assert 'fixture-sensitive-callback' not in str(raised.value)

    asyncio.run(scenario())


def test_stored_account_issuer_is_checked_before_refresh(tmp_path):
    async def scenario():
        fixture = ProviderFixture()
        oauth, store = service(tmp_path, fixture)
        result = await oauth.login(
            'chatgpt', 'profile-a', module().OAuthConfig(), on_url=fixture.on_url
        )
        record = store.get(result['credential_ref'], 'profile-a', 'https://api.openai.com')
        tokens = json.loads(record.value.get_secret_value())
        tokens['account']['issuer'] = 'https://wrong.invalid'
        tokens['expires_at'] = 0
        store.put('profile-a', 'https://api.openai.com', 'oauth_chatgpt', tokens, ref=record.ref)
        with pytest.raises(module().AuthError):
            await oauth.refresh(
                'chatgpt',
                'profile-a',
                record.ref,
                module().OAuthConfig(client_id=result['client_id']),
            )
        assert fixture.refresh_calls == 0

    asyncio.run(scenario())


def test_reauthorization_cannot_change_subject_binding_of_an_existing_registration(tmp_path):
    async def scenario():
        fixture = ProviderFixture()
        oauth, store = service(tmp_path, fixture)
        result = await oauth.login(
            'chatgpt', 'profile-a', module().OAuthConfig(), on_url=fixture.on_url
        )
        fixture.subject = 'fixture-account-b'
        config = module().OAuthConfig(
            client_id=result['client_id'],
            expected_subject=fixture.subject,
            credential_ref=result['credential_ref'],
        )
        with pytest.raises(module().AuthError):
            await oauth.login('chatgpt', 'profile-a', config, on_url=fixture.on_url)
        assert store.metadata(result['credential_ref'], 'profile-a')['version'] == 1

    asyncio.run(scenario())


def test_session_lock_is_released_if_cancellation_follows_successful_acquisition(
    tmp_path, monkeypatch
):
    async def scenario():
        fixture = ProviderFixture()
        oauth, store = service(tmp_path, fixture)
        auth = module()
        ref = store.put('profile-a', 'https://api.openai.com', 'api_key', 'fixture-only')['ref']
        original = auth._wait
        first = True

        async def cancelled_after_acquire(awaitable, cancel, seconds):
            nonlocal first
            if first:
                first = False
                await awaitable
                raise auth.AuthError('cancelled', '테스트 취소')
            return await original(awaitable, cancel, seconds)

        monkeypatch.setattr(auth, '_wait', cancelled_after_acquire)
        with pytest.raises(auth.AuthError):
            async with oauth._session_lock(
                ref, 'profile-a', auth.OAuthConfig(http_timeout=0.08), None
            ):
                pass
        async with oauth._session_lock(ref, 'profile-a', auth.OAuthConfig(http_timeout=0.08), None):
            assert store.metadata(ref, 'profile-a')['version'] == 1

    asyncio.run(scenario())


def test_google_unrelated_api_scope_is_not_a_gemini_inference_grant(tmp_path):
    async def scenario():
        fixture = ProviderFixture('google', mutation='unrelated_google_scope')
        oauth, _ = service(tmp_path, fixture)
        result = await oauth.login(
            'google',
            'profile-a',
            module().OAuthConfig(client_id='fixture.apps.googleusercontent.com'),
            on_url=fixture.on_url,
        )
        assert (
            result['account']['subject'] == 'fixture-account-a'
            and result['inference_authorized'] is False
        )

    asyncio.run(scenario())


def test_desktop_client_secret_is_protected_and_reused_without_exporting_it(tmp_path):
    async def scenario():
        fixture = ProviderFixture('google')
        oauth, store = service(tmp_path, fixture)
        config = module().OAuthConfig(
            client_id='fixture.apps.googleusercontent.com', client_secret='fixture-desktop-secret'
        )
        result = await oauth.login('google', 'profile-a', config, on_url=fixture.on_url)
        assert 'fixture-desktop-secret' not in repr(config)
        record = store.get(
            result['credential_ref'], 'profile-a', 'https://generativelanguage.googleapis.com'
        )
        tokens = json.loads(record.value.get_secret_value())
        tokens['expires_at'] = 0
        store.put(
            'profile-a',
            'https://generativelanguage.googleapis.com',
            'oauth_google',
            tokens,
            ref=record.ref,
        )
        await oauth.refresh(
            'google', 'profile-a', record.ref, module().OAuthConfig(client_id=config.client_id)
        )
        assert all(
            data.get('client_secret') == 'fixture-desktop-secret'
            for url, data in fixture.requests
            if url == GOOGLE['token_endpoint']
        )
        assert all(
            b'fixture-desktop-secret' not in path.read_bytes()
            for path in tmp_path.rglob('*')
            if path.is_file()
        )

    asyncio.run(scenario())


def test_refresh_transport_failure_cannot_escape_with_token_text(tmp_path):
    async def scenario():
        fixture = ProviderFixture()
        oauth, store = service(tmp_path, fixture)
        result = await oauth.login(
            'chatgpt', 'profile-a', module().OAuthConfig(), on_url=fixture.on_url
        )
        record = store.get(result['credential_ref'], 'profile-a', 'https://api.openai.com')
        tokens = json.loads(record.value.get_secret_value())
        tokens['expires_at'] = 0
        store.put('profile-a', 'https://api.openai.com', 'oauth_chatgpt', tokens, ref=record.ref)
        original = fixture.handler

        async def broken(request):
            if str(request.url) == CHATGPT['token_endpoint']:
                raise RuntimeError('fixture-refresh-initial')
            return await original(request)

        oauth.transport = httpx.MockTransport(broken)
        with pytest.raises(module().AuthError) as raised:
            await oauth.refresh(
                'chatgpt',
                'profile-a',
                record.ref,
                module().OAuthConfig(client_id=result['client_id']),
            )
        assert 'fixture-refresh-initial' not in str(raised.value)

    asyncio.run(scenario())
