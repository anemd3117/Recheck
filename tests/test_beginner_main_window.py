"""First-run guidance, saved capability reuse and automatic follow-up cancellation."""

import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QPoint
from shiboken6 import delete
from ai_providers.contracts import ProviderProfile
from recheck.gui import MainWindow
from recheck.models import Settings, atomic_json


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    monkeypatch.setenv('RECHECK_DATA_DIR', str(tmp_path))
    settings = Settings()
    atomic_json(tmp_path / 'settings.json', settings.model_dump())
    widget = MainWindow(settings, auto_check=False)
    yield widget
    widget.close()
    delete(widget)
    app.processEvents()


def test_first_run_explains_the_next_action_and_settings_open_on_simple_ai_connection(window):
    assert hasattr(window, 'guide_next'), 'A first-time user needs one next action'
    assert 'ChatGPT' in window.guide_next.text()
    assert window.settings_tabs.currentWidget() is window.provider_panel
    assert '1' in window.guide_status.text() and 'AI' in window.guide_status.text()


def test_home_connect_commits_the_preset_before_starting_its_worker(window):
    pending = []
    window.provider_panel._launcher = lambda title, work, done: pending.append((work, done)) or True
    window.follow_next_step()
    assert (
        len(pending) == 1
    ), 'Preset changes must be synchronized before the unsaved-settings check'
    assert window.settings.model_dump() == window.provider_manager.settings.model_dump()
    assert window.provider_panel.auth_combo.currentData() == 'chatgpt'
    window.provider_panel._operation_active = False


def test_cancelling_a_workflow_discards_its_automatic_followup(window):
    assert hasattr(
        window, 'after_current_task'
    ), 'Follow-up tasks must wait for full worker cleanup'
    window._task_join_pending = True
    window.after_current_task(lambda: None)
    assert window._after_task_action is not None
    window.cancel_work()
    assert window._after_task_action is None


def test_full_ai_mode_is_selected_after_proof_and_a_later_manual_choice_is_kept(window):
    profile = window.provider_manager.save_profile(
        ProviderProfile(
            profile_id='gpt',
            provider='openai',
            auth_mode='api_key',
            model='gpt-6.1-sol',
            enabled=True,
            external_consent=True,
            allowed_evidence=('runtime', 'static', 'code'),
        )
    )
    window.provider_manager.select_profile(profile.profile_id)
    window.provider_settings_changed()
    proof = {
        'profile_fingerprint': profile.fingerprint(),
        'model': profile.model,
        'basic': True,
        'tools': True,
        'tool_roundtrip': True,
        'actual_inference_verified': True,
    }
    window.provider_capabilities_done(proof)
    assert window.mode.currentData() == 'agent'
    window.mode.setCurrentIndex(0)
    window.mode.activated.emit(0)
    window.provider_capabilities_done(proof)
    assert window.mode.currentData() == 'automatic'


def test_comparison_action_remains_reachable_at_the_small_supported_window(window, app):
    window.navigation.setCurrentRow(1)
    window.resize(960, 640)
    window.show()
    app.processEvents()
    assert (
        window.minimumSizeHint().height() <= 640
    ), 'The input form must scroll instead of forcing an oversized window'
    bottom = window.start_button.mapTo(window, QPoint(0, window.start_button.height())).y()
    assert window.start_button.isVisible() and bottom <= window.height()


def test_activity_shows_plain_progress_and_keeps_technical_details_in_the_closed_drawer(window):
    window.accept_event(
        {
            'kind': 'quick_setup',
            'step': 'models',
            'status': 'running',
            'profile_id': 'technical-profile-id',
        }
    )
    assert hasattr(window, 'activity_log'), 'Users need a readable activity summary'
    assert '모델' in window.activity_log.toPlainText()
    assert 'technical-profile-id' not in window.activity_log.toPlainText()
    assert 'technical-profile-id' in window.logs.toPlainText()
    assert not window.logs.isVisible()


def test_advanced_backend_choice_roundtrips_through_settings_sync(window):
    field = window.setting_fields.get('static_backend')
    assert field is not None
    field.setCurrentIndex(field.findData('direct'))
    assert window.collect_settings().static_backend == 'direct'
    updated = window.settings.model_copy(update={'static_backend': 'rea'})
    from recheck.models import save_settings

    updated = save_settings(updated, window.performance_panel.root)
    window.performance_panel.sync_settings(updated)
    assert field.currentData() == 'rea'


def test_automatic_preparation_uses_packaged_environment_flow(window, monkeypatch):
    import asyncio
    from recheck import environment_setup

    calls = []

    async def prepare(settings, **kwargs):
        calls.append(settings.static_backend)
        return {'status': 'ready', 'patch': {}, 'managed_runtime': {'ready': True}}

    monkeypatch.setattr(environment_setup, 'prepare_analysis_environment', prepare)
    pending = []
    monkeypatch.setattr(window, 'launch_task', lambda title, work, done: pending.append(work))
    window.find_setup(auto_apply=True)
    result = asyncio.run(pending[0](window.settings, None, lambda event: None))
    assert calls == ['auto'] and result['managed_runtime']['ready']


