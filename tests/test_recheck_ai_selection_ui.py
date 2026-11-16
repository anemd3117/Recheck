"""Recheck analysis-provider UI: fake engines and managers, no external calls."""

import os
import asyncio
import threading
import time
import subprocess
import sys

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtWidgets import QApplication, QLineEdit
from shiboken6 import delete

from recheck.gui import MainWindow
from recheck.models import Settings
from ai_providers.contracts import ProviderProfile
from recheck.provider_ui import ProviderPanel


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


class Manager:
    def __init__(self):
        self.calls = []
        self.selected = 'legacy-local'
        self.profiles = {
            'legacy-local': ProviderProfile(
                profile_id='legacy-local', model='fixture-local', enabled=True
            ),
            'remote': ProviderProfile(
                profile_id='remote',
                name='API fixture',
                provider='openai',
                auth_mode='api_key',
                endpoint='https://api.openai.com/v1',
                model='direct-model',
                structured_mode='native_schema',
            ),
        }

    def list_profiles(self):
        return list(self.profiles.values())

    def get_profile(self, pid):
        return self.profiles[pid]

    def selected_profile_id(self, role='analysis'):
        return self.selected

    def save_profile(self, profile):
        value = ProviderProfile.model_validate(profile)
        self.profiles[value.profile_id] = value
        self.calls.append(('save', value.profile_id))
        return value

    def select_profile(self, pid, role='analysis'):
        self.selected = pid
        self.calls.append(('select', pid, role))
        return self.get_profile(pid)

    def account_status(self, pid):
        self.calls.append(('account', pid))
        return {'authenticated': False}

    def save_secret(self, pid, secret):
        self.calls.append(('save_secret', pid, len(secret)))
        value = self.get_profile(pid).model_copy(update={'credential_ref': 'cred_' + 'a' * 32})
        self.profiles[pid] = value
        return value

    async def probe_connection(self, pid, cancel=None, on_event=None):
        self.calls.append(('connection', pid, cancel.is_set()))
        return {'connected': True}

    async def list_models(self, pid, cancel=None, on_event=None):
        self.calls.append(('models', pid))
        return [{'id': 'listed-model'}]

    async def probe_capabilities(self, pid, cancel=None, on_event=None):
        self.calls.append(('inference', pid))
        return {'basic': True, 'json_schema': True, 'tools': False}


@pytest.fixture
def panel(app):
    manager = Manager()
    widget = ProviderPanel(manager)
    yield widget, manager
    widget.cancel_operation()
    if widget._worker and widget._worker.isRunning():
        widget._worker.wait(3000)
    app.processEvents()
    widget.close()
    app.processEvents()
    delete(widget)
    app.processEvents()


def remote(widget):
    widget.profile_combo.setCurrentIndex(widget.profile_combo.findData('remote'))


def idle(widget, app):
    until = time.monotonic() + 3
    while widget._operation_active and time.monotonic() < until:
        app.processEvents()
        time.sleep(0.005)
    app.processEvents()
    assert not widget._operation_active


def test_new_openai_and_compatible_profiles_default_to_json_prompt_without_overriding_saved_native(
    panel,
):
    widget, manager = panel
    remote(widget)
    assert widget.schema_mode.currentData() == 'native_schema'
    widget.new_profile()
    assert widget.provider_combo.currentData() == 'gemini'
    widget.provider_combo.setCurrentIndex(widget.provider_combo.findData('openai'))
    assert widget.schema_mode.currentData() == 'json_prompt'
    widget.provider_combo.setCurrentIndex(widget.provider_combo.findData('compatible'))
    assert widget.schema_mode.currentData() == 'json_prompt'
    remote(widget)
    assert widget.schema_mode.currentData() == 'native_schema'
    assert manager.calls == []


def test_analysis_only_panel_does_not_probe_or_offer_reconstruction_scope(panel):
    widget, manager = panel
    assert [widget.role_combo.itemData(i) for i in range(widget.role_combo.count())] == ['analysis']
    assert set(widget.evidence_checks) == {'runtime', 'static', 'code'}
    assert manager.calls == []
    remote(widget)
    assert not widget.enabled_check.isChecked()
    assert not widget.external_consent.isChecked()
    assert not widget.test_button.isEnabled()


def test_all_visible_recheck_scopes_authorize_decompiled_function_analysis(panel):
    widget, manager = panel
    remote(widget)
    widget.enabled_check.setChecked(True)
    widget.external_consent.setChecked(True)
    for checkbox in widget.evidence_checks.values():
        checkbox.setChecked(True)
    saved = widget.save_current()
    assert saved is not None
    saved.authorize(('runtime', 'static', 'code'))
    assert set(saved.allowed_evidence) == {'runtime', 'static', 'code'}
    assert 'source' not in widget.evidence_checks
    assert not any(
        call[0] in ('inference', 'connection', 'models', 'account') for call in manager.calls
    )


