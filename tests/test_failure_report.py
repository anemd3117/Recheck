"""A separate failure export preserves distinctions and omits opaque private data by default."""

import base64
import copy
import importlib
import json

import pytest


def module():
    assert importlib.util.find_spec('recheck.failure_report'), 'Separate failure reporting is missing'
    return importlib.import_module('recheck.failure_report')


def observed(stdout='', stderr='', exit_code=0, **changes):
    return dict(status='exited', stdout=stdout, stderr=stderr,
                stdout_b64=base64.b64encode(stdout.encode()).decode(),
                stderr_b64=base64.b64encode(stderr.encode()).decode(),
                exit_code=exit_code, infrastructure_ok=True, **changes)


def run_fixture():
    return dict(run_id='run_' + 'a' * 32, attempt_id='attempt_' + 'b' * 32,
                mode='dynamic', analysis_mode='dynamic', ai_mode='off', status='success',
                created='2026-11-16T12:00:00+00:00',
                files={'original': {'path': r'C:\Users\private-person\original.exe', 'sha256': 'a' * 64},
                       'reimplementation': {'path': r'C:\Users\private-person\changed.exe', 'sha256': 'b' * 64}},
                tests=[dict(id='different', args=['--secret', 'OPAQUE_PRIVATE_INPUT'], stdin='sensitive stdin',
                            env={'API_KEY': 'DO-NOT-EXPORT-THIS'}, aux_files=[r'C:\Users\private-person\input.dat'],
                            timeout_seconds=5),
                       dict(id='same-nonzero', args=[], stdin='', timeout_seconds=5),
                       dict(id='slow', args=[], stdin='', timeout_seconds=1)],
                dynamic=[dict(test_id='different', original=observed('900'), reimplementation=observed('1000')),
                         dict(test_id='same-nonzero', original=observed('same', 'valid error', 2),
                              reimplementation=observed('same', 'valid error', 2)),
                         dict(test_id='slow', original=observed('', '', None, timed_out=True),
                              reimplementation=observed('', '', None, timed_out=True))],
                stages={'metadata': {'status': 'success'}, 'image': {'status': 'success'},
                        'dynamic': {'status': 'partial'}, 'static_original': {'status': 'blocked', 'reason': 'not requested'},
                        'llm': {'status': 'blocked', 'reason': 'not requested'}},
                settings={'model': 'private-model', 'normalize_newlines': False, 'trim_trailing_whitespace': False,
                          'api_key': 'DO-NOT-EXPORT-THIS',
                          'ai_profiles': [{'credential_ref': 'PRIVATE_CREDENTIAL', 'endpoint': 'https://private.invalid'}]},
                diagnostics=[dict(stage='dynamic', code='timeout', error='DO-NOT-EXPORT-THIS',
                                  resolution=['open C:\\Users\\private-person\\secret.log'])])


def test_failures_separate_mismatch_unmeasured_and_valid_matching_nonzero_exit():
    source = run_fixture()
    before = copy.deepcopy(source)
    report = module().build_failure_report(source)
    assert source == before
    test_issues = [issue for issue in report['issues'] if issue.get('test_id')]
    assert [(row['test_id'], row['category']) for row in test_issues] == [
        ('different', 'behavior_mismatch'), ('slow', 'execution_unmeasured')]
    assert report['counts']['behavior_mismatch'] == 1
    assert report['counts']['execution_unmeasured'] == 1
    assert not any(row.get('stage') == 'llm' or row.get('stage') == 'static_original'
                   for row in report['issues'])
    assert report['source']['run_id'] == source['run_id']
    assert report['source']['files']['original']['sha256'] == 'a' * 64


def test_default_report_has_no_opaque_inputs_outputs_profiles_or_private_paths():
    source = run_fixture()
    source['dynamic'][0]['original'] = observed('OPAQUE_PRIVATE_OUTPUT')
    report = module().build_failure_report(source)
    text = json.dumps(report)
    for secret in ('OPAQUE_PRIVATE_INPUT', 'OPAQUE_PRIVATE_OUTPUT', 'sensitive stdin',
                   'DO-NOT-EXPORT-THIS', 'PRIVATE_CREDENTIAL', 'private.invalid', 'private-person'):
        assert secret not in text
    assert report['privacy']['observations_included'] is False
    assert report['source']['files']['original']['name'] == 'original.exe'
    assert 'settings' not in report


