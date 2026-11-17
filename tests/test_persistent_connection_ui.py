"""Saved ChatGPT state survives UI refresh without restarting authentication."""

import asyncio
import threading

import pytest
from shiboken6 import delete

from ai_providers import profiles
from ai_providers.contracts import ModelDescriptor, ProviderError
from ai_providers.profiles import ProfileManager
from recheck.provider_ui import ProviderPanel
from recheck.setup_flow import ChatGPTSetup
from test_embedded_provider_ui import app, panel, Browser
from test_provider_profiles import MockOAuth
from test_setup_ui import window


@pytest.fixture
def connected(panel, monkeypatch):
    widget, manager, pending, folder = panel
    network = []

    class Remote:
        def __init__(self, profile):
            self.profile = profile

        async def list_models(self, **kwargs):
            network.append('models')
            return [ModelDescriptor(id=self.profile.model)]

        async def close(self):
            pass

    async def probe(provider, **kwargs):
        network.append('response')
        profile = provider.profile
        return dict(basic=True, json_schema=True, actual_inference_verified=True,
                    model=profile.model, provider=profile.provider,
                    profile_fingerprint=profile.fingerprint())

    monkeypatch.setattr(profiles, 'probe_provider_capabilities', probe)
    manager.provider_factory = Remote
    manager._oauth_service = MockOAuth(manager.store)
    profile = ChatGPTSetup(manager).prepare_profile()
    profile = manager.save_profile({**profile.model_dump(), 'model': 'saved-custom-model',
                                    'allowed_evidence': ('runtime',)})
    asyncio.run(manager.login(profile.profile_id))
    asyncio.run(manager.probe_capabilities(profile.profile_id))
    manager.select_profile(profile.profile_id)
    widget.reload_profiles(profile.profile_id)
    network.clear()
    return widget, manager, pending, folder, profile.profile_id, network


def finish(pending):
    work, done = pending.pop()
    done(asyncio.run(work(threading.Event(), lambda event: None)))


def state(manager, profile_id=None):
    from recheck import provider_onboarding

    assert hasattr(provider_onboarding, 'chatgpt_connection_state'), 'Persistent state reader missing'
    return provider_onboarding.chatgpt_connection_state(manager, profile_id)


def test_ready_state_requires_account_and_bound_persisted_proof(connected):
    _, manager, _, _, pid, network = connected
    assert state(manager, pid)['status'] == 'ready'
    manager._discard_proof(pid)
    assert state(manager, pid)['status'] == 'account_saved'
    assert network == []


def test_lost_credential_does_not_reuse_a_previous_success(connected):
    _, manager, _, _, pid, _ = connected
    manager.store.delete(manager.get_profile(pid).credential_ref, pid)
    assert state(manager, pid)['status'] == 'needs_attention'


def test_expired_access_is_reported_without_discarding_saved_connection(connected, monkeypatch):
    _, manager, _, _, pid, _ = connected
    account = manager.account_status(pid)
    monkeypatch.setattr(manager, 'account_status', lambda _: {**account, 'expired': True})
    result = state(manager, pid)
    assert result['status'] == 'expired' and result['profile'].profile_id == pid


def test_ambiguous_existing_connections_do_not_create_another_profile(connected):
    widget, manager, pending, _, pid, _ = connected
    from ai_providers.contracts import ProviderProfile

    manager.save_profile(ProviderProfile(profile_id='second-chat', provider='openai', auth_mode='chatgpt'))
    manager.select_profile('legacy-local')
    widget.reload_profiles('legacy-local')
    before = manager.settings.model_dump()
    assert widget.quick_connect() is False
    assert manager.settings.model_dump() == before and not pending


def test_ready_click_is_read_only_and_keeps_verified_status(connected, monkeypatch):
    widget, manager, pending, folder, pid, network = connected
    ready = []
    widget.setup_ready.connect(ready.append)
    before = (folder / 'settings.json').read_bytes()
    monkeypatch.setattr(manager, 'save_profile', lambda *_: pytest.fail('status check rewrote settings'))
    monkeypatch.setattr(manager, 'login', lambda *_a, **_kw: pytest.fail('status check started OAuth'))
    assert widget.quick_button.text() == '연결 상태 확인'
    for _ in range(2):
        assert widget.quick_connect()
        assert '연결 완료' in widget.quick_summary.text()
        assert 'saved-custom-model' in widget.quick_summary.text()
        assert '연결 완료' in widget.chatgpt_saved_summary.text()
        assert '활성 계정:' in widget.account_label.text()
    assert pending == [] and network == [] and ready == []
    assert manager.selected_profile_id() == pid
    assert (folder / 'settings.json').read_bytes() == before


