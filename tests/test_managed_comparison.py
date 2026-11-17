import copy
import json
import pytest

from recheck.engine import Engine
from recheck.models import Settings

from recheck.comparison import compare_static_artifacts
from recheck.static_backend import _validate_artifact
from test_static_backend import artifact as static_artifact, DIGEST


def artifact(token='0x06000001', digest='a'):
    return dict(analysis_kind='managed', functions=[dict(
        id=token, name='Demo.Add', evidence_id='ev_' + token,
        managed_method=dict(declaring_type='Demo', name='Add',
            signature=dict(parse_status='decoded', resolved_sha256='b' * 64),
            body=dict(status='present', il_sha256=digest * 64,
                max_stack=8, init_locals=False, local_var_sig_token=None,
                exception_regions=[], normalized_il_sha256='c' * 64)),
    )])


def test_managed_methods_match_by_resolved_signature_instead_of_build_local_token():
    result = compare_static_artifacts(artifact(), artifact('0x06000009'))
    assert len(result['matches']) == 1
    assert result['matches'][0]['cil_observation'] == 'equal_bytes_and_headers'
    assert result['analysis_kind'] == 'managed'
    assert '의미' in result['producer']


def test_managed_cil_change_is_visible_without_inventing_pseudocode():
    result = compare_static_artifacts(artifact(), artifact(digest='d'))
    assert result['matches'][0]['cil_observation'] == 'different'
    assert len(result['managed_differences']) == 1
    assert not result['decompile_differences']


def test_ambiguous_signature_is_not_forced_into_a_member_pair():
    right = artifact()
    right['functions'].append(copy.deepcopy(right['functions'][0]))
    right['functions'][1]['id'] = '0x06000002'
    result = compare_static_artifacts(artifact(), right)
    assert not result['matches']
    assert len(result['unmatched_reimplementation']) == 2


def test_absent_cil_is_unobserved_instead_of_equal():
    left, right = artifact(), artifact()
    for value in (left, right):
        value['functions'][0]['managed_method']['body'] = dict(status='absent', il_sha256=None)
    result = compare_static_artifacts(left, right)
    assert result['matches'][0]['cil_observation'] == 'unobserved'


@pytest.mark.parametrize('digest', ['not-a-hash', 'g' * 64, 'a' * 63, True, 7])
def test_invalid_cil_digest_cannot_claim_equal_bytes_and_headers(digest):
    left, right = artifact(), artifact()
    for value in (left, right):
        value['functions'][0]['managed_method']['body']['il_sha256'] = digest
    result = compare_static_artifacts(left, right)
    assert result['matches'][0]['cil_observation'] == 'unobserved'
    assert result['managed_differences'] == []


@pytest.mark.parametrize('field', [
    'il_sha256', 'max_stack', 'init_locals', 'local_var_sig_token', 'exception_regions',
])
def test_missing_cil_header_is_unobserved_even_when_missing_on_both_sides(field):
    left, right = artifact(), artifact()
    for value in (left, right):
        value['functions'][0]['managed_method']['body'].pop(field)
    result = compare_static_artifacts(left, right)
    assert result['matches'][0]['cil_observation'] == 'unobserved'
    assert result['managed_differences'] == []


@pytest.mark.parametrize('field,value', [
    ('max_stack', True), ('max_stack', -1), ('max_stack', None),
    ('init_locals', 0), ('init_locals', None),
    ('local_var_sig_token', 0), ('local_var_sig_token', 'invalid-token'),
    ('exception_regions', None), ('exception_regions', {}), ('exception_regions', [False]),
])
def test_invalid_cil_header_on_either_side_does_not_become_a_comparison(field, value):
    left, right = artifact(), artifact()
    left['functions'][0]['managed_method']['body'][field] = value
    result = compare_static_artifacts(left, right)
    assert result['matches'][0]['cil_observation'] == 'unobserved'
    assert result['managed_differences'] == []


@pytest.mark.parametrize('body', [None, [], 'present'])
def test_invalid_cil_body_is_unobserved_instead_of_crashing(body):
    left, right = artifact(), artifact()
    left['functions'][0]['managed_method']['body'] = body
    result = compare_static_artifacts(left, right)
    assert result['matches'][0]['cil_observation'] == 'unobserved'


