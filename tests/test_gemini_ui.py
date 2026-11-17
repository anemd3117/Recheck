"""Gemini onboarding uses explicit consent, personal secure keys and verified selection."""

import asyncio
import os
import threading

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtWidgets import QApplication, QLineEdit
from shiboken6 import delete

from ai_providers.contracts import ProviderError
from ai_providers.profiles import ProfileManager
from recheck.models import Settings
from recheck.provider_ui import ProviderPanel


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def panel(app, tmp_path):
    manager = ProfileManager(
        Settings(active_ai_profile='legacy-local', ai_profiles=[]), root=tmp_path
    )
    pending = []
    widget = ProviderPanel(
        manager, launch_task=lambda title, work, done: pending.append((work, done)) or True
    )
    widget.resize(1000, 950)
    widget.show()
    app.processEvents()
    yield widget, manager, pending, tmp_path
    widget.cancel_operation()
    widget._operation_active = False
    widget.close()
    delete(widget)
    app.processEvents()


def consent(widget):
    widget.gemini_free_confirm.setChecked(True)
    widget.gemini_evidence_consent.setChecked(True)


def finish(pending):
    work, done = pending.pop()
    done(asyncio.run(work(threading.Event(), lambda event: None)))


def test_opening_gemini_guide_does_not_create_profiles_or_make_requests(panel):
    widget, manager, pending, folder = panel
    assert hasattr(widget, 'open_gemini_setup'), 'Gemini key onboarding is missing'
    widget.open_gemini_setup()
    assert widget.gemini_api_key.echoMode() == QLineEdit.EchoMode.Password
    assert widget.gemini_card.isVisible() and widget.quick_button.isVisible()
    assert widget.quick_button.parent().y() < widget.gemini_card.y()
    assert widget.gemini_card.y() < widget.onboarding_combo.parent().y()
    assert not widget.gemini_free_confirm.isChecked()
    assert not widget.gemini_evidence_consent.isChecked()
    assert manager.selected_profile_id() == 'legacy-local'
    assert len(manager.list_profiles()) == 1 and pending == []
    assert not (folder / 'settings.json').exists()


@pytest.mark.parametrize('free_confirm,evidence_consent', [(False, False), (True, False), (False, True)])
def test_no_key_storage_or_request_until_both_explicit_confirmations(panel, free_confirm, evidence_consent):
    widget, manager, pending, folder = panel
    widget.gemini_api_key.setText('fixture-user-key')
    widget.gemini_free_confirm.setChecked(free_confirm)
    widget.gemini_evidence_consent.setChecked(evidence_consent)
    assert widget.connect_gemini() is False
    assert widget.gemini_api_key.text() == ''
    assert manager.selected_profile_id() == 'legacy-local'
    assert len(manager.list_profiles()) == 1 and pending == []
    assert not (folder / 'settings.json').exists()


def test_empty_key_is_not_replaced_by_another_profiles_key(panel):
    widget, manager, pending, folder = panel
    consent(widget)
    assert widget.connect_gemini() is False
    assert pending == [] and not (folder / 'settings.json').exists()
    assert '키' in widget.gemini_progress.text()


def test_success_stores_personal_key_securely_and_emits_ready_only_after_verification(panel, monkeypatch):
    from recheck.gemini_setup import GeminiSetup

    widget, manager, pending, folder = panel
    secret = 'fixture-personal-key-only'
    ready, changed, checked = [], [], []
    widget.setup_ready.connect(ready.append)
    widget.profile_changed.connect(changed.append)
    widget.capabilities_checked.connect(checked.append)

    async def connect(setup, pid, **kwargs):
        profile = manager.get_profile(pid)
        assert profile.provider == 'gemini' and profile.credential_ref
        capabilities = dict(basic=True, json_schema=True, tools=False,
                            actual_inference_verified=True, proof_persisted=True)
        profile = manager.select_profile(pid, role='analysis')
        return dict(ready=True, profile=profile.model_dump(), model=profile.model,
                    capabilities=capabilities)

    monkeypatch.setattr(GeminiSetup, 'connect', connect)
    consent(widget)
    widget.gemini_api_key.setText(secret)
    assert widget.connect_gemini() is True
    assert widget.gemini_api_key.text() == ''
    assert manager.selected_profile_id() == 'legacy-local' and ready == []
    assert len(pending) == 1
    finish(pending)
    selected = manager.get_profile(manager.selected_profile_id())
    assert selected.provider == 'gemini' and widget._current_id == selected.profile_id
    assert len(ready) == 1 and checked[0]['actual_inference_verified']
    assert changed[-1].profile_id == selected.profile_id
    assert not widget._operation_active and 'Gemini' in widget.gemini_summary.text()
    assert secret not in (folder / 'settings.json').read_text(encoding='utf-8')
    assert all(secret.encode() not in path.read_bytes()
               for path in (folder / 'credentials').rglob('*') if path.is_file())


