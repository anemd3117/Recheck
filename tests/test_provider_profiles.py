"""임시 루트와 시험용 비밀값만 사용하는 프로필·DPAPI·모의 통신 계약 검사."""

import asyncio
import copy
import json
from pathlib import Path
import time

import pytest
from ai_providers.contracts import (
    GenerationOptions,
    ModelDescriptor,
    ProviderError,
    ProviderProfile,
)
from ai_providers.credentials import AuthError, CredentialStore
from ai_providers.oauth import OAuthService
from ai_providers import profiles
from recheck.models import Settings, atomic_json
from recheck.recommendations import SettingsHistory


class FakeProvider:
    def __init__(self, profile):
        self.profile = profile
        self.closed = False

    async def probe(self, cancel=None):
        return {'connected': True, 'model': self.profile.model}

    async def list_models(self, cancel=None):
        return [ModelDescriptor(id='listed', display_name='목록')]

    async def close(self):
        self.closed = True


@pytest.fixture
def manager(tmp_path, monkeypatch):
    settings = Settings(model='gemma4:e4b')
    store = CredentialStore(tmp_path / 'credentials')
    manager = profiles.ProfileManager(
        settings, root=tmp_path, store=store, provider_factory=FakeProvider
    )

    # 설정 이력 구현은 root가 병렬 소유하므로 이 테스트는 CAS 경계만 대체한다.
    def replace(current, updated, action='ai_profile_update'):
        assert current == manager.settings
        (tmp_path / 'settings.json').write_text(
            json.dumps(updated.model_dump(), ensure_ascii=False), encoding='utf-8'
        )
        return updated

    if not hasattr(manager, 'history'):
        manager.history = type('History', (), {})()
    monkeypatch.setattr(manager.history, 'replace', replace, raising=False)
    return manager


def remote(profile_id='remote', **updates):
    data = dict(
        profile_id=profile_id,
        name='원격 연결',
        provider='compatible',
        auth_mode='api_key',
        endpoint='https://first.example/v1',
        model='chosen',
        enabled=False,
        experimental=True,
    )
    data.update(updates)
    return ProviderProfile.model_validate(data)


def capability_result(profile, **values):
    result = dict(
        basic=True,
        json_schema=True,
        tools=True,
        tool_roundtrip=True,
        actual_inference_verified=True,
        model=profile.model,
        provider=profile.provider,
        profile_fingerprint=profile.fingerprint(),
    )
    result.update(values)
    return result


def test_legacy_profile_is_derived_without_duplicating_custom_profiles(manager):
    profile = profiles.legacy_profile(manager.settings)
    assert profile.profile_id == 'legacy-local'
    assert profile.model == 'gemma4:e4b'
    assert profile.options.max_requests == 3
    assert profile.options.context_budget == manager.settings.num_ctx
    assert profile.options.max_output_tokens == manager.settings.num_predict
    assert manager.settings.ai_profiles == []
    assert profiles.resolve_profile(manager.settings).profile_id == 'legacy-local'


def test_bad_legacy_endpoint_does_not_prevent_opening_api_settings_but_cannot_execute(manager):
    manager.settings.ollama_url = 'http://remote.example:11434'
    profile = manager.get_profile('legacy-local')
    assert not profile.enabled
    assert '확인' in profile.name
    manager.save_profile(remote())
    assert len(manager.list_profiles()) == 2
    with pytest.raises((ValueError, ProviderError)):
        profiles.resolve_profile(manager.settings)


def test_profile_crud_role_selection_and_delete_restore_legacy_without_model_replacement(manager):
    saved = manager.save_profile(
        remote(enabled=True, external_consent=True, allowed_evidence=('runtime',))
    )
    assert saved.model == 'chosen'
    assert manager.get_profile('remote') == saved
    manager.select_profile('remote')
    manager.select_profile('remote', role='generation')
    assert profiles.resolve_profile(manager.settings).profile_id == 'remote'
    assert profiles.resolve_profile(manager.settings, 'generation').profile_id == 'remote'
    manager.delete_profile('remote')
    assert manager.selected_profile_id() == 'legacy-local'
    assert manager.selected_profile_id('generation') == 'legacy-local'
    assert manager.settings.model == 'gemma4:e4b'


def test_legacy_profile_cannot_be_deleted_or_replaced_by_remote_provider(manager):
    with pytest.raises((ValueError, ProviderError)):
        manager.delete_profile('legacy-local')
    with pytest.raises((ValueError, ProviderError)):
        manager.save_profile(remote('legacy-local'))


