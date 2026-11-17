"""Immutable saved problem inputs produce new comparisons, never edits to their source run."""
import base64
import copy
import hashlib
import importlib
import json
from pathlib import Path

import pytest

from recheck.models import Settings
from recheck.storage import Store, new_id
from test_analysis_modes import configured


def library(root):
    assert importlib.util.find_spec('recheck.regression'), 'Regression case storage is missing'
    return importlib.import_module('recheck.regression').RegressionLibrary(root)


def observation(data=b'old', **changes):
    value = dict(status='success', exit_code=0, stdout_b64=base64.b64encode(data).decode(), stderr_b64='')
    value.update(changes)
    return value


@pytest.fixture
def source(tmp_path):
    store = Store(tmp_path / 'data')
    run = dict(run_id=new_id('run'), attempt_id=new_id('attempt'), status='partial',
               mode='dynamic', analysis_mode='dynamic', ai_mode='off', stages={},
               files={'original': {'name': 'original.bin', 'path': str(tmp_path / 'original.bin'),
                                   'sha256': 'a' * 64},
                      'reimplementation': {'name': 'changed.bin', 'path': str(tmp_path / 'changed.bin'),
                                           'sha256': 'b' * 64}},
               tests=[{'id': 'same'}, {'id': 'different', 'args': ['100'], 'stdin': 'hello\n',
                                       'env': {'LANGUAGE': 'ko'}, 'timeout_seconds': 7},
                      {'id': 'timeout'}, {'id': 'missing'}],
               settings={**Settings().model_dump(), 'secret_private_marker': 'not part of a suite'},
               dynamic=[dict(test_id='same', original=observation(), reimplementation=observation()),
                        dict(test_id='different', original=observation(), reimplementation=observation(b'new')),
                        dict(test_id='timeout', original=observation(timed_out=True), reimplementation=observation())],
               behavior={'tests': [{'test_id': 'different', 'match': True}]})
    store.save(run)
    return store, run


def test_save_recomputes_real_problem_cases_and_preserves_original_bytes(source):
    store, run = source
    original = (store.run_dir(run['run_id']) / 'result.json').read_bytes()
    cases = library(store.root)
    suite = cases.save_from_run(run['run_id'], name='문제 다시 확인')
    assert [test['id'] for test in suite['tests']] == ['different', 'timeout', 'missing']
    assert suite['cases'][0]['baseline']['match'] is False
    assert suite['cases'][1]['baseline']['execution_status'] == 'unmeasured'
    assert suite['source_run_id'] == run['run_id']
    assert suite['source_files']['original']['sha256'] == 'a' * 64
    assert suite['comparison_policy'] == {'normalize_newlines': False, 'trim_trailing_whitespace': False}
    assert 'secret_private_marker' not in json.dumps(suite)
    loaded = cases.load_suite(suite['suite_id'])
    assert loaded == suite
    test = cases.prepare_tests(suite['suite_id'])[0]
    assert test['args'] == ['100'] and test['stdin'] == 'hello\n'
    assert test['env'] == {'LANGUAGE': 'ko'} and test['timeout_seconds'] == 7
    assert (store.run_dir(run['run_id']) / 'result.json').read_bytes() == original
    assert cases.list_suites()[0]['suite_id'] == suite['suite_id']


@pytest.mark.parametrize('ids', [['same'], ['unknown'], ['different', 'different'], []])
def test_save_rejects_nonproblem_or_ambiguous_selection_without_creating_suite(source, ids):
    store, run = source
    cases = library(store.root)
    with pytest.raises(ValueError):
        cases.save_from_run(run['run_id'], test_ids=ids)
    assert cases.list_suites() == []


def test_static_only_inputs_are_not_saved_as_unexecuted_regressions(source):
    store, run = source
    run['analysis_mode'] = 'static'
    store.save(run)
    with pytest.raises(ValueError, match='동적'):
        library(store.root).save_from_run(run['run_id'])


def test_unknown_comparison_policy_is_not_assumed_to_use_default_byte_comparison(source):
    store, run = source
    run['settings'] = {}
    store.save(run)
    cases = library(store.root)
    with pytest.raises(ValueError, match='정규화'):
        cases.save_from_run(run['run_id'])
    assert cases.list_suites() == []


