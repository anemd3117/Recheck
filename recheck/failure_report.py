"""Bounded, explicit failure exports; opaque evidence is excluded by default."""

from __future__ import annotations

import base64
import binascii
from collections import Counter
import hashlib
import html
import json
import os
from pathlib import Path, PureWindowsPath
import re
import tempfile

from .comparison import compare_behavior
from .display import stage_label, status_label, target_label
from .models import Settings
from .report import analysis_scope_summary
from .paths import filesystem_path


CATEGORY_LABELS = {
    'behavior_mismatch': '동작 불일치',
    'execution_unmeasured': '실행 실패·미측정',
    'static_incomplete': '정적 근거 수집 미완료',
    'stage_failure': '분석 단계 미완료',
    'ai_failure': 'AI 설명·요청 실패',
}
MAX_ITEMS = 10000
MAX_TEXT = 65536
MAX_OUTPUT_BYTES = 256 * 1024
_ACTIONS = {
    'behavior_mismatch': ['양쪽 출력·종료 코드와 등록 입력을 확인하세요.',
                          '이 입력을 회귀 테스트에 저장하고 수정한 파일로 다시 검사하세요.'],
    'execution_unmeasured': ['시간 제한·자원·실행 환경과 파일 지원 범위를 확인하세요.',
                            '유효한 관찰이 없는 입력은 일치율에서 제외하고 같은 조건으로 다시 검사하세요.'],
    'static_incomplete': ['함수·디컴파일·관계별 수집 범위와 미관측 수를 확인하세요.',
                          '정적 도구 준비 상태·시간 제한을 확인하고 필요한 범위를 다시 분석하세요.'],
    'stage_failure': ['표시된 단계의 도구·파일·환경 준비 상태를 확인하세요.',
                       '기존 근거를 보존하고 준비 상태를 다시 확인한 뒤 재검사하세요.'],
    'ai_failure': ['선택한 AI 연결·근거 전송 동의·요청 한도와 문맥 예산을 확인하세요.',
                    '기존 실측 결과와 실패한 AI 요청을 구분하고 필요할 때 설명만 다시 요청하세요.'],
}
_STATUSES = {'success', 'partial', 'failed', 'blocked', 'cancelled', 'pending', 'running',
             'not_requested', 'unrecorded', 'exited', 'completed', 'unavailable'}
_OBSERVATION_CODES = {'invalid_observation', 'infrastructure_failure', 'unsupported_or_blocked',
                       'cancelled', 'timed_out', 'oom', 'output_truncated', 'missing_observation',
                       'comparison_policy_unknown', 'saved_input_not_reproduced'}
REASON_LABELS = {
    'mismatched': '양쪽 표준출력·표준오류 또는 종료 코드가 다릅니다.',
    'invalid_observation': '관찰 형식·원시 출력·입력 ID가 유효하지 않습니다.',
    'infrastructure_failure': '실행 환경 또는 관찰 수집을 완료하지 못했습니다.',
    'unsupported_or_blocked': '지원 범위 또는 선행 조건 때문에 실행하지 못했습니다.',
    'cancelled': '사용자 또는 상위 작업이 중단되었습니다.',
    'timed_out': '시간 초과로 유효한 실행 쌍을 수집하지 못했습니다.',
    'oom': '메모리 한도 또는 메모리 부족을 확인하세요.',
    'output_truncated': '출력이 잘려 전체 출력 비교를 완료하지 못했습니다.',
    'missing_observation': '등록한 입력의 실행 관찰이 없습니다.',
    'comparison_policy_unknown': '당시 출력 정규화 정책이 기록되지 않아 일치 여부를 재판정하지 않았습니다.',
    'saved_input_not_reproduced': '저장한 보조 파일·입력의 동일한 내용이 이번 실행에서 확인되지 않았습니다.',
    'inventory_incomplete': '함수 목록 수집을 완료하지 못했습니다.',
    'inventory_unknown': '함수 목록의 전체 수집 여부가 기록되지 않았습니다.',
    'relationship_partial': '일부 함수 관계만 수집했습니다.',
    'relationship_unknown': '함수 관계의 수집 범위를 확인할 수 없습니다.',
    'evidence_context': 'AI에 보낼 근거가 문맥 예산 안에 들어가지 않습니다.',
    'context_budget': 'AI 문맥 예산을 초과했습니다.',
    'request_cancelled': 'AI 설명 요청을 중단했습니다.',
    'interpretation_failed': '요청한 AI 설명을 완료하지 못했습니다.',
    'quota': 'AI 제공자의 사용 한도 또는 할당량을 확인하세요.',
    'rate_limit': 'AI 요청 속도·사용 한도를 확인하세요.',
    'credential_required': '선택한 AI 연결의 인증 상태를 확인하세요.',
    'credential_auth': '선택한 AI 연결의 인증 상태를 확인하세요.',
    'permission': '선택한 AI 모델·프로젝트의 사용 권한을 확인하세요.',
    'invalid_json': 'AI 응답의 JSON 형식을 검증하지 못했습니다.',
    'response_schema': 'AI 응답 형식을 검증하지 못했습니다.',
    'incomplete_stream': 'AI 응답 스트림이 완료되기 전에 종료되었습니다.',
    'stage_incomplete': '선택한 분석 단계가 완료되지 않았습니다.',
}