def test_new_profile_cannot_import_an_arbitrary_credential_reference(manager):
    with pytest.raises((ValueError, ProviderError)):
        manager.save_profile(remote(credential_ref='cred_' + 'a' * 32))


def test_api_secret_uses_real_dpapi_and_never_enters_settings_or_plain_files(manager):
    manager.save_profile(remote())
    secret = 'test-only-secret-7a791'
    saved = manager.save_secret('remote', secret)
    assert saved.credential_ref
    assert (
        manager.store.get(saved.credential_ref, 'remote', saved.origin).value.get_secret_value()
        == secret
    )
    assert secret not in json.dumps(manager.settings.model_dump())
    assert all(
        secret.encode() not in path.read_bytes()
        for path in manager.root.rglob('*')
        if path.is_file()
    )


def test_secret_replacement_settings_failure_deletes_only_new_reference(manager, monkeypatch):
    manager.save_profile(remote())
    before = manager.save_secret('remote', 'old-test-key')
    before_files = {path.name for path in manager.store.root.glob('cred_*.json')}

    def rejected(*args, **kwargs):
        raise ValueError('CAS conflict')

    monkeypatch.setattr(manager.history, 'replace', rejected)
    with pytest.raises((ValueError, ProviderError)):
        manager.save_secret('remote', 'new-test-key')
    assert manager.get_profile('remote').credential_ref == before.credential_ref
    assert (
        manager.store.get(before.credential_ref, 'remote', before.origin).value.get_secret_value()
        == 'old-test-key'
    )
    assert {path.name for path in manager.store.root.glob('cred_*.json')} == before_files


def test_endpoint_change_detaches_key_and_resets_scope_consent(manager):
    profile = manager.save_profile(
        remote(enabled=True, external_consent=True, allowed_evidence=('runtime',))
    )
    profile = manager.save_secret(profile.profile_id, 'test-key')
    updated = manager.save_profile(
        {**profile.model_dump(), 'endpoint': 'https://second.example/v1'}
    )
    assert not updated.credential_ref
    assert not updated.external_consent
    assert not updated.allowed_evidence
    with pytest.raises(AuthError):
        manager.store.metadata(profile.credential_ref, profile.profile_id)


def test_connection_and_models_are_explicit_and_provider_is_closed(manager):
    created = []
    manager.provider_factory = lambda profile: created.append(FakeProvider(profile)) or created[-1]
    manager.save_profile(remote())
    assert not created
    assert asyncio.run(manager.probe_connection('remote', None, None))['connected']
    assert created[-1].closed
    models = asyncio.run(manager.list_models('remote', None, None))
    assert models[0].id == 'listed'
    assert created[-1].closed


def test_inflight_response_is_rejected_after_profile_changed(manager):
    manager.save_profile(remote())

    class Slow(FakeProvider):
        async def list_models(self, cancel=None):
            manager.save_profile(
                {**manager.get_profile('remote').model_dump(), 'model': 'new-choice'}
            )
            return [ModelDescriptor(id='stale')]

    manager.provider_factory = Slow
    with pytest.raises(ProviderError, match='변경'):
        asyncio.run(manager.list_models('remote', None, None))


def test_capability_cache_invalidates_when_secure_store_version_changes(manager, monkeypatch):
    manager.save_profile(remote(enabled=True, external_consent=True, allowed_evidence=('runtime',)))
    saved = manager.save_secret('remote', 'first-key')

    async def probe(provider, cancel=None, on_event=None):
        return capability_result(provider.profile, tools=False, tool_roundtrip=False)

    monkeypatch.setattr(profiles, 'probe_provider_capabilities', probe, raising=False)
    result = asyncio.run(manager.probe_capabilities('remote', None, None))
    assert result['basic'] is True
    assert manager.cached_capabilities('remote')['basic'] is True
    manager.store.put(
        'remote',
        saved.origin,
        'api_key',
        'rotated-in-store',
        ref=saved.credential_ref,
        expected_version=1,
    )
    assert manager.cached_capabilities('remote') is None


