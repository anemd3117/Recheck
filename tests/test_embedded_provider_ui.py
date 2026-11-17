"""Native onboarding keeps authorization URLs private and scopes browser attempts."""

import asyncio
import os
import threading
import time

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QLineEdit
from shiboken6 import delete

from ai_providers.contracts import ProviderError
from ai_providers.profiles import ProfileManager
from recheck.models import Settings, atomic_json
from recheck.provider_ui import ProviderPanel


class Browser(QObject):
    cancelled = Signal(object)
    notice = Signal(str)
    external_opened = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.calls = []
        self.threads = []
        self.active = False
        self.attempt_id = None

    def open_console(self, provider, url, title):
        self.calls.append(('console', provider, url))
        self.active = True
        self.attempt_id = None
        return True

    def open_authorization(self, url, *, provider, attempt_id):
        self.calls.append(('authorization', attempt_id, url))
        self.threads.append(threading.get_ident())
        self.active = True
        self.attempt_id = attempt_id
        return True

    def finish_attempt(self, attempt_id):
        self.calls.append(('finish', attempt_id))
        if attempt_id == self.attempt_id:
            self.active = False

    def cancel_attempt(self, attempt_id=None):
        self.calls.append(('cancel', attempt_id))
        if attempt_id is None or attempt_id == self.attempt_id:
            self.active = False

    def shutdown(self):
        self.calls.append(('shutdown',))
        self.active = False

    def set_stage(self, text):
        self.calls.append(('stage', text))


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def panel(app, tmp_path):
    initial = Settings(model='existing-local')
    atomic_json(tmp_path / 'settings.json', initial.model_dump())
    manager = ProfileManager(initial, root=tmp_path)
    pending = []
    widget = ProviderPanel(manager, launch_task=lambda title, work, done: pending.append((work, done)) or True,
                           browser_dialog_factory=Browser)
    widget.resize(1050, 900)
    widget.show()
    app.processEvents()
    yield widget, manager, pending, tmp_path
    widget.cancel_operation()
    widget._operation_active = False
    widget.close()
    delete(widget)
    app.processEvents()


def pump(app, condition):
    deadline = time.monotonic() + 4
    while not condition() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    assert condition(), 'GUI handoff did not finish'


def test_provider_console_is_native_and_does_not_store_or_select_any_key(panel):
    widget, manager, pending, _ = panel
    widget.begin_provider_setup('deepseek', open_browser=True)
    assert widget.onboarding_key.echoMode() == QLineEdit.EchoMode.Password
    assert widget._browser_dialog.calls[0] == (
        'console', 'deepseek', 'https://platform.deepseek.com/api_keys')
    assert pending == [] and manager.selected_profile_id() == 'legacy-local'
    assert len(manager.list_profiles()) == 1


def test_gemini_key_page_uses_app_browser_without_opening_system_browser(panel, monkeypatch):
    from PySide6.QtGui import QDesktopServices

    widget, manager, pending, _ = panel
    monkeypatch.setattr(QDesktopServices, 'openUrl', lambda url: pytest.fail('implicit external browser'))
    widget.open_gemini_key_page()
    assert widget._browser_dialog.calls[0] == (
        'console', 'gemini', 'https://aistudio.google.com/apikey')
    assert pending == [] and manager.selected_profile_id() == 'legacy-local'


def test_key_and_consent_are_required_before_creating_a_remote_profile(panel):
    widget, manager, pending, _ = panel
    widget.begin_provider_setup('grok')
    widget.onboarding_key.setText('fixture-key-not-to-log')
    assert widget.connect_selected_preset() is False
    assert widget.onboarding_key.text() == ''
    assert pending == [] and len(manager.list_profiles()) == 1


def test_key_is_bound_to_named_provider_and_cleared_even_when_connection_fails(panel, monkeypatch):
    from recheck.provider_onboarding import APIKeySetup

    widget, manager, pending, folder = panel
    widget.begin_provider_setup('deepseek')
    widget.onboarding_consent.setChecked(True)
    widget.onboarding_key.setText('fixture-deepseek-personal-key')

    async def fail(setup, pid, **kwargs):
        profile = manager.get_profile(pid)
        assert profile.provider == 'compatible' and profile.endpoint == 'https://api.deepseek.com/v1'
        assert profile.credential_ref and profile.external_consent
        raise ProviderError('authentication', 'RAW private?token=fixture-deepseek-personal-key')

    monkeypatch.setattr(APIKeySetup, 'connect', fail)
    assert widget.connect_selected_preset(), widget.status.text()
    assert widget.onboarding_key.text() == ''
    work, done = pending.pop()
    done(asyncio.run(work(threading.Event(), lambda event: None)))
    assert manager.selected_profile_id() == 'legacy-local'
    assert 'fixture-deepseek-personal-key' not in (folder / 'settings.json').read_text(encoding='utf-8')
    assert 'RAW' not in widget.status.text() and 'fixture-' not in widget.status.text()