def test_legacy_recorded_normalization_policy_can_be_preserved(source):
    store, run = source
    run['settings'] = {}
    run['behavior']['normalization'] = {'newlines': True, 'trailing_whitespace': False}
    store.save(run)
    suite = library(store.root).save_from_run(run['run_id'])
    assert suite['comparison_policy'] == {'normalize_newlines': True, 'trim_trailing_whitespace': False}


@pytest.mark.parametrize('extra', [dict(test_id='unregistered'), dict(test_id=['invalid'])])
def test_ambiguous_or_unregistered_observation_ids_are_rejected(source, extra):
    store, run = source
    run['dynamic'].append(dict(extra, original=observation(), reimplementation=observation(b'new')))
    store.save(run)
    with pytest.raises(ValueError, match='입력 ID'):
        library(store.root).save_from_run(run['run_id'])


def add_auxiliary(store, run, tmp_path):
    registered = tmp_path / 'source-input.json'
    registered.write_bytes(b'{"quantity":100}\n')
    expected = hashlib.sha256(registered.read_bytes()).hexdigest()
    run['tests'][1]['aux_files'] = [str(registered)]
    for side in ('original', 'reimplementation'):
        stage = store.run_dir(run['run_id']) / run['attempt_id'] / 'dynamic/different' / side / 'input-retained'
        stage.mkdir(parents=True)
        (stage / registered.name).write_bytes(registered.read_bytes())
        run['dynamic'][1][side]['actual_input'] = {'auxiliary_files': [{'name': registered.name, 'sha256': expected}]}
        run['dynamic'][1][side]['input_staging'] = {'artifact': str(stage)}
    store.save(run)
    return registered, expected


def test_auxiliary_reuses_historical_bytes_after_registered_file_changes(source, tmp_path):
    store, run = source
    registered, expected = add_auxiliary(store, run, tmp_path)
    registered.write_bytes(b'changed after the source run')
    cases = library(store.root)
    suite = cases.save_from_run(run['run_id'], test_ids=['different'])
    prepared = cases.prepare_tests(suite['suite_id'])
    saved = Path(prepared[0]['aux_files'][0])
    assert saved.name == 'source-input.json'
    assert hashlib.sha256(saved.read_bytes()).hexdigest() == expected
    assert saved.is_relative_to(store.root / 'regressions')


def test_auxiliary_tamper_blocks_preparation_and_preserves_suite_metadata(source, tmp_path):
    store, run = source
    add_auxiliary(store, run, tmp_path)
    cases = library(store.root)
    suite = cases.save_from_run(run['run_id'], test_ids=['different'])
    path = Path(cases.prepare_tests(suite['suite_id'])[0]['aux_files'][0])
    original = cases.load_suite(suite['suite_id'])
    path.write_bytes(b'tamper')
    with pytest.raises(ValueError, match='보조'):
        cases.prepare_tests(suite['suite_id'])
    assert cases.load_suite(suite['suite_id']) == original


def test_missing_historical_auxiliary_is_not_replaced_by_changed_current_file(source, tmp_path):
    store, run = source
    registered, _ = add_auxiliary(store, run, tmp_path)
    registered.write_bytes(b'new data')
    for side in ('original', 'reimplementation'):
        stage = Path(run['dynamic'][1][side]['input_staging']['artifact'])
        (stage / registered.name).unlink()
    cases = library(store.root)
    with pytest.raises(ValueError, match='보조'):
        cases.save_from_run(run['run_id'], test_ids=['different'])
    assert cases.list_suites() == []


def test_suite_corruption_is_reported_without_overwriting_the_original(source):
    store, run = source
    cases = library(store.root)
    suite = cases.save_from_run(run['run_id'])
    path = store.root / 'regressions' / suite['suite_id'] / 'suite.json'
    value = json.loads(path.read_text(encoding='utf-8'))
    value['tests'][0]['stdin'] = 'changed after save'
    path.write_text(json.dumps(value), encoding='utf-8')
    damaged = path.read_bytes()
    with pytest.raises(ValueError):
        cases.load_suite(suite['suite_id'])
    assert cases.list_suites()[0]['status'] == 'unreadable'
    assert path.read_bytes() == damaged