@pytest.mark.parametrize('code,expected', [('rate_limit', '한도'), ('authentication', 'API 키'), ('permission', '프로젝트')])
def test_failed_gemini_attempt_preserves_default_and_never_displays_raw_errors(panel, monkeypatch, code, expected):
    from recheck.gemini_setup import GeminiSetup

    widget, manager, pending, _ = panel
    ready = []
    widget.setup_ready.connect(ready.append)

    async def fail(*args, **kwargs):
        raise ProviderError(code, 'RAW fixture-secret private?key=fixture-token')

    monkeypatch.setattr(GeminiSetup, 'connect', fail)
    consent(widget)
    widget.gemini_api_key.setText('fixture-key')
    assert widget.connect_gemini()
    finish(pending)
    assert manager.selected_profile_id() == 'legacy-local' and ready == []
    assert not widget._operation_active
    text = widget.gemini_progress.text() + widget.status.text()
    assert 'Gemini' in text and expected in text
    assert not any(value in text for value in ('RAW', 'fixture-secret', 'fixture-token', 'ChatGPT'))


def test_busy_and_cancel_controls_keep_active_default_and_release_ui(panel, monkeypatch):
    from recheck.gemini_setup import GeminiSetup

    widget, manager, pending, _ = panel

    async def connect(*args, cancel=None, **kwargs):
        assert cancel.is_set()
        raise ProviderError('cancelled', 'not displayed')

    monkeypatch.setattr(GeminiSetup, 'connect', connect)
    consent(widget)
    widget.gemini_api_key.setText('fixture-key')
    assert widget.connect_gemini()
    assert not widget.gemini_connect_button.isEnabled()
    assert not widget.gemini_api_key.isEnabled()
    assert widget.gemini_cancel_button.isEnabled()
    widget.gemini_cancel_button.click()
    finish(pending)
    assert widget.gemini_connect_button.isEnabled()
    assert not widget.gemini_cancel_button.isEnabled()
    assert manager.selected_profile_id() == 'legacy-local'


def test_reuses_key_only_for_explicitly_selected_gemini_profile(panel, monkeypatch):
    from recheck.gemini_setup import GeminiSetup

    widget, manager, pending, _ = panel
    profile = GeminiSetup(manager).prepare_profile(consent=True)
    manager.save_secret(profile.profile_id, 'fixture-saved-key')
    widget.reload_profiles(profile.profile_id)
    consent(widget)

    async def fail(*args, **kwargs):
        raise ProviderError('rate_limit', 'not displayed')

    monkeypatch.setattr(GeminiSetup, 'connect', fail)
    monkeypatch.setattr(manager, 'save_secret', lambda *a, **k: pytest.fail('stored key replaced'))
    assert widget.connect_gemini()
    finish(pending)
    assert widget._current_id == profile.profile_id
    assert manager.selected_profile_id() == 'legacy-local'


