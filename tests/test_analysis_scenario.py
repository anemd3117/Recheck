"""Developer verification follows the visible scope and checks persisted evidence."""

import json

import pytest

from test_gui import app, window, settle
from recheck.gui import DEFAULT_TESTS, load_verification_scenario, verification_artifacts
from recheck.report import render_report


def scenario_file(tmp_path, **options):
    (tmp_path / 'original').write_bytes(b'header fixture')
    (tmp_path / 'changed').write_bytes(b'header fixture')
    config = dict(original='original', reimplementation='changed', tests=DEFAULT_TESTS,
                  output_dir='evidence', **options)
    path = tmp_path / 'verification-config.json'
    path.write_text(json.dumps(config), encoding='utf-8')
    return path


def test_scenario_accepts_explicit_static_without_execution_inputs(tmp_path):
    path = scenario_file(tmp_path, analysis_mode='static', ai_mode='off')
    config = json.loads(path.read_text(encoding='utf-8'))
    config['tests'] = []
    path.write_text(json.dumps(config), encoding='utf-8')
    scenario = load_verification_scenario(path)
    assert scenario['analysis_mode'] == 'static'
    assert scenario['ai_mode'] == 'off'
    assert scenario['tests'] == []


@pytest.mark.parametrize('key,value', [
    ('analysis_mode', 'automatic'), ('analysis_mode', None), ('analysis_mode', True),
    ('ai_mode', 'automatic'), ('ai_mode', None), ('ai_mode', {}),
])
def test_scenario_rejects_invalid_scope_and_ai_before_starting(tmp_path, key, value):
    path = scenario_file(tmp_path, **{key: value})
    with pytest.raises(ValueError):
        load_verification_scenario(path)


def store_static_result(window, tmp_path):
    run = dict(run_id='static-scenario', attempt_id='static-attempt', status='success',
               analysis_mode='static', ai_mode='off', mode='automatic', files={},
               analysis_status={'static': 'success', 'dynamic': 'not_requested', 'ai': 'not_requested'},
               behavior={'matched': 0, 'denominator': 0, 'match_rate': None, 'tests': []},
               dynamic=[], tests=[])
    folder = tmp_path / 'runs' / run['run_id']
    folder.mkdir(parents=True)
    (folder / 'result.json').write_text(json.dumps(run), encoding='utf-8')
    render_report(run, folder / 'report.html')
    window.show_result(run)
    return run, folder


def test_static_artifact_checker_accepts_unselected_report_rate_and_measured_gui_absence(window, tmp_path):
    store_static_result(window, tmp_path)
    checks = verification_artifacts(window)
    assert checks['gui_match_rate'] == '미측정'
    assert checks['analysis_mode'] == 'static'
    assert checks['ai_mode'] == 'off'
    assert checks['analysis_status'] == {'static': 'success', 'dynamic': 'not_requested', 'ai': 'not_requested'}


def test_static_artifact_checker_rejects_contradictory_execution_observations(window, tmp_path):
    run, folder = store_static_result(window, tmp_path)
    run['dynamic'] = [{'test_id': 'unexpected-run'}]
    (folder / 'result.json').write_text(json.dumps(run), encoding='utf-8')
    window.show_result(run)
    with pytest.raises(ValueError):
        verification_artifacts(window)


def test_scenario_records_selected_scope_and_finishes_blocked_preflight_promptly(window, app, tmp_path, monkeypatch):
    from recheck.gui import start_verification_scenario

    original, changed = tmp_path / 'original', tmp_path / 'changed'
    original.write_bytes(b'header fixture')
    changed.write_bytes(b'header fixture')
    exits = []
    monkeypatch.setattr(app, 'exit', lambda code: exits.append(code))
    monkeypatch.setattr(window, 'persist_settings', lambda: True)

    async def blocked(settings, cancel, emit, analysis_mode, paths):
        return {'can_analyze': False, 'environment': {'ghidra': {'ready': False}}, 'files': []}

    monkeypatch.setattr(window, 'analysis_preflight', blocked)
    output = tmp_path / 'blocked-scenario'
    start_verification_scenario(app, window, dict(
        original=str(original), reimplementation=str(changed), tests=[], output_dir=str(output),
        analysis_mode='static', ai_mode='off',
    ))
    settle(app, lambda: bool(exits))
    summary = json.loads((output / 'scenario.json').read_text(encoding='utf-8'))
    assert exits == [1]
    assert summary['requested_analysis_mode'] == 'static'
    assert summary['requested_ai_mode'] == 'off'
    assert summary['analysis_mode'] is None
    assert summary['analysis_status'] == {}
    assert summary['timed_out'] is False
    assert summary['preflight']['can_analyze'] is False
    assert (output / 'scenario-setup.png').is_file()