class MockOAuth(OAuthService):
    """브라우저/원격교환만 대체하고 계정 상태·DPAPI 경계는 실제 서비스 사용."""

    def __init__(self, store):
        super().__init__(store)
        self.last_config = None
        self.after_login = None
        self.revocation = 'not_confirmed'
        self.logout_calls = 0

    async def login(self, provider, profile_id, config, cancel=None, on_url=None):
        self.last_config = config
        account = {
            'issuer': (
                'https://auth.openai.com'
                if provider == 'chatgpt'
                else 'https://accounts.google.com'
            ),
            'subject': 'account-subject',
            'email': 'fixture@example.invalid',
        }
        tokens = {
            'schema': 1,
            'provider': provider,
            'client_id': config.client_id or 'issued-client',
            'account': account,
            'host_id': config.host_id,
            'scopes': ['openid', 'chatgpt.tokens.use.direct'],
            'expires_at': time.time() + 3600,
            'access_token': 'oauth-fixture-access',
            'refresh_token': 'oauth-fixture-refresh',
            'id_token': 'oauth-fixture-id',
            'nonce': 'fixture-nonce',
            'token_type': 'Bearer',
        }
        saved = self.store.put(profile_id, config.api_origin, 'oauth_' + provider, tokens)
        if self.after_login:
            self.after_login()
        return self._result(saved, tokens)

    async def logout(self, provider, profile_id, ref, config, cancel=None):
        self.logout_calls += 1
        self.store.delete(ref, profile_id)
        return {
            'local_deleted': True,
            'remote_revocation': self.revocation,
            'client_id': config.client_id,
            'host_id': config.host_id,
        }


def chatgpt_profile():
    return ProviderProfile(
        profile_id='chat',
        name='ChatGPT 시험',
        provider='openai',
        auth_mode='chatgpt',
        model='selected',
    )


def test_oauth_login_binds_new_ref_account_and_persistent_host_without_claiming_inference(manager):
    manager.save_profile(chatgpt_profile())
    service = MockOAuth(manager.store)
    manager._oauth_service = service
    result = asyncio.run(manager.login('chat', {}, None, None))
    saved = manager.get_profile('chat')
    assert result['authenticated'] is True
    assert result['inference_authorized'] is True
    assert result['actual_inference_verified'] is False
    assert saved.oauth_client_id == 'issued-client'
    assert saved.oauth_subject == 'account-subject'
    assert manager.settings.ai_host_id.startswith('urn:uuid:')
    status = manager.account_status('chat')
    assert status['account']['email'] == 'fixture@example.invalid'
    assert 'access_token' not in repr(status)
    assert 'oauth-fixture-access' not in json.dumps(manager.settings.model_dump())


def test_oauth_login_commit_failure_preserves_old_session_and_removes_new_ref(manager, monkeypatch):
    manager.save_profile(chatgpt_profile())
    service = MockOAuth(manager.store)
    manager._oauth_service = service
    asyncio.run(manager.login('chat', {}, None, None))
    before = manager.get_profile('chat')
    refs = {path.name for path in manager.store.root.glob('cred_*.json')}
    monkeypatch.setattr(
        manager.history, 'replace', lambda *a, **k: (_ for _ in ()).throw(ValueError('CAS failure'))
    )
    with pytest.raises((ValueError, ProviderError)):
        asyncio.run(manager.login('chat', {}, None, None))
    assert manager.get_profile('chat').credential_ref == before.credential_ref
    assert manager.account_status('chat')['authenticated'] is True
    assert {path.name for path in manager.store.root.glob('cred_*.json')} == refs


def test_oauth_login_rejects_profile_change_before_committing_account(manager):
    manager.save_profile(chatgpt_profile())
    service = MockOAuth(manager.store)
    manager._oauth_service = service
    service.after_login = lambda: manager.save_profile(
        {**manager.get_profile('chat').model_dump(), 'model': 'changed'}
    )
    with pytest.raises(ProviderError, match='변경'):
        asyncio.run(manager.login('chat', {}, None, None))
    assert not manager.get_profile('chat').credential_ref
    assert not list(manager.store.root.glob('cred_*.json'))


def test_logout_clears_token_ref_preserves_registration_and_reports_remote_unconfirmed(manager):
    manager.save_profile(chatgpt_profile())
    manager._oauth_service = MockOAuth(manager.store)
    asyncio.run(manager.login('chat', {}, None, None))
    before = manager.get_profile('chat')
    host = manager.settings.ai_host_id
    result = asyncio.run(manager.logout('chat', None, None))
    after = manager.get_profile('chat')
    assert result['local_deleted'] is True and result['remote_revoked'] is False
    assert not after.credential_ref
    assert after.oauth_client_id == before.oauth_client_id
    assert after.oauth_subject == before.oauth_subject
    assert manager.settings.ai_host_id == host
    assert manager.account_status('chat')['authenticated'] is False