def _stage_reason_codes(run, stage):
    from .diagnostics import RULES

    known = {rule[0] for rule in RULES}
    result = []
    for diagnostic in run.get('diagnostics') or []:
        if (isinstance(diagnostic, dict) and diagnostic.get('stage') == stage
                and isinstance(diagnostic.get('code'), str) and diagnostic['code'] in known):
            result.append(diagnostic['code'])
    if stage in ('llm', 'agent'):
        code = (run.get('llm' if stage == 'llm' else 'agent') or {}).get('error_code')
        if isinstance(code, str) and code in REASON_LABELS:
            result.append(code)
    if not result:
        text = str((run.get('stages', {}).get(stage) or {}).get('reason') or '').lower()
        for code, needles, _, _ in RULES:
            if any(needle in text for needle in needles):
                result.append(code)
                break
    return sorted(set(result)) or ['stage_incomplete']


def _reason_label(code):
    if code in REASON_LABELS:
        return REASON_LABELS[code]
    from .diagnostics import RULES
    for known, _, summary, _ in RULES:
        if code == known:
            return summary
    match = re.fullmatch(r'(decompile|cfg|assembly|calls|cil)_(unknown|not_selected|failed|unavailable|partial)', code)
    if match:
        names = {'decompile': '의사 코드', 'cfg': '제어 흐름', 'assembly': '명령 정보',
                  'calls': '호출 관계', 'cil': '.NET CIL'}
        states = {'unknown': '미확인', 'not_selected': '미선택', 'failed': '수집 실패',
                  'unavailable': '수집 불가', 'partial': '부분 수집'}
        return names[match[1]] + '에 ' + states[match[2]] + ' 항목이 있습니다.'
    return '추가 근거와 단계별 상태를 확인하세요.'


def _text(value, limit=MAX_TEXT):
    return value[:limit] if isinstance(value, str) else ''


def _name(value):
    return _text(PureWindowsPath(str(value or '').replace('/', '\\')).name, 200)


def _identifier(value):
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}', value) else '<invalid-id>'


def _safe_status(value):
    return value if isinstance(value, str) and value in _STATUSES else 'unrecorded'


def _hash(value):
    return value if isinstance(value, str) and re.fullmatch('[a-fA-F0-9]{64}', value) else None


def _time(value):
    return value if isinstance(value, str) and re.fullmatch(r'[0-9T:.+Z-]{10,40}', value) else None


def _stream(observation, channel):
    value = observation.get(channel + '_b64')
    try:
        if not isinstance(value, str):
            raise ValueError('missing raw output')
        raw = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        return dict(status='invalid_observation')
    retained = raw[:MAX_OUTPUT_BYTES]
    return dict(status='observed', bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(),
                base64=base64.b64encode(retained).decode('ascii'),
                text=retained.decode('utf-8', errors='replace'),
                truncated_for_report=len(raw) > len(retained))


def _observation_details(value):
    value = value if isinstance(value, dict) else {}
    exit_code = value.get('exit_code')
    result = dict(status=_safe_status(value.get('status')),
                  exit_code=exit_code if type(exit_code) is int else None,
                  stdout=_stream(value, 'stdout'), stderr=_stream(value, 'stderr'))
    for key in ('timed_out', 'cancelled', 'oom_killed', 'truncated', 'infrastructure_ok'):
        if type(value.get(key)) is bool:
            result[key] = value[key]
    for key in ('image_id', 'image'):
        if isinstance(value.get(key), str) and re.fullmatch(r'sha256:[a-f0-9]{64}', value[key]):
            result[key] = value[key]
    return result