@pytest.mark.asyncio
async def test_rerun_uses_normal_dynamic_engine_new_record_and_explicit_pair_meaning(configured, monkeypatch):
    engine, artifact = configured
    calls = []
    class Runtime:
        async def prepare_image(self, *args, **kwargs):
            return dict(status='success', image='fixture', image_id='sha256:fixture')
        async def execute(self, binary, test, *args, **kwargs):
            calls.append(test['id'])
            return observation(b'new' if 'reimplementation' in str(binary) else b'old')
    monkeypatch.setattr(engine, '_execution_adapter', lambda run: Runtime())
    source_run = await engine.analyze(str(artifact), str(artifact), [{'id': 'case-100', 'args': ['100']}],
                                      analysis_mode='dynamic', ai_mode='off')
    cases = library(engine.store.root)
    suite = cases.save_from_run(source_run['run_id'])
    old_bytes = (engine.store.run_dir(source_run['run_id']) / 'result.json').read_bytes()
    def forbidden():
        pytest.fail('Regression checks must not request AI or static tools')
    monkeypatch.setattr(engine, '_client', forbidden)
    monkeypatch.setattr(engine, '_ghidra', forbidden)
    rerun = await engine.recheck_regression(suite['suite_id'], str(artifact), str(artifact))
    assert rerun['run_id'] != source_run['run_id']
    assert rerun['analysis_mode'] == 'dynamic' and rerun['ai_mode'] == 'off'
    assert rerun['regression']['suite_id'] == suite['suite_id']
    assert rerun['regression']['source_run_id'] == source_run['run_id']
    assert rerun['regression']['case_results'][0]['status'] == 'still_different'
    assert rerun['regression']['policy'] == 'pair_equivalence'
    assert rerun['regression']['reference_changed'] is False
    assert calls == ['case-100'] * 4
    assert (engine.store.run_dir(source_run['run_id']) / 'result.json').read_bytes() == old_bytes
    assert engine.store.load_run(rerun['run_id'])['regression'] == rerun['regression']
    assert suite['suite_id'] in engine.report(rerun['run_id']).read_text(encoding='utf-8')


@pytest.mark.asyncio
async def test_rerun_does_not_call_engine_when_comparison_policy_changed(source, monkeypatch):
    from recheck.engine import Engine
    store, run = source
    cases = library(store.root)
    suite = cases.save_from_run(run['run_id'])
    engine = Engine(Settings(normalize_newlines=True), store.root)
    async def forbidden(*args, **kwargs):
        pytest.fail('A policy change must not turn an old mismatch into an unmarked resolution')
    monkeypatch.setattr(engine, 'analyze', forbidden)
    with pytest.raises(ValueError, match='정규화'):
        await engine.recheck_regression(suite['suite_id'], 'original', 'changed')


def test_recheck_distinguishes_equal_pair_from_original_reference_behavior(source):
    store, run = source
    cases = library(store.root)
    suite = cases.save_from_run(run['run_id'], test_ids=['different'])
    new = copy.deepcopy(run)
    new['files']['original']['sha256'] = 'c' * 64
    new['dynamic'] = [dict(test_id='different', input=copy.deepcopy(suite['tests'][0]),
                           original=observation(b'new'), reimplementation=observation(b'new'))]
    result = cases.recheck_result(suite, new)
    assert result['reference_changed'] is True
    assert result['case_results'][0]['status'] == 'resolved'
    assert result['policy'] == 'pair_equivalence'
    assert result['counts'] == {'resolved': 1, 'still_different': 0, 'unmeasured': 0}


