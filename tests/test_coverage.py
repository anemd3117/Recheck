"""Coverage describes retained observations; completion flags do not invent evidence."""
import base64
import copy
import importlib
import importlib.util

import pytest


def summary(run):
    assert importlib.util.find_spec('recheck.coverage'), 'Explicit observation coverage is missing'
    return importlib.import_module('recheck.coverage').coverage_summary(run)


def native(identifier, **values):
    return dict(id=str(identifier), name='fn' + str(identifier), external=False,
                decompile_status='success', decompile='int fn() { return 1; }',
                blocks=[dict(id='b', start='0', end='1', size=1)], edges=[],
                calls=[], mnemonics=['ret'], cfg_truncated=False,
                instructions_truncated=False, **values)


def static_run(artifact, scope='static'):
    return dict(mode='automatic', analysis_mode=scope, ai_mode='off',
                analysis_status=dict(static='success', dynamic='not_requested', ai='not_requested'),
                static=dict(original=artifact))


def test_direct_inventory_does_not_turn_100_of_130_decompilations_into_full_coverage():
    functions = [native(index) for index in range(130)]
    for function in functions[100:]:
        function.update(decompile_status='not_selected', decompile=None)
    run = static_run(dict(status='success', complete=True, total_functions=130,
                         functions_truncated=False, functions=functions))
    result = summary(run)['static']['targets']['original']
    assert result['inventory'] == dict(returned=130, total=130, complete=True,
                                       native=130, external=0, managed=0, invalid_rows=0)
    assert result['facets']['decompile']['observed'] == 100
    assert result['facets']['decompile']['not_selected'] == 30
    assert result['facets']['decompile']['total_eligible'] == 130
    assert result['facets']['decompile']['status'] == 'partial'
    assert result['has_gaps'] is True
    assert result['functions'][128]['facets']['decompile'] == 'not_selected'
    assert run['static']['original']['status'] == 'success', 'Presentation must preserve recorded results'


def test_legacy_success_without_inventory_total_does_not_invent_full_inventory_or_calls():
    result = summary(static_run(dict(status='success', complete=True,
                                      functions=[dict(id='a', name='legacy', decompile='code',
                                                      blocks=[], edges=[], calls=[], mnemonics=[])])))
    artifact = result['static']['targets']['original']
    assert artifact['inventory']['returned'] == 1
    assert artifact['inventory']['total'] is None and artifact['inventory']['complete'] is None
    assert artifact['facets']['decompile']['observed'] == 1
    assert artifact['facets']['decompile']['complete'] == 0
    assert artifact['facets']['decompile']['total_eligible'] is None
    assert artifact['facets']['cfg']['unknown'] == 1
    assert artifact['facets']['calls']['unknown'] == 1


def test_truncated_failed_and_empty_successful_decompiles_remain_distinct():
    functions = [native(index) for index in range(4)]
    functions[0].update(decompile_truncated=True)
    functions[1].update(decompile=None, decompile_status='failed')
    functions[2].update(decompile='', decompile_status='success')
    functions[3].update(decompile='retained text', decompile_status='truncated')
    facet = summary(static_run(dict(total_functions=4, functions=functions)))['static']['targets']['original']['facets']['decompile']
    assert facet['observed'] == 2 and facet['complete'] == 0 and facet['partial'] == 2
    assert facet['failed'] == 1 and facet['unavailable'] == 1


def test_external_functions_do_not_reduce_native_facet_denominators():
    external = native('import')
    external.update(external=True, decompile=None, decompile_status='external', blocks=[],
                    coverage=dict(cfg='not_applicable_external', assembly='not_applicable_external',
                                  calls='not_applicable_external'))
    result = summary(static_run(dict(total_functions=2, functions=[native('main'), external])))['static']['targets']['original']
    assert result['inventory']['external'] == 1 and result['inventory']['native'] == 1
    assert result['facets']['decompile']['eligible_returned'] == 1
    assert result['facets']['decompile']['total_eligible'] == 1
    assert result['facets']['decompile']['not_applicable'] == 1


def test_zero_functions_is_known_empty_only_with_explicit_complete_inventory():
    known = summary(static_run(dict(total_functions=0, functions=[], complete=True)))['static']['targets']['original']
    unknown = summary(static_run(dict(status='failed', functions=[])))['static']['targets']['original']
    assert known['inventory']['total'] == 0 and known['inventory']['complete'] is True
    assert unknown['inventory']['total'] is None and unknown['inventory']['complete'] is None
    assert known['inventory'] != unknown['inventory']


def test_conflicting_inventory_counts_cannot_be_reported_complete():
    artifact = dict(total_functions=1, functions=[native(0), native(1)],
                    functions_truncated=False, complete=True)
    result = summary(static_run(artifact))['static']['targets']['original']
    assert result['inventory']['complete'] is False
    assert result['has_gaps'] is True


