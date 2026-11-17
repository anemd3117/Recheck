"""Developer GUI verification uses normal review/save/prepare/recheck handlers."""
import base64
import copy
import importlib
import json

import pytest

from recheck.models import Settings, inspect_binary
from recheck.storage import Store
from test_core import elf_header
from test_gui import app, settle
from review_fixtures import issue_run


def verification():
    assert importlib.util.find_spec('recheck.review_verification'), 'Review verification hook is missing'
    return importlib.import_module('recheck.review_verification')


def config_file(tmp_path, **changes):
    binary = tmp_path / 'fixed'
    binary.write_bytes(elf_header())
    config = dict(run_id='run_' + 'a' * 32, output_dir='review-output', reimplementation='fixed')
    config.update(changes)
    path = tmp_path / 'review-config.json'
    path.write_text(json.dumps(config), encoding='utf-8')
    return path


def test_review_config_resolves_paths_and_rejects_reused_output(tmp_path):
    module = verification()
    path = config_file(tmp_path)
    config = module.load_review_scenario(path)
    assert config['output_dir'] == str(tmp_path / 'review-output')
    assert config['reimplementation'] == str(tmp_path / 'fixed')
    (tmp_path / 'review-output').mkdir()
    with pytest.raises(ValueError):
        module.load_review_scenario(path)


@pytest.mark.parametrize('changes', [dict(run_id='../outside'), dict(run_id=True),
                                    dict(output_dir=None), dict(unexpected_action='execute')])
def test_review_config_rejects_unknown_or_invalid_actions(tmp_path, changes):
    with pytest.raises(ValueError):
        verification().load_review_scenario(config_file(tmp_path, **changes))


def test_review_config_rejects_non_elf_execution_targets(tmp_path):
    module = verification()
    path = config_file(tmp_path)
    (tmp_path / 'fixed').write_bytes(b'not an execution target')
    with pytest.raises(ValueError, match='Linux'):
        module.load_review_scenario(path)


def test_review_result_does_not_accept_stale_behavior_or_unmeasured_as_resolved():
    module = verification()
    source = issue_run()
    suite = dict(suite_id='suite_' + 'b' * 32, source_run_id=source['run_id'],
                 comparison_policy=dict(normalize_newlines=False, trim_trailing_whitespace=False),
                 tests=[{'id': 'changed'}])
    result = copy.deepcopy(source)
    result.update(run_id='run_' + 'b' * 32, ai_mode='off', static={}, llm={}, agent={})
    result['tests'] = [{'id': 'changed'}]
    result['dynamic'] = [source['dynamic'][1]]
    result['behavior'] = dict(denominator=1, matched=1, match_rate=1)
    result['regression'] = dict(suite_id=suite['suite_id'], source_run_id=source['run_id'],
                                case_results=[dict(test_id='changed', status='resolved')])
    with pytest.raises(ValueError):
        module.verify_recheck_result(source, suite, result)


@pytest.mark.parametrize('field,value', [('args', ['changed']), ('stdin', 'changed'),
                                        ('env', {'LANGUAGE': 'en'}), ('timeout_seconds', 8)])
def test_review_proof_rejects_changed_input_despite_cached_resolved_claim(field, value):
    source = issue_run()
    test = dict(id='changed', args=['100'], stdin='saved', env={'LANGUAGE': 'ko'}, timeout_seconds=7)
    suite = dict(suite_id='suite_' + 'b' * 32, name='saved input', source_run_id=source['run_id'],
                 source_files=copy.deepcopy(source['files']),
                 comparison_policy=dict(normalize_newlines=False, trim_trailing_whitespace=False),
                 tests=[copy.deepcopy(test)],
                 cases=[dict(test_id='changed', baseline={}, auxiliary=[])])
    result = copy.deepcopy(source)
    result.update(run_id='run_' + 'b' * 32, status='success', tests=[test], static={}, llm={}, agent={})
    result['dynamic'] = [dict(test_id='changed', input=copy.deepcopy(test),
                              original=copy.deepcopy(source['dynamic'][0]['original']),
                              reimplementation=copy.deepcopy(source['dynamic'][0]['original']))]
    result['tests'][0][field] = value
    result['regression'] = dict(suite_id=suite['suite_id'], source_run_id=source['run_id'],
                                policy='pair_equivalence', reference_changed=False,
                                case_results=[dict(test_id='changed', status='resolved', saved_input_reproduced=True)])
    with pytest.raises(ValueError):
        verification().verify_recheck_result(source, suite, result)


def test_normal_gui_review_exports_and_rechecks_new_record(app, tmp_path, monkeypatch):
    module = verification()
    from recheck.engine import Engine
    from recheck.gui import MainWindow
    from shiboken6 import delete

    data = tmp_path / 'data'
    monkeypatch.setenv('RECHECK_DATA_DIR', str(data))
    binary = tmp_path / 'fixed'
    binary.write_bytes(elf_header())
    source = issue_run()
    source['files'] = {side: inspect_binary(binary) for side in ('original', 'reimplementation')}
    store = Store(data)
    store.save(source)
    before = (store.run_dir(source['run_id']) / 'result.json').read_bytes()
    async def environment(self, cancel=None, **kwargs):
        return dict(docker=dict(ready=True), ghidra=dict(status='not_requested'))
    class Runtime:
        async def prepare_image(self, *args, **kwargs):
            return dict(status='success', image='fixture', image_id='sha256:fixture')
        async def execute(self, *args, **kwargs):
            return dict(status='success', exit_code=0, stdout_b64=base64.b64encode(b'fixed').decode(), stderr_b64='')
    def forbidden(self):
        pytest.fail('Review verification must not contact AI or static tools')
    monkeypatch.setattr(Engine, 'environment', environment)
    monkeypatch.setattr(Engine, '_execution_adapter', lambda self, run: Runtime())
    monkeypatch.setattr(Engine, '_client', forbidden)
    monkeypatch.setattr(Engine, '_ghidra', forbidden)
    exits = []
    monkeypatch.setattr(app, 'exit', lambda code: exits.append(code))
    window = MainWindow(Settings(), auto_check=False)
    window.show()
    output = tmp_path / 'review-output'
    try:
        controller = module.start_review_verification(app, window, dict(
            run_id=source['run_id'], output_dir=str(output), reimplementation=str(binary)))
        settle(app, lambda: bool(exits), timeout=12)
        proof = json.loads((output / 'review-proof.json').read_text(encoding='utf-8'))
        assert exits == [0] and proof['status'] == 'verified'
        assert all(proof['checks'].values())
        assert proof['new_run_id'] != source['run_id']
        assert proof['source_run_id'] == source['run_id']
        assert proof['regression']['counts'] == {'resolved': 2, 'still_different': 0, 'unmeasured': 0}
        assert proof['source_coverage']['dynamic']['registered'] == 3
        assert proof['source_coverage']['dynamic']['measured'] == 2
        assert proof['rechecked_coverage']['dynamic']['registered'] == 2
        assert proof['rechecked_coverage']['dynamic']['measured'] == 2
        assert len(list(output.glob('failures-*.json'))) == 2
        assert len(list(output.glob('failures-*.html'))) == 2
        assert (output / 'source-coverage.png').is_file()
        assert (output / 'recheck-prefill.png').is_file()
        assert (store.run_dir(source['run_id']) / 'result.json').read_bytes() == before
        assert controller.finished
    finally:
        if window.worker and window.worker.isRunning():
            window.cancel_work()
            window.worker.wait(5000)
        window.close()
        app.processEvents()
        delete(window)
        app.processEvents()
