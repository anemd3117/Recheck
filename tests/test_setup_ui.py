"""First-run workflow fixtures; never scans the PC or starts external services."""

import asyncio
import json
import os
import time

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest
from PySide6.QtWidgets import QApplication
from recheck.gui import MainWindow
from recheck.models import Settings, load_settings


@pytest.fixture
def window(tmp_path, monkeypatch):
    monkeypatch.setenv('RECHECK_DATA_DIR', str(tmp_path))
    app = QApplication.instance() or QApplication([])

    class Engine:
        def __init__(self, settings, on_event=None):
            self.store = type('Store', (), {'root': tmp_path, 'list_runs': lambda self: []})()

    widget = MainWindow(Settings(), engine_factory=Engine, auto_check=False)
    yield widget, app
    if widget.worker and widget.worker.isRunning():
        widget.cancel_work()
        widget.worker.wait(5000)
    widget.close()
    app.processEvents()


def setup_result():
    return dict(
        status='partial',
        patch={'ghidra_path': 'C:/Fixture/Ghidra'},
        components={
            'ghidra_path': dict(
                current='', proposed='C:/Fixture/Ghidra', status='found', reason='설치 폴더 확인'
            )
        },
        warnings=[],
    )


def test_first_run_shortcuts_and_result_empty_state(window, monkeypatch):
    widget, _ = window
    assert not widget.result_empty.isHidden()
    assert widget.result_content.isHidden()
    pending = []
    monkeypatch.setattr(
        widget.provider_panel,
        '_launcher',
        lambda title, work, done: pending.append((work, done)) or True,
    )
    widget.compare_shortcut.click()
    assert widget.navigation.currentRow() == 5
    assert widget.settings_tabs.currentWidget() is widget.provider_panel
    assert len(pending) == 1
    widget.provider_panel._operation_active = False
    widget.benchmark_shortcut.click()
    assert widget.navigation.currentRow() == 5
    assert widget.settings_tabs.currentIndex() == 1
    widget.show_result({'run_id': 'run_fixture', 'status': 'partial'})
    assert widget.result_empty.isHidden()
    assert not widget.result_content.isHidden()


def test_finding_paths_never_saves_without_explicit_apply(window, tmp_path, monkeypatch):
    from recheck import setup_discovery

    widget, app = window

    async def discover(settings, cancel, on_progress):
        on_progress('설치 위치 확인 중')
        await asyncio.sleep(0.01)
        return setup_result()

    monkeypatch.setattr(setup_discovery, 'discover_setup', discover)
    widget.setup_shortcut.click()
    until = time.monotonic() + 3
    while widget.worker.isRunning() and time.monotonic() < until:
        app.processEvents()
        time.sleep(0.005)
    app.processEvents()
    assert not widget.worker.isRunning()
    assert widget.navigation.currentRow() == 5
    assert widget.setup_result['patch']['ghidra_path'] == 'C:/Fixture/Ghidra'
    assert not (tmp_path / 'settings.json').exists()
    monkeypatch.setattr(widget.performance_panel, 'confirm', lambda *a: False)
    widget.apply_setup()
    assert not (tmp_path / 'settings.json').exists()


def test_approved_setup_is_backed_up_persistent_and_restorable(window, tmp_path, monkeypatch):
    widget, _ = window
    widget._setup_inputs = widget.collect_settings().model_dump()
    widget.setup_done(setup_result())
    monkeypatch.setattr(widget.performance_panel, 'confirm', lambda *a: True)
    widget.apply_setup()
    assert load_settings().ghidra_path == 'C:/Fixture/Ghidra'
    assert widget.setting_fields['ghidra_path'].text() == 'C:/Fixture/Ghidra'
    backups = list((tmp_path / 'settings-history').glob('backup_*.json'))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text(encoding='utf-8'))['action'] == 'setup'
    widget.performance_panel.restore_settings()
    assert load_settings().ghidra_path == ''
    assert widget.setting_fields['ghidra_path'].text() == ''


def test_manual_edits_after_scan_and_cancelled_scan_cannot_be_applied(
    window, tmp_path, monkeypatch
):
    widget, _ = window
    widget._setup_inputs = widget.collect_settings().model_dump()
    widget.setup_done(setup_result())
    widget.setting_fields['java_home'].setText('C:/User/Manual')
    confirmations = []
    monkeypatch.setattr(
        widget.performance_panel, 'confirm', lambda *a: confirmations.append(True) or True
    )
    widget.apply_setup()
    assert '설정이 바뀌었습니다' in widget.banner.text()
    assert confirmations == []
    assert not (tmp_path / 'settings.json').exists()
    widget.setup_done(dict(setup_result(), status='cancelled'))
    assert widget.setup_apply.isHidden()
    widget.apply_setup()
    assert not (tmp_path / 'settings.json').exists()


def test_save_and_recheck_persists_edits_before_checking(window, monkeypatch):
    widget, _ = window
    widget.setting_fields['memory_mb'].setValue(768)
    observed = []
    monkeypatch.setattr(
        widget, 'check_environment', lambda: observed.append(load_settings().memory_mb)
    )
    widget.save_and_check_environment()
    assert observed == [768]


def test_failed_save_does_not_start_environment_check(window, monkeypatch):
    widget, _ = window
    checked = []
    monkeypatch.setattr(widget, 'persist_settings', lambda: False)
    monkeypatch.setattr(widget, 'check_environment', lambda: checked.append(True))
    widget.save_and_check_environment()
    assert checked == []


def test_first_use_missing_images_opens_installation_instead_of_repeating_import(window, monkeypatch):
    widget, _ = window
    widget.environment_done({
        'ollama': {'connected': False},
        'docker': {'ready': False, 'engine_ready': True},
        'ghidra': {'ready': False},
    })
    widget._ai_ready = True
    opened, imported = [], []
    monkeypatch.setattr(widget, 'open_tool_installation', lambda: opened.append(True))
    monkeypatch.setattr(widget, 'find_setup', lambda **kwargs: imported.append(kwargs))
    widget.follow_next_step()
    assert opened == [True]
    assert imported == []
