import asyncio
import os
import threading
import zipfile

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest
from PySide6.QtWidgets import QApplication
from shiboken6 import delete
from recheck.gui import MainWindow
from recheck.models import Settings, atomic_json, load_settings


@pytest.fixture
def window(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setenv('RECHECK_DATA_DIR', str(tmp_path))
    settings = Settings()
    atomic_json(tmp_path / 'settings.json', settings.model_dump())
    widget = MainWindow(settings, auto_check=False)
    yield widget
    if getattr(widget, 'package_dialog', None):
        widget.package_dialog.close()
    widget.close()
    delete(widget)
    app.processEvents()


def test_package_mode_compares_files_without_tests_settings_or_ai(window, tmp_path, monkeypatch):
    left, right = tmp_path / 'old.zip', tmp_path / 'new.apk'
    for path, value in ((left, 'old'), (right, 'new')):
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('config.json', value)
    index = window.product_mode.findData('package')
    assert index >= 0, 'Users need a package comparison mode in the desktop app'
    window.product_mode.setCurrentIndex(index)
    window.original.path.setText(str(left))
    window.reimplementation.path.setText(str(right))
    window.tests_editor.setPlainText('not test JSON')
    monkeypatch.setattr(window, 'persist_settings', lambda: pytest.fail('Package comparison must not alter AI settings'))
    pending = []
    monkeypatch.setattr(window, 'launch_task', lambda title, work, done: pending.append((work, done)))
    window.start_analysis()
    assert len(pending) == 1
    result = asyncio.run(pending[0][0](window.settings, threading.Event(), lambda event: None))
    assert result['comparison']['changed'] == ['config.json']
    pending[0][1](result)
    assert window.package_result['scope'] == 'package_inventory'
    assert window.package_dialog.isVisible()
    assert window.run_result == {}
    window.reimplementation.path.setText(str(tmp_path / 'another.zip'))
    window.follow_next_step()
    assert len(pending) == 2, 'New inputs must compare again instead of showing the previous result'


def test_fresh_home_opens_gemini_key_guide_without_starting_network(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setenv('RECHECK_DATA_DIR', str(tmp_path))
    settings = load_settings(root=tmp_path)
    window = MainWindow(settings, auto_check=False)
    opened = []
    monkeypatch.setattr(window.provider_panel, 'open_gemini_setup', lambda: opened.append(True))
    window.provider_panel._launcher = lambda *args: pytest.fail('Missing key must open help before any network worker')
    try:
        window.follow_next_step()
        assert opened == [True]
    finally:
        window.close()
        delete(window)
        app.processEvents()


def test_embedded_gemini_connect_synchronizes_saved_metadata_before_worker(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setenv('RECHECK_DATA_DIR', str(tmp_path))
    settings = load_settings(root=tmp_path)
    atomic_json(tmp_path / 'settings.json', settings.model_dump())
    window = MainWindow(settings, auto_check=False)
    panel = window.provider_panel
    pending = []
    panel._launcher = lambda title, work, done: pending.append((work, done)) or True
    panel.gemini_api_key.setText('local-test-fixture-not-a-real-key')
    panel.gemini_free_confirm.setChecked(True)
    panel.gemini_evidence_consent.setChecked(True)
    try:
        panel.connect_gemini()
        assert len(pending) == 1, panel.status.text()
        assert window.settings == window.provider_manager.settings
        assert not panel.gemini_api_key.text()
    finally:
        panel._operation_active = False
        window.close()
        delete(window)
        app.processEvents()


def test_home_gemini_guide_targets_active_profile_instead_of_hidden_detail_selection(window):
    from recheck.gemini_setup import new_free_profile

    active = window.provider_manager.save_profile({**new_free_profile(profile_id='active-gemini').model_dump(), 'enabled': True})
    other = window.provider_manager.save_profile(new_free_profile(profile_id='other-gemini'))
    window.provider_manager.select_profile(active.profile_id)
    window.provider_settings_changed()
    window.provider_panel.reload_profiles(other.profile_id)
    window.follow_next_step()
    assert window.provider_panel._current_id == active.profile_id