def test_rea_facets_and_inventory_relationship_scope_are_separate():
    functions = [native(0), native(1), native(2)]
    functions[0].update(coverage=dict(cfg='complete', assembly='unavailable', calls='complete_resolved_only'))
    functions[1].update(decompile=None, decompile_status='not_requested', blocks=[], mnemonics=[],
                        coverage=dict(cfg='not_requested', assembly='not_requested', calls='not_requested'))
    functions[2].update(coverage=dict(cfg='partial', assembly='complete', calls='unavailable'))
    artifact = dict(backend='rea', status='partial', complete=False, functions=functions,
                    partial_reasons=['Relationship observations unavailable'],
                    raw_evidence=dict(coverage=dict(inventory_observed=3, inventory_returned=3,
                                                    inventory_complete=True)))
    result = summary(static_run(artifact))['static']['targets']['original']
    assert result['inventory']['complete'] is True
    assert result['relationship_status'] == 'unknown'
    assert result['facets']['cfg']['complete'] == 1
    assert result['facets']['cfg']['partial'] == 1
    assert result['facets']['cfg']['not_selected'] == 1
    assert result['facets']['calls']['partial'] == 1, 'Resolved-only calls do not prove all indirect calls'
    assert result['facets']['assembly']['unavailable'] == 1
    assert result['partial_reasons'] == ['Relationship observations unavailable']


def test_managed_cil_is_separate_from_native_decompilation_and_cfg():
    functions = [dict(id=str(index), name='Demo.M' + str(index),
                      decompile=None, decompile_status='not_applicable_managed',
                      managed_method=dict(body=body), coverage=dict(cfg='not_collected_managed',
                                                                   assembly='not_applicable_managed', calls='complete'))
                 for index, body in enumerate([dict(status='present', il_sha256='a' * 64),
                                               dict(status='absent'), dict(status='malformed')])]
    artifact = dict(analysis_kind='managed', functions=functions,
                    raw_evidence=dict(coverage=dict(inventory_observed=3, inventory_complete=True)))
    result = summary(static_run(artifact))['static']['targets']['original']
    assert result['inventory']['managed'] == 3 and result['inventory']['native'] == 0
    assert result['facets']['decompile']['not_applicable'] == 3
    assert result['facets']['cil']['observed'] == 1
    assert result['facets']['cil']['not_applicable'] == 1
    assert result['facets']['cil']['unavailable'] == 1
    assert result['facets']['cfg']['unavailable'] == 3


def observation(stdout=b'', **changes):
    value = dict(status='success', infrastructure_ok=True, exit_code=0,
                 stdout_b64=base64.b64encode(stdout).decode(), stderr_b64='')
    value.update(changes)
    return value


def dynamic_run(rows):
    return dict(analysis_mode='dynamic', ai_mode='off', tests=[dict(id='a'), dict(id='b'), dict(id='c')],
                dynamic=rows, behavior=dict(matched=999, denominator=999),
                settings=dict(normalize_newlines=False, trim_trailing_whitespace=False))


def test_dynamic_coverage_recounts_raw_valid_pairs_instead_of_trusting_stored_metrics():
    run = dynamic_run([dict(test_id='a', original=observation(), reimplementation=observation()),
                       dict(test_id='b', original=observation(b'left'), reimplementation=observation(b'right'))])
    result = summary(run)['dynamic']
    assert result['registered'] == 3 and result['measured'] == 2
    assert result['matched'] == 1 and result['mismatched'] == 1
    assert result['unmeasured'] == 1 and result['not_run'] == 1
    assert result['invalid_observations'] == 0


@pytest.mark.parametrize('changes', [dict(stdout_b64='%%%'), dict(exit_code='0'), dict(exit_code=False)])
def test_malformed_dynamic_observations_are_unmeasured(changes):
    result = summary(dynamic_run([dict(test_id='a', original=observation(**changes),
                                       reimplementation=observation(**changes))]))['dynamic']
    assert result['measured'] == 0 and result['matched'] == 0
    assert result['invalid_observations'] == 1 and result['unmeasured'] == 3


def test_legacy_text_only_observation_is_not_measured_as_valid_empty_output():
    old = dict(status='success', stdout='', stderr='', exit_code=0)
    result = summary(dynamic_run([dict(test_id='a', original=old, reimplementation=old)]))['dynamic']
    assert result['measured'] == 0 and result['invalid_observations'] == 1


def test_duplicate_or_unregistered_rows_do_not_inflate_dynamic_coverage():
    row = dict(test_id='a', original=observation(), reimplementation=observation())
    foreign = dict(row, test_id='outside')
    result = summary(dynamic_run([row, copy.deepcopy(row), foreign]))['dynamic']
    assert result['measured'] == 0 and result['matched'] == 0
    assert result['duplicate_ids'] == ['a'] and result['unregistered_ids'] == ['outside']
    assert result['unmeasured'] == 3


def test_dynamic_coverage_uses_recorded_normalization_policy():
    run = dynamic_run([dict(test_id='a', original=observation(b'value\r\n'),
                            reimplementation=observation(b'value\n'))])
    run['settings']['normalize_newlines'] = True
    assert summary(run)['dynamic']['matched'] == 1