def _input_details(value):
    value = value if isinstance(value, dict) else {}
    env = value.get('env') if isinstance(value.get('env'), dict) else {}
    args = value.get('args') if isinstance(value.get('args'), list) else []
    auxiliary = value.get('aux_files') if isinstance(value.get('aux_files'), list) else []
    truncated = []
    if len(args) > 256 or any(isinstance(arg, str) and len(arg) > 4096 for arg in args):
        truncated.append('args')
    if isinstance(value.get('stdin'), str) and len(value['stdin']) > MAX_TEXT:
        truncated.append('stdin')
    if len(env) > 128 or any(isinstance(item, str) and len(item) > 4096 for item in env.values()):
        truncated.append('env')
    if len(auxiliary) > 100:
        truncated.append('auxiliary_files')
    return dict(
        args=[_text(arg, 4096) for arg in args[:256] if isinstance(arg, str)],
        stdin=_text(value.get('stdin')),
        env={_text(key, 200): '<MASKED>' if re.search(r'(?i)key|token|secret|password|authorization', key)
             else _text(item, 4096) for key, item in list(env.items())[:128] if isinstance(key, str)},
        auxiliary_files=[_name(path) for path in auxiliary[:100]],
        timeout_seconds=value.get('timeout_seconds') if type(value.get('timeout_seconds')) in (int, float) else None,
        truncated_for_report=bool(truncated), truncated_fields=truncated,
    )


def _coverage_details(artifact):
    """Only allowlisted numeric coverage; source bodies and freeform reasons stay local."""
    result = dict(analysis_kind=artifact.get('analysis_kind'), status=_safe_status(artifact.get('status')))
    inventory = artifact.get('inventory') or {}
    result['inventory'] = {key: value for key, value in inventory.items()
                           if key in ('returned', 'total', 'complete', 'native', 'external', 'managed', 'invalid_rows')
                           and (value is None or type(value) in (int, bool))}
    result['facets'] = {}
    for name in ('decompile', 'cfg', 'assembly', 'calls', 'cil'):
        facet = artifact.get('facets', {}).get(name)
        if isinstance(facet, dict):
            result['facets'][name] = {key: value for key, value in facet.items()
                                      if key in ('eligible_returned', 'total_eligible', 'observed', 'complete',
                                                 'partial', 'not_selected', 'failed', 'unavailable', 'unknown',
                                                 'not_applicable') and (value is None or type(value) is int)}
    return result