def test_local_ai_is_an_explicit_choice_with_download_notice(window):
    assert hasattr(window, 'managed_local_button')
    assert '다운로드' in window.managed_local_button.text()


def test_local_ai_failure_keeps_the_current_selected_ai(window):
    before = window.provider_manager.selected_profile_id()
    window.managed_local_ready({'ready': False, 'message': '연결을 중단했습니다.'})
    assert window.provider_manager.selected_profile_id() == before


def test_local_ai_selects_only_after_verified_inference(window, monkeypatch):
    import asyncio

    pending = []
    monkeypatch.setattr(window, 'after_current_task', lambda work: work())
    monkeypatch.setattr(
        window, 'launch_task', lambda title, work, done: pending.append((work, done))
    )

    async def proof(profile_id, **kwargs):
        assert window.provider_manager.selected_profile_id() == 'legacy-local'
        return {'basic': True, 'actual_inference_verified': True, 'proof_persisted': True}

    monkeypatch.setattr(window.provider_manager, 'probe_capabilities', proof)
    window.managed_local_ready(
        {'ready': True, 'url': 'http://127.0.0.1:11435', 'model': 'qwen3:4b'}
    )
    assert window.provider_manager.selected_profile_id() == 'legacy-local'
    assert len(pending) == 1
    result = asyncio.run(pending[0][0](window.settings, None, lambda event: None))
    assert result['ready']
    assert (
        window.provider_manager.get_profile(window.provider_manager.selected_profile_id()).endpoint
        == 'http://127.0.0.1:11435'
    )


def test_local_reconnect_never_overwrites_the_current_profile_before_proof(window, monkeypatch):
    old = window.provider_manager.save_profile(
        ProviderProfile(
            profile_id='managed-ollama-qwen3-4b',
            name='내가 고른 로컬 모델',
            provider='ollama',
            model='qwen3:8b',
            endpoint='http://127.0.0.1:11435',
            enabled=True,
        )
    )
    window.provider_manager.select_profile(old.profile_id)
    window.performance_panel.sync_settings(window.provider_manager.settings.model_copy(deep=True))
    monkeypatch.setattr(window, 'after_current_task', lambda work: None)
    window.managed_local_ready({'ready': True, 'url': old.endpoint, 'model': 'qwen3:4b'})
    assert window.provider_manager.get_profile(old.profile_id) == old
    assert window.provider_manager.selected_profile_id() == old.profile_id


def test_switching_to_ai_without_tools_selects_automatic_mode(window):
    profile = window.provider_manager.save_profile(
        ProviderProfile(
            provider='ollama', model='qwen3:4b', endpoint='http://127.0.0.1:11435', enabled=True
        )
    )
    window.provider_manager.select_profile(profile.profile_id)
    window.performance_panel.sync_settings(window.provider_manager.settings.model_copy(deep=True))
    window.mode.model().item(1).setEnabled(True)
    window.mode.setCurrentIndex(1)
    window.provider_capabilities_done(
        {
            'profile_fingerprint': profile.fingerprint(),
            'model': profile.model,
            'basic': True,
            'tools': False,
            'actual_inference_verified': True,
        }
    )
    assert window.mode.currentData() == 'automatic'
    assert not window.mode.model().item(1).isEnabled()


def test_environment_check_resumes_selected_managed_service_before_ai_probe(window, monkeypatch):
    import asyncio
    from recheck import local_ai_setup

    calls, pending = [], []

    async def ensure(settings, **kwargs):
        calls.append('ensure-selected-only')
        return {'required': False, 'status': 'skipped'}

    class Environment:
        async def environment(self, cancel=None):
            calls.append('probe')
            return {'ai': {}, 'docker': {}, 'ghidra': {}}

    monkeypatch.setattr(local_ai_setup, 'ensure_selected_local_ai', ensure)
    monkeypatch.setattr(window, 'engine_for', lambda settings, emit: Environment())
    monkeypatch.setattr(window, 'launch_task', lambda title, work, done: pending.append(work))
    window.check_environment()
    asyncio.run(pending[0](window.settings, None, lambda event: None))
    assert calls == ['ensure-selected-only', 'probe']


def test_running_ollama_without_a_model_is_not_shown_as_ready_ai(window):
    window.environment_done(
        {
            'ollama': {'connected': True, 'version': 'fixture'},
            'docker': {'ready': True},
            'ghidra': {'ready': True},
        }
    )
    assert not window._ai_ready
    assert window.readiness_cards['ollama'].value.text() != '연결됨'


def test_missing_packaged_image_does_not_tell_user_to_reinstall_running_docker(window):
    window.environment_done(
        {'ollama': {}, 'docker': {'ready': False, 'engine_ready': True}, 'ghidra': {'ready': False}}
    )
    assert '분석 도구 내려받아 준비' in window.tool_help_text.text()
    assert 'Docker)를 사용할 수 없습니다' not in window.tool_help_text.text()


def test_accessible_navigation_selection_opens_the_selected_page(window):
    window.navigation.setCurrentRow(0)
    window.navigation.item(2).setSelected(True)
    assert window.navigation.currentRow() == 2
    assert window.pages.currentIndex() == 2
