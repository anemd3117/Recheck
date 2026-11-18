"""Exercise callback documents over the real loopback listener without an account."""

import asyncio
import base64
import hashlib
from html.parser import HTMLParser
import threading
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from ai_providers.credentials import AuthError
from ai_providers.oauth import OAuthConfig, OAuthService


class CallbackDocument(HTMLParser):
    def __init__(self):
        super().__init__()
        self.elements = []
        self.text = []
        self.styles = []
        self.in_style = False

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))
        self.in_style = tag == 'style'

    def handle_endtag(self, tag):
        if tag == 'style':
            self.in_style = False

    def handle_data(self, data):
        (self.styles if self.in_style else self.text).append(data)


async def callback_responses(outcome, *, plan_hint='private-plan-hint'):
    """Stop after reading callback responses, before token exchange or storage."""
    cancel = threading.Event()
    responses = []

    def discovery(request):
        assert str(request.url) == 'https://accounts.google.com/.well-known/openid-configuration'
        return httpx.Response(200, json={
            'issuer': 'https://accounts.google.com',
            'authorization_endpoint': 'https://accounts.google.com/o/oauth2/v2/auth',
            'token_endpoint': 'https://oauth2.googleapis.com/token',
            'jwks_uri': 'https://www.googleapis.com/oauth2/v3/certs',
            'id_token_signing_alg_values_supported': ['RS256'],
        })

    async def on_url(url):
        authorization = parse_qs(urlsplit(url).query)
        params = {
            'state': authorization['state'][0],
            'client_id': 'private-client.apps.googleusercontent.com',
            'code': 'private-code-<script>alert(1)</script>',
            'email': 'private-person@example.invalid',
            'name': 'private-display-name',
            'profile': 'private-profile',
            'plan_type': plan_hint,
            'is_paid': 'private-untrusted-paid-flag',
        }
        if outcome == 'denied':
            params.pop('code')
            params['error'] = 'access_denied'
            params['error_description'] = 'private-provider-error'
        async with httpx.AsyncClient(trust_env=False) as client:
            if outcome == 'invalid':
                responses.append(await client.get(
                    authorization['redirect_uri'][0], params={**params, 'state': 'private-wrong-state'},
                ))
            # A bad request must not consume the authorization's valid state.
            responses.append(await client.get(authorization['redirect_uri'][0], params=params))
        cancel.set()

    # No credential store is supplied: this response-only scenario must not use one.
    service = OAuthService(object(), transport=httpx.MockTransport(discovery))
    with pytest.raises(AuthError) as raised:
        await service.login(
            'google', 'synthetic-preview',
            OAuthConfig(client_id='private-client.apps.googleusercontent.com'),
            cancel=cancel, on_url=on_url,
        )
    assert raised.value.code == 'cancelled'
    return responses


@pytest.mark.parametrize('outcome,status', [('received', 200), ('invalid', 400), ('denied', 200)])
def test_loopback_callback_serves_private_self_contained_korean_html(outcome, status):
    responses = asyncio.run(callback_responses(outcome))
    response = responses[0]
    assert response.status_code == status
    assert response.headers['content-type'] == 'text/html; charset=utf-8'
    assert int(response.headers['content-length']) == len(response.content)
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['x-content-type-options'] == 'nosniff'
    assert response.headers['referrer-policy'] == 'no-referrer'
    assert response.headers['connection'] == 'close'

    document = CallbackDocument()
    document.feed(response.content.decode('utf-8'))
    assert ('html', {'lang': 'ko'}) in document.elements
    assert any(tag == 'meta' and attrs.get('name') == 'viewport'
               for tag, attrs in document.elements)
    assert sum(tag == 'main' for tag, _ in document.elements) == 1
    assert sum(tag == 'h1' for tag, _ in document.elements) == 1
    assert not {'script', 'link', 'img', 'svg', 'iframe', 'form', 'a', 'button'} & {
        tag for tag, _ in document.elements
    }
    assert not any(
        key.startswith('on') or key in {'src', 'href', 'style', 'action'}
        for _, attrs in document.elements for key in attrs
    )
    assert not any(
        tag == 'meta' and attrs.get('http-equiv', '').lower() == 'refresh'
        for tag, attrs in document.elements
    )
    assert len(document.styles) == 1
    css = document.styles[0]
    assert 'url(' not in css.lower() and '@import' not in css.lower()
    digest = base64.b64encode(hashlib.sha256(css.encode('utf-8')).digest()).decode('ascii')
    assert response.headers['content-security-policy'] == (
        "default-src 'none'; style-src 'sha256-" + digest
        + "'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )
    text = ' '.join(document.text)
    assert 'Recheck' in text
    assert ('로그인 결과를 받았습니다' if status == 200 else '로그인 요청을 확인할 수 없습니다') in text
    assert 'Recheck 앱으로 돌아가 연결 상태를 확인해 주세요' in text
    assert all(value not in response.text for value in (
        'private-', 'access_denied', 'accounts.google.com', 'oauth2.googleapis.com',
        '127.0.0.1', '연결이 완료', 'AI를 사용할 준비',
    ))
    if outcome == 'invalid':
        assert responses[1].status_code == 200


def test_callback_eligibility_guidance_does_not_trust_plan_query_hints():
    free = asyncio.run(callback_responses('received', plan_hint='free'))[0]
    paid = asyncio.run(callback_responses('received', plan_hint='enterprise'))[0]
    assert free.status_code == paid.status_code == 200
    assert free.content == paid.content