def test_logout_settings_failure_does_not_destroy_original_local_session(manager, monkeypatch):
    manager.save_profile(chatgpt_profile())
    manager._oauth_service = MockOAuth(manager.store)
    asyncio.run(manager.login('chat', {}, None, None))
    before = manager.get_profile('chat')
    deletions = []
    original_delete = manager.store.delete
    monkeypatch.setattr(
        manager.store,
        'delete',
        lambda ref, owner: deletions.append(ref) or original_delete(ref, owner),
    )
    monkeypatch.setattr(
        manager.history, 'replace', lambda *a, **k: (_ for _ in ()).throw(ValueError('CAS failure'))
    )
    with pytest.raises((ValueError, ProviderError)):
        asyncio.run(manager.logout('chat', None, None))
    assert manager.get_profile('chat').credential_ref == before.credential_ref
    assert manager.account_status('chat')['authenticated'] is True
    assert manager.oauth_service.logout_calls == 0
    assert deletions == []


def test_logout_remote_failure_after_cas_keeps_local_logout_and_reports_unconfirmed(manager):
    manager.save_profile(chatgpt_profile())
    manager._oauth_service = MockOAuth(manager.store)
    asyncio.run(manager.login('chat', {}, None, None))
    before = manager.get_profile('chat')

    async def failed_remote(*args, **kwargs):
        assert not manager.get_profile('chat').credential_ref
        raise AuthError('network', '원격 폐기 실패')

    manager.oauth_service.logout = failed_remote
    result = asyncio.run(manager.logout('chat'))
    assert result['local_deleted'] is True and result['remote_revoked'] is False
    assert result['remote_revocation'] == 'not_confirmed'
    assert not manager.get_profile('chat').credential_ref
    with pytest.raises(AuthError):
        manager.store.metadata(before.credential_ref, 'chat')


def test_real_settings_history_cas_backup_rejects_other_manager_update(tmp_path):
    initial = Settings(model='gemma4:e4b')
    atomic_json(tmp_path / 'settings.json', initial.model_dump())
    first = profiles.ProfileManager(initial, root=tmp_path)
    second = profiles.ProfileManager(initial, root=tmp_path)
    first.save_profile(remote())
    with pytest.raises(ValueError, match='변경'):
        second.save_profile(remote('other'))
    saved = Settings.model_validate_json((tmp_path / 'settings.json').read_text(encoding='utf-8'))
    assert [profile.profile_id for profile in saved.ai_profiles] == ['remote']
    history = first.history.list_history()
    assert history[0]['before_settings']['model'] == 'gemma4:e4b'
    assert history[0]['status'] == 'applied'


def test_invalid_legacy_display_placeholder_cannot_silently_replace_server(manager):
    manager.settings.ollama_url = 'http://remote.example:11434'
    placeholder = manager.get_profile('legacy-local')
    with pytest.raises(ProviderError):
        manager.save_profile(placeholder)
    assert manager.settings.ollama_url == 'http://remote.example:11434'


def test_external_settings_writer_invalidates_inflight_model_result(manager):
    manager.save_profile(remote())

    class ExternalChange(FakeProvider):
        async def list_models(self, cancel=None):
            path = manager.root / 'settings.json'
            content = json.loads(path.read_text(encoding='utf-8'))
            content['ai_profiles'][0]['model'] = 'other-manager-choice'
            path.write_text(json.dumps(content), encoding='utf-8')
            return [ModelDescriptor(id='old-result')]

    manager.provider_factory = ExternalChange
    with pytest.raises(ProviderError, match='변경'):
        asyncio.run(manager.list_models('remote'))


def test_explicit_environment_reference_does_not_access_vault_and_tracks_only_selected_env(
    manager, monkeypatch
):
    monkeypatch.setenv('REBUILD_TEST_PROFILE_KEY', 'first-env-fixture')
    profile = remote(
        credential_ref='env:REBUILD_TEST_PROFILE_KEY', credential_origin='https://first.example'
    )
    saved = manager.save_profile(profile)

    async def probe(provider, cancel=None, on_event=None):
        return capability_result(provider.profile)

    monkeypatch.setattr(profiles, 'probe_provider_capabilities', probe)
    manager.save_profile(
        {
            **saved.model_dump(),
            'enabled': True,
            'external_consent': True,
            'allowed_evidence': ('runtime',),
        }
    )
    asyncio.run(manager.probe_capabilities('remote'))
    assert manager.cached_capabilities('remote')['basic'] is True
    monkeypatch.setenv('REBUILD_TEST_PROFILE_KEY', 'second-env-fixture')
    assert manager.cached_capabilities('remote') is None
    refs = list(manager.store.root.glob('cred_*.json'))
    assert refs and all(
        manager.store.metadata(path.stem, 'remote')['kind'] == 'capability_proof' for path in refs
    )


