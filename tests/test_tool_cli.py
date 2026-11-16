"""CLI setup has explicit mutation and keeps diagnostics useful on missing tools."""

import json

import pytest

from recheck import cli


def test_tools_reports_missing_tool_rows_without_creating_analysis_engine(monkeypatch, tmp_path, capsys):
    from recheck import prerequisites
    monkeypatch.setenv('RECHECK_DATA_DIR', str(tmp_path / 'settings'))

    async def inspect(**kwargs):
        return {'status': 'partial', 'packaged': True, 'tools': [
            {'id': 'docker', 'name': 'Docker Desktop', 'status': 'missing', 'optional': False}],
            'winget': {'ready': False}}

    def forbidden(*args, **kwargs):
        pytest.fail('Read-only tool inspection must not instantiate Engine or build images')

    monkeypatch.setattr(prerequisites, 'inspect_prerequisites', inspect)
    monkeypatch.setattr(cli, 'Engine', forbidden)
    code = cli.main(['--data-dir', str(tmp_path / 'results'), 'tools'])
    report = json.loads(capsys.readouterr().out)
    assert code == 2 and report['status'] == 'partial'
    assert report['tools'][0]['id'] == 'docker'
    assert not (tmp_path / 'results').exists()
    assert not (tmp_path / 'settings').exists()


def test_prepare_tools_without_explicit_build_does_not_mutate(monkeypatch, tmp_path, capsys):
    from recheck import runtime_source
    monkeypatch.setenv('RECHECK_DATA_DIR', str(tmp_path / 'settings'))

    async def forbidden(*args, **kwargs):
        pytest.fail('Building without explicit --build must be rejected')

    monkeypatch.setattr(runtime_source, 'build_analysis_environment', forbidden)
    with pytest.raises(SystemExit) as error:
        cli.main(['prepare-tools'])
    assert error.value.code == 2
    assert json.loads(capsys.readouterr().out)['code'] == 'invalid_arguments'
    assert not (tmp_path / 'settings').exists()


@pytest.mark.parametrize('options,local_ai', [([], False), (['--include-local-ai'], True)])
def test_explicit_prepare_builds_selected_tools_and_returns_receipt_result(monkeypatch, tmp_path, capsys, options, local_ai):
    from recheck import runtime_source
    monkeypatch.setenv('RECHECK_DATA_DIR', str(tmp_path / 'settings'))
    calls = []

    async def build(settings, **kwargs):
        calls.append(kwargs)
        return {'status': 'success', 'ready': True, 'origin': 'local_source_build',
                'images': {'static': {'image_id': 'sha256:' + 'a' * 64}}}

    monkeypatch.setattr(runtime_source, 'build_analysis_environment', build)
    result = cli.main(['prepare-tools', '--build', *options])
    report = json.loads(capsys.readouterr().out)
    assert result == 0 and report['ready'] is True
    assert report['images']['static']['image_id'] == 'sha256:' + 'a' * 64
    assert len(calls) == 1 and calls[0]['include_ollama'] is local_ai
    assert calls[0]['cancel'] is not None


def test_failed_prepare_build_returns_failure_exit(monkeypatch, tmp_path, capsys):
    from recheck import runtime_source
    monkeypatch.setenv('RECHECK_DATA_DIR', str(tmp_path / 'settings'))

    async def build(settings, **kwargs):
        return {'status': 'blocked', 'ready': False, 'message': 'Docker not ready'}

    monkeypatch.setattr(runtime_source, 'build_analysis_environment', build)
    assert cli.main(['prepare-tools', '--build']) == 2
    assert json.loads(capsys.readouterr().out)['status'] == 'blocked'