def test_key_input_is_masked_explicit_and_never_saved_in_profile(panel):
    widget, manager = panel
    remote(widget)
    widget.api_key.setText('synthetic-key-only')
    assert widget.api_key.echoMode() == QLineEdit.EchoMode.Password
    assert manager.calls == []
    widget.save_key()
    assert ('save_secret', 'remote', 18) in manager.calls
    assert widget.api_key.text() == ''
    assert 'synthetic-key-only' not in repr(manager.get_profile('remote').model_dump())


def test_direct_model_selection_uses_analysis_role(panel):
    widget, manager = panel
    remote(widget)
    widget.model_combo.setEditText('my-explicit-model')
    widget.use_current()
    assert ('select', 'remote', 'analysis') in manager.calls
    assert manager.get_profile('remote').model == 'my-explicit-model'


def test_connection_model_list_and_inference_are_separate_manual_operations(panel, app):
    widget, manager = panel
    remote(widget)
    widget.check_connection()
    idle(widget, app)
    widget.refresh_models()
    idle(widget, app)
    assert any(call[0] == 'connection' for call in manager.calls)
    assert any(call[0] == 'models' for call in manager.calls)
    assert not any(call[0] == 'inference' for call in manager.calls)
    widget.enabled_check.setChecked(True)
    widget.external_consent.setChecked(True)
    widget.evidence_checks['runtime'].setChecked(True)
    widget.test_capabilities()
    idle(widget, app)
    assert sum(call[0] == 'inference' for call in manager.calls) == 1
    assert widget._worker.work is None and widget._worker.result is None


def test_old_profile_response_cannot_replace_new_selection(panel, app):
    widget, manager = panel
    remote(widget)

    async def slow(pid, cancel=None, on_event=None):
        await asyncio.sleep(0.03)
        return [{'id': 'obsolete-model'}]

    manager.list_models = slow
    widget.refresh_models()
    widget.profile_combo.setCurrentIndex(widget.profile_combo.findData('legacy-local'))
    idle(widget, app)
    assert widget.model_combo.currentText() == 'fixture-local'
    assert widget.model_combo.findText('obsolete-model') == -1


def test_shared_launcher_preserves_cancel_before_worker_start(panel):
    widget, manager = panel
    remote(widget)
    pending = []
    widget._launcher = lambda title, work, done: pending.append((work, done))
    widget.check_connection()
    widget.cancel_operation()
    cancel = threading.Event()
    work, done = pending[0]
    done(asyncio.run(work(cancel, lambda event: None)))
    assert ('connection', 'remote', True) in manager.calls
    assert not widget._operation_active


@pytest.fixture
def main_window(app, tmp_path, monkeypatch):
    from recheck.models import save_settings

    monkeypatch.setenv('RECHECK_DATA_DIR', str(tmp_path))
    settings = Settings(model='fixture-local')
    save_settings(settings, root=tmp_path)

    class Engine:
        def __init__(self, settings, on_event=None):
            self.settings = settings
            self.store = type('Store', (), {'list_runs': lambda self: []})()

    window = MainWindow(settings, engine_factory=Engine, auto_check=False)
    yield window
    if window.worker and window.worker.isRunning():
        window.worker.cancel_event.set()
        window.worker.wait(5000)
    app.processEvents()
    window.close()
    app.processEvents()
    delete(window)
    app.processEvents()


def settle_window(window, app):
    until = time.monotonic() + 3
    while window._task_join_pending and time.monotonic() < until:
        app.processEvents()
        time.sleep(0.005)
    app.processEvents()
    assert not window._task_join_pending


def test_mainwindow_profile_selection_preserves_legacy_settings_and_routes_remote_controls(
    main_window, monkeypatch
):
    window = main_window
    window.provider_manager.save_profile(
        ProviderProfile(
            profile_id='remote',
            name='Remote fixture',
            provider='compatible',
            auth_mode='none',
            endpoint='https://fixture.example/v1',
            model='custom-model',
            structured_mode='json_prompt',
            enabled=True,
            external_consent=True,
            allowed_evidence=('runtime',),
        )
    )
    window.provider_settings_changed()
    window.provider_panel.reload_profiles('remote')
    window.provider_panel.use_current()
    assert window.settings.active_ai_profile == 'remote'
    assert window.settings.model == 'fixture-local'
    assert (window.settings.num_ctx, window.settings.num_predict) == (8192, 2048)
    assert 'custom-model' in window.selected_provider_summary.text()
    assert not window.model_combo.isEnabled()

    def forbidden(*args, **kwargs):
        raise AssertionError('remote buttons must not contact Ollama')

    monkeypatch.setattr(window, 'with_ollama', forbidden)
    window.refresh_models()
    window.test_model()
    assert window.settings_tabs.currentWidget() is window.provider_panel
    assert window.worker is None


