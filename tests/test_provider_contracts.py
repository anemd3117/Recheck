"""공통 프로필의 인증/전송 경계와 도구 일괄 검증 계약."""

import importlib
import json

import pytest


def api():
    return importlib.import_module('ai_providers.contracts')


@pytest.mark.parametrize(
    'endpoint',
    [
        'http://example.com/v1',
        'https://user:private-value@example.com/v1',
        'https://example.com/v1?api_key=private-value',
        'file:///tmp/service',
    ],
)
def test_remote_profiles_reject_unsafe_credential_destinations(endpoint):
    with pytest.raises(ValueError):
        api().ProviderProfile(
            provider='compatible', auth_mode='api_key', endpoint=endpoint, model='chosen'
        )


def test_remote_request_requires_activation_and_selected_evidence_consent():
    c = api()
    profile = c.ProviderProfile(provider='openai', auth_mode='api_key', model='chosen')
    with pytest.raises(c.ProviderError):
        profile.authorize(('runtime',), purpose='analysis')
    enabled = profile.model_copy(
        update={'enabled': True, 'external_consent': True, 'allowed_evidence': ('runtime',)}
    )
    enabled.authorize(('runtime',), purpose='analysis')
    with pytest.raises(c.ProviderError):
        enabled.authorize(('code',), purpose='analysis')


def test_loopback_compatible_server_still_requires_selected_data_consent():
    c = api()
    profile = c.ProviderProfile(
        provider='compatible',
        auth_mode='none',
        endpoint='http://127.0.0.1:8080/v1',
        model='chosen',
        enabled=True,
    )
    with pytest.raises(c.ProviderError):
        profile.authorize(('runtime',), purpose='analysis')


def test_profile_snapshot_is_frozen_and_authentication_change_invalidates_fingerprint():
    c = api()
    profile = c.ProviderProfile(
        provider='compatible',
        auth_mode='api_key',
        endpoint='https://example.com/v1',
        model='chosen',
    )
    fingerprint = profile.fingerprint()
    with pytest.raises(ValueError):
        profile.model = 'changed'
    assert profile.model_copy(update={'credential_version': 2}).fingerprint() != fingerprint
    assert profile.model_copy(update={'model': 'changed'}).fingerprint() != fingerprint
    assert (
        profile.model_copy(update={'endpoint': 'https://example.com/other'}).fingerprint()
        != fingerprint
    )


def test_tool_batch_rejects_later_invalid_arguments_without_returning_a_partial_batch():
    c = api()
    tool = c.ToolDefinition(
        name='echo',
        description='한정된 값',
        parameters={
            'type': 'object',
            'properties': {'value': {'type': 'integer', 'minimum': 0}},
            'required': ['value'],
            'additionalProperties': False,
        },
    )
    calls = [
        c.ToolCall(id='first', name='echo', arguments={'value': 1}),
        c.ToolCall(id='second', name='echo', arguments={'value': -1}),
    ]
    with pytest.raises(c.ProviderError):
        c.validate_tool_calls(calls, [tool])


def test_tool_batch_rejects_duplicate_ids_and_remote_schema_references():
    c = api()
    tool = c.ToolDefinition(name='echo', description='값', parameters={'type': 'object'})
    with pytest.raises(c.ProviderError):
        c.validate_tool_calls([c.ToolCall(id='same', name='echo', arguments={})] * 2, [tool])
    with pytest.raises(ValueError):
        c.ToolDefinition(name='remote', parameters={'$ref': 'https://example.com/schema'})


def test_redaction_removes_nested_credentials_and_reflected_secret_values():
    c = api()
    result = c.redact(
        {
            'authorization': 'Bearer private-key',
            'raw': 'reflected private-key',
            'nested': {'refresh_token': 'other-private'},
        },
        secrets=('private-key',),
    )
    serialized = json.dumps(result)
    assert 'private-key' not in serialized and 'other-private' not in serialized


def test_credential_binding_prevents_a_key_being_reused_at_a_different_origin():
    c = api()
    material = c.CredentialMaterial(
        value='private-key', origin='https://service.example', version=1
    )
    with pytest.raises(c.ProviderError):
        material.require_origin('https://different.example')
    assert 'private-key' not in repr(material)


def test_profile_cannot_mislabel_a_plain_api_key_as_a_saved_credential_reference():
    with pytest.raises(ValueError):
        api().ProviderProfile(
            provider='openai',
            auth_mode='api_key',
            model='chosen',
            credential_ref='sk-plain-private-value',
        )
