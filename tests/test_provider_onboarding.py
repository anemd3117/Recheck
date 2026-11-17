"""Key onboarding must preserve the current AI until a bound response proof exists."""

import importlib
import threading

import pytest

from ai_providers import profiles
from ai_providers.contracts import ModelDescriptor, ProviderError, ProviderProfile
from recheck.models import Settings, atomic_json, load_settings


def module():
    return importlib.import_module('recheck.provider_onboarding')


@pytest.fixture
def account(tmp_path, monkeypatch):
    state = {'network': [], 'proof': {}, 'after_models': None, 'after_probe': None}

    class Remote:
        def __init__(self, profile):
            self.profile = profile

        async def list_models(self, **kwargs):
            state['network'].append('models')
            if state['after_models']:
                state['after_models']()
            return [ModelDescriptor(id='visible-model'),
                    ModelDescriptor(id='hidden-model', metadata={'visibility': 'hidden'})]

        async def close(self):
            pass

    async def probe(provider, **kwargs):
        state['network'].append('response')
        if state['after_probe']:
            state['after_probe']()
        return {'basic': True, 'json_schema': True, 'actual_inference_verified': True,
                'tools': False, 'model': provider.profile.model,
                'provider': provider.profile.provider,
                'profile_fingerprint': provider.profile.fingerprint(), **state['proof']}

    monkeypatch.setattr(profiles, 'probe_provider_capabilities', probe)
    state['factory'] = Remote
    initial = Settings(model='existing-local')
    atomic_json(tmp_path / 'settings.json', initial.model_dump())
    manager = profiles.ProfileManager(initial, root=tmp_path, provider_factory=Remote)
    return manager, state


def prepared(account, preset='deepseek'):
    manager, _ = account
    setup = module().APIKeySetup(manager)
    profile = setup.prepare_profile(preset, consent=True)
    return setup, manager.save_secret(profile.profile_id, 'fixture-personal-key')


@pytest.mark.parametrize('preset,provider,endpoint', [
    ('openai', 'openai', 'https://api.openai.com/v1'),
    ('grok', 'compatible', 'https://api.x.ai/v1'),
    ('deepseek', 'compatible', 'https://api.deepseek.com/v1'),
    ('claude', 'anthropic', 'https://api.anthropic.com/v1'),
])
def test_preset_draft_routes_to_official_api_without_persisting_or_consent(account, preset, provider, endpoint):
    manager, state = account
    profile = module().preset_profile(preset)
    assert profile.provider == provider and profile.endpoint == endpoint
    assert profile.auth_mode == 'api_key' and not profile.credential_ref
    assert not profile.enabled and not profile.external_consent
    assert manager.selected_profile_id() == 'legacy-local'
    assert not state['network'] and not manager.settings.ai_profiles


def test_profile_preparation_requires_explicit_consent_and_preserves_unrelated_profile(account):
    manager, state = account
    setup = module().APIKeySetup(manager)
    existing = manager.save_profile(ProviderProfile(profile_id='other', provider='compatible',
                                                  auth_mode='api_key', endpoint='https://example.com/v1'))
    with pytest.raises(ProviderError):
        setup.prepare_profile('deepseek')
    with pytest.raises(ProviderError):
        setup.prepare_profile('deepseek', 'other', consent=True)
    assert manager.get_profile('other') == existing and not state['network']


@pytest.mark.asyncio
async def test_no_key_cannot_request_models_or_replace_selection(account):
    manager, state = account
    setup = module().APIKeySetup(manager)
    profile = setup.prepare_profile('openai', consent=True)
    with pytest.raises(ProviderError) as raised:
        await setup.connect(profile.profile_id)
    assert raised.value.code == 'credential_required'
    assert not state['network'] and manager.selected_profile_id() == 'legacy-local'


@pytest.mark.asyncio
async def test_actual_response_proof_is_persisted_before_selecting_and_reused_after_restart(account):
    manager, state = account
    setup, profile = prepared(account)
    result = await setup.connect(profile.profile_id)
    assert result['ready'] and result['capabilities']['proof_persisted']
    assert state['network'] == ['models', 'response']
    assert manager.selected_profile_id() == profile.profile_id
    assert manager.get_profile(profile.profile_id).model == 'visible-model'
    restarted = profiles.ProfileManager(root=manager.root, provider_factory=state['factory'])
    assert restarted.cached_capabilities(profile.profile_id)['actual_inference_verified']
    result2 = await module().APIKeySetup(restarted).connect(profile.profile_id)
    assert result2['capabilities_reused']
    assert state['network'].count('response') == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', [{'basic': False}, {'json_schema': False},
                                     {'actual_inference_verified': False}, {'model': 'wrong-model'}])
async def test_unverified_response_does_not_replace_the_existing_default(account, mutation):
    manager, state = account
    setup, profile = prepared(account)
    state['proof'] = mutation
    with pytest.raises(ProviderError):
        await setup.connect(profile.profile_id)
    assert manager.selected_profile_id() == 'legacy-local'
    assert load_settings(root=manager.root).active_ai_profile == 'legacy-local'


@pytest.mark.asyncio
async def test_cancellation_after_models_preserves_selection(account):
    manager, state = account
    setup, profile = prepared(account)
    cancel = threading.Event()
    state['after_models'] = cancel.set
    with pytest.raises(ProviderError) as raised:
        await setup.connect(profile.profile_id, cancel=cancel)
    assert raised.value.code == 'cancelled'
    assert state['network'] == ['models'] and manager.selected_profile_id() == 'legacy-local'


@pytest.mark.asyncio
async def test_another_selection_during_response_is_not_overwritten(account):
    manager, state = account
    setup, profile = prepared(account)
    other = manager.save_profile(ProviderProfile(profile_id='other-local', model='other', enabled=True))
    state['after_probe'] = lambda: manager.select_profile(other.profile_id)
    with pytest.raises(ProviderError):
        await setup.connect(profile.profile_id)
    assert manager.selected_profile_id() == other.profile_id
