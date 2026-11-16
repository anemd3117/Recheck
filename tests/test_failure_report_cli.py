"""CLI exports remain read-only and requested-operation failures remain failures."""

import asyncio
import json
from pathlib import Path

import pytest

from recheck import cli
from recheck.models import Settings
from recheck.storage import Store
from test_failure_report import run_fixture
from test_regression import source, library, observation
from test_analysis_modes import configured


def test_failure_report_cli_exports_a_new_html_without_constructing_engine(tmp_path, monkeypatch, capsys):
    data = tmp_path / 'results'
    store = Store(data)
    record = run_fixture()
    store.save(record)
    before = (store.run_dir(record['run_id']) / 'result.json').read_bytes()
    destination = tmp_path / 'failures.html'

    def forbidden(*args, **kwargs):
        pytest.fail('Failure export must not construct an analysis engine or read AI settings')
    monkeypatch.setattr(cli, 'Engine', forbidden)
    monkeypatch.setattr(cli, '_selected_settings', forbidden)
    code = cli.main(['--data-dir', str(data), 'failure-report', record['run_id'],
                     '--output', str(destination), '--format', 'html'])
    report = json.loads(capsys.readouterr().out)
    assert code == 0 and report['status'] == 'success'
    assert Path(report['report']) == destination
    assert '동작 불일치' in destination.read_text(encoding='utf-8')
    assert (store.run_dir(record['run_id']) / 'result.json').read_bytes() == before


def test_failure_report_cli_existing_output_is_unchanged(tmp_path, monkeypatch, capsys):
    data = tmp_path / 'results'
    store = Store(data)
    record = run_fixture()
    store.save(record)
    destination = tmp_path / 'failures.json'
    destination.write_bytes(b'keep previous report')
    code = cli.main(['--data-dir', str(data), 'failure-report', record['run_id'],
                     '--output', str(destination), '--format', 'json'])
    assert code == 2
    assert destination.read_bytes() == b'keep previous report'
    assert json.loads(capsys.readouterr().out)['status'] == 'failed'


def test_cli_failed_reinterpret_returns_operation_failure_and_preserves_original_analysis(monkeypatch, tmp_path, capsys):
    class Engine:
        def __init__(self, *args, **kwargs):
            pass

        async def reinterpret(self, run_id, cancel):
            return dict(run_id=run_id, status='success', analysis_status={'static': 'success', 'ai': 'not_requested'},
                        last_operation={'kind': 'reinterpret', 'operation_id': 'op_' + 'a' * 32,
                                        'status': 'failed', 'error_code': 'evidence_context'})

    monkeypatch.setattr(cli, 'Engine', Engine)
    monkeypatch.setattr(cli, '_selected_settings', lambda args: Settings())
    assert cli.main(['reinterpret', 'run_' + 'a' * 32]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'failed'
    assert result['analysis_result_status'] == 'success'
    assert result['code'] == 'evidence_context'
    assert result['operation']['kind'] == 'reinterpret'


def test_failed_preparation_keeps_safe_cause_and_next_action_without_raw_error(monkeypatch, tmp_path, capsys):
    from recheck import runtime_source

    async def build(settings, **kwargs):
        return dict(status='blocked', ready=False,
                    message=r'Docker unavailable. api_key=PRIVATE-KEY C:\Users\private-person\log.txt',
                    environment={'ready': False, 'status': 'not_installed',
                                 'error': 'PRIVATE-KEY'})
    monkeypatch.setattr(runtime_source, 'build_analysis_environment', build)
    monkeypatch.setattr(cli, '_selected_settings', lambda args: Settings())
    assert cli.main(['prepare-tools', '--build']) == 2
    result = json.loads(capsys.readouterr().out)
    assert result['code'] == 'docker_not_installed'
    assert 'Docker' in result['message']
    assert result['next_actions']
    assert '보고서' not in result['message']
    assert 'PRIVATE-KEY' not in json.dumps(result)
    assert 'private-person' not in json.dumps(result)


def test_cli_regression_save_and_list_preserve_inputs_without_echoing_them(source, monkeypatch, capsys):
    store, record = source
    before = (store.run_dir(record['run_id']) / 'result.json').read_bytes()
    def forbidden(*args, **kwargs):
        pytest.fail('Saving/listing a regression must not construct an analysis engine')
    monkeypatch.setattr(cli, 'Engine', forbidden)
    code = cli.main(['--data-dir', str(store.root), 'regression-save', record['run_id'],
                     '--test-id', 'different', '--name', 'repeat check'])
    saved = json.loads(capsys.readouterr().out)
    assert code == 0 and saved['case_count'] == 1
    assert 'hello' not in json.dumps(saved)
    suite = library(store.root).load_suite(saved['suite_id'])
    assert suite['tests'][0]['stdin'] == 'hello\n'
    assert cli.main(['--data-dir', str(store.root), 'regression-list']) == 0
    records = json.loads(capsys.readouterr().out)
    assert records[0]['suite_id'] == suite['suite_id']
    assert (store.run_dir(record['run_id']) / 'result.json').read_bytes() == before


def test_cli_regression_run_creates_new_dynamic_result_and_returns_safe_summary(configured, monkeypatch, capsys):
    engine, artifact = configured
    class Runtime:
        async def prepare_image(self, *args, **kwargs):
            return dict(status='success', image='fixture', image_id='sha256:fixture')
        async def execute(self, binary, test, *args, **kwargs):
            return observation(b'new' if 'reimplementation' in str(binary) else b'old')
    monkeypatch.setattr(engine, '_execution_adapter', lambda run: Runtime())
    initial = asyncio.run(engine.analyze(str(artifact), str(artifact), [{'id': 'case-100', 'args': ['100']}],
                                        analysis_mode='dynamic', ai_mode='off'))
    suite = library(engine.store.root).save_from_run(initial['run_id'])
    old_bytes = (engine.store.run_dir(initial['run_id']) / 'result.json').read_bytes()
    monkeypatch.setattr(cli, 'Engine', lambda *args, **kwargs: engine)
    monkeypatch.setattr(cli, '_selected_settings', lambda args: engine.settings)
    assert cli.main(['regression-run', suite['suite_id'], '--original', str(artifact),
                     '--reimplementation', str(artifact)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['run_id'] != initial['run_id']
    assert result['regression']['suite_id'] == suite['suite_id']
    assert result['regression']['case_results'][0]['status'] == 'still_different'
    assert 'settings' not in result and 'dynamic' not in result
    assert (engine.store.run_dir(initial['run_id']) / 'result.json').read_bytes() == old_bytes