def test_unselected_scope_is_not_a_failed_or_missing_analysis():
    run = static_run(dict(total_functions=0, functions=[]))
    result = summary(run)
    assert result['static']['requested'] is True
    assert result['dynamic']['requested'] is False and result['dynamic']['status'] == 'not_requested'
    assert result['ai']['requested'] is False and result['ai']['status'] == 'not_requested'


def test_unknown_historical_scope_and_missing_artifact_remain_unrecorded():
    result = summary(dict(status='success'))
    assert result['analysis_mode'] is None
    assert result['static']['requested'] is None
    assert result['static']['targets']['original']['inventory']['total'] is None
    assert result['dynamic']['registered'] is None


def test_unknown_normalization_does_not_invent_a_historical_match_verdict():
    run = dynamic_run([dict(test_id='a', original=observation(), reimplementation=observation())])
    run.pop('settings')
    result = summary(run)['dynamic']
    assert result['measured'] == 1
    assert result['matched'] is None and result['mismatched'] is None
    assert result['comparison_policy_known'] is False


def test_target_nonzero_exit_is_still_a_valid_observation():
    run = dynamic_run([dict(test_id='a', original=observation(exit_code=17),
                            reimplementation=observation(exit_code=17))])
    result = summary(run)['dynamic']
    assert result['measured'] == 1 and result['matched'] == 1
    assert result['invalid_observations'] == 0


def test_timeout_is_a_known_unmeasured_observation_not_corrupt_data():
    run = dynamic_run([dict(test_id='a', original=observation(timed_out=True),
                            reimplementation=observation())])
    result = summary(run)['dynamic']
    assert result['measured'] == 0 and result['invalid_observations'] == 0
    assert result['category_counts'] == {'timed_out': 1}


def test_coverage_projection_does_not_mutate_stored_runs_or_share_mutable_rows():
    run = static_run(dict(functions=[native(0)], total_functions=1,
                          partial_reasons=['original reason']))
    before = copy.deepcopy(run)
    result = summary(run)
    result['static']['targets']['original']['partial_reasons'].append('later')
    result['static']['targets']['original']['functions'][0]['name'] = 'changed'
    assert run == before


def test_duplicate_and_invalid_function_ids_cannot_inflate_observed_facets():
    functions = [native('repeated'), native('repeated'), native('valid'), native('invalid')]
    functions[-1]['id'] = None
    result = summary(static_run(dict(total_functions=4, functions=functions)))['static']['targets']['original']
    assert result['inventory']['returned'] == 4
    assert result['inventory']['invalid_rows'] == 3
    assert result['inventory']['complete'] is False
    assert result['facets']['decompile']['observed'] == 1
    assert result['facets']['decompile']['total_eligible'] is None


@pytest.mark.parametrize('facet,flag', [('cfg', 'cfg_truncated'), ('assembly', 'instructions_truncated')])
def test_explicit_truncation_overrides_a_facet_complete_label(facet, flag):
    function = native('truncated')
    function['coverage'] = {facet: 'complete'}
    function[flag] = True
    result = summary(static_run(dict(total_functions=1, functions=[function])))['static']['targets']['original']['facets'][facet]
    assert result['complete'] == 0 and result['partial'] == 1


@pytest.mark.parametrize('facet,field', [('cfg', 'blocks'), ('assembly', 'mnemonics')])
def test_partial_facet_without_retained_content_is_not_counted_observed(facet, field):
    function = native('empty')
    function['coverage'] = {facet: 'partial'}
    function[field] = []
    result = summary(static_run(dict(total_functions=1, functions=[function])))['static']['targets']['original']['facets'][facet]
    assert result['observed'] == 0
    assert result['unavailable'] == 1


@pytest.mark.parametrize('mode', [[], {}, False, 42])
def test_malformed_historical_mode_is_unknown_instead_of_crashing_report(mode):
    result = summary(dict(mode=mode, status='success'))
    assert result['analysis_mode'] is None
    assert result['static']['requested'] is None


def test_saved_input_not_reproduced_is_not_measured_as_a_resolved_regression():
    run = dynamic_run([dict(test_id='a', original=observation(), reimplementation=observation())])
    run['regression'] = dict(case_results=[dict(test_id='a', status='unmeasured',
                                               saved_input_reproduced=False)])
    original = copy.deepcopy(run)
    result = summary(run)['dynamic']
    assert result['measured'] == 0 and result['matched'] == 0
    assert result['unmeasured'] == 3 and result['not_run'] == 2
    assert result['category_counts'] == {'saved_input_not_reproduced': 1}
    assert result['unreproduced_inputs'] == 1 and result['unreproduced_ids'] == ['a']
    assert result['invalid_observations'] == 0
    assert run == original


def test_reproduced_saved_input_retains_its_valid_pair_observation():
    run = dynamic_run([dict(test_id='a', original=observation(), reimplementation=observation())])
    run['regression'] = dict(case_results=[dict(test_id='a', status='resolved',
                                               saved_input_reproduced=True)])
    result = summary(run)['dynamic']
    assert result['measured'] == 1 and result['matched'] == 1
    assert result['unreproduced_inputs'] == 0
