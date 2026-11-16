"""Tool setup clicks never install or build before the user chooses to do so."""

import asyncio
import importlib
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest
from PySide6.QtWidgets import QApplication


@pytest.fixture
def panel():
    assert importlib.util.find_spec('recheck.prerequisite_ui'), 'Tool installation screen is missing'
    module = importlib.import_module('recheck.prerequisite_ui')
    app = QApplication.instance() or QApplication([])
    pending, opened = [], []

    def launch(title, work, done):
        pending.append((work, done))
        return True

    widget = module.ToolSetupPanel(launch=launch, open_url=opened.append)
    yield widget, pending, opened
    widget.close()
    app.processEvents()


def tool(tool_id='docker', **updates):
    value = dict(id=tool_id, name='Docker Desktop', purpose='프로그램 격리 실행',
                 optional=False, required_for='execution',
                 download_url='https://docs.docker.com/desktop/setup/install/windows-install/',
                 status='missing', path='', version='', can_install=True, reason='설치 필요')
    value.update(updates)
    return value


def show_tools(widget, tools):
    widget.inspection_done(dict(status='partial', packaged=True,
                                winget={'ready': True, 'path': 'winget.exe'}, tools=tools))


def test_opening_screen_never_installs_and_known_paths_are_displayed(panel):
    widget, pending, _ = panel
    assert pending == []
    show_tools(widget, [tool(), tool('git', name='Git', optional=True, status='ready',
                                   path='C:/Program Files/Git/cmd/git.exe', can_install=False)])
    assert pending == []
    assert widget.tools.topLevelItemCount() == 2
    assert '설치 필요' in widget.tools.topLevelItem(0).text(1)
    assert 'git.exe' in widget.tools.topLevelItem(1).text(3)


def test_declining_install_never_starts_external_operation(panel, monkeypatch):
    widget, pending, _ = panel
    show_tools(widget, [tool()])
    monkeypatch.setattr(widget, 'confirm', lambda *args: False)
    widget.install_button.click()
    assert pending == []
    assert widget.tools.topLevelItem(0).text(1) == '설치 필요'


def test_installer_reboot_result_is_not_reported_as_ready(panel, monkeypatch):
    widget, pending, _ = panel
    show_tools(widget, [tool()])
    monkeypatch.setattr(widget, 'confirm', lambda *args: True)

    async def install(tool_id, *, cancel, on_event):
        assert tool_id == 'docker'
        return dict(status='reboot_required', tool=tool(), message='PC를 재시작한 뒤 다시 확인하세요.')

    backend = importlib.import_module('recheck.prerequisites')
    monkeypatch.setattr(backend, 'install_prerequisite', install)
    widget.install_button.click()
    assert len(pending) == 1
    assert not widget.install_button.isEnabled()
    work, done = pending.pop()
    done(asyncio.run(work(None, lambda event: None)))
    assert '재시작' in widget.summary.text()
    assert widget.tools.topLevelItem(0).text(1) == '설치 필요'


def test_missing_package_manager_offers_selected_official_download(panel):
    widget, pending, opened = panel
    selected = tool(can_install=False)
    show_tools(widget, [selected])
    assert not widget.install_button.isEnabled()
    widget.download_button.click()
    assert opened == ['https://docs.docker.com/desktop/setup/install/windows-install/']
    assert pending == []


def test_runtime_download_requires_explicit_confirmation_and_no_optional_ai_by_default(panel, monkeypatch):
    widget, pending, _ = panel
    monkeypatch.setattr(widget, 'confirm', lambda *args: False)
    widget.build_button.click()
    assert pending == []
    monkeypatch.setattr(widget, 'confirm', lambda *args: True)
    widget.build_button.click()
    assert len(pending) == 1
    assert not widget.include_ollama.isChecked()


def test_ready_tools_are_never_reinstalled_and_busy_screen_blocks_actions(panel):
    widget, pending, _ = panel
    show_tools(widget, [tool(status='ready', path='docker.exe', can_install=True)])
    assert not widget.install_button.isEnabled()
    widget.set_busy(True)
    assert not widget.refresh_button.isEnabled()
    assert not widget.build_button.isEnabled()
    widget.install_button.click()
    assert pending == []


def test_invalid_settings_show_actionable_message_without_starting_build(panel, monkeypatch):
    widget, pending, _ = panel
    monkeypatch.setattr(widget, 'confirm', lambda *args: True)

    def invalid():
        raise ValueError('invalid private setting details')

    widget.settings = invalid
    widget.build_environment()
    assert pending == []
    assert '설정' in widget.summary.text()
    assert 'private setting' not in widget.summary.text()


def test_long_setup_has_its_own_cancellation_control(panel):
    widget, _, _ = panel
    cancelled = []
    widget.on_cancel = lambda: cancelled.append(True)
    widget.set_busy(True)
    widget.cancel_button.click()
    assert cancelled == [True]
    assert not widget.cancel_button.isEnabled()