def test_environment_reference_must_match_origin_and_api_key_auth(manager):
    with pytest.raises(ProviderError):
        manager.save_profile(
            remote(
                credential_ref='env:REBUILD_TEST_PROFILE_KEY',
                credential_origin='https://other.example',
            )
        )


def test_disabled_paid_probe_never_constructs_provider(manager):
    created = []
    manager.provider_factory = lambda profile: created.append(FakeProvider(profile)) or created[-1]
    manager.save_profile(remote())
    with pytest.raises(ProviderError):
        asyncio.run(manager.probe_capabilities('remote'))
    assert created == []


def test_real_disk_writer_conflict_prevents_logout_revocation_and_local_delete(
    manager, monkeypatch
):
    manager.save_profile(chatgpt_profile())
    manager._oauth_service = MockOAuth(manager.store)
    asyncio.run(manager.login('chat'))
    manager.history = SettingsHistory(manager.root)
    other = profiles.ProfileManager(manager.settings, root=manager.root, store=manager.store)
    other.save_profile(remote('other'))
    deletions = []
    original_delete = manager.store.delete
    monkeypatch.setattr(
        manager.store,
        'delete',
        lambda ref, owner: deletions.append(ref) or original_delete(ref, owner),
    )
    with pytest.raises((ValueError, ProviderError), match='변경'):
        asyncio.run(manager.logout('chat'))
    assert manager.oauth_service.logout_calls == 0
    assert deletions == []


def test_key_deletion_cas_failure_preserves_original_key(manager, monkeypatch):
    manager.save_profile(remote())
    saved = manager.save_secret('remote', 'existing-test-key')
    monkeypatch.setattr(
        manager.history, 'replace', lambda *a, **k: (_ for _ in ()).throw(ValueError('CAS failure'))
    )
    with pytest.raises(ValueError):
        manager.delete_secret('remote')
    assert manager.get_profile('remote').credential_ref == saved.credential_ref
    assert (
        manager.store.get(saved.credential_ref, 'remote', saved.origin).value.get_secret_value()
        == 'existing-test-key'
    )


def _ready_api(manager):
    manager.save_profile(remote(enabled=True, external_consent=True, allowed_evidence=('runtime',)))
    return manager.save_secret('remote', 'test-key-proof-only')


def test_capability_proof_is_dpapi_protected_ref_only_and_survives_manager_restart(
    manager, monkeypatch
):
    selected = _ready_api(manager)

    async def probe(provider, **kwargs):
        return capability_result(
            provider.profile, basic_response={'raw': {'do_not_persist': 'raw-fixture-secret'}}
        )

    monkeypatch.setattr(profiles, 'probe_provider_capabilities', probe)
    result = asyncio.run(manager.probe_capabilities('remote'))
    ref = manager.settings.capabilities['ai_proofs']['remote']
    assert isinstance(ref, str) and ref.startswith('cred_') and result['proof_status'] == 'verified'
    record = manager.store.get(ref, 'remote', selected.origin)
    assert (
        record.kind == 'capability_proof'
        and 'raw-fixture-secret' not in record.value.get_secret_value()
    )
    assert 'basic_response' not in record.value.get_secret_value()
    restarted = profiles.ProfileManager(
        manager.settings, root=manager.root, store=manager.store, provider_factory=FakeProvider
    )
    cached = restarted.cached_capabilities('remote')
    assert (
        cached['tools'] is True
        and cached['tool_roundtrip'] is True
        and cached['actual_inference_verified'] is True
    )
    assert (
        cached['profile_fingerprint'] == selected.fingerprint()
        and cached['origin'] == selected.origin
    )
    assert 'basic_response' not in cached
    assert 'raw-fixture-secret' not in json.dumps(manager.settings.model_dump())


def test_arbitrary_settings_capability_booleans_are_not_proof(manager):
    selected = _ready_api(manager)
    manager.settings.capabilities = capability_result(selected)
    assert manager.cached_capabilities('remote') is None


