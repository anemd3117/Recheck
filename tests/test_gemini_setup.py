"""Free Gemini setup selects a role only after a bound, persisted JSON proof."""

import importlib
import importlib.util
import threading

import pytest

from ai_providers import profiles
from ai_providers.contracts import ModelDescriptor, ProviderError, ProviderProfile
from ai_providers.credentials import AuthError
from recheck.models import Settings, atomic_json, load_settings


def gemini_module():
    assert importlib.util.find_spec('recheck.gemini_setup') is not None, 'Gemini setup is missing'
    return importlib.import_module('recheck.gemini_setup')


@pytest.fixture
def account(tmp_path, monkeypatch):
    state = {
        'models': [ModelDescriptor(id='gemini-4-carbon', display_name='Free offer'),
                   ModelDescriptor(id='models/gemini-4-hydrogen'),
                   ModelDescriptor(id='gemini-3.5-flash-lite')],
        'network': [], 'probe': {}, 'after_models': None, 'after_probe': None,
    }

    class Remote:
        def __init__(self, profile):
            self.profile = profile

        async def list_models(self, **kwargs):
            state['network'].append('models')
            if state['after_models']:
                state['after_models']()
            return state['models']

        async def close(self):
            pass

    async def capability_probe(provider, **kwargs):
        state['network'].append('capabilities')
        if state['after_probe']:
            state['after_probe']()
        return {
            'basic': True, 'json_schema': True, 'actual_inference_verified': True,
            'tools': False, 'tool_roundtrip': False, 'model': provider.profile.model,
            'provider': 'gemini', 'profile_fingerprint': provider.profile.fingerprint(),
            **state['probe'],
        }

    monkeypatch.setattr(profiles, 'probe_provider_capabilities', capability_probe)
    initial = Settings(model='existing-local')
    atomic_json(tmp_path / 'settings.json', initial.model_dump())
    manager = profiles.ProfileManager(settings=initial, root=tmp_path, provider_factory=Remote)
    return manager, state


def prepared(account, *, secret=True):
    manager, _ = account
    setup = gemini_module().GeminiSetup(manager)
    profile = setup.prepare_profile(consent=True)
    if secret:
        profile = manager.save_secret(profile.profile_id, 'fixture-only-gemini-key')
    return setup, profile


def test_new_draft_builder_has_no_consent_key_or_selection_side_effect(account):
    manager, _ = account
    profile = gemini_module().new_free_profile(profile_id='fresh-gemini')
    assert profile.provider == 'gemini' and profile.auth_mode == 'api_key'
    assert profile.model == 'gemini-3.5-flash-lite'
    assert not profile.enabled and not profile.external_consent and not profile.credential_ref
    assert profile.allowed_evidence == () and profile.structured_mode == 'json_prompt'
    assert not manager.settings.ai_profiles and manager.selected_profile_id() == 'legacy-local'
    assert load_settings(root=manager.root) == manager.settings


def test_preparing_free_setup_requires_an_explicit_consent_action(account):
    manager, state = account
    with pytest.raises(ProviderError) as raised:
        gemini_module().GeminiSetup(manager).prepare_profile()
    assert raised.value.code == 'external_consent'
    assert not manager.settings.ai_profiles and not state['network']


def test_explicit_new_setup_has_analysis_preset_but_keeps_current_ai(account):
    manager, _ = account
    setup, profile = prepared(account, secret=False)
    assert profile.enabled and profile.external_consent
    assert profile.allowed_evidence == ('runtime', 'static', 'code')
    assert profile.options.context_budget == 16384 and profile.options.max_output_tokens == 4096
    assert profile.options.max_requests == 5 and profile.options.timeout_seconds == 300
    assert manager.selected_profile_id() == 'legacy-local'
    assert load_settings(root=manager.root).active_ai_profile == 'legacy-local'