def test_explicit_details_add_only_problem_inputs_and_exact_outputs():
    source = run_fixture()
    source['dynamic'][0]['original'] = observed('<script>alert(1)</script>')
    report = module().build_failure_report(source, include_observations=True)
    issue = next(row for row in report['issues'] if row.get('test_id') == 'different')
    assert issue['input']['stdin'] == 'sensitive stdin'
    assert issue['observations']['original']['stdout']['base64'] == base64.b64encode(b'<script>alert(1)</script>').decode()
    assert report['privacy']['observations_included'] is True
    assert report['privacy']['warning']
    assert 'PRIVATE_CREDENTIAL' not in json.dumps(report)
    html = module().failure_report_html(report)
    assert '<script>alert(1)</script>' not in html
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in html
    assert 'same-nonzero' not in html


def test_static_partial_and_ai_failure_are_separate_from_unrequested_dynamic():
    source = run_fixture()
    source.update(mode='automatic', analysis_mode='static', ai_mode='explain', dynamic=[], tests=[],
                  static={'original': {'status': 'partial', 'complete': False,
                           'functions': [{'id': 'fn1', 'name': 'one', 'decompile': 'int one(){}',
                                          'decompile_status': 'success'},
                                         {'id': 'fn2', 'name': 'two', 'decompile_status': 'not_selected'}]}},
                  stages={'metadata': {'status': 'success'}, 'static_original': {'status': 'partial'},
                          'static_reimplementation': {'status': 'failed'}, 'structure': {'status': 'blocked'},
                          'dynamic': {'status': 'blocked'}, 'llm': {'status': 'failed'}},
                  llm={'status': 'failed', 'error_code': 'quota', 'error': 'DO-NOT-EXPORT-THIS'})
    report = module().build_failure_report(source)
    assert any(row['category'] == 'static_incomplete' and row.get('target') == 'original' for row in report['issues'])
    assert any(row['category'] == 'ai_failure' for row in report['issues'])
    assert not any(row.get('stage') == 'dynamic' for row in report['issues'])
    assert not any(row.get('test_id') for row in report['issues'])


@pytest.mark.parametrize('format,suffix', [('json', 'json'), ('html', 'html')])
def test_export_is_complete_and_never_overwrites_existing_report(tmp_path, format, suffix):
    path = tmp_path / ('failure.' + suffix)
    exported = module().export_failure_report(run_fixture(), path, format=format)
    assert exported == path
    before = path.read_bytes()
    if format == 'json':
        assert json.loads(before)['counts']['behavior_mismatch'] == 1
    else:
        assert '동작 불일치' in before.decode('utf-8')
    with pytest.raises(FileExistsError):
        module().export_failure_report(run_fixture(), path, format=format)
    assert path.read_bytes() == before
    assert not list(tmp_path.glob('*.tmp'))


def test_no_issues_is_explicit_and_does_not_invent_failures():
    source = run_fixture()
    source['tests'] = source['tests'][1:2]
    source['dynamic'] = source['dynamic'][1:2]
    source['stages'] = {'metadata': {'status': 'success'}, 'image': {'status': 'success'}, 'dynamic': {'status': 'success'}}
    report = module().build_failure_report(source)
    assert report['issues'] == []
    assert report['issue_count'] == 0
    assert '보고할 실패·차이 항목이 없습니다' in module().failure_report_html(report)


def test_invalid_raw_observation_is_unmeasured_even_if_saved_verdict_claims_mismatch():
    source = run_fixture()
    source['tests'] = source['tests'][:1]
    source['dynamic'] = source['dynamic'][:1]
    source['dynamic'][0]['original']['stdout_b64'] = '%%%'
    source['behavior'] = {'tests': [{'test_id': 'different', 'match': False, 'execution_status': 'measured'}]}
    report = module().build_failure_report(source)
    assert report['counts']['behavior_mismatch'] == 0
    issue = next(row for row in report['issues'] if row.get('test_id'))
    assert issue['category'] == 'execution_unmeasured'
    assert 'invalid_observation' in issue['reasons']