def test_valid_cil_locals_signature_token_keeps_recorded_header_comparison():
    left, right = artifact(), artifact()
    for value in (left, right):
        value['functions'][0]['managed_method']['body']['local_var_sig_token'] = '0x11000001'
    result = compare_static_artifacts(left, right)
    assert result['matches'][0]['cil_observation'] == 'equal_bytes_and_headers'


def test_managed_worker_artifact_retains_its_own_tool_identity():
    value = static_artifact(analysis_kind='managed', tool='REA MCP / Managed metadata')
    assert _validate_artifact(value, DIGEST)


def managed_record(left, right):
    evidence = []
    for target, value in (('original', left), ('reimplementation', right)):
        for function in value['functions']:
            evidence.append(dict(evidence_id=function['evidence_id'], kind='function',
                                 target=target, data=function))
    structure = compare_static_artifacts(left, right)
    evidence.append(dict(evidence_id='ev-structure', kind='structure', target='both', data=structure))
    return dict(run_id='run_' + 'b' * 32, attempt_id='attempt_fixture', tests=[],
                static={'original': left, 'reimplementation': right}, structure=structure,
                dynamic=[], behavior={}, evidence=evidence)


def test_ai_context_keeps_managed_cil_changes_and_both_method_citations(tmp_path):
    value = managed_record(artifact(), artifact('0x06000009', digest='d'))
    context = Engine(Settings(num_ctx=8192), tmp_path)._context(value)
    structure = next(item for item in context['evidence_data'] if item['kind'] == 'structure')
    assert structure['data']['analysis_kind'] == 'managed'
    change = structure['data']['managed_differences'][0]
    assert change['original_il_sha256'] == 'a' * 64
    assert change['reimplementation_il_sha256'] == 'd' * 64
    assert change['changed_fields'] == ['il_sha256']
    assert structure['function_records'] == [
        dict(target='original', id='0x06000001', name='Demo.Add', evidence_id='ev_0x06000001'),
        dict(target='reimplementation', id='0x06000009', name='Demo.Add', evidence_id='ev_0x06000009'),
    ]
    assert context['metrics']['denominator'] is None
    assert context['wire_characters'] <= context['budget_characters']


def test_ai_context_bounds_large_managed_exception_metadata(tmp_path):
    left, right = artifact(), artifact('0x06000009')
    right['functions'][0]['managed_method']['body']['exception_regions'] = [
        dict(kind='catch', catch_type='LargeType' * 100) for _ in range(1000)
    ]
    context = Engine(Settings(num_ctx=8192), tmp_path)._context(managed_record(left, right))
    structure = next(item for item in context['evidence_data'] if item['kind'] == 'structure')
    change = structure['data']['managed_differences'][0]
    assert change['changed_fields'] == ['exception_regions']
    assert 'LargeType' not in json.dumps(structure['data'])
    assert context['wire_characters'] <= context['budget_characters']


def test_ai_context_preserves_managed_unobserved_body_count(tmp_path):
    left, right = artifact(), artifact('0x06000009')
    left['functions'][0]['managed_method']['body'] = dict(status='absent', il_sha256=None)
    context = Engine(Settings(num_ctx=8192), tmp_path)._context(managed_record(left, right))
    structure = next(item for item in context['evidence_data'] if item['kind'] == 'structure')
    assert structure['data']['cil_observation_counts'] == {'unobserved': 1}
    assert structure['data']['managed_difference_count'] == 0


def test_managed_difference_citation_requires_actual_method_link(tmp_path):
    value = managed_record(artifact(), artifact('0x06000009', digest='d'))
    answer = dict(summary='정적 메서드 본문에서 차이를 관찰했습니다.', findings=[dict(
        finding_id='cil-change', claim='CIL 바이트 해시가 다릅니다.',
        rationale='실행 없이 수집한 정적 자료의 차이입니다.', targets=['both'],
        function_ids=[], test_ids=[], evidence_ids=['ev-structure'],
    )])
    with pytest.raises(ValueError, match='함수.*연결'):
        Engine(Settings(), tmp_path)._validate_interpretation(value, json.dumps(answer))