def test_recheck_missing_and_duplicate_actual_observations_are_unmeasured(source):
    store, run = source
    cases = library(store.root)
    suite = cases.save_from_run(run['run_id'], test_ids=['different', 'missing'])
    new = copy.deepcopy(run)
    new['dynamic'] = [dict(test_id='different', original=observation(), reimplementation=observation())] * 2
    result = cases.recheck_result(suite, new)
    assert [case['status'] for case in result['case_results']] == ['unmeasured', 'unmeasured']
    assert result['counts'] == {'resolved': 0, 'still_different': 0, 'unmeasured': 2}


def test_recheck_changed_auxiliary_is_not_a_resolved_saved_input(source, tmp_path):
    store, run = source
    add_auxiliary(store, run, tmp_path)
    cases = library(store.root)
    suite = cases.save_from_run(run['run_id'], test_ids=['different'])
    new = copy.deepcopy(run)
    new['dynamic'] = [dict(test_id='different', original=observation(), reimplementation=observation())]
    result = cases.recheck_result(suite, new)
    assert result['case_results'][0]['status'] == 'unmeasured'
    assert result['case_results'][0]['saved_input_reproduced'] is False


@pytest.mark.parametrize('field,value', [
    ('args', ['changed']), ('stdin', 'changed'), ('env', {'LANGUAGE': 'en'}),
    ('timeout_seconds', 8), ('limits', {'memory_mb': 512}),
])
@pytest.mark.parametrize('location', ['registered', 'row', 'actual'])
def test_recheck_different_input_with_same_id_is_unmeasured(source, field, value, location):
    store, run = source
    cases = library(store.root)
    suite = cases.save_from_run(run['run_id'], test_ids=['different'])
    new = copy.deepcopy(run)
    test = copy.deepcopy(suite['tests'][0])
    row = dict(test_id='different', input=copy.deepcopy(test),
               original=observation(), reimplementation=observation())
    new.update(tests=[test], dynamic=[row])
    if location == 'registered':
        test[field] = value
    elif location == 'row':
        row['input'][field] = value
    else:
        for side in ('original', 'reimplementation'):
            row[side]['actual_input'] = dict(args=['100'], stdin='hello\n', env={'LANGUAGE': 'ko'},
                                             auxiliary_files=[])
            row[side]['limits'] = dict(memory_mb=256, cpus=1, output_bytes=1048576, timeout_seconds=7)
        if field in ('timeout_seconds', 'limits'):
            row['reimplementation']['limits'].update(
                {'timeout_seconds': value} if field == 'timeout_seconds' else value)
            if field == 'limits':
                suite['tests'][0]['limits'] = {'memory_mb': 256}
                test['limits'] = {'memory_mb': 256}
                row['input']['limits'] = {'memory_mb': 256}
        else:
            row['reimplementation']['actual_input'][field] = value
    outcome = cases.recheck_result(suite, new)
    assert outcome['case_results'][0]['status'] == 'unmeasured'
    assert outcome['case_results'][0]['saved_input_reproduced'] is False
    assert outcome['counts'] == {'resolved': 0, 'still_different': 0, 'unmeasured': 1}


@pytest.mark.parametrize('missing', ['registered', 'row'])
def test_recheck_missing_registered_or_row_input_cannot_claim_reproduction(source, missing):
    store, run = source
    cases = library(store.root)
    suite = cases.save_from_run(run['run_id'], test_ids=['different'])
    new = copy.deepcopy(run)
    new['dynamic'] = [dict(test_id='different', input=copy.deepcopy(suite['tests'][0]),
                           original=observation(), reimplementation=observation())]
    if missing == 'registered':
        new['tests'] = []
    else:
        del new['dynamic'][0]['input']
    assert cases.recheck_result(suite, new)['case_results'][0]['status'] == 'unmeasured'


def test_recheck_equivalent_defaults_and_changed_descriptions_preserve_input(source):
    store, run = source
    cases = library(store.root)
    suite = cases.save_from_run(run['run_id'], test_ids=['timeout'])
    new = copy.deepcopy(run)
    new['tests'] = [dict(id='timeout', args=[], stdin='', env={}, timeout_seconds=5,
                         limits={'memory_mb': None}, description='new explanation',
                         verification_status='executed')]
    new['dynamic'] = [dict(test_id='timeout', input={'id': 'timeout'},
                           original=observation(), reimplementation=observation())]
    outcome = cases.recheck_result(suite, new)
    assert outcome['counts'] == {'resolved': 1, 'still_different': 0, 'unmeasured': 0}
    assert outcome['case_results'][0]['saved_input_reproduced'] is True


