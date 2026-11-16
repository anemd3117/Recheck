"""Selected analysis prerequisites, without installing or running a target."""

import importlib
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest


def readiness(mode, environment, **kwargs):
    assert importlib.util.find_spec('recheck.analysis_setup'), 'Analysis prerequisites are missing'
    return importlib.import_module('recheck.analysis_setup').analysis_readiness(
        mode, environment, **kwargs)


def test_static_ready_does_not_require_docker_or_ai():
    result = readiness('static', {'ghidra': {'ready': True, 'backend': 'direct'}})
    assert result['can_analyze'] is True
    assert result['blockers'] == []
    assert [row['id'] for row in result['requirements']] == ['static']


def test_dynamic_ready_does_not_require_static_tools_or_ai():
    result = readiness('dynamic', {'docker': {'ready': True}})
    assert result['can_analyze'] is True
    assert [row['id'] for row in result['requirements']] == ['dynamic']


def test_both_keeps_missing_dynamic_requirement_instead_of_downgrading():
    result = readiness('both', {
        'ghidra': {'ready': True, 'backend': 'direct'},
        'docker': {'ready': False, 'status': 'not_installed'},
    })
    assert result['status'] == 'blocked'
    assert result['can_analyze'] is False
    assert [row['id'] for row in result['blockers']] == ['dynamic']
    assert result['blockers'][0]['can_install'] is True
    assert 'Docker' in result['blockers'][0]['guidance']


def test_unchecked_environment_is_unknown_not_missing_or_ready():
    result = readiness('both', {})
    assert result['status'] == 'unknown'
    assert result['ready'] is None
    assert result['can_analyze'] is False
    assert result['blockers'] == []
    assert len(result['unknown']) == 2


def test_omitted_scope_probe_stays_unknown_when_the_user_changes_scope():
    result = readiness('dynamic', {'docker': {'status': 'not_requested', 'ready': False}})
    assert result['status'] == 'unknown'
    assert result['blockers'] == []
    assert result['unknown'][0]['can_install'] is False


def test_direct_static_missing_tools_guide_ghidra_and_compatible_jdk():
    result = readiness('static', {'ghidra': {'ready': False, 'backend': 'direct'}},
                       static_backend='direct')
    blocker = result['blockers'][0]
    assert blocker['can_install'] is True
    assert 'Ghidra' in blocker['guidance'] and 'JDK' in blocker['guidance']
    assert blocker['can_configure'] is True


def test_rea_static_missing_worker_guides_managed_environment_not_host_java():
    result = readiness('static', {'ghidra': {'ready': False, 'backend': 'rea'}})
    guidance = result['blockers'][0]['guidance']
    assert 'Docker' in guidance and '분석 도구 내려받아 준비' in guidance
    assert 'JDK를 설치' not in guidance


def test_missing_execution_image_is_environment_preparation_not_docker_reinstall():
    result = readiness('dynamic', {'docker': {
        'ready': False, 'engine_ready': True,
        'managed_environment': {'status': 'not_ready'},
    }})
    assert '분석 도구 내려받아 준비' in result['blockers'][0]['guidance']
    assert '재설치' not in result['blockers'][0]['guidance']


@pytest.mark.parametrize('probe', [
    {'ready': True, 'local_endpoint': False, 'os': 'linux', 'arch': 'amd64'},
    {'ready': True, 'local_endpoint': True, 'os': 'windows', 'arch': 'amd64'},
    {'ready': True, 'local_endpoint': True, 'os': 'linux', 'arch': 'arm64'},
])
def test_connected_docker_must_match_the_supported_local_execution_environment(probe):
    result = readiness('dynamic', {'docker': probe})
    assert result['can_analyze'] is False
    assert result['blockers'][0]['kind'] == 'tool'
    assert result['blockers'][0]['can_install'] is False
    assert result['blockers'][0]['can_configure'] is True


@pytest.mark.parametrize('mode, info', [
    ('dynamic', {'name': 'library.dll', 'execution_supported': False,
                 'execution_reason': 'DLL은 직접 실행할 수 없습니다.'}),
    ('static', {'name': 'unknown.bin', 'static_supported': False,
                'static_reason': '지원하지 않는 파일 형식입니다.'}),
    ('dynamic', {'name': 'console.exe', 'execution_supported': True,
                 'os': 'windows', 'execution_validation': 'unverified'}),
])
def test_unsupported_input_is_not_presented_as_a_missing_installation(mode, info):
    result = readiness(mode, {'ghidra': {'ready': True, 'backend': 'rea'},
                              'docker': {'ready': True}}, files=[info])
    assert result['can_analyze'] is False
    assert len(result['blockers']) == 1
    assert result['blockers'][0]['kind'] == 'input'
    assert result['blockers'][0]['can_install'] is False


def test_dotnet_requires_rea_even_when_direct_ghidra_is_ready():
    result = readiness('static', {'ghidra': {'ready': True, 'backend': 'direct'}},
                       files=[{'name': 'managed.dll', 'static_supported': True,
                               'analysis_kind': 'managed'}])
    assert result['can_analyze'] is False
    assert 'REA' in result['blockers'][0]['guidance']
    assert result['blockers'][0]['can_configure'] is True


def test_panel_mode_updates_do_not_install_or_check_until_user_acts():
    assert importlib.util.find_spec('recheck.analysis_setup'), 'Analysis prerequisites are missing'
    from PySide6.QtWidgets import QApplication
    from shiboken6 import delete
    from recheck.analysis_setup import AnalysisSetupPanel

    app = QApplication.instance() or QApplication([])
    actions = []
    panel = AnalysisSetupPanel(check_environment=lambda: actions.append('check'),
                               install_tools=lambda: actions.append('install'),
                               open_settings=lambda: actions.append('settings'))
    try:
        panel.update_state('static', {'ghidra': {'ready': False, 'backend': 'direct'}})
        panel.update_state('dynamic', {'docker': {'ready': False, 'status': 'not_installed'}})
        assert actions == []
        assert panel.install_button.isEnabled()
        panel.install_button.click()
        panel.check_button.click()
        assert actions == ['install', 'check']
        panel.set_busy(True)
        panel.check_button.click()
        panel.install_button.click()
        assert actions == ['install', 'check']
    finally:
        panel.close()
        delete(panel)
        app.processEvents()


def test_panel_unsupported_files_offer_no_installation_fix():
    assert importlib.util.find_spec('recheck.analysis_setup'), 'Analysis prerequisites are missing'
    from PySide6.QtWidgets import QApplication
    from shiboken6 import delete
    from recheck.analysis_setup import AnalysisSetupPanel

    app = QApplication.instance() or QApplication([])
    panel = AnalysisSetupPanel(check_environment=lambda: None, install_tools=lambda: None)
    try:
        state = panel.update_state('dynamic', {'docker': {'ready': True}}, files=[{
            'name': '<bad.dll>', 'execution_supported': False,
            'execution_reason': 'DLL은 독립 실행 파일이 아닙니다.',
        }])
        assert state['can_analyze'] is False
        assert not panel.install_button.isEnabled()
        assert '<bad.dll>' in panel.details.text()
        assert 'DLL' in panel.details.text()
    finally:
        panel.close()
        delete(panel)
        app.processEvents()
