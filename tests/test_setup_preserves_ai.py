"""Finding local tools must not disconnect a separately verified remote AI."""

import pytest
from ai_providers.contracts import ProviderProfile
from recheck.models import Settings, atomic_json
from recheck.recommendations import SettingsHistory


@pytest.mark.parametrize(
    'patch,keep_legacy',
    [
        ({'ghidra_path': 'C:/Fixture/Ghidra'}, True),
        ({'model': 'a-different-local-model'}, False),
        ({'ollama_url': 'http://127.0.0.1:11435'}, False),
    ],
)
def test_local_discovery_keeps_bound_remote_proof_and_invalidates_only_changed_local_ai(
    tmp_path, patch, keep_legacy
):
    remote = ProviderProfile(
        profile_id='remote-gpt', provider='openai', auth_mode='chatgpt', model='gpt-6.1-sol'
    )
    refs = {'remote-gpt': 'cred_' + 'a' * 32, 'legacy-local': 'cred_' + 'b' * 32}
    current = Settings(ai_profiles=[remote], capabilities={'ai_proofs': refs})
    atomic_json(tmp_path / 'settings.json', current.model_dump())
    result = SettingsHistory(tmp_path).apply_setup(current, patch)
    expected = {'remote-gpt': refs['remote-gpt']}
    if keep_legacy:
        expected['legacy-local'] = refs['legacy-local']
    assert result.capabilities == {'ai_proofs': expected}


def test_plain_unverified_flags_are_not_preserved_by_discovery(tmp_path):
    current = Settings(capabilities={'basic': True, 'tools': True})
    atomic_json(tmp_path / 'settings.json', current.model_dump())
    result = SettingsHistory(tmp_path).apply_setup(current, {'ghidra_path': 'C:/Fixture/Ghidra'})
    assert result.capabilities == {}


@pytest.mark.parametrize('key,value,keep_legacy', [('cpu', 0.5, True), ('num_ctx', 4096, False)])
def test_local_performance_recommendations_keep_remote_ai_connected(
    tmp_path, key, value, keep_legacy
):
    remote = ProviderProfile(
        profile_id='remote-gpt', provider='openai', auth_mode='chatgpt', model='gpt-6.1-sol'
    )
    refs = {'remote-gpt': 'cred_' + 'a' * 32, 'legacy-local': 'cred_' + 'b' * 32}
    current = Settings(ai_profiles=[remote], capabilities={'ai_proofs': refs})
    atomic_json(tmp_path / 'settings.json', current.model_dump())
    proposed = {'items': [{'key': key, 'current': getattr(current, key), 'recommended': value}]}
    result = SettingsHistory(tmp_path).apply(current, proposed, [key])
    expected = {'remote-gpt': refs['remote-gpt']}
    if keep_legacy:
        expected['legacy-local'] = refs['legacy-local']
    assert result.capabilities == {'ai_proofs': expected}