def build_failure_report(run, *, include_observations=False):
    """Build a failure-only projection, never a dump of the run or user settings.

    A matching nonzero program exit is a valid observation. Unrequested stages
    are excluded. Runtime comparisons are recomputed from raw observations so a
    damaged saved verdict cannot turn an invalid observation into a mismatch.
    """
    if not isinstance(run, dict):
        raise ValueError('분석 결과 형식을 확인하세요.')
    scope = analysis_scope_summary(run)
    settings = run.get('settings') if isinstance(run.get('settings'), dict) else {}
    policy_known = all(type(settings.get(name)) is bool
                       for name in ('normalize_newlines', 'trim_trailing_whitespace'))
    normalization = (run.get('behavior') or {}).get('normalization') or {}
    if policy_known:
        policy_values = (settings['normalize_newlines'], settings['trim_trailing_whitespace'])
    elif all(type(normalization.get(name)) is bool for name in ('newlines', 'trailing_whitespace')):
        policy_known = True
        policy_values = (normalization['newlines'], normalization['trailing_whitespace'])
    else:
        policy_values = (False, False)  # Used only for raw validity, never a verdict when unknown.
    policy = Settings(normalize_newlines=policy_values[0], trim_trailing_whitespace=policy_values[1])
    issues = []

    def add(category, **fields):
        reasons = fields.get('reason_codes', fields.get('reasons', []))
        fields['reason_codes'] = list(reasons)
        issues.append(dict(issue_id=f'failure-{len(issues) + 1}', category=category,
                           title=CATEGORY_LABELS[category], next_actions=list(_ACTIONS[category]), **fields))

    if scope['analysis_mode'] != 'static':
        inputs = {item['id']: item for item in run.get('tests', [])[:100]
                  if isinstance(item, dict) and isinstance(item.get('id'), str)}
        raw_rows = [row for row in run.get('dynamic', [])[:100]
                    if isinstance(row, dict) and isinstance(row.get('test_id'), str)]
        occurrences = Counter(row['test_id'] for row in raw_rows)
        actual = {row['test_id']: row for row in raw_rows if occurrences[row['test_id']] == 1}
        usable = [row for row in raw_rows if occurrences[row['test_id']] == 1 and row['test_id'] in inputs]
        comparisons = compare_behavior(usable, policy).get('tests', [])
        if not policy_known:
            for comparison in comparisons:
                if comparison.get('execution_status') == 'measured':
                    comparison.update(execution_status='unmeasured', match=None,
                                      observation_categories=['comparison_policy_unknown'])
        for test_id in occurrences:
            if occurrences[test_id] != 1 or test_id not in inputs:
                comparisons.append(dict(test_id=test_id, execution_status='unmeasured', match=None,
                                        observation_categories=['invalid_observation']))
        # Registered inputs without raw observations must stay unmeasured.
        for test_id in inputs.keys() - occurrences.keys():
            comparisons.append(dict(test_id=test_id, execution_status='unmeasured', match=None,
                                    observation_categories=['missing_observation']))
        regression = run.get('regression') or {}
        unreproduced = {row.get('test_id') for row in regression.get('case_results', [])
                        if isinstance(row, dict) and row.get('saved_input_reproduced') is False}
        for comparison in comparisons:
            if comparison.get('test_id') in unreproduced:
                comparison.update(execution_status='unmeasured', match=None,
                                  observation_categories=['saved_input_not_reproduced'])
        for row in comparisons:
            if row.get('execution_status') == 'measured' and row.get('match') is True:
                continue
            measured = row.get('execution_status') == 'measured' and row.get('match') is False
            category = 'behavior_mismatch' if measured else 'execution_unmeasured'
            test_id = row['test_id']
            values = dict(test_id=_identifier(test_id), status='different' if measured else 'unmeasured',
                          reasons=['mismatched'] if measured else [code for code in row.get('observation_categories', [])
                                                                   if code in _OBSERVATION_CODES])
            for name in ('original_exit_code', 'reimplementation_exit_code'):
                values[name] = row.get(name) if type(row.get(name)) is int else None
            if include_observations:
                values['input'] = _input_details(inputs.get(test_id))
                values['observations'] = {target: _observation_details(actual.get(test_id, {}).get(target))
                                           for target in ('original', 'reimplementation')}
            add(category, **values)

    static_artifacts = run.get('static') or {}
    if scope['analysis_mode'] != 'dynamic' and static_artifacts:
        from .coverage import coverage_summary
        coverage = coverage_summary(run)['static']
        for target, artifact in coverage.get('targets', {}).items():
            if artifact.get('has_gaps') or artifact.get('status') in ('partial', 'failed', 'blocked', 'cancelled'):
                reasons = []
                complete = (artifact.get('inventory') or {}).get('complete')
                if complete is not True:
                    reasons.append('inventory_incomplete' if complete is False else 'inventory_unknown')
                relation = artifact.get('relationship_status')
                if relation in ('partial', 'unknown', 'unrecorded'):
                    reasons.append('relationship_partial' if relation == 'partial' else 'relationship_unknown')
                for facet, values in artifact.get('facets', {}).items():
                    if facet in ('decompile', 'cfg', 'assembly', 'calls', 'cil'):
                        reasons.extend(f'{facet}_{state}' for state in ('unknown', 'not_selected', 'failed', 'unavailable', 'partial')
                                       if type(values.get(state)) is int and values[state] > 0)
                affected = []
                for function in artifact.get('functions', []):
                    missing = {facet: state for facet, state in function.get('facets', {}).items()
                               if state in ('unknown', 'not_selected', 'failed', 'unavailable', 'partial')}
                    if missing:
                        row = dict(function_id=_identifier(function.get('id')), facets=missing)
                        if include_observations:
                            row['name'] = _text(function.get('name'), 200)
                        affected.append(row)
                add('static_incomplete', target=target, stage='static_' + target,
                    status=_safe_status(artifact.get('status')), coverage=_coverage_details(artifact), reason_codes=reasons,
                    affected_functions=affected[:500], affected_function_count=len(affected),
                    affected_functions_truncated=len(affected) > 500)

    stages = run.get('stages') or {}
    permitted = {'metadata', 'environment', 'report'}
    if scope['analysis_mode'] != 'dynamic':
        permitted.update(('static_original', 'static_reimplementation', 'structure'))
    if scope['analysis_mode'] != 'static':
        permitted.update(('image', 'dynamic'))
    if scope['ai_mode'] not in ('off', None):
        permitted.add('llm')
        if scope['ai_mode'] == 'agent':
            permitted.add('agent')
    for name in sorted(permitted):
        stage = stages.get(name) or {}
        if stage.get('status') not in ('failed', 'partial', 'blocked', 'cancelled'):
            continue
        if name == 'dynamic' and any(row['category'] == 'execution_unmeasured' for row in issues):
            continue
        if name.startswith('static_') and any(row.get('stage') == name for row in issues):
            continue
        add('ai_failure' if name in ('llm', 'agent') else 'stage_failure',
            stage=name, status=_safe_status(stage.get('status')), reason_codes=_stage_reason_codes(run, name))
    operation = run.get('last_operation') or {}
    if operation.get('kind') == 'reinterpret' and operation.get('status') in ('failed', 'cancelled'):
        add('ai_failure', operation_id=_identifier(operation.get('operation_id')),
            operation='reinterpret', status=_safe_status(operation.get('status')),
            analysis_result_status=_safe_status(run.get('status')),
            reason_codes=[operation['error_code']] if isinstance(operation.get('error_code'), str) and operation['error_code'] in REASON_LABELS
            else ['interpretation_failed'])

    counts = Counter(row['category'] for row in issues)
    files = run.get('files') or {}
    source_files = {}
    for target in ('original', 'reimplementation'):
        info = files.get(target) or {}
        source_files[target] = dict(name=_name(info.get('name') or info.get('path')),
                                    sha256=_hash(info.get('sha256')))
    return dict(
        schema_version=1, report_kind='recheck_failure_report',
        source=dict(run_id=_identifier(run.get('run_id')), attempt_id=_identifier(run.get('attempt_id')),
                    status=_safe_status(run.get('status')), created=_time(run.get('created')),
                    updated=_time(run.get('updated')), files=source_files),
        analysis_mode=scope['analysis_mode'], ai_mode=scope['ai_mode'],
        analysis_status={key: _safe_status(scope[key]) for key in ('static', 'dynamic', 'ai')},
        privacy=dict(observations_included=bool(include_observations),
                     warning=('입력·출력 원문과 함수 이름에는 민감 정보가 포함될 수 있습니다. 공유 전에 검토하세요. '
                              '보조 파일 본문·실행 파일·설정·인증은 포함하지 않으며 일부 내용은 크기 제한으로 잘릴 수 있습니다.')
                     if include_observations else '공유용 기본 보고서: 입력·출력·원문 로그·설정·인증·전체 경로를 제외했습니다.'),
        issue_count=len(issues), counts={category: counts[category] for category in CATEGORY_LABELS},
        issues=issues[:MAX_ITEMS], truncated_issues=len(issues) > MAX_ITEMS,
        scope_note='차이는 결함 확정이 아니며 미측정은 불일치나 일치로 계산하지 않습니다. 이 보고서는 기존 관찰을 정리하며 새 대상을 실행하지 않습니다.',
    )