def test_unchanged_save_and_selection_preserve_same_proof_without_disk_write(manager, monkeypatch):
    selected = _ready_api(manager)

    async def probe(provider, **kwargs):
        return capability_result(provider.profile)

    monkeypatch.setattr(profiles, 'probe_provider_capabilities', probe)
    asyncio.run(manager.probe_capabilities('remote'))
    ref = manager.settings.capabilities['ai_proofs']['remote']
    manager.select_profile('remote')
    before = (manager.root / 'settings.json').read_bytes()
    manager.save_profile(manager.get_profile('remote'))
    manager.select_profile('remote')
    assert (manager.root / 'settings.json').read_bytes() == before
    assert manager.settings.capabilities['ai_proofs']['remote'] == ref
    assert manager.cached_capabilities('remote')['tools'] is True


def test_proof_storage_failure_is_partial_and_cannot_promote_capabilities(manager, monkeypatch):
    _ready_api(manager)

    async def probe(provider, **kwargs):
        return capability_result(provider.profile)

    monkeypatch.setattr(profiles, 'probe_provider_capabilities', probe)
    asyncio.run(manager.probe_capabilities('remote'))
    original_put = manager.store.put

    def reject_proof(owner, origin, kind, value, **kwargs):
        if kind == 'capability_proof':
            raise AuthError('credential_storage', 'fixture safe failure')
        return original_put(owner, origin, kind, value, **kwargs)

    monkeypatch.setattr(manager.store, 'put', reject_proof)
    result = asyncio.run(manager.probe_capabilities('remote'))
    assert result['proof_status'] == 'partial' and result['actual_inference_verified'] is False
    assert result['tools'] is False and manager.cached_capabilities('remote') is None


def test_proof_cas_failure_removes_only_new_record_and_preserves_previous_proof(
    manager, monkeypatch
):
    _ready_api(manager)

    async def probe(provider, **kwargs):
        return capability_result(provider.profile)

    monkeypatch.setattr(profiles, 'probe_provider_capabilities', probe)
    asyncio.run(manager.probe_capabilities('remote'))
    ref = manager.settings.capabilities['ai_proofs']['remote']
    before = {p.name for p in manager.store.root.glob('cred_*.json')}
    monkeypatch.setattr(
        manager.history,
        'replace',
        lambda *a, **k: (_ for _ in ()).throw(ValueError('CAS conflict')),
    )
    with pytest.raises(ProviderError) as error:
        asyncio.run(manager.probe_capabilities('remote'))
    assert error.value.code == 'profile_stale'
    assert manager.settings.capabilities['ai_proofs']['remote'] == ref
    assert {p.name for p in manager.store.root.glob('cred_*.json')} == before


@pytest.mark.parametrize('change', ['same_session', 'subject', 'scopes', 'client', 'issuer'])
def test_oauth_inflight_normal_refresh_is_allowed_but_binding_changes_are_rejected(manager, change):
    manager.save_profile(chatgpt_profile())
    manager._oauth_service = MockOAuth(manager.store)
    asyncio.run(manager.login('chat'))

    class Refresh(FakeProvider):
        async def probe(self, cancel=None):
            record = manager.store.get(self.profile.credential_ref, 'chat', self.profile.origin)
            tokens = json.loads(record.value.get_secret_value())
            tokens['access_token'] = 'new-test-access'
            if change == 'subject':
                tokens['account']['subject'] = 'different'
            if change == 'scopes':
                tokens['scopes'] = ['openid']
            if change == 'client':
                tokens['client_id'] = 'different'
            if change == 'issuer':
                tokens['account']['issuer'] = 'https://other.invalid'
            manager.store.put(
                'chat',
                self.profile.origin,
                'oauth_chatgpt',
                tokens,
                ref=record.ref,
                expected_version=record.version,
            )
            return {'connected': True}

    manager.provider_factory = Refresh
    if change == 'same_session':
        assert asyncio.run(manager.probe_connection('chat'))['connected'] is True
    else:
        with pytest.raises(ProviderError) as error:
            asyncio.run(manager.probe_connection('chat'))
        assert error.value.code == 'profile_stale'


def test_local_proof_records_verified_digest_without_network_on_cache_read(manager, monkeypatch):
    class Local(FakeProvider):
        async def list_models(self, cancel=None):
            return [ModelDescriptor(id=self.profile.model, digest='verified-local-digest')]

    manager.provider_factory = Local

    async def probe(provider, **kwargs):
        return capability_result(provider.profile)

    monkeypatch.setattr(profiles, 'probe_provider_capabilities', probe)
    result = asyncio.run(manager.probe_capabilities('legacy-local'))
    assert result['local_digest'] == 'verified-local-digest'
    restarted = profiles.ProfileManager(
        manager.settings,
        root=manager.root,
        store=manager.store,
        provider_factory=lambda p: (_ for _ in ()).throw(
            AssertionError('cache read must not use network')
        ),
    )
    assert restarted.cached_capabilities('legacy-local')['local_digest'] == 'verified-local-digest'
    assert restarted.cached_capabilities('legacy-local', local_digest='changed-digest') is None