def test_duplicate_observations_do_not_become_two_measured_mismatches():
    source = run_fixture()
    source['tests'] = source['tests'][:1]
    source['dynamic'] = [source['dynamic'][0], copy.deepcopy(source['dynamic'][0])]
    report = module().build_failure_report(source)
    test_issues = [row for row in report['issues'] if row.get('test_id')]
    assert len(test_issues) == 1
    assert test_issues[0]['category'] == 'execution_unmeasured'
    assert test_issues[0]['reasons'] == ['invalid_observation']


def test_corrupt_identifier_and_status_cannot_leak_private_paths_or_break_export():
    source = run_fixture()
    source['run_id'] = r'C:\Users\private-person\private.db'
    source['status'] = {'error': 'DO-NOT-EXPORT-THIS'}
    source['tests'] = []
    source['dynamic'] = []
    report = module().build_failure_report(source)
    assert report['source']['run_id'] == '<invalid-id>'
    assert report['source']['status'] == 'unrecorded'
    assert 'private-person' not in json.dumps(report)


def test_explicit_failed_reinterpret_is_reported_even_when_initial_ai_was_off():
    source = run_fixture()
    source['tests'] = []
    source['dynamic'] = []
    source['stages'] = {'metadata': {'status': 'success'}}
    source['last_operation'] = {'operation_id': 'op_' + 'a' * 32, 'kind': 'reinterpret',
                                'status': 'failed', 'error_code': 'context_budget'}
    report = module().build_failure_report(source)
    assert report['counts']['ai_failure'] == 1
    issue = report['issues'][0]
    assert issue['operation'] == 'reinterpret'
    assert issue['analysis_result_status'] == 'success'


def test_unregistered_observation_is_never_called_a_measured_mismatch():
    source = run_fixture()
    source['tests'] = source['tests'][1:2]
    source['dynamic'] = source['dynamic'][:1]
    report = module().build_failure_report(source)
    assert report['counts']['behavior_mismatch'] == 0
    foreign = next(row for row in report['issues'] if row.get('test_id') == 'different')
    assert foreign['category'] == 'execution_unmeasured'
    assert 'invalid_observation' in foreign['reasons']


def test_missing_policy_does_not_invent_a_mismatch_and_recorded_policy_is_reused():
    source = run_fixture()
    source['tests'] = source['tests'][:1]
    source['dynamic'] = source['dynamic'][:1]
    source['dynamic'][0]['original'] = observed('line\r\n')
    source['dynamic'][0]['reimplementation'] = observed('line\n')
    source['settings'] = {}
    report = module().build_failure_report(source)
    assert report['counts']['behavior_mismatch'] == 0
    issue = next(row for row in report['issues'] if row.get('test_id'))
    assert issue['category'] == 'execution_unmeasured'
    assert issue['reasons'] == ['comparison_policy_unknown']
    source['behavior'] = {'normalization': {'newlines': True, 'trailing_whitespace': False}}
    report = module().build_failure_report(source)
    assert not any(row.get('test_id') for row in report['issues'])


def test_human_html_shows_problem_cause_actions_and_side_by_side_observations():
    report = module().build_failure_report(run_fixture(), include_observations=True)
    document = module().failure_report_html(report)
    assert '원인·확인할 점' in document
    assert '다음 조치' in document
    assert '<th>원본</th><th>비교할 프로그램</th>' in document
    assert '표준출력' in document and '표준오류' in document and '종료 코드' in document
    assert '<pre>900</pre>' in document and '<pre>1000</pre>' in document
    assert '시간 초과' in document
    assert '"issue_id"' not in document


def test_ai_failure_preserves_allowlisted_cause_without_echoing_provider_error():
    source = run_fixture()
    source.update(analysis_mode='static', ai_mode='explain', dynamic=[], tests=[],
                  stages={'llm': {'status': 'failed', 'reason': 'DO-NOT-EXPORT-THIS'}},
                  llm={'status': 'failed', 'error_code': 'evidence_context', 'error': 'DO-NOT-EXPORT-THIS'})
    report = module().build_failure_report(source)
    issue = next(row for row in report['issues'] if row['category'] == 'ai_failure')
    assert 'evidence_context' in issue['reason_codes']
    assert '문맥' in ' '.join(issue['next_actions'])
    assert 'DO-NOT-EXPORT-THIS' not in json.dumps(report)


