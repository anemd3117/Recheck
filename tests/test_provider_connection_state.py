"""All providers inspect saved connections without restarting or selecting them."""

import asyncio
import threading

import pytest

from ai_providers.contracts import ModelDescriptor
from recheck import provider_onboarding
from test_provider_onboarding import account
from test_embedded_provider_ui import app, panel
from test_persistent_connection_ui import connected


def reader(manager, pid):
    assert hasattr(provider_onboarding, 'connection_state'), 'Shared saved connection reader missing'
    return provider_onboarding.connection_state(manager, pid)


def ready_api(manager, preset='gemini'):
    profile = provider_onboarding.preset_profile(preset)
    profile = manager.save_profile({**profile.model_dump(), 'enabled': True,
                                    'external_consent': True, 'allowed_evidence': ('runtime',),
                                    'model': 'gemini-3.5-flash-lite' if preset == 'gemini' else 'saved-model'})
    profile = manager.save_secret(profile.profile_id, 'synthetic-personal-key')
    asyncio.run(manager.probe_capabilities(profile.profile_id))
    return profile


@pytest.mark.parametrize('preset', ['gemini', 'openai', 'grok', 'deepseek', 'claude'])
def test_api_readiness_requires_real_saved_key_and_bound_proof(account, preset):
    manager, network = account
    profile = ready_api(manager, preset)
    before = manager.settings.model_dump()
    network['network'].clear()
    assert reader(manager, profile.profile_id)['status'] == 'ready'
    assert manager.settings.model_dump() == before and network['network'] == []
    manager._discard_proof(profile.profile_id)
    assert reader(manager, profile.profile_id)['status'] == 'account_saved'
    manager.store.delete(profile.credential_ref, profile.profile_id)
    assert reader(manager, profile.profile_id)['status'] == 'needs_attention'


def test_ready_list_includes_unselected_saved_profiles_without_selecting_any(account):
    manager, network = account
    first, second = ready_api(manager, 'gemini'), ready_api(manager, 'deepseek')
    manager._discard_proof(second.profile_id)
    before = manager.settings.model_dump()
    network['network'].clear()
    assert hasattr(provider_onboarding, 'ready_connection_profiles'), 'Shared ready-profile list missing'
    assert [p.profile_id for p in provider_onboarding.ready_connection_profiles(manager)] == [first.profile_id]
    assert manager.settings.model_dump() == before and network['network'] == []


def test_shared_reader_reuses_actual_chatgpt_state_without_requiring_tools(connected):
    _, manager, _, _, pid, _ = connected
    state = reader(manager, pid)
    assert state['status'] == 'ready' and state['proof']['tools'] is False


def test_saved_chatgpt_idle_guidance_and_stages_survive_reload(connected):
    widget, manager, _, _, pid, _ = connected
    widget.reload_profiles(pid)
    assert '저장된 연결 상태' in widget.quick_progress.text()
    assert '승인해 주세요' not in widget.quick_progress.text()
    assert all(item.text().startswith('✓ ') for item in widget.connection_steps)
    asyncio.run(manager.logout(pid))
    widget.reload_profiles(pid)
    assert '승인해 주세요' in widget.quick_progress.text()
    assert widget.connection_steps[0].text().startswith('● ')


def test_idle_common_stages_follow_saved_state_and_preset_change(saved_panel):
    widget, manager, _, _ = saved_panel
    profile = ready_api(manager, 'deepseek')
    widget.reload_profiles(profile.profile_id)
    widget.begin_provider_setup('deepseek')
    assert all(item.text().startswith('✓ ') for item in widget.connection_steps)
    widget.begin_provider_setup('grok')
    assert widget.connection_steps[0].text() == '● 키 입력'
    assert widget.connection_steps[-1].text() == '○ 연결 완료'
    widget.begin_provider_setup('deepseek')
    assert all(item.text().startswith('✓ ') for item in widget.connection_steps)


def test_saved_refresh_preserves_active_retry_and_terminal_failure(connected):
    widget, _, pending, _, _, _ = connected
    widget.chatgpt_verify_button.click()
    widget.show_setup_progress({'kind': 'oauth_login', 'status': 'authorization_restart'})
    progress = widget.quick_progress.text()
    stages = [item.text() for item in widget.connection_steps]
    widget.refresh_connection_states()
    assert widget.quick_progress.text() == progress
    assert [item.text() for item in widget.connection_steps] == stages
    _, done = pending.pop()
    done({'ok': False, 'code': 'network'})
    progress = widget.quick_progress.text()
    stages = [item.text() for item in widget.connection_steps]
    assert any(item.startswith('! ') for item in stages)
    widget.refresh_connection_states()
    assert widget.quick_progress.text() == progress
    assert [item.text() for item in widget.connection_steps] == stages


def test_local_saved_model_requires_bound_inference_proof_without_remote_consent(account):
    manager, _ = account

    class Local:
        def __init__(self, profile):
            self.profile = profile

        async def list_models(self, **kwargs):
            return [ModelDescriptor(id=self.profile.model, digest='sha256:' + 'a' * 64)]

        async def close(self):
            pass

    manager.provider_factory = Local
    assert reader(manager, 'legacy-local')['status'] == 'verification_needed'
    asyncio.run(manager.probe_capabilities('legacy-local'))
    assert reader(manager, 'legacy-local')['status'] == 'ready'


