"""Gemini's initial model_output content must survive subsequent SSE deltas."""

import json

import httpx
import pytest

from ai_providers.contracts import CredentialMaterial, Message, ProviderProfile, ProviderRequest
from ai_providers.http_provider import HTTPProvider


def stream_events(initial, deltas):
    # Independent REST fixture from Google's May-2026 migration guide:
    # step.start may already contain model output before step.delta arrives.
    events = [
        {
            'event_type': 'interaction.created',
            'interaction': {'id': 'int-initial', 'model': 'gemini-4-hydrogen'},
        },
        {
            'event_type': 'step.start',
            'index': 0,
            'step': {'type': 'model_output', 'content': initial},
        },
    ]
    events.extend(
        {'event_type': 'step.delta', 'index': 0, 'delta': {'type': 'text', 'text': text}}
        for text in deltas
    )
    events.extend([
        {'event_type': 'step.stop', 'index': 0},
        {
            'event_type': 'interaction.completed',
            'interaction': {
                'id': 'int-initial',
                'model': 'gemini-4-hydrogen',
                'status': 'completed',
            },
        },
    ])
    return ''.join('data: ' + json.dumps(item, ensure_ascii=False) + '\n\n' for item in events)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'initial,deltas,expected',
    [
        ([{'type': 'text', 'text': 'Once upon'}], [' a time', '...'], 'Once upon a time...'),
        ([{'type': 'text', 'text': '{"ok":'}], ['true', '}'], '{"ok":true}'),
        ([{'type': 'text', 'text': '이미 완료된 답변'}], [], '이미 완료된 답변'),
        (
            [{'type': 'text', 'text': '첫 '}, {'type': 'text', 'text': '부분'}],
            ['과 나머지'],
            '첫 부분과 나머지',
        ),
    ],
)
async def test_gemini_stream_preserves_initial_text_before_following_deltas(initial, deltas, expected):
    selected = ProviderProfile(
        provider='gemini', auth_mode='api_key', model='gemini-4-hydrogen',
        credential_version=1, enabled=True, external_consent=True,
        allowed_evidence=('runtime',),
    )

    def credential(profile, cancel=None):
        return CredentialMaterial(value='fixture-api-key', origin=profile.origin, version=1)

    adapter = HTTPProvider(
        selected,
        credential_getter=credential,
        transport=httpx.MockTransport(lambda request: httpx.Response(
            200,
            text=stream_events(initial, deltas),
            headers={'content-type': 'text/event-stream'},
        )),
    )
    try:
        response = await adapter.chat(ProviderRequest(
            messages=(Message(role='user', content='고정된 관측을 설명하세요.'),),
            evidence_kinds=('runtime',), stream=True,
        ))
    finally:
        await adapter.close()
    assert response.finish_reason == 'stop'
    assert response.content == expected


@pytest.mark.asyncio
async def test_gemini_stream_preserves_initial_safety_refusal_instead_of_reporting_empty():
    selected = ProviderProfile(
        provider='gemini', auth_mode='api_key', model='gemini-4-hydrogen',
        credential_version=1, enabled=True, external_consent=True,
        allowed_evidence=('runtime',),
    )

    def credential(profile, cancel=None):
        return CredentialMaterial(value='fixture-api-key', origin=profile.origin, version=1)

    adapter = HTTPProvider(
        selected,
        credential_getter=credential,
        transport=httpx.MockTransport(lambda request: httpx.Response(
            200,
            text=stream_events([{'type': 'refusal'}], []),
            headers={'content-type': 'text/event-stream'},
        )),
    )
    try:
        response = await adapter.chat(ProviderRequest(
            messages=(Message(role='user', content='고정된 관측을 설명하세요.'),),
            evidence_kinds=('runtime',), stream=True,
        ))
    finally:
        await adapter.close()
    assert response.finish_reason == 'safety'
    assert response.content == ''
