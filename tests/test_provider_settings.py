"""기존 설정·모델을 보존하며 프로필 설정을 백업·원자 이전한다."""

import base64
import json

import pytest

from ai_providers.contracts import ProviderProfile
from recheck.models import Settings, load_settings
from recheck.recommendations import SettingsHistory


def test_old_settings_migrate_with_exact_byte_backup_and_model_selection_preserved(tmp_path):
    raw = b'{"model":"user-selected","num_ctx":4096,"num_predict":1024,"capabilities":{"tools":false}}'
    (tmp_path / 'settings.json').write_bytes(raw)
    settings = load_settings(root=tmp_path)
    assert (settings.model, settings.num_ctx, settings.num_predict) == ('user-selected', 4096, 1024)
    assert settings.active_ai_profile == 'legacy-local'
    assert json.loads((tmp_path / 'settings.json').read_text())['settings_version'] == 2
    history = SettingsHistory(tmp_path).list_history()
    assert len(history) == 1 and history[0]['action'] == 'ai_settings_migration'
    assert base64.b64decode(history[0]['before_bytes_base64']) == raw
    assert load_settings(root=tmp_path) == settings
    assert len(SettingsHistory(tmp_path).list_history()) == 1


def test_profile_selection_and_legacy_options_round_trip_without_plain_credentials(tmp_path):
    profile = ProviderProfile(
        provider='openai',
        auth_mode='api_key',
        model='chosen',
        credential_ref='cred_' + 'a' * 32,
        credential_version=1,
    )
    current = Settings(model='local-user-model')
    (tmp_path / 'settings.json').write_text(current.model_dump_json(), encoding='utf-8')
    updated = Settings(
        **{
            **current.model_dump(),
            'ai_profiles': [profile.model_dump()],
            'active_ai_profile': profile.profile_id,
        }
    )
    SettingsHistory(tmp_path).replace(current, updated, action='ai_profile_update')
    loaded = load_settings(root=tmp_path)
    assert loaded.model == 'local-user-model'
    assert loaded.ai_profiles[0].credential_ref == 'cred_' + 'a' * 32
    assert loaded.active_ai_profile == profile.profile_id


def test_profile_update_rejects_another_writers_change_and_preserves_it(tmp_path):
    current = Settings()
    history = SettingsHistory(tmp_path)
    (tmp_path / 'settings.json').write_text(json.dumps({'model': 'another-writer'}))
    with pytest.raises(ValueError):
        history.replace(current, current.model_copy(update={'model': 'stale-ui'}))
    assert json.loads((tmp_path / 'settings.json').read_text())['model'] == 'another-writer'


def test_settings_reject_duplicate_profile_ids_and_dangling_role_references():
    profile = ProviderProfile(profile_id='same')
    with pytest.raises(ValueError):
        Settings(ai_profiles=[profile, profile])
    with pytest.raises(ValueError):
        Settings(ai_role_profiles={'generation': 'missing-profile'})


def test_invalid_settings_error_does_not_echo_a_secret_value(tmp_path):
    (tmp_path / 'settings.json').write_text('{"api_key":"private-settings-secret"}')
    with pytest.raises(ValueError) as error:
        load_settings(root=tmp_path)
    assert 'private-settings-secret' not in str(error.value)
