"""Selected analysis scope controls execution and setup without changing AI profiles."""

from test_gui import app, window, settle
from recheck.gui import DEFAULT_TESTS, dump
import asyncio
import threading
import pytest


def select_scope(window, scope):
    assert hasattr(window, 'analysis_mode'), 'Analysis scope must be independent of AI mode'
    window.analysis_mode.setCurrentIndex(window.analysis_mode.findData(scope))


def test_static_selection_hides_execution_inputs_without_changing_ai_profile(window):
    before = window.settings.model_dump()
    select_scope(window, 'static')
    assert window.inputs_group.isHidden()
    assert window.ai_explanation.isChecked()
    assert window.settings.model_dump() == before
    select_scope(window, 'dynamic')
    assert not window.inputs_group.isHidden()
    select_scope(window, 'both')
    assert not window.inputs_group.isHidden()


def test_default_scope_is_both_and_ai_can_be_disabled_independently(window):
    assert hasattr(window, 'analysis_mode'), 'The visible selector needs three analysis scopes'
    assert window.analysis_mode.currentData() == 'both'
    window.ai_explanation.setChecked(False)
    assert window.analysis_mode.currentData() == 'both'
    assert not window.mode.isEnabled()
    window.ai_explanation.setChecked(True)
    assert window.mode.isEnabled()


def test_scope_selection_is_visible_while_ai_detail_drawer_is_closed(window):
    select_scope(window, 'static')
    assert not window.analysis_scope_panel.isHidden()
    assert not window.analysis_setup.isHidden()
    assert not window.analysis_options.findChild(type(window.start_button)).isChecked()


@pytest.mark.parametrize('scope,ai,expected_ai', [('static', False, 'off'), ('dynamic', True, 'explain')])
def test_gui_request_captures_scope_and_ai_separately(window, app, monkeypatch, scope, ai, expected_ai):
    select_scope(window, scope)
    window.ai_explanation.setChecked(ai)
    window.original.path.setText('original')
    window.reimplementation.path.setText('changed')
    window.tests_editor.setPlainText('invalid JSON ignored only in static mode' if scope == 'static' else dump(DEFAULT_TESTS))
    captured = []

    class Engine:
        async def analyze(self, original, rebuilt, tests, **kwargs):
            captured.append((tests, kwargs))
            return {'status': 'success', 'run_id': 'mode-request'}

    monkeypatch.setattr(window, 'persist_settings', lambda: True)
    monkeypatch.setattr(window, 'engine_for', lambda settings, emit: Engine())
    # Preflight owns external tool probes; this test covers the normal request boundary.
    async def ready(settings, cancel, emit, analysis_mode, paths):
        return {'can_analyze': True, 'environment': {}, 'files': []}
    monkeypatch.setattr(window, 'analysis_preflight', ready)
    monkeypatch.setattr(window, 'show_result', lambda result: None)
    window.start_analysis()
    settle(app, lambda: bool(captured))
    tests, options = captured[0]
    assert options['analysis_mode'] == scope
    assert options['ai_mode'] == expected_ai
    if scope == 'static':
        assert tests == []
    else:
        assert len(tests) == 1 and tests[0]['id'] == 'baseline'
        assert tests[0]['args'] == [] and tests[0]['stdin'] == ''


def test_missing_tools_stop_before_target_execution_and_keep_selected_scope(window, app, monkeypatch):
    select_scope(window, 'both')
    window.original.path.setText('original')
    window.reimplementation.path.setText('changed')
    monkeypatch.setattr(window, 'persist_settings', lambda: True)
    ran = []

    class Engine:
        async def analyze(self, *args, **kwargs):
            ran.append(True)
            raise AssertionError('Missing prerequisites must stop before target analysis')

    monkeypatch.setattr(window, 'engine_for', lambda settings, emit: Engine())
    async def blocked(settings, cancel, emit, analysis_mode, paths):
        return {'can_analyze': False, 'environment': {'docker': {'ready': False},
                'ghidra': {'ready': False}}, 'files': []}
    monkeypatch.setattr(window, 'analysis_preflight', blocked)
    window.start_analysis()
    settle(app, lambda: not window._task_join_pending)
    assert ran == []
    assert window.analysis_mode.currentData() == 'both'
    assert window.analysis_setup.isVisibleTo(window.pages.widget(1))
    assert '도구' in window.banner.text() or '설치' in window.banner.text()


def test_result_labels_excluded_dynamic_as_unverified(window):
    window.show_result({'run_id': 'static-result', 'status': 'success',
                       'analysis_mode': 'static', 'ai_mode': 'off',
                       'analysis_status': {'static': 'success', 'dynamic': 'not_requested', 'ai': 'not_requested'}})
    assert '정적분석' in window.result_title.text()
    assert '동적분석: 선택하지 않음' in window.result_title.text()
    assert '동작 미검증' in window.result_title.text()


def test_ai_off_home_guidance_does_not_demand_an_ai_connection(window):
    window.ai_explanation.setChecked(False)
    assert window._guide_action == 'prepare_tools'


def test_preflight_explains_missing_file_without_suggesting_tool_install(window, tmp_path, monkeypatch):
    class Engine:
        async def environment(self, *, cancel, analysis_mode, include_ai):
            assert analysis_mode == 'static' and include_ai is False
            return {'ghidra': {'ready': True, 'backend': 'direct'}}
    monkeypatch.setattr(window, 'engine_for', lambda settings, emit: Engine())
    result = asyncio.run(window.analysis_preflight(
        window.settings, threading.Event(), lambda event: None, 'static',
        (str(tmp_path / 'missing-input.bin'), '')))
    assert result['can_analyze'] is False
    blocker = result['blockers'][0]
    assert 'missing-input.bin' in blocker['name']
    assert result['files'][0]['reason'] in blocker['guidance']
    assert blocker['can_install'] is False
