"""Responses API explicitly returns null incomplete_details on success."""

import httpx
import pytest
from ai_providers.contracts import (
    CredentialMaterial,
    Message,
    ProviderError,
    ProviderProfile,
    ProviderRequest,
)
from ai_providers.http_provider import HTTPProvider


async def response(details, status='completed'):
    body = {
        'id': 'response-test',
        'model': 'gpt-6.1-sol',
        'status': status,
        'incomplete_details': details,
        'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'RECHECK_OK'}]}],
        'usage': {'input_tokens': 8, 'output_tokens': 4},
    }
    provider = HTTPProvider(
        ProviderProfile(
            provider='openai',
            auth_mode='api_key',
            enabled=True,
            model='gpt-6.1-sol',
            credential_version=1,
        ),
        credential_getter=lambda profile, cancel=None: CredentialMaterial(
            value='fixture-key', origin=profile.origin, version=1
        ),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body)),
    )
    try:
        return await provider.chat(
            ProviderRequest(messages=(Message(role='user', content='Test'),), purpose='probe')
        )
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_completed_response_with_null_incomplete_details_is_a_real_success():
    answer = await response(None)
    assert answer.finish_reason == 'stop' and answer.content == 'RECHECK_OK'


@pytest.mark.asyncio
async def test_null_details_do_not_upgrade_an_incomplete_response_to_success():
    answer = await response(None, status='incomplete')
    assert answer.finish_reason == 'incomplete'


@pytest.mark.asyncio
@pytest.mark.parametrize('details', [False, 0, [], 'invalid'])
async def test_non_object_non_null_details_remain_rejected(details):
    with pytest.raises(ProviderError) as error:
        await response(details)
    assert error.value.code == 'malformed_response'
