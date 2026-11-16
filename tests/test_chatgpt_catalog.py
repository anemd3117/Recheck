"""The account catalog uses models/slug, unlike API-key data/id catalogs."""

import httpx
import pytest
from ai_providers.contracts import CredentialMaterial, ProviderError, ProviderProfile
from ai_providers.http_provider import HTTPProvider


def make_provider(catalog, auth='chatgpt'):
    profile = ProviderProfile(provider='openai', auth_mode=auth, enabled=True, credential_version=1)

    def credentials(profile, cancel=None):
        return CredentialMaterial(value='test-token', origin=profile.origin, version=1)

    return HTTPProvider(
        profile,
        credential_getter=credentials,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=catalog)),
    )


@pytest.mark.asyncio
async def test_chatgpt_catalog_uses_visible_slugs_and_keeps_server_order():
    provider = make_provider(
        {
            'models': [
                {'slug': 'gpt-6.1-sol', 'display_name': 'GPT-6.1 Sol', 'visibility': 'list'},
                {'slug': 'internal-review', 'display_name': 'Internal', 'visibility': 'hide'},
                {'slug': 'gpt-6-astra', 'display_name': 'GPT-6 Astra', 'visibility': 'list'},
            ]
        }
    )
    try:
        models = await provider.list_models()
    finally:
        await provider.close()
    assert [m.id for m in models] == ['gpt-6.1-sol', 'gpt-6-astra']
    assert [m.display_name for m in models] == ['GPT-6.1 Sol', 'GPT-6 Astra']


@pytest.mark.asyncio
async def test_api_key_catalog_keeps_its_separate_data_id_contract():
    provider = make_provider({'data': [{'id': 'my-api-model', 'object': 'model'}]}, auth='api_key')
    try:
        models = await provider.list_models()
    finally:
        await provider.close()
    assert [m.id for m in models] == ['my-api-model']


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'rows',
    [
        'not-a-list',
        [{'slug': '', 'visibility': 'list'}],
        [{'slug': 123, 'visibility': 'list'}],
        [{'slug': 'same', 'visibility': 'list'}, {'slug': 'same', 'visibility': 'list'}],
    ],
)
async def test_invalid_account_catalog_cannot_be_used_for_automatic_selection(rows):
    provider = make_provider({'models': rows})
    try:
        with pytest.raises(ProviderError) as error:
            await provider.list_models()
    finally:
        await provider.close()
    assert error.value.code == 'malformed_response'