def test_latest_failed_or_downgraded_manual_test_cannot_reuse_old_tool_success(
    manager, monkeypatch
):
    _ready_api(manager)

    async def success(provider, **kwargs):
        return capability_result(provider.profile)

    monkeypatch.setattr(profiles, 'probe_provider_capabilities', success)
    asyncio.run(manager.probe_capabilities('remote'))
    original_ref = manager.settings.capabilities['ai_proofs']['remote']

    async def downgrade(provider, **kwargs):
        return capability_result(provider.profile, tools=False, tool_roundtrip=False)

    monkeypatch.setattr(profiles, 'probe_provider_capabilities', downgrade)
    asyncio.run(manager.probe_capabilities('remote'))
    assert manager.cached_capabilities('remote')['tools'] is False
    with pytest.raises(AuthError):
        manager.store.metadata(original_ref, 'remote')

    async def fail(provider, **kwargs):
        raise ProviderError('network', 'fixture offline')

    monkeypatch.setattr(profiles, 'probe_provider_capabilities', fail)
    with pytest.raises(ProviderError):
        asyncio.run(manager.probe_capabilities('remote'))
    assert manager.cached_capabilities('remote') is None
    assert 'remote' not in manager.settings.capabilities.get('ai_proofs', {})


def test_proof_settings_storage_failure_is_partial_even_when_old_ref_cannot_be_removed(
    manager, monkeypatch
):
    _ready_api(manager)

    async def probe(provider, **kwargs):
        return capability_result(provider.profile)

    monkeypatch.setattr(profiles, 'probe_provider_capabilities', probe)
    asyncio.run(manager.probe_capabilities('remote'))
    monkeypatch.setattr(
        manager.history,
        'replace',
        lambda *a, **k: (_ for _ in ()).throw(OSError('fixture disk full')),
    )
    result = asyncio.run(manager.probe_capabilities('remote'))
    assert result['proof_status'] == 'partial' and result['actual_inference_verified'] is False
    assert manager.cached_capabilities('remote') is None


def test_local_digest_change_during_manual_probe_never_creates_verified_proof(manager, monkeypatch):
    class Local(FakeProvider):
        digest = 'before-probe'

        async def list_models(self, cancel=None):
            return [ModelDescriptor(id=self.profile.model, digest=self.digest)]

    manager.provider_factory = Local

    async def probe(provider, **kwargs):
        provider.digest = 'changed-during-probe'
        return capability_result(provider.profile)

    monkeypatch.setattr(profiles, 'probe_provider_capabilities', probe)
    result = asyncio.run(manager.probe_capabilities('legacy-local'))
    assert result['proof_status'] != 'verified' and result['actual_inference_verified'] is False
    assert manager.cached_capabilities('legacy-local') is None


def test_other_disk_writer_before_operation_is_rejected_before_provider_construction(manager):
    _ready_api(manager)
    created = []
    manager.provider_factory = lambda p: created.append(FakeProvider(p)) or created[-1]
    path = manager.root / 'settings.json'
    saved = json.loads(path.read_text(encoding='utf-8'))
    saved['num_ctx'] = 16384
    path.write_text(json.dumps(saved), encoding='utf-8')
    with pytest.raises(ProviderError) as error:
        asyncio.run(manager.probe_connection('remote'))
    assert error.value.code == 'profile_stale' and not created


def test_oauth_proof_survives_verified_same_session_refresh_but_not_scope_loss(
    manager, monkeypatch
):
    profile = chatgpt_profile().model_copy(
        update={'enabled': True, 'external_consent': True, 'allowed_evidence': ('runtime',)}
    )
    manager.save_profile(profile)
    manager._oauth_service = MockOAuth(manager.store)
    asyncio.run(manager.login('chat'))

    async def probe(provider, **kwargs):
        return capability_result(provider.profile)

    monkeypatch.setattr(profiles, 'probe_provider_capabilities', probe)
    asyncio.run(manager.probe_capabilities('chat'))
    selected = manager.get_profile('chat')
    record = manager.store.get(selected.credential_ref, 'chat', selected.origin)
    tokens = json.loads(record.value.get_secret_value())
    tokens['access_token'] = 'refreshed-test-value'
    manager.store.put(
        'chat',
        selected.origin,
        record.kind,
        tokens,
        ref=record.ref,
        expected_version=record.version,
    )
    assert manager.cached_capabilities('chat')['actual_inference_verified'] is True
    tokens['scopes'] = ['openid']
    manager.store.put(
        'chat',
        selected.origin,
        record.kind,
        tokens,
        ref=record.ref,
        expected_version=record.version + 1,
    )
    assert manager.cached_capabilities('chat') is None


