"""Describe retained evidence without mistaking operation completion for coverage.

This is a read-only projection: it performs no extraction, target execution,
network request, settings change or repair of historical records. Counts of
facets refer to the returned function inventory. A total denominator is exposed
only when the inventory explicitly supports it; no similarity or percentage is
invented for unobserved code or unexecuted inputs.
"""
from __future__ import annotations

from collections import Counter


_SCOPES = ('static', 'dynamic', 'both')
_FACETS = ('decompile', 'cfg', 'assembly', 'calls', 'cil')
_STATES = ('complete', 'partial', 'not_selected', 'failed', 'unavailable',
           'unknown', 'not_applicable')
_RECORDED_STATUSES = ('success', 'partial', 'failed', 'blocked', 'cancelled',
                      'running', 'pending', 'not_requested', 'unrecorded')


def _dict(value):
    return value if isinstance(value, dict) else {}


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _modes(run):
    legacy = run.get('mode') if isinstance(run.get('mode'), str) else None
    scope = run.get('analysis_mode')
    if scope not in _SCOPES:
        scope = {'static': 'static', 'dynamic': 'dynamic',
                 'automatic': 'both', 'agent': 'both'}.get(legacy)
    ai = run.get('ai_mode')
    if ai not in ('off', 'explain', 'agent'):
        settings = _dict(run.get('settings'))
        if legacy in ('static', 'dynamic'):
            ai = 'off'
        elif legacy == 'agent':
            ai = 'agent'
        elif (_dict(run.get('llm')) or settings.get('model')
              or settings.get('active_ai_profile') not in (None, '', 'legacy-local')):
            ai = 'explain'
        else:
            ai = None
    return scope, ai


def _stage_status(run, kind, requested, names):
    if requested is False:
        return 'not_requested'
    recorded = _dict(run.get('analysis_status')).get(kind)
    if recorded in _RECORDED_STATUSES:
        return recorded
    stages = _dict(run.get('stages'))
    states = [_dict(stages.get(name)).get('status') for name in names]
    if not states or any(value not in _RECORDED_STATUSES for value in states):
        return 'unrecorded'
    if all(value == 'success' for value in states):
        return 'success'
    if 'cancelled' in states:
        return 'cancelled'
    if 'success' in states or 'partial' in states:
        return 'partial'
    for state in ('failed', 'blocked', 'running', 'pending'):
        if state in states:
            return state
    return 'unrecorded'


def _native_decompile(function):
    status = function.get('decompile_status')
    observed = isinstance(function.get('decompile'), str) and bool(function['decompile'].strip())
    if status in ('not_selected', 'not_requested'):
        return 'not_selected'
    if status in ('failed', 'error'):
        return 'failed'
    if status in ('unavailable', 'not_available'):
        return 'unavailable'
    if not observed:
        return 'unavailable' if status in ('success', 'truncated') else 'unknown'
    if status == 'success' and function.get('decompile_truncated') is not True:
        return 'complete'
    # Text with no completion contract is observed, but not proved complete.
    return 'partial'


def _native_facet(function, name):
    declared = _dict(function.get('coverage')).get(name)
    if declared in ('not_selected', 'not_requested'):
        return 'not_selected'
    if declared in ('unavailable', 'not_collected', 'not_collected_managed'):
        return 'unavailable'
    if declared in ('failed', 'error'):
        return 'failed'
    if isinstance(declared, str) and declared.startswith('not_applicable'):
        return 'not_applicable'
    field = {'cfg': 'blocks', 'assembly': 'mnemonics', 'calls': 'calls'}[name]
    data = function.get(field)
    truncated = function.get('cfg_truncated' if name == 'cfg' else 'instructions_truncated') if name != 'calls' else None
    if truncated is True:
        return 'partial' if isinstance(data, list) and data else 'unavailable'
    if declared in ('complete_resolved_only', 'partial', 'truncated'):
        return 'partial' if isinstance(data, list) and (data or name == 'calls') else 'unavailable'
    if declared == 'complete':
        if not isinstance(data, list) or (name == 'cfg' and not data):
            return 'unavailable'
        return 'complete'
    if name == 'calls':
        return 'partial' if isinstance(data, list) and data else 'unknown'
    if isinstance(data, list) and data:
        return 'complete' if truncated is False else 'partial'
    return 'unknown'