def test_restart_reconstructs_ready_card_from_disk(connected, app):
    _, _, _, folder, pid, _ = connected
    restarted = ProfileManager(root=folder)
    widget = ProviderPanel(restarted, browser_dialog_factory=Browser)
    try:
        assert widget._current_id == pid
        assert widget.quick_button.text() == '연결 상태 확인'
        assert '활성 계정:' in widget.account_label.text()
        assert '연결 완료' in widget.quick_summary.text()
        assert 'saved-custom-model' in widget.quick_summary.text()
        widget.reload_profiles()
        assert '연결 완료' in widget.quick_summary.text()
    finally:
        widget.close()
        delete(widget)
        app.processEvents()


def test_saved_account_status_does_not_automatically_probe(connected):
    widget, manager, pending, _, pid, network = connected
    manager._discard_proof(pid)
    widget.reload_profiles(pid)
    assert widget.quick_connect()
    assert pending == [] and network == []
    assert '응답 확인 필요' in widget.quick_summary.text()
    assert widget.chatgpt_verify_button.isEnabled()


def test_explicit_response_check_reuses_profile_without_reapplying_preset(connected, monkeypatch):
    widget, manager, pending, _, pid, network = connected
    manager._discard_proof(pid)
    widget.reload_profiles(pid)
    before = manager.get_profile(pid)
    monkeypatch.setattr(ChatGPTSetup, 'prepare_profile', lambda *_: pytest.fail('saved profile reset'))
    assert hasattr(widget, 'chatgpt_verify_button'), 'Explicit response action missing'
    widget.chatgpt_verify_button.click()
    finish(pending)
    assert network == ['models', 'response']
    assert manager.get_profile(pid) == before
    assert '연결 완료' in widget.quick_summary.text()


@pytest.mark.parametrize('embedded', [False, True])
def test_explicit_relogin_failure_preserves_old_binding(connected, monkeypatch, embedded):
    widget, manager, pending, folder, pid, _ = connected
    widget.embedded_login_check.setChecked(embedded)
    before = (folder / 'settings.json').read_bytes()
    calls = []

    async def login(profile_id, *, cancel=None, on_event=None, on_url=None):
        calls.append((profile_id, on_url is not None))
        raise ProviderError('access_denied', 'fixture reauthentication denied')

    monkeypatch.setattr(manager, 'login', login)
    assert hasattr(widget, 'chatgpt_relogin_button'), 'Explicit reauthentication action missing'
    widget.chatgpt_relogin_button.click()
    finish(pending)
    assert calls == [(pid, embedded)]
    assert (folder / 'settings.json').read_bytes() == before
    assert manager.cached_capabilities(pid)['actual_inference_verified']
    assert '연결 완료' in widget.quick_summary.text()


def test_explicit_logout_removes_ready_state_and_restart_agrees(connected):
    widget, manager, pending, folder, pid, _ = connected
    assert hasattr(widget, 'chatgpt_logout_button'), 'Visible logout action missing'
    widget.chatgpt_logout_button.click()
    finish(pending)
    assert not manager.get_profile(pid).credential_ref
    assert widget.quick_button.text() == 'ChatGPT 로그인'
    assert '연결 완료' not in widget.quick_summary.text()
    assert state(ProfileManager(root=folder), pid)['status'] == 'disconnected'


def test_profile_change_does_not_keep_another_profiles_ready_card(connected):
    widget, manager, _, _, _, _ = connected
    from ai_providers.contracts import ProviderProfile

    manager.save_profile(ProviderProfile(profile_id='new-chat', provider='openai', auth_mode='chatgpt'))
    widget.reload_profiles('new-chat')
    assert '연결 완료' not in widget.quick_summary.text()
    assert widget.quick_button.text() == 'ChatGPT 로그인'