def test_untrusted_proof_reference_can_never_delete_an_api_key(manager, monkeypatch):
    selected = _ready_api(manager)
    values = manager.settings.model_dump()
    values['capabilities'] = {'ai_proofs': {'remote': selected.credential_ref}}
    manager._persist(values)

    async def fail(provider, **kwargs):
        raise ProviderError('network', 'fixture offline')

    monkeypatch.setattr(profiles, 'probe_provider_capabilities', fail)
    with pytest.raises(ProviderError):
        asyncio.run(manager.probe_capabilities('remote'))
    assert manager.store.get(selected.credential_ref, 'remote', selected.origin).kind == 'api_key'


def test_real_cas_proof_restart_and_multiple_profile_reference_preservation(tmp_path, monkeypatch):
    settings = Settings(model='fixture')
    atomic_json(tmp_path / 'settings.json', settings.model_dump())
    manager = profiles.ProfileManager(settings, root=tmp_path, provider_factory=FakeProvider)
    first = _ready_api(manager)

    async def probe(provider, **kwargs):
        return capability_result(provider.profile)

    monkeypatch.setattr(profiles, 'probe_provider_capabilities', probe)
    asyncio.run(manager.probe_capabilities('remote'))
    first_ref = manager.settings.capabilities['ai_proofs']['remote']
    second = manager.save_profile(
        remote(
            'second',
            auth_mode='none',
            enabled=True,
            external_consent=True,
            allowed_evidence=('runtime',),
        )
    )
    asyncio.run(manager.probe_capabilities('second'))
    second_ref = manager.settings.capabilities['ai_proofs']['second']
    manager.select_profile('second')
    assert manager.settings.capabilities['ai_proofs'] == {'remote': first_ref, 'second': second_ref}
    restarted = profiles.ProfileManager(root=tmp_path)
    assert restarted.cached_capabilities('remote')['tools'] is True
    assert restarted.cached_capabilities('second')['tools'] is True
    manager.save_profile({**first.model_dump(), 'name': 'changed-name'})
    assert manager.settings.capabilities['ai_proofs'] == {'second': second_ref}
    with pytest.raises(AuthError):
        manager.store.metadata(first_ref, 'remote')
    assert manager.cached_capabilities('second')['tools'] is True


def test_cached_proof_accepts_explicit_ephemeral_role_selection_only(manager, monkeypatch):
    _ready_api(manager)

    async def probe(provider, **kwargs):
        return capability_result(provider.profile)

    monkeypatch.setattr(profiles, 'probe_provider_capabilities', probe)
    asyncio.run(manager.probe_capabilities('remote'))
    selected = manager.settings.model_copy(deep=True)
    selected.active_ai_profile = 'remote'
    selected.ai_role_profiles['generation'] = 'remote'
    reader = profiles.ProfileManager(settings=selected, root=manager.root, store=manager.store)
    assert reader.cached_capabilities('remote')['tools'] is True
    changed = selected.model_copy(deep=True)
    changed.ai_profiles[0] = changed.ai_profiles[0].model_copy(update={'model': 'other'})
    assert (
        profiles.ProfileManager(
            settings=changed, root=manager.root, store=manager.store
        ).cached_capabilities('remote')
        is None
    )


def test_adopt_committed_general_settings_refreshes_cas_and_rejects_unsaved_snapshot(tmp_path):
    from recheck.models import save_settings

    initial = Settings(model='fixture')
    save_settings(initial, root=tmp_path)
    manager = profiles.ProfileManager(initial, root=tmp_path)
    changed = initial.model_copy(update={'num_ctx': 16384})
    save_settings(changed, root=tmp_path)
    with pytest.raises(ProviderError):
        manager.adopt_committed_settings(initial)
    manager.adopt_committed_settings(changed)
    manager.save_profile(remote())
    assert manager.settings.num_ctx == 16384 and manager.get_profile('remote').model == 'chosen'