def _function(function, managed):
    external = function.get('external') is True
    if external:
        facets = {name: 'not_applicable' for name in _FACETS}
    elif managed:
        body = _dict(_dict(function.get('managed_method')).get('body'))
        body_status = body.get('status')
        digest = body.get('il_sha256')
        cil = ('complete' if body_status == 'present' and isinstance(digest, str)
               and len(digest) == 64 and all(char in '0123456789abcdefABCDEF' for char in digest)
               else 'not_applicable' if body_status == 'absent'
               else 'unknown' if body_status is None else 'unavailable')
        facets = dict(decompile='not_applicable', cfg='unavailable',
                      assembly='not_applicable', calls=_native_facet(function, 'calls'), cil=cil)
    else:
        facets = dict(decompile=_native_decompile(function), cfg=_native_facet(function, 'cfg'),
                      assembly=_native_facet(function, 'assembly'),
                      calls=_native_facet(function, 'calls'), cil='not_applicable')
    return dict(id=str(function.get('id', '?')), name=str(function.get('name', '?')),
                external=external, kind='external' if external else 'managed' if managed else 'native',
                evidence_id=function.get('evidence_id'), facets=facets)


def _facet_summary(rows, name, inventory_complete):
    counts = {state: sum(row['facets'][name] == state for row in rows) for state in _STATES}
    eligible = len(rows) - counts['not_applicable']
    observed = counts['complete'] + counts['partial']
    status = ('not_applicable' if not eligible and rows else
              'complete' if eligible and counts['complete'] == eligible else
              'partial' if observed else
              'unknown' if counts['unknown'] or not rows else 'unobserved')
    return dict(eligible_returned=eligible,
                total_eligible=eligible if inventory_complete is True else None,
                observed=observed, status=status, **counts)


def _artifact_coverage(value):
    artifact = _dict(value)
    raw = _dict(_dict(artifact.get('raw_evidence')).get('coverage'))
    source_rows = artifact.get('functions')
    rows = source_rows if isinstance(source_rows, list) else []
    objects = [row for row in rows if isinstance(row, dict)]
    identifiers = [row.get('id') for row in objects]
    # Invalid/duplicate inventory rows cannot provide a complete denominator.
    valid_ids = [value for value in identifiers if isinstance(value, str) and value]
    id_counts = Counter(valid_ids)
    valid = [row for row in objects if isinstance(row.get('id'), str)
             and row['id'] and id_counts[row['id']] == 1]
    invalid = len(rows) - len(valid)
    managed = artifact.get('analysis_kind') == 'managed'
    functions = [_function(row, managed or isinstance(row.get('managed_method'), dict)) for row in valid]
    total = _count(artifact.get('total_functions'))
    observed_total = _count(raw.get('inventory_observed'))
    if total is None:
        total = observed_total
    explicit = raw.get('inventory_complete')
    complete = explicit if type(explicit) is bool else None
    conflict = ((total is not None and total < len(rows))
                or (total is not None and observed_total is not None and total != observed_total)
                or (_count(raw.get('inventory_returned')) not in (None, len(rows))))
    if invalid or conflict or artifact.get('functions_truncated') is True:
        complete = False
    elif total is not None:
        if len(rows) != total:
            complete = False
        elif complete is None:
            complete = artifact.get('complete') is not False and artifact.get('analysis_timed_out') is not True
    if not isinstance(source_rows, list):
        complete = None
    inventory = dict(returned=len(rows), total=total, complete=complete,
                     native=sum(row['kind'] == 'native' for row in functions),
                     external=sum(row['external'] for row in functions),
                     managed=sum(row['kind'] == 'managed' for row in functions), invalid_rows=invalid)
    relationship = _dict(artifact.get('relationship_coverage')).get('status')
    if relationship not in ('complete', 'partial', 'unknown', 'unavailable'):
        relationship = _dict(_dict(artifact.get('managed_metadata')).get('coverage')).get('state') if managed else None
    if relationship not in ('complete', 'partial', 'unknown', 'unavailable'):
        relationship = 'unknown' if artifact else 'unrecorded'
    facets = {name: _facet_summary(functions, name, complete) for name in _FACETS}
    reasons = artifact.get('partial_reasons')
    reasons = [value for value in reasons if isinstance(value, str)] if isinstance(reasons, list) else []
    has_gaps = (complete is not True or bool(invalid) or bool(reasons)
                or relationship != 'complete'
                or any(facet['status'] not in ('complete', 'not_applicable') for facet in facets.values()))
    return dict(analysis_kind='managed' if managed else 'native',
                status=artifact.get('status', 'unrecorded'), inventory=inventory,
                facets=facets, functions=functions, partial_reasons=reasons,
                relationship_status=relationship, has_gaps=has_gaps)