def test_main_readiness_does_not_trust_in_memory_success_after_credential_loss(connected, window):
    _, manager, _, _, pid, _ = connected
    widget, _ = window
    profile = manager.get_profile(pid)
    widget.settings = manager.settings
    widget.provider_manager = manager
    widget._provider_capability_summaries[profile.fingerprint()] = manager.cached_capabilities(pid)
    manager.store.delete(profile.credential_ref, pid)
    widget.refresh_selected_provider()
    assert widget._ai_ready is False
    assert '응답 확인됨' not in widget.selected_provider_summary.text()


def test_failed_connection_journal_retains_stage_without_sensitive_details(panel, monkeypatch):
    widget, manager, pending, folder = panel
    from recheck.connection_journal import read_connection_outcome

    async def fail(setup, pid, *, cancel=None, on_event=None, on_url=None):
        await on_event({'kind': 'oauth_login', 'status': 'token_exchange',
                        'url': 'https://private.invalid?code=secret'})
        raise ProviderError('oauth_http', 'secret raw endpoint account information')

    monkeypatch.setattr(ChatGPTSetup, 'connect', fail)
    assert widget.quick_connect()
    finish(pending)
    record = read_connection_outcome(folder)
    assert record.get('stage') == 'token_exchange'
    assert record['status'] == 'failed' and record['code'] == 'oauth_http'
    assert record['account_saved'] is False and record['proof_saved'] is False
    assert record['selected'] is False
    assert 'secret' not in (folder / 'last-connection-status.json').read_text()


def test_success_journal_distinguishes_saved_account_from_verified_response(connected):
    widget, manager, pending, folder, pid, _ = connected
    from recheck.connection_journal import read_connection_outcome

    manager._discard_proof(pid)
    widget.reload_profiles(pid)
    widget.chatgpt_verify_button.click()
    finish(pending)
    record = read_connection_outcome(folder)
    assert record.get('stage') == 'complete' and record['status'] == 'success'
    assert record['account_saved'] and record['proof_saved'] and record['selected']


def test_cancellation_journal_does_not_claim_authentication_or_completion(panel, monkeypatch):
    widget, _, pending, folder = panel
    from recheck.connection_journal import read_connection_outcome

    async def cancelled(setup, pid, *, cancel=None, on_event=None, on_url=None):
        await on_event({'kind': 'oauth_login', 'status': 'browser_requested'})
        raise ProviderError('cancelled', 'fixture cancelled')

    monkeypatch.setattr(ChatGPTSetup, 'connect', cancelled)
    widget.quick_connect()
    finish(pending)
    record = read_connection_outcome(folder)
    assert record['status'] == 'cancelled' and record['stage'] == 'browser_requested'
    assert not record['account_saved'] and not record['proof_saved']


@pytest.mark.parametrize('changed', ['credential', 'proof', 'selection', 'profile'])
def test_late_storage_change_cannot_announce_connection_success(connected, changed):
    widget, manager, pending, folder, pid, _ = connected
    from recheck.connection_journal import read_connection_outcome

    ready, capabilities = [], []
    widget.setup_ready.connect(ready.append)
    widget.capabilities_checked.connect(capabilities.append)
    widget.chatgpt_verify_button.click()
    work, done = pending.pop()
    envelope = asyncio.run(work(threading.Event(), lambda event: None))
    assert envelope['ok']
    profile = manager.get_profile(pid)
    if changed == 'credential':
        manager.store.delete(profile.credential_ref, pid)
    elif changed == 'proof':
        manager._discard_proof(pid)
    elif changed == 'selection':
        manager.select_profile('legacy-local')
    else:
        manager.save_profile({**profile.model_dump(), 'model': 'changed-model'})
    done(envelope)
    assert ready == [] and capabilities == []
    record = read_connection_outcome(folder)
    assert record['status'] == 'failed' and record['code'] == 'profile_stale'


def test_fresh_authorization_retry_requests_approval_without_claiming_success(panel):
    widget, _, _, _ = panel
    widget.quick_connect()
    widget.show_setup_progress({'kind': 'oauth_login', 'status': 'authorization_restart',
                                'connection_attempt': widget._operation_id})
    assert '새 인증 요청' in widget.quick_progress.text()
    assert '승인해 주세요' in widget.quick_progress.text()
    assert widget.connection_steps[0].text().startswith('● ')
    assert not widget.connection_steps[1].text().startswith('✓ ')