def test_recheck_matching_actual_provenance_preserves_saved_input(source):
    store, run = source
    cases = library(store.root)
    suite = cases.save_from_run(run['run_id'], test_ids=['different'])
    new = copy.deepcopy(run)
    observed = observation(actual_input=dict(args=['100'], stdin='hello\n', env={'LANGUAGE': 'ko'},
                                            auxiliary_files=[]),
                           limits=dict(memory_mb=256, cpus=1, output_bytes=1048576, timeout_seconds=7))
    new['dynamic'] = [dict(test_id='different', input=copy.deepcopy(suite['tests'][0]),
                           original=copy.deepcopy(observed), reimplementation=copy.deepcopy(observed))]
    outcome = cases.recheck_result(suite, new)
    assert outcome['counts'] == {'resolved': 1, 'still_different': 0, 'unmeasured': 0}
    assert outcome['case_results'][0]['saved_input_reproduced'] is True


@pytest.mark.parametrize('location', ['registered', 'row', 'actual'])
def test_recheck_unexpected_auxiliary_file_is_not_the_saved_input(source, location):
    store, run = source
    cases = library(store.root)
    suite = cases.save_from_run(run['run_id'], test_ids=['different'])
    new = copy.deepcopy(run)
    test = copy.deepcopy(suite['tests'][0])
    row = dict(test_id='different', input=copy.deepcopy(test),
               original=observation(), reimplementation=observation())
    new.update(tests=[test], dynamic=[row])
    if location == 'registered':
        test['aux_files'] = ['extra.dat']
    elif location == 'row':
        row['input']['aux_files'] = ['extra.dat']
    else:
        row['reimplementation']['actual_input'] = dict(
            args=['100'], stdin='hello\n', env={'LANGUAGE': 'ko'},
            auxiliary_files=[dict(name='extra.dat', sha256='a' * 64)])
    outcome = cases.recheck_result(suite, new)
    assert outcome['counts'] == {'resolved': 0, 'still_different': 0, 'unmeasured': 1}
    assert outcome['case_results'][0]['saved_input_reproduced'] is False


def test_recheck_changed_recorded_policy_cannot_claim_resolution(source):
    store, run = source
    cases = library(store.root)
    suite = cases.save_from_run(run['run_id'], test_ids=['different'])
    new = copy.deepcopy(run)
    new['settings']['normalize_newlines'] = True
    new['dynamic'] = [dict(test_id='different', input=copy.deepcopy(suite['tests'][0]),
                           original=observation(), reimplementation=observation())]
    assert cases.recheck_result(suite, new)['case_results'][0]['status'] == 'unmeasured'


@pytest.mark.asyncio
async def test_unreproduced_saved_input_makes_requested_recheck_partial(source, tmp_path, monkeypatch):
    from recheck.engine import Engine
    store, source_run = source
    add_auxiliary(store, source_run, tmp_path)
    suite = library(store.root).save_from_run(source_run['run_id'], test_ids=['different'])
    engine = Engine(Settings(), store.root)
    result = copy.deepcopy(source_run)
    result.update(run_id=new_id('run'), attempt_id=new_id('attempt'), status='success',
                  tests=[{'id': 'different'}], evidence=[], events=[],
                  dynamic=[dict(test_id='different', original=observation(), reimplementation=observation())])
    async def observed(*args, **kwargs):
        # External execution boundary; wrapper provenance/store/report stay real.
        engine.store.save(result)
        return result
    monkeypatch.setattr(engine, 'analyze', observed)
    outcome = await engine.recheck_regression(suite['suite_id'], 'reference', 'fixed')
    assert outcome['regression']['counts']['unmeasured'] == 1
    assert outcome['status'] == 'partial'
    assert outcome['analysis_result_status'] == 'success'
    assert engine.store.load_run(outcome['run_id'])['status'] == 'partial'
