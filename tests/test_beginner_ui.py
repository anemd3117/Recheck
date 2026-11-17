"""A novice can connect GPT without editing provider/model/permission fields."""

import asyncio
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest
from PySide6.QtWidgets import QApplication
from shiboken6 import delete
from ai_providers import profiles
from ai_providers.contracts import ModelDescriptor
from recheck.models import Settings
from recheck.provider_ui import ProviderPanel
from test_provider_profiles import MockOAuth


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def beginner_panel(app, tmp_path, monkeypatch):
    # The ready card must read real protected account/proof records. Replace
    # only provider I/O; the older SetupManager double never persisted either.
    class Remote:
        def __init__(self, profile):
            self.profile = profile

        async def list_models(self, **kwargs):
            return [ModelDescriptor(id='gpt-6.1-sol')]

        async def close(self):
            pass

    async def probe(provider, **kwargs):
        profile = provider.profile
        return dict(basic=True, json_schema=True, tools=True, tool_roundtrip=True,
                    actual_inference_verified=True, model=profile.model,
                    provider=profile.provider, profile_fingerprint=profile.fingerprint())

    monkeypatch.setattr(profiles, 'probe_provider_capabilities', probe)
    manager = profiles.ProfileManager(Settings(), root=tmp_path, provider_factory=Remote)
    manager._oauth_service = MockOAuth(manager.store)
    pending = []

    def launch(title, work, done):
        pending.append((work, done))
        return True

    widget = ProviderPanel(manager, launch_task=launch)
    widget.resize(1000, 760)
    widget.show()
    app.processEvents()
    yield widget, manager, pending
    widget.close()
    delete(widget)
    app.processEvents()


def test_simple_view_starts_with_chatgpt_and_hides_technical_controls(beginner_panel):
    widget, _, _ = beginner_panel
    assert hasattr(
        widget, 'quick_button'
    ), 'The recommended Connect button must be the first action'
    assert widget.quick_button.isVisible() and 'ChatGPT' in widget.quick_button.text()
    assert widget.details_scroll.isHidden()
    assert 'GPT-6.1' in widget.quick_summary.text()
    widget.details_toggle.click()
    assert not widget.details_scroll.isHidden()


def test_new_connection_defaults_to_unconnected_gemini_without_consent_or_shared_key(beginner_panel):
    widget, _, _ = beginner_panel
    widget.new_profile()
    values = widget.profile_values()
    assert values['provider'] == 'gemini' and values['auth_mode'] == 'api_key'
    assert values['model'] == 'gemini-3.5-flash-lite'
    assert not values['enabled'] and not values['external_consent']
    assert not values['credential_ref'] and not values['allowed_evidence']
    assert values['options']['max_requests'] == 5


def test_one_click_runs_the_complete_connection_flow_and_updates_the_screen(beginner_panel, app):
    widget, manager, pending = beginner_panel
    assert hasattr(widget, 'quick_connect'), 'One-click connection is missing'
    widget.quick_connect()
    assert len(pending) == 1
    work, done = pending.pop()
    result = asyncio.run(work(__import__('threading').Event(), lambda event: None))
    done(result)
    app.processEvents()
    assert manager.selected_profile_id() != 'legacy-local'
    assert manager.get_profile(manager.selected_profile_id()).model == 'gpt-6.1-sol'
    assert '연결 완료' in widget.quick_summary.text()
    assert not widget._operation_active


def test_chatgpt_login_in_details_also_finishes_model_selection(beginner_panel, app):
    widget, manager, pending = beginner_panel
    widget.new_profile()
    widget.provider_combo.setCurrentIndex(widget.provider_combo.findData('openai'))
    widget.auth_combo.setCurrentIndex(widget.auth_combo.findData('chatgpt'))
    widget.login()
    assert len(pending) == 1
    work, done = pending.pop()
    done(asyncio.run(work(__import__('threading').Event(), lambda event: None)))
    app.processEvents()
    assert manager.selected_profile_id() != 'legacy-local', 'Logging in must also activate the working GPT model'


def test_reopening_verified_connection_shows_saved_capabilities_and_edit_invalidates_them(
    beginner_panel,
):
    from recheck.setup_flow import ChatGPTSetup

    widget, manager, _ = beginner_panel
    profile = ChatGPTSetup(manager).prepare_profile()
    manager.cached_capabilities = lambda pid: {
        'profile_fingerprint': profile.fingerprint(),
        'model': profile.model,
        'basic': True,
        'json_schema': True,
        'tools': True,
        'actual_inference_verified': True,
        'proof_persisted': True,
    }
    widget.reload_profiles(profile.profile_id)
    assert '응답 확인됨' in widget.support_note.text()
    assert '기본 응답 가능' in widget.capability_summary.text()
    widget.model_combo.setEditText('another-model')
    assert '응답 확인됨' not in widget.support_note.text()
    assert '다시 확인' in widget.capability_summary.text()