@pytest.fixture
def saved_panel(panel, account):
    widget, _, pending, _ = panel
    manager, network = account
    widget.manager = manager
    return widget, manager, pending, network


def finish(pending):
    work, done = pending.pop()
    done(asyncio.run(work(threading.Event(), lambda event: None)))


def test_gemini_primary_status_does_not_require_new_consent_or_change_selection(saved_panel):
    widget, manager, pending, network = saved_panel
    profile = ready_api(manager)
    widget.reload_profiles(profile.profile_id)
    before = manager.settings.model_dump()
    network['network'].clear()
    assert widget.gemini_connect_button.text() == '연결 상태 확인'
    assert widget.gemini_cancel_button.text() == '연결 해제' and widget.gemini_cancel_button.isEnabled()
    assert widget.connect_gemini()
    assert pending == [] and network['network'] == []
    assert manager.settings.model_dump() == before
    assert '연결 완료' in widget.gemini_summary.text()


@pytest.mark.parametrize('preset', ['openai', 'grok', 'deepseek', 'claude'])
def test_common_primary_status_is_read_only_for_each_saved_api(saved_panel, preset):
    widget, manager, pending, network = saved_panel
    profile = ready_api(manager, preset)
    widget.reload_profiles(profile.profile_id)
    widget.begin_provider_setup(preset)
    before = manager.settings.model_dump()
    network['network'].clear()
    assert widget.onboarding_open.text() == '연결 상태 확인'
    assert widget.open_selected_provider()
    assert widget.connect_selected_preset()
    assert pending == [] and network['network'] == []
    assert manager.settings.model_dump() == before
    assert widget.onboarding_cancel_button.text() == '연결 해제'


@pytest.mark.parametrize('preset', ['gemini', 'openai', 'grok', 'deepseek', 'claude'])
def test_disconnect_removes_only_the_target_key_and_does_not_switch_selected_ai(saved_panel, preset):
    widget, manager, pending, _ = saved_panel
    target = ready_api(manager, preset)
    other = ready_api(manager, 'deepseek' if preset != 'deepseek' else 'openai')
    manager.select_profile(target.profile_id)
    other_before = manager.get_profile(other.profile_id)
    widget.reload_profiles(target.profile_id)
    widget.begin_provider_setup(preset)
    button = widget.gemini_cancel_button if preset == 'gemini' else widget.onboarding_cancel_button
    button.click()
    if pending:
        finish(pending)
    assert not manager.get_profile(target.profile_id).credential_ref
    assert manager.selected_profile_id() == target.profile_id
    assert manager.get_profile(other.profile_id) == other_before
    assert manager.cached_capabilities(other.profile_id)['actual_inference_verified']
    assert reader(manager, target.profile_id)['status'] != 'ready'
    assert button.isEnabled() is False


def test_chatgpt_secondary_action_disconnects_when_idle_and_cancels_without_deleting_when_running(connected):
    widget, manager, pending, _, pid, _ = connected
    assert widget.quick_cancel.text() == '연결 해제' and widget.quick_cancel.isEnabled()
    before = manager.get_profile(pid).credential_ref
    widget.chatgpt_verify_button.click()
    assert widget.quick_cancel.text() == '연결 중단'
    widget.quick_cancel.click()
    assert manager.get_profile(pid).credential_ref == before
    finish(pending)
    assert manager.get_profile(pid).credential_ref == before


def test_idle_chatgpt_disconnect_preserves_selection_and_other_profile(connected):
    widget, manager, pending, _, pid, _ = connected
    from recheck.provider_onboarding import preset_profile

    other = manager.save_profile(preset_profile('deepseek'))
    widget.quick_cancel.click()
    finish(pending)
    assert not manager.get_profile(pid).credential_ref
    assert manager.selected_profile_id() == pid
    assert manager.get_profile(other.profile_id) == other
    assert not widget.quick_cancel.isEnabled()


@pytest.mark.parametrize('preset', ['gemini', 'deepseek'])
def test_running_key_verification_cancel_keeps_saved_key_and_selection(saved_panel, preset):
    widget, manager, pending, _ = saved_panel
    profile = ready_api(manager, preset)
    widget.reload_profiles(profile.profile_id)
    widget.begin_provider_setup(preset)
    before = manager.settings.model_dump()
    widget.verify_saved_preset(preset)
    button = widget.gemini_cancel_button if preset == 'gemini' else widget.onboarding_cancel_button
    assert button.text() == '연결 중단'
    button.click()
    finish(pending)
    assert manager.settings.model_dump() == before
    assert manager.get_profile(profile.profile_id).credential_ref == profile.credential_ref
    assert button.text() == '연결 해제' and button.isEnabled()