def failure_report_html(report):
    esc = lambda value: html.escape(str(value))
    def table(rows):
        return '<table>' + ''.join('<tr><th>' + esc(label) + '</th><td>' + esc(value) + '</td></tr>'
                                  for label, value in rows) + '</table>'

    def stream_html(stream):
        if not isinstance(stream, dict) or stream.get('status') != 'observed':
            return '<p>유효한 원시 출력을 읽을 수 없습니다.</p>'
        text = stream.get('text') or '(빈 출력)'
        note = '<p>전체 ' + esc(stream.get('bytes', '?')) + ' bytes · SHA-256: <code>' + esc(stream.get('sha256', '')) + '</code></p>'
        if stream.get('truncated_for_report'):
            note += '<p><b>보고서 크기 제한으로 앞부분만 표시했습니다.</b></p>'
        return '<pre>' + esc(text) + '</pre>' + note

    source = report.get('source') or {}
    names = {'static': '정적분석', 'dynamic': '동적분석', 'both': '둘 다 분석'}
    pieces = ['<!doctype html><html lang="ko"><meta charset="utf-8"><title>Recheck 실패 항목 보고서</title>',
              '<style>body{font-family:Segoe UI,Malgun Gothic,sans-serif;max-width:1100px;margin:32px auto;padding:0 20px;color:#18344b}section{border:1px solid #ccd8e3;border-radius:8px;margin:16px 0;padding:18px}pre{white-space:pre-wrap;overflow-wrap:anywhere}table{border-collapse:collapse;width:100%}td,th{padding:8px;border:1px solid #ccd8e3;text-align:left}</style>',
              '<h1>Recheck · 실패 항목 보고서</h1>',
              '<p>' + esc(report.get('scope_note', '')) + '</p>',
              '<p>' + esc(report.get('privacy', {}).get('warning', '')) + '</p>',
              '<section><h2>분석 식별과 범위</h2>'
              + table([('기록 ID', source.get('run_id', '미기록')), ('시도 ID', source.get('attempt_id', '미기록')),
                       ('원래 분석 상태', status_label(source.get('status'))), ('분석 시각', source.get('created') or '미기록'),
                       ('선택한 분석', names.get(report.get('analysis_mode'), '미기록'))])]
    files = source.get('files') or {}
    for target in ('original', 'reimplementation'):
        file = files.get(target) or {}
        pieces.append(table([(target_label(target), file.get('name', '미기록')),
                             ('SHA-256', file.get('sha256') or '미기록')]))
    pieces.extend(['</section>',
              '<section><h2>항목 수</h2><table><tr><th>분류</th><th>개수</th></tr>']
    )
    for category, label in CATEGORY_LABELS.items():
        pieces.append(f'<tr><td>{esc(label)}</td><td>{esc(report.get("counts", {}).get(category, 0))}</td></tr>')
    pieces.append('</table></section>')
    if not report.get('issues'):
        pieces.append('<p>보고할 실패·차이 항목이 없습니다.</p>')
    for issue in report.get('issues', []):
        label = CATEGORY_LABELS.get(issue.get('category'), '확인 필요')
        subject = issue.get('test_id') or ('AI 설명 다시 받기' if issue.get('operation') == 'reinterpret'
                                          else stage_label(issue.get('stage', '')))
        if issue.get('target'):
            subject += ' · ' + target_label(issue['target'])
        state = {'different': '불일치', 'unmeasured': '미측정'}.get(issue.get('status'), status_label(issue.get('status')))
        pieces.append('<section><h2>' + esc(label) + ' · ' + esc(subject) + '</h2><p>상태: <b>'
                      + esc(state) + '</b> · 항목 ID: ' + esc(issue.get('issue_id', '')) + '</p>')
        if 'original_exit_code' in issue:
            pieces.append(table([('원본 종료 코드', issue.get('original_exit_code') if issue.get('original_exit_code') is not None else '미측정'),
                                 ('비교할 프로그램 종료 코드', issue.get('reimplementation_exit_code') if issue.get('reimplementation_exit_code') is not None else '미측정')]))
        pieces.append('<h3>원인·확인할 점</h3><ul>')
        for code in issue.get('reason_codes', []) or ['stage_incomplete']:
            pieces.append('<li>' + esc(_reason_label(code)) + ' <code>(' + esc(code) + ')</code></li>')
        pieces.append('</ul><h3>다음 조치</h3><ol>')
        for action in issue.get('next_actions', []):
            pieces.append('<li>' + esc(action) + '</li>')
        pieces.append('</ol>')
        coverage = issue.get('coverage') or {}
        if coverage:
            inventory = coverage.get('inventory') or {}
            pieces.append('<h3>수집 범위</h3>' + table([
                ('확인한 함수·메서드', inventory.get('returned', '미기록')),
                ('전체 함수·메서드', inventory.get('total') if inventory.get('total') is not None else '미확인')]))
            pieces.append('<table><tr><th>근거</th><th>관측</th><th>완료</th><th>부분</th><th>미선택</th><th>실패·불가</th><th>미확인</th></tr>')
            for facet, values in coverage.get('facets', {}).items():
                pieces.append('<tr>' + ''.join('<td>' + esc(value) + '</td>' for value in (
                    {'decompile': '의사 코드', 'cfg': '제어 흐름', 'assembly': '명령 정보', 'calls': '호출 관계', 'cil': '.NET CIL'}.get(facet, facet),
                    values.get('observed', 0), values.get('complete', 0), values.get('partial', 0), values.get('not_selected', 0),
                    values.get('failed', 0) + values.get('unavailable', 0), values.get('unknown', 0))) + '</tr>')
            pieces.append('</table>')
            affected = issue.get('affected_functions') or []
            if affected:
                pieces.append('<h3>미수집·부분 근거가 있는 함수</h3><table><tr><th>함수 ID</th><th>함수 이름</th><th>확인할 근거</th></tr>')
                facet_names = {'decompile': '의사 코드', 'cfg': '제어 흐름', 'assembly': '명령 정보', 'calls': '호출 관계', 'cil': '.NET CIL'}
                facet_states = {'unknown': '미확인', 'not_selected': '미선택', 'failed': '실패', 'unavailable': '수집 불가', 'partial': '부분 수집'}
                for function in affected:
                    detail = ' / '.join(facet_names.get(name, name) + ': ' + facet_states.get(state, state)
                                        for name, state in function.get('facets', {}).items())
                    pieces.append('<tr><td>' + esc(function.get('function_id')) + '</td><td>'
                                  + esc(function.get('name') or '원문 정보 제외') + '</td><td>' + esc(detail) + '</td></tr>')
                pieces.append('</table>')
                if issue.get('affected_functions_truncated'):
                    pieces.append('<p>총 ' + esc(issue.get('affected_function_count')) + '개 중 처음 500개를 표시했습니다.</p>')
        test = issue.get('input')
        if isinstance(test, dict):
            pieces.append('<h3>재현 입력</h3>' + table([('명령 인자 배열', json.dumps(test.get('args', []), ensure_ascii=False)),
                                                     ('제한 시간(초)', test.get('timeout_seconds')),
                                                     ('보조 파일 이름', ', '.join(test.get('auxiliary_files', [])) or '없음')])
                          + '<p>표준입력</p><pre>' + esc(test.get('stdin') or '(빈 입력)') + '</pre>')
            if test.get('truncated_for_report'):
                pieces.append('<p><b>입력은 보고서 크기 제한으로 앞부분만 포함했습니다.</b></p>')
            if test.get('env'):
                pieces.append('<p>환경 변수 · 인증 관련 이름의 값은 마스킹합니다.</p>' + table(list(test['env'].items())))
        observations = issue.get('observations')
        if isinstance(observations, dict):
            pieces.append('<h3>양쪽 실행 관찰</h3><p>출력은 UTF-8 표시용 텍스트입니다. 정확한 바이트·해시는 상세 JSON 보고서에서 확인하세요.</p>'
                          '<table><tr><th>항목</th><th>원본</th><th>비교할 프로그램</th></tr>')
            for channel, title in (('stdout', '표준출력'), ('stderr', '표준오류')):
                pieces.append('<tr><th>' + title + '</th>' + ''.join('<td>' + stream_html((observations.get(target) or {}).get(channel)) + '</td>'
                                                                 for target in ('original', 'reimplementation')) + '</tr>')
            pieces.append('</table>')
        pieces.append('</section>')
    pieces.append('</html>')
    return ''.join(pieces)


def export_failure_report(run, destination, *, format='html', include_observations=False):
    """Atomically create one new report. Existing files are never replaced."""
    if format not in ('html', 'json'):
        raise ValueError('실패 보고서는 HTML 또는 JSON으로 저장하세요.')
    destination = Path(destination)
    io_destination = filesystem_path(destination)
    if io_destination.exists() or io_destination.is_symlink():
        raise FileExistsError('기존 보고서를 덮어쓰지 않습니다.')
    if not io_destination.parent.is_dir():
        raise ValueError('존재하는 보고서 저장 폴더를 선택하세요.')
    report = build_failure_report(run, include_observations=include_observations)
    content = failure_report_html(report) if format == 'html' else json.dumps(report, ensure_ascii=False, indent=2)
    handle, temporary = tempfile.mkstemp(prefix='.recheck-failure-', suffix='.tmp', dir=io_destination.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(handle, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(content + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        if os.name == 'nt':
            os.rename(temporary, io_destination)
        else:
            os.link(temporary, io_destination)
        return destination
    finally:
        temporary.unlink(missing_ok=True)