def test_prepare_reuses_only_explicit_gemini_id_and_preserves_manual_budget_and_free_choice(account):
    manager, _ = account
    existing = manager.save_profile(ProviderProfile(
        profile_id='manual', provider='gemini', auth_mode='api_key',
        model='gemini-3.5-flash-lite', options=dict(context_budget=8192, max_output_tokens=1024,
                                               max_requests=2, timeout_seconds=90),
    ))
    setup = gemini_module().GeminiSetup(manager)
    fresh = setup.prepare_profile(consent=True)
    assert fresh.profile_id != existing.profile_id
    reused = setup.prepare_profile(existing.profile_id, consent=True)
    assert reused.profile_id == 'manual' and reused.model == 'gemini-3.5-flash-lite'
    assert reused.options == existing.options
    assert manager.selected_profile_id() == 'legacy-local'


@pytest.mark.parametrize('provider,auth_mode,model', [
    ('openai', 'chatgpt', 'gpt-6.1-sol'),
    ('gemini', 'google_oauth', 'gemini-4-hydrogen'),
    ('gemini', 'api_key', 'gemini-4-carbon'),
])
def test_prepare_never_converts_other_or_paid_profiles_to_free_setup(account, provider, auth_mode, model):
    manager, _ = account
    original = manager.save_profile(ProviderProfile(
        profile_id='other', provider=provider, auth_mode=auth_mode, model=model,
    ))
    with pytest.raises(ProviderError):
        gemini_module().GeminiSetup(manager).prepare_profile('other', consent=True)
    assert manager.get_profile('other') == original
    assert manager.selected_profile_id() == 'legacy-local'


def test_catalog_uses_exact_normalized_ids_never_free_words_in_display_names():
    module = gemini_module()
    models = [ModelDescriptor(id='gemini-4-carbon', display_name='gemini-4-hydrogen free'),
              ModelDescriptor(id='models/gemini-3.5-flash-lite')]
    assert module.choose_free_model(models) == 'gemini-3.5-flash-lite'
    assert module.choose_free_model([
        ModelDescriptor(id='gemini-4-hydrogen'), ModelDescriptor(id='gemini-3.5-flash-lite')
    ], preferred='gemini-3.5-flash-lite') == 'gemini-3.5-flash-lite'
    with pytest.raises(ProviderError) as raised:
        module.choose_free_model(models[:1])
    assert raised.value.code == 'free_model_unavailable'


@pytest.mark.asyncio
async def test_successful_setup_persists_json_proof_and_selects_only_requested_role(account):
    manager, state = account
    setup, profile = prepared(account)
    result = await setup.connect(profile.profile_id, role='review')
    assert result['ready'] and result['role'] == 'review'
    assert result['capabilities']['proof_persisted'] and result['capabilities']['json_schema']
    assert result['free_tier_independently_verified'] is False
    assert state['network'] == ['models', 'capabilities']
    assert manager.selected_profile_id() == 'legacy-local'
    assert manager.selected_profile_id('review') == profile.profile_id
    restarted = profiles.ProfileManager(root=manager.root)
    assert restarted.selected_profile_id('review') == profile.profile_id
    assert restarted.cached_capabilities(profile.profile_id)['json_schema'] is True


@pytest.mark.asyncio
async def test_missing_saved_key_cannot_send_or_select(account):
    manager, state = account
    setup, profile = prepared(account, secret=False)
    with pytest.raises(ProviderError) as raised:
        await setup.connect(profile.profile_id)
    assert raised.value.code == 'credential_required'
    assert not state['network'] and manager.selected_profile_id() == 'legacy-local'


@pytest.mark.asyncio
async def test_paid_only_account_catalog_never_falls_back_to_its_first_model(account):
    manager, state = account
    setup, profile = prepared(account)
    state['models'] = [ModelDescriptor(id='gemini-4-carbon', display_name='free')]
    with pytest.raises(ProviderError) as raised:
        await setup.connect(profile.profile_id)
    assert raised.value.code == 'free_model_unavailable'
    assert state['network'] == ['models'] and manager.selected_profile_id() == 'legacy-local'