def test_gemini_new_typed_key_still_requires_deliberate_verification(saved_panel):
    widget, manager, pending, _ = saved_panel
    profile = ready_api(manager)
    widget.reload_profiles(profile.profile_id)
    widget.gemini_api_key.setText('new-synthetic-key')
    assert widget.gemini_connect_button.text() != '연결 상태 확인'
    assert widget.connect_gemini() is False  # new key still requires explicit confirmations
    assert pending == [] and manager.get_profile(profile.profile_id).credential_ref == profile.credential_ref


def test_saved_without_proof_has_separate_response_verification_action(saved_panel):
    widget, manager, pending, network = saved_panel
    profile = ready_api(manager)
    manager._discard_proof(profile.profile_id)
    widget.reload_profiles(profile.profile_id)
    assert widget.connect_gemini()
    assert pending == []
    assert '응답 확인 필요' in widget.gemini_summary.text()
    assert hasattr(widget, 'gemini_verify_button') and widget.gemini_verify_button.isEnabled()


@pytest.mark.parametrize('preset', ['gemini', 'deepseek'])
@pytest.mark.parametrize('failure_stage', ['', 'models', 'response'])
def test_explicit_saved_response_check_sends_fresh_inference(
        saved_panel, preset, failure_stage, monkeypatch):
    widget, manager, pending, network = saved_panel
    profile = ready_api(manager, preset)
    from ai_providers import profiles
    from ai_providers.contracts import ProviderError

    if failure_stage == 'response':
        async def failed_probe(*args, **kwargs):
            network['network'].append('response')
            raise ProviderError('network', 'Synthetic response failure')

        monkeypatch.setattr(profiles, 'probe_provider_capabilities', failed_probe)

    class Remote:
        def __init__(self, profile):
            self.profile = profile

        async def list_models(self, **kwargs):
            network['network'].append('models')
            if failure_stage == 'models':
                raise ProviderError('network', 'Synthetic catalog failure')
            return [ModelDescriptor(id=self.profile.model)]

        async def close(self):
            pass

    manager.provider_factory = Remote
    widget.reload_profiles(profile.profile_id)
    network['network'].clear()
    results = []
    widget.setup_ready.connect(results.append)
    assert widget.verify_saved_preset(preset)
    finish(pending)
    assert network['network'] == (['models'] if failure_stage == 'models' else ['models', 'response'])
    if failure_stage:
        assert not results
        assert (reader(manager, profile.profile_id)['status'] == 'ready') == (failure_stage == 'models')
    else:
        assert results and results[0]['capabilities_reused'] is False
    assert manager.get_profile(profile.profile_id).credential_ref == profile.credential_ref


@pytest.mark.parametrize('failure_stage', ['', 'models', 'response'])
def test_explicit_chatgpt_response_check_sends_fresh_inference(connected, failure_stage, monkeypatch):
    widget, manager, pending, _, pid, network = connected
    from ai_providers import profiles
    from ai_providers.contracts import ProviderError

    if failure_stage == 'response':
        async def failed_probe(*args, **kwargs):
            network.append('response')
            raise ProviderError('network', 'Synthetic response failure')

        monkeypatch.setattr(profiles, 'probe_provider_capabilities', failed_probe)
    elif failure_stage == 'models':
        async def failed_models(*args, **kwargs):
            network.append('models')
            raise ProviderError('network', 'Synthetic catalog failure')

        monkeypatch.setattr(manager, 'list_models', failed_models)
    credential = manager.get_profile(pid).credential_ref
    results = []
    widget.setup_ready.connect(results.append)
    widget.chatgpt_verify_button.click()
    finish(pending)
    assert network == (['models'] if failure_stage == 'models' else ['models', 'response'])
    if failure_stage:
        assert not results
        assert (reader(manager, pid)['status'] == 'ready') == (failure_stage == 'models')
    else:
        assert results and results[0]['capabilities_reused'] is False
    assert manager.get_profile(pid).credential_ref == credential


@pytest.mark.parametrize('preset', ['gemini', 'deepseek'])
def test_new_key_success_is_revalidated_before_announcing_ready(saved_panel, preset):
    widget, manager, pending, _ = saved_panel

    class Remote:
        def __init__(self, profile):
            self.profile = profile

        async def list_models(self, **kwargs):
            return [ModelDescriptor(id='gemini-3.5-flash-lite' if preset == 'gemini' else 'saved-model')]

        async def close(self):
            pass

    manager.provider_factory = Remote
    widget.reload_profiles()
    ready = []
    widget.setup_ready.connect(ready.append)
    if preset == 'gemini':
        widget.gemini_free_confirm.setChecked(True)
        widget.gemini_evidence_consent.setChecked(True)
        widget.gemini_api_key.setText('synthetic-new-key')
        assert widget.connect_gemini()
    else:
        widget.begin_provider_setup(preset)
        widget.onboarding_consent.setChecked(True)
        widget.onboarding_key.setText('synthetic-new-key')
        assert widget.connect_selected_preset()
    work, done = pending.pop()
    envelope = asyncio.run(work(threading.Event(), lambda event: None))
    assert envelope['ok']
    manager._discard_proof(envelope['result']['profile']['profile_id'])
    done(envelope)
    assert ready == []