def test_regression_input_reproduction_failure_is_unmeasured_even_if_current_pair_matches():
    source = run_fixture()
    source['tests'] = source['tests'][1:2]
    source['dynamic'] = source['dynamic'][1:2]
    source['stages'] = {'dynamic': {'status': 'success'}}
    source['regression'] = {'suite_id': 'suite_' + 'a' * 32,
                            'case_results': [{'test_id': 'same-nonzero', 'status': 'unmeasured',
                                              'saved_input_reproduced': False}]}
    report = module().build_failure_report(source)
    issue = next(row for row in report['issues'] if row.get('test_id'))
    assert issue['category'] == 'execution_unmeasured'
    assert 'saved_input_not_reproduced' in issue['reasons']


def test_large_explicit_details_keep_hashes_and_mark_all_truncation():
    source = run_fixture()
    source['tests'] = source['tests'][:1]
    source['dynamic'] = source['dynamic'][:1]
    source['tests'][0]['args'] = ['x' * 5000]
    source['tests'][0]['stdin'] = 's' * 70000
    source['dynamic'][0]['original'] = observed('a' * (256 * 1024 + 1))
    report = module().build_failure_report(source, include_observations=True)
    issue = next(row for row in report['issues'] if row.get('test_id'))
    assert set(issue['input']['truncated_fields']) == {'args', 'stdin'}
    assert issue['input']['truncated_for_report'] is True
    stream = issue['observations']['original']['stdout']
    assert stream['bytes'] == 256 * 1024 + 1
    assert len(base64.b64decode(stream['base64'])) == 256 * 1024
    assert stream['truncated_for_report'] is True
    import hashlib
    assert stream['sha256'] == hashlib.sha256(b'a' * (256 * 1024 + 1)).hexdigest()


def test_sensitive_profile_data_stays_omitted_even_with_raw_details():
    source = run_fixture()
    report = module().build_failure_report(source, include_observations=True)
    text = json.dumps(report)
    assert 'DO-NOT-EXPORT-THIS' not in text
    assert 'PRIVATE_CREDENTIAL' not in text
    assert 'private.invalid' not in text
    assert 'private-person' not in text
    issue = next(row for row in report['issues'] if row.get('test_id') == 'different')
    assert issue['input']['env']['API_KEY'] == '<MASKED>'


def test_static_failure_export_identifies_missing_functions_without_source_bodies():
    complete = dict(id='fn1', name='PRIVATE_SYMBOL_NAME', external=False,
                    decompile='PRIVATE_SOURCE_BODY', decompile_status='success',
                    blocks=[{'id': 'b'}], mnemonics=['ret'], calls=[],
                    cfg_truncated=False, instructions_truncated=False,
                    coverage={'cfg': 'complete', 'assembly': 'complete', 'calls': 'complete'})
    missing = copy.deepcopy(complete)
    missing.update(id='fn128', decompile=None, decompile_status='not_selected')
    artifact = dict(status='success', complete=True, total_functions=2,
                    functions=[complete, missing])
    source = run_fixture()
    source.update(analysis_mode='static', ai_mode='off', tests=[], dynamic=[],
                  static={'original': artifact, 'reimplementation': copy.deepcopy(artifact)},
                  stages={'static_original': {'status': 'success'}, 'static_reimplementation': {'status': 'success'},
                          'structure': {'status': 'success'}})
    report = module().build_failure_report(source)
    issue = next(row for row in report['issues'] if row.get('target') == 'original')
    assert issue['affected_functions'] == [{'function_id': 'fn128', 'facets': {'decompile': 'not_selected'}}]
    assert issue['affected_function_count'] == 1
    assert 'PRIVATE_SYMBOL_NAME' not in json.dumps(report)
    assert 'PRIVATE_SOURCE_BODY' not in json.dumps(report)
    assert 'fn128' in module().failure_report_html(report)