def test_mainwindow_legacy_manual_capability_check_uses_profile_manager(
    main_window, monkeypatch, app
):
    window = main_window
    calls = []
    window.models_done([{'name': 'fixture-local', 'digest': 'fixture-digest'}])

    async def probe(pid, cancel=None, on_event=None):
        calls.append(pid)
        return {
            'model': 'fixture-local',
            'basic': True,
            'tools': True,
            'actual_inference_verified': True,
            'profile_fingerprint': window.provider_manager.get_profile(pid).fingerprint(),
        }

    def forbidden(*args, **kwargs):
        raise AssertionError('manual check must use the selected profile manager')

    monkeypatch.setattr(window.provider_manager, 'probe_capabilities', probe)
    monkeypatch.setattr(window, 'with_ollama', forbidden)
    window.test_model()
    settle_window(window, app)
    assert calls == ['legacy-local']
    assert window.worker.work is None


def test_claude_does_not_offer_subscription_account_login(panel):
    widget, manager = panel
    widget.new_profile()
    widget.provider_combo.setCurrentIndex(widget.provider_combo.findData('anthropic'))
    assert [widget.auth_combo.itemData(i) for i in range(widget.auth_combo.count())] == ['api_key']
    assert not widget.oauth_box.isVisible()


def test_secret_storage_error_does_not_expose_raw_exception(panel):
    widget, manager = panel
    remote(widget)

    def fail(pid, secret):
        raise ValueError('synthetic-secret-raw-exception')

    manager.save_secret = fail
    widget.api_key.setText('synthetic-secret-raw-exception')
    widget.save_key()
    assert 'synthetic-secret-raw-exception' not in widget.status.text()


@pytest.mark.skipif(os.name != 'nt', reason='native Windows Qt lifetime boundary')
def test_native_panel_finishes_before_background_gc_without_thread_destruction(tmp_path):
    script = r'''
import asyncio, gc, threading, time, weakref
from PySide6.QtWidgets import QApplication
from shiboken6 import delete
from ai_providers.contracts import ProviderProfile
from recheck.provider_ui import ProviderPanel

class Manager:
    def __init__(self):
        self.profile = ProviderProfile(profile_id='legacy-local', model='fixture-local')
    def list_profiles(self): return [self.profile]
    def get_profile(self, pid): return self.profile
    def selected_profile_id(self, role='analysis'): return 'legacy-local'
    def save_profile(self, profile):
        self.profile = ProviderProfile.model_validate(profile)
        return self.profile
    async def probe_connection(self, pid, cancel=None, on_event=None):
        await asyncio.sleep(.001)
        return {'connected': True}

app = QApplication([])
for _ in range(15):
    panel = ProviderPanel(Manager())
    panel.check_connection()
    until = time.monotonic() + 3
    while panel._operation_active and time.monotonic() < until:
        app.processEvents()
        time.sleep(.001)
    assert not panel._operation_active
    ref = weakref.ref(panel)
    panel.close()
    delete(panel)
    del panel
    app.processEvents()
    worker = threading.Thread(target=gc.collect)
    worker.start()
    worker.join()
    app.processEvents()
    assert ref() is None
print('native-panel-lifetime-15-ok')
'''
    env = os.environ.copy()
    env['QT_QPA_PLATFORM'] = 'windows'
    env['RECHECK_DATA_DIR'] = str(tmp_path)
    result = subprocess.run(
        [sys.executable, '-c', script], capture_output=True, timeout=25, env=env
    )
    assert result.returncode == 0, result.stderr.decode('utf-8', 'replace')
    assert b'native-panel-lifetime-15-ok' in result.stdout
    assert b'QThread: Destroyed' not in result.stderr
    assert b'Cannot create children' not in result.stderr


def test_mainwindow_opens_analysis_ai_settings_without_ollama(app, tmp_path, monkeypatch):
    monkeypatch.setenv('RECHECK_DATA_DIR', str(tmp_path))

    class Store:
        def list_runs(self):
            return []

    class Engine:
        def __init__(self, settings, on_event=None):
            self.store = Store()

    window = MainWindow(
        Settings(model='fixture-local', num_ctx=8192, num_predict=2048),
        engine_factory=Engine,
        auto_check=False,
    )
    try:
        assert hasattr(
            window, 'provider_panel'
        ), 'Recheck needs an analysis AI provider settings tab'
        assert window.pages.count() == 6
        assert 'Recheck' in window.windowTitle()
        assert 'Rebuild' not in window.windowTitle()
        assert window.provider_panel.role_combo.count() == 1
        assert window.provider_panel.role_combo.currentData() == 'analysis'
        assert window.settings.model == 'fixture-local'
        assert (window.settings.num_ctx, window.settings.num_predict) == (8192, 2048)
        window.open_provider_settings()
        assert window.settings_tabs.currentWidget() is window.provider_panel
    finally:
        if window.worker and window.worker.isRunning():
            window.worker.cancel_event.set()
            window.worker.wait(5000)
        window.close()
        app.processEvents()
        delete(window)
        app.processEvents()
