from ai_providers.contracts import ProviderProfile
from ai_providers.profiles import ProfileManager, resolve_profile
from recheck.models import Settings, atomic_json, load_settings


def test_new_installation_starts_with_unconnected_gemini_without_a_shared_key(tmp_path):
    settings = load_settings(root=tmp_path)
    profile = resolve_profile(settings)
    assert profile.provider == 'gemini'
    assert profile.model == 'gemini-3.5-flash-lite'
    assert not profile.credential_ref and not profile.enabled and not profile.external_consent
    assert not (tmp_path / 'settings.json').exists()


def test_existing_gpt_selection_and_personal_budgets_survive_new_default(tmp_path):
    profile = ProviderProfile(profile_id='personal-gpt', provider='openai',
                              auth_mode='chatgpt', model='user-chosen', enabled=True)
    settings = Settings(ai_profiles=[profile], active_ai_profile=profile.profile_id,
                        num_ctx=4096, llm_timeout=287)
    atomic_json(tmp_path / 'settings.json', settings.model_dump())
    assert load_settings(root=tmp_path) == settings


def test_existing_legacy_settings_are_not_replaced_with_gemini(tmp_path):
    settings = Settings(model='user-local-model')
    atomic_json(tmp_path / 'settings.json', settings.model_dump())
    assert load_settings(root=tmp_path) == settings


def test_fresh_install_can_save_gemini_metadata_with_settings_backup(tmp_path):
    manager = ProfileManager(root=tmp_path)
    profile = resolve_profile(manager.settings)
    saved = manager.save_profile({**profile.model_dump(), 'enabled': True})
    assert saved.enabled and not saved.credential_ref
    assert load_settings(root=tmp_path) == manager.settings
    assert manager.settings.active_ai_profile == profile.profile_id
    history = manager.history.list_history()
    assert len(history) == 1 and history[0]['before_exists'] is False