def test_worker_url_is_presented_on_gui_thread_and_never_sent_to_progress(panel, app, monkeypatch):
    from recheck.setup_flow import ChatGPTSetup

    widget, manager, pending, _ = panel
    public_events = []
    outcomes = []
    entered = threading.Event()
    release = threading.Event()

    async def connect(setup, pid, *, cancel=None, on_event=None, on_url=None):
        entered.set()
        await on_url('https://auth.openai.com/api/accounts/authorize?state=PRIVATE_AUTH_QUERY')
        release.wait(3)
        raise ProviderError('cancelled', 'not displayed')

    monkeypatch.setattr(ChatGPTSetup, 'connect', connect)
    assert widget.quick_connect(), widget.status.text()
    work, done = pending.pop()
    thread = threading.Thread(target=lambda: outcomes.append(asyncio.run(work(threading.Event(), public_events.append))))
    thread.start()
    pump(app, lambda: widget._browser_dialog is not None and bool(widget._browser_dialog.threads))
    assert widget._browser_dialog.threads == [threading.get_ident()]
    assert 'PRIVATE_AUTH_QUERY' not in str(public_events)
    assert 'PRIVATE_AUTH_QUERY' not in widget.status.text()
    widget.cancel_operation()
    release.set()
    thread.join(4)
    assert not thread.is_alive()
    done(outcomes[0])
    assert manager.selected_profile_id() == 'legacy-local'
    assert any(call[0] == 'finish' for call in widget._browser_dialog.calls)


def test_a_cancelled_queued_url_cannot_reopen_browser(panel, app, monkeypatch):
    from recheck.setup_flow import ChatGPTSetup

    widget, _, pending, _ = panel
    entered = threading.Event()
    holder = {}
    outcomes = []

    async def connect(setup, pid, *, cancel=None, on_event=None, on_url=None):
        holder['on_url'] = on_url
        entered.set()
        await on_url('https://auth.openai.com/api/accounts/authorize?state=LATE_PRIVATE')
        return {}

    monkeypatch.setattr(ChatGPTSetup, 'connect', connect)
    assert widget.quick_connect(), widget.status.text()
    work, done = pending.pop()
    thread = threading.Thread(target=lambda: outcomes.append(asyncio.run(work(threading.Event(), lambda event: None))))
    thread.start()
    assert entered.wait(3)
    widget.cancel_operation()
    pump(app, lambda: not thread.is_alive())
    done(outcomes[0])
    assert widget._browser_dialog is None or not any(
        call[0] == 'authorization' for call in widget._browser_dialog.calls)


def test_failed_new_key_preserves_the_selected_profiles_existing_credential(panel, monkeypatch):
    from recheck.provider_onboarding import APIKeySetup

    widget, manager, pending, _ = panel
    setup = APIKeySetup(manager)
    old = setup.prepare_profile('deepseek', consent=True)
    old = manager.save_secret(old.profile_id, 'fixture-working-old-key')
    manager.select_profile(old.profile_id)
    widget.reload_profiles(old.profile_id)
    widget.begin_provider_setup('deepseek')
    widget.onboarding_key.setText('fixture-replacement-key')
    widget.onboarding_consent.setChecked(True)

    async def fail(*args, **kwargs):
        raise ProviderError('authentication', 'not displayed')

    monkeypatch.setattr(APIKeySetup, 'connect', fail)
    assert widget.connect_selected_preset()
    work, done = pending.pop()
    done(asyncio.run(work(threading.Event(), lambda event: None)))
    assert manager.selected_profile_id() == old.profile_id
    assert manager.get_profile(old.profile_id).credential_ref == old.credential_ref
    assert manager.store.get(old.credential_ref, old.profile_id, old.origin).value.get_secret_value() == 'fixture-working-old-key'


def test_starting_key_verification_clears_the_unbound_console_session(panel):
    widget, _, pending, _ = panel
    widget.begin_provider_setup('deepseek', open_browser=True)
    widget.onboarding_key.setText('fixture-key')
    widget.onboarding_consent.setChecked(True)
    assert widget._browser_dialog.active
    assert widget.connect_selected_preset()
    assert not widget._browser_dialog.active
    widget.cancel_operation()
    work, done = pending.pop()
    done(asyncio.run(work(threading.Event(), lambda event: None)))


def test_old_progress_does_not_relabel_a_newly_selected_profile(panel):
    widget, _, pending, _ = panel
    assert widget.quick_connect()
    old_attempt = widget._operation_id
    widget.reload_profiles('legacy-local')
    widget.status.setText('new profile untouched')
    widget.show_setup_progress({'kind': 'oauth_login', 'status': 'callback_validated',
                                'connection_attempt': old_attempt})
    assert widget.status.text() == 'new profile untouched'
    widget.cancel_operation()
    # Do not invoke the network work; an early cancellation completion still releases UI.
    _, done = pending.pop()
    done({'ok': False, 'code': 'cancelled'})


@pytest.mark.parametrize('embedded', [True, False])
def test_login_location_choice_controls_the_presenter_without_changing_the_action(panel, monkeypatch, embedded):
    from recheck.setup_flow import ChatGPTSetup

    widget, _, pending, _ = panel
    observed = []

    async def connect(setup, pid, *, cancel=None, on_event=None, on_url=None):
        observed.append(callable(on_url))
        raise ProviderError('cancelled', 'not displayed')

    monkeypatch.setattr(ChatGPTSetup, 'connect', connect)
    widget.embedded_login_check.setChecked(embedded)
    widget.onboarding_open.click()
    work, done = pending.pop()
    done(asyncio.run(work(threading.Event(), lambda event: None)))
    assert observed == [embedded]