def test_failed_new_gemini_key_keeps_the_previously_selected_key(panel, monkeypatch):
    from recheck.gemini_setup import GeminiSetup

    widget, manager, pending, _ = panel
    old = GeminiSetup(manager).prepare_profile(consent=True)
    old = manager.save_secret(old.profile_id, 'fixture-working-gemini-key')
    manager.select_profile(old.profile_id)
    widget.reload_profiles(old.profile_id)

    async def fail(*args, **kwargs):
        raise ProviderError('authentication', 'not displayed')

    monkeypatch.setattr(GeminiSetup, 'connect', fail)
    consent(widget)
    widget.gemini_api_key.setText('fixture-new-gemini-key')
    assert widget.connect_gemini()
    finish(pending)
    assert manager.selected_profile_id() == old.profile_id
    assert manager.get_profile(old.profile_id).credential_ref == old.credential_ref
    assert manager.store.get(old.credential_ref, old.profile_id, old.origin).value.get_secret_value() == 'fixture-working-gemini-key'


def test_key_help_opens_only_the_official_key_creation_page(panel, monkeypatch):
    from PySide6.QtGui import QDesktopServices

    widget, manager, pending, _ = panel
    opened = []
    monkeypatch.setattr(QDesktopServices, 'openUrl', lambda url: opened.append(url.toString()) or True)
    widget.gemini_key_help.click()
    assert opened == ['https://aistudio.google.com/apikey']
    assert widget._browser_dialog is None
    assert pending == [] and manager.selected_profile_id() == 'legacy-local'


def test_small_window_can_scroll_from_key_guide_to_gpt_recommendation(panel, app):
    widget, _, _, _ = panel
    widget.resize(850, 560)
    widget.gemini_help_toggle.setChecked(True)
    app.processEvents()
    assert hasattr(widget, 'setup_scroll'), 'Both setup choices need a shared scrollable viewport'
    assert widget.setup_scroll.verticalScrollBar().maximum() > 0
    widget.setup_scroll.ensureWidgetVisible(widget.quick_button)
    app.processEvents()
    center = widget.quick_button.mapTo(widget.setup_scroll.viewport(), widget.quick_button.rect().center())
    assert widget.setup_scroll.viewport().rect().contains(center)
    widget.open_gemini_setup()
    app.processEvents()
    center = widget.gemini_api_key.mapTo(widget.setup_scroll.viewport(), widget.gemini_api_key.rect().center())
    assert widget.setup_scroll.viewport().rect().contains(center)


def test_new_connection_is_unconnected_gemini_draft_and_does_not_replace_existing_default(panel):
    widget, manager, pending, folder = panel
    widget.new_profile()
    values = widget.profile_values()
    assert values['provider'] == 'gemini' and values['auth_mode'] == 'api_key'
    assert values['model'] == 'gemini-3.5-flash-lite'
    assert not values['enabled'] and not values['external_consent']
    assert not values['credential_ref'] and not any(values['allowed_evidence'])
    assert manager.selected_profile_id() == 'legacy-local' and pending == []
    assert not (folder / 'settings.json').exists()


def test_connecting_a_new_gemini_draft_saves_its_key_before_the_explicit_probe(panel, monkeypatch):
    from recheck.gemini_setup import GeminiSetup

    widget, manager, pending, _ = panel
    widget.new_profile()
    draft_id = widget._current_id
    consent(widget)
    widget.gemini_api_key.setText('fixture-draft-key')

    async def fail(*args, **kwargs):
        raise ProviderError('rate_limit', 'not displayed')

    monkeypatch.setattr(GeminiSetup, 'connect', fail)
    assert widget.connect_gemini()
    assert manager.get_profile(draft_id).credential_ref
    assert manager.selected_profile_id() == 'legacy-local'
    finish(pending)
    assert widget._current_id == draft_id


def test_user_key_edit_requires_confirming_the_new_keys_free_project(panel):
    from PySide6.QtTest import QTest

    widget, _, pending, _ = panel
    consent(widget)
    widget.gemini_api_key.setFocus()
    QTest.keyClicks(widget.gemini_api_key, 'different-key')
    assert not widget.gemini_free_confirm.isChecked()
    assert pending == []


def test_switching_profiles_clears_key_and_free_confirmation(panel):
    widget, _, pending, _ = panel
    consent(widget)
    widget.gemini_api_key.setText('unsaved-key-for-previous-project')
    widget.new_profile()
    assert not widget.gemini_free_confirm.isChecked()
    assert not widget.gemini_api_key.text()
    assert pending == []