@pytest.mark.asyncio
@pytest.mark.parametrize('missing', ['basic', 'json_schema', 'actual_inference_verified'])
async def test_incomplete_capability_results_keep_previous_selection(account, missing):
    manager, state = account
    setup, profile = prepared(account)
    state['probe'][missing] = False
    with pytest.raises(ProviderError) as raised:
        await setup.connect(profile.profile_id)
    assert raised.value.code == 'setup_inference'
    assert manager.selected_profile_id() == 'legacy-local'


@pytest.mark.asyncio
async def test_failed_proof_storage_cannot_activate_even_when_remote_responded(account, monkeypatch):
    manager, _ = account
    setup, profile = prepared(account)
    original = manager.store.put

    def fail_proof(pid, origin, kind, value):
        if kind == 'capability_proof':
            raise AuthError('credential_protection', 'fixture storage failure')
        return original(pid, origin, kind, value)

    monkeypatch.setattr(manager.store, 'put', fail_proof)
    with pytest.raises(ProviderError) as raised:
        await setup.connect(profile.profile_id)
    assert raised.value.code == 'setup_inference'
    assert manager.selected_profile_id() == 'legacy-local'


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['before', 'models', 'activate'])
async def test_cancel_at_each_boundary_keeps_previous_selection(account, stage):
    manager, state = account
    setup, profile = prepared(account)
    cancel = threading.Event()
    if stage == 'before':
        cancel.set()
    elif stage == 'models':
        state['after_models'] = cancel.set

    def event(value):
        if stage == 'activate' and value.get('step') == 'activate':
            cancel.set()

    with pytest.raises(ProviderError) as raised:
        await setup.connect(profile.profile_id, cancel=cancel, on_event=event)
    assert raised.value.code == 'cancelled'
    assert manager.selected_profile_id() == 'legacy-local'
    if stage == 'before':
        assert not state['network']


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['models', 'probe', 'activate'])
async def test_changed_profile_at_any_boundary_is_stale_and_cannot_select(account, stage):
    manager, state = account
    setup, profile = prepared(account)

    def mutate():
        current = manager.get_profile(profile.profile_id)
        manager.save_profile({**current.model_dump(), 'name': 'changed elsewhere'})

    if stage == 'models':
        state['after_models'] = mutate
    elif stage == 'probe':
        state['after_probe'] = mutate

    def event(value):
        if stage == 'activate' and value.get('step') == 'activate':
            mutate()

    with pytest.raises(ProviderError) as raised:
        await setup.connect(profile.profile_id, on_event=event)
    assert raised.value.code == 'profile_stale'
    assert manager.selected_profile_id() == 'legacy-local'


@pytest.mark.asyncio
async def test_reconnect_reuses_only_current_bound_persisted_json_proof(account):
    manager, state = account
    setup, profile = prepared(account)
    first = await setup.connect(profile.profile_id)
    assert not first['capabilities_reused']
    state['network'].clear()
    again = await setup.connect(profile.profile_id)
    assert again['capabilities_reused'] and state['network'] == ['models']


@pytest.mark.asyncio
async def test_a_new_user_selection_during_activation_is_not_overwritten(account):
    manager, _ = account
    setup, profile = prepared(account)
    other = manager.save_profile(ProviderProfile(
        profile_id='other-choice', provider='openai', auth_mode='chatgpt',
        model='gpt-6.1-sol', enabled=True,
    ))

    def event(value):
        if value.get('step') == 'activate':
            manager.select_profile(other.profile_id)

    with pytest.raises(ProviderError) as raised:
        await setup.connect(profile.profile_id, on_event=event)
    assert raised.value.code == 'profile_stale'
    assert manager.selected_profile_id() == 'other-choice'


@pytest.mark.asyncio
async def test_a_bound_basic_proof_without_json_is_rechecked_instead_of_reused(account):
    manager, state = account
    setup, profile = prepared(account)
    state['probe']['json_schema'] = False
    with pytest.raises(ProviderError):
        await setup.connect(profile.profile_id)
    assert manager.cached_capabilities(profile.profile_id)['json_schema'] is False
    state['probe'].clear()
    state['network'].clear()
    result = await setup.connect(profile.profile_id)
    assert result['ready'] and not result['capabilities_reused']
    assert state['network'] == ['models', 'capabilities']