def _dynamic_coverage(run, requested, status):
    from .comparison import compare_behavior, observation_issues
    from .models import Settings

    registered_rows = run.get('tests')
    registered_ids = ([row.get('id') for row in registered_rows if isinstance(row, dict)]
                      if isinstance(registered_rows, list) else None)
    known = (registered_ids is not None and len(registered_ids) == len(registered_rows)
             and all(isinstance(value, str) and value for value in registered_ids)
             and len(registered_ids) == len(set(registered_ids)))
    registered = len(registered_ids) if known else None
    allowed = set(registered_ids) if known else None
    rows = run.get('dynamic') if isinstance(run.get('dynamic'), list) else []
    counts = Counter(row.get('test_id') for row in rows
                     if isinstance(row, dict) and isinstance(row.get('test_id'), str))
    duplicates = sorted(identifier for identifier, count in counts.items() if count > 1)
    foreign = sorted(identifier for identifier in counts if allowed is not None and identifier not in allowed)
    case_results = _dict(run.get('regression')).get('case_results')
    case_results = case_results if isinstance(case_results, list) else []
    unreproduced = {case['test_id'] for case in case_results if isinstance(case, dict)
                    and isinstance(case.get('test_id'), str)
                    and case.get('saved_input_reproduced') is False
                    and (allowed is None or case['test_id'] in allowed)}
    eligible = []
    invalid = 0
    categories = Counter()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('test_id'), str):
            invalid += 1
            continue
        identifier = row['test_id']
        if identifier in duplicates or identifier in foreign:
            continue
        issues = set(observation_issues(row.get('original'))) | set(observation_issues(row.get('reimplementation')))
        if identifier in unreproduced:
            issues.add('saved_input_not_reproduced')
        if 'invalid_observation' in issues:
            invalid += 1
        if issues:
            categories.update(issues)
        else:
            eligible.append(row)
    settings = _dict(run.get('settings'))
    recorded_policy = _dict(_dict(run.get('behavior')).get('normalization'))
    normalize = settings.get('normalize_newlines', recorded_policy.get('newlines'))
    trailing = settings.get('trim_trailing_whitespace', recorded_policy.get('trailing_whitespace'))
    policy_known = type(normalize) is bool and type(trailing) is bool
    matched = mismatched = None
    if policy_known:
        compared = compare_behavior(eligible, Settings(normalize_newlines=normalize, trim_trailing_whitespace=trailing))
        matched = compared['matched']
        mismatched = len(eligible) - matched
    elif not eligible:
        matched = mismatched = 0
    attempted = len(set(counts) & allowed) if allowed is not None else None
    return dict(requested=requested, status=status, registered=registered, attempted=attempted,
                measured=len(eligible), matched=matched, mismatched=mismatched,
                unmeasured=max(0, registered - len(eligible)) if registered is not None else None,
                not_run=max(0, registered - attempted) if registered is not None else None,
                invalid_observations=invalid, duplicate_ids=duplicates, unregistered_ids=foreign,
                unreproduced_inputs=len(unreproduced), unreproduced_ids=sorted(unreproduced),
                category_counts=dict(categories), comparison_policy_known=policy_known,
                observations_outside_selected_scope=requested is False and bool(rows),
                scope='등록한 입력 중 양쪽의 유효한 원시 관찰이 있는 쌍만 측정; 모든 입력의 동등성 증명이 아닙니다.')


def coverage_summary(run):
    """Return mode, per-facet static scope and strict raw dynamic coverage.

    Facet ``observed`` is ``complete + partial``; truncated pseudocode is observed
    but remains partial. ``eligible_returned`` concerns only retained inventory;
    ``total_eligible`` is unknown when that inventory is incomplete or unrecorded.
    Native external functions and managed bodyless methods are not counted as
    missing native decompilations or missing CIL bodies, respectively.
    """
    run = _dict(run)
    scope, ai_mode = _modes(run)
    static_requested = scope != 'dynamic' if scope is not None else None
    dynamic_requested = scope != 'static' if scope is not None else None
    ai_requested = ai_mode != 'off' if ai_mode is not None else None
    artifacts = _dict(run.get('static'))
    targets = {target: _artifact_coverage(artifacts.get(target))
               for target in ('original', 'reimplementation')}
    static = dict(requested=static_requested,
                  status=_stage_status(run, 'static', static_requested,
                                       ('metadata', 'static_original', 'static_reimplementation', 'structure')),
                  targets=targets,
                  has_gaps=static_requested is not False and any(value['has_gaps'] for value in targets.values()),
                  observations_outside_selected_scope=static_requested is False and bool(artifacts))
    dynamic = _dynamic_coverage(run, dynamic_requested,
                                _stage_status(run, 'dynamic', dynamic_requested, ('metadata', 'image', 'dynamic')))
    ai = dict(requested=ai_requested,
              status=_stage_status(run, 'ai', ai_requested, ('llm', 'agent') if ai_mode == 'agent' else ('llm',)))
    return dict(schema_version='1.0', analysis_mode=scope, ai_mode=ai_mode,
                static=static, dynamic=dynamic, ai=ai)
