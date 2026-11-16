from collections import Counter
from bisect import bisect_left
import base64
import difflib
import re


def _name(fn):
    name = fn.get('name', '')
    return '' if re.match(r'^(FUN_|sub_|thunk_|LAB_|fcn\.)', name) else name


def _ratio(a, b):
    return 1 - abs(a - b) / max(a, b, 1)


def _features(fn):
    nodes = fn.get('blocks', fn.get('cfg_nodes', []))
    edges = fn.get('edges', fn.get('cfg_edges', []))
    cfg = fn.get('cfg', {})
    return (
        len(cfg.get('nodes', nodes)),
        len(cfg.get('edges', edges)),
        len(fn.get('calls', [])),
        Counter(fn.get('mnemonics', fn.get('normalized_instructions', []))),
    )


def similarity(a, b):
    na, ea, ca, ma = _features(a)
    nb, eb, cb, mb = _features(b)
    union = sum((ma | mb).values())
    instr = sum((ma & mb).values()) / union if union else 0
    name = float(bool(_name(a)) and _name(a) == _name(b))
    return round(
        0.25 * name
        + 0.2 * _ratio(na, nb)
        + 0.15 * _ratio(ea, eb)
        + 0.1 * _ratio(ca, cb)
        + 0.3 * instr,
        6,
    )


def match_functions(original, rebuilt):
    candidates = []
    examined = 0
    ordered = sorted(rebuilt, key=lambda f: len(f.get('mnemonics', [])))
    sizes = [len(f.get('mnemonics', [])) for f in ordered]
    by_name = {}
    for f in rebuilt:
        if _name(f):
            by_name.setdefault(_name(f), []).append(f)
    for a in original:
        # Bound the candidate set for large real-world files; disclose candidate omission.
        if len(rebuilt) > 256:
            at = bisect_left(sizes, len(a.get('mnemonics', [])))
            pool = ordered[max(0, at - 64) : at + 64] + by_name.get(_name(a), [])[:128]
            pool = list({id(b): b for b in pool}.values())
        else:
            pool = rebuilt
        for b in pool:
            examined += 1
            score = similarity(a, b)
            if score >= 0.5:
                candidates.append(
                    dict(
                        original=a.get('id', a.get('entry', a.get('address'))),
                        reimplementation=b.get('id', b.get('entry', b.get('address'))),
                        score=score,
                        reason='이름·CFG 노드/간선·호출 수·명령 빈도',
                        source='Recheck',
                        verification_status='heuristic',
                    )
                )
    candidates.sort(key=lambda x: (-x['score'], str(x['original']), str(x['reimplementation'])))
    used_a, used_b, matches = set(), set(), []
    for candidate in candidates:
        a, b = candidate['original'], candidate['reimplementation']
        if a in used_a or b in used_b:
            continue
        alternatives = [
            x
            for x in candidates
            if x is not candidate
            and (x['original'] == a or x['reimplementation'] == b)
            and abs(x['score'] - candidate['score']) < 0.03
        ]
        candidate['ambiguous'] = bool(alternatives)
        candidate['alternatives'] = alternatives[:5]
        matches.append(candidate)
        used_a.add(a)
        used_b.add(b)
    fid = lambda f: f.get('id', f.get('entry', f.get('address')))
    originals = {fid(f): f for f in original}
    rebuilds = {fid(f): f for f in rebuilt}
    code_differences = []
    for match in matches:
        left, right = originals[match['original']], rebuilds[match['reimplementation']]
        ca, cb = left.get('decompile'), right.get('decompile')
        if isinstance(ca, str) and isinstance(cb, str):
            match['decompile_text_equal'] = ca == cb
            if ca != cb:
                diff = list(
                    difflib.unified_diff(
                        ca.splitlines(),
                        cb.splitlines(),
                        fromfile='original/' + left.get('name', ''),
                        tofile='reimplementation/' + right.get('name', ''),
                        lineterm='',
                    )
                )
                code_differences.append(
                    dict(
                        original=match['original'],
                        reimplementation=match['reimplementation'],
                        original_name=left.get('name'),
                        reimplementation_name=right.get('name'),
                        evidence_ids=[
                            f['evidence_id'] for f in (left, right) if f.get('evidence_id')
                        ],
                        diff='\n'.join(diff[:80]),
                        truncated=len(diff) > 80,
                        source='Ghidra 추정 디컴파일의 텍스트 비교; 원본 소스 또는 의미 동등성 판정 아님',
                    )
                )
    return dict(
        matches=matches,
        unmatched_original=[fid(x) for x in original if fid(x) not in used_a],
        unmatched_reimplementation=[fid(x) for x in rebuilt if fid(x) not in used_b],
        formula='0.25×비자동이름일치 + 0.20×노드수비 + 0.15×간선수비 + 0.10×호출수비 + 0.30×명령다중집합Jaccard; 수비=1-|a-b|/max(a,b,1); 후보≥0.5; 모호성차<0.03',
        decompile_differences=code_differences,
        score=sum(x['score'] for x in matches) / max(len(original), len(rebuilt), 1),
        producer='Recheck (휴리스틱, 의미적 동등성 증명 아님)',
        candidate_search=dict(
            examined_pairs=examined,
            total_possible_pairs=len(original) * len(rebuilt),
            policy='재구현 함수가 256개를 넘으면 명령 수 인접 128개와 동일 이름 최대128개 후보; 나머지는 미탐색',
            omitted_pairs=len(original) * len(rebuilt) - examined,
        ),
    )


def compare_static_artifacts(original, rebuilt):
    """Compare native dossiers or execution-free managed member observations."""
    kinds = {value.get('analysis_kind', 'native') for value in (original, rebuilt)}
    if len(kinds) != 1:
        raise ValueError('관리 코드와 네이티브 코드의 함수 구조는 같은 기준으로 비교할 수 없습니다.')
    if kinds == {'native'}:
        return match_functions(original['functions'], rebuilt['functions'])
    if kinds != {'managed'}:
        raise ValueError('지원하지 않는 정적 비교 자료입니다.')

    def index(artifact):
        groups = {}
        for fn in artifact['functions']:
            member = fn.get('managed_method', {})
            signature = member.get('signature', {})
            resolved = signature.get('resolved_sha256')
            if (member.get('declaring_type') is not None
                    and signature.get('parse_status') == 'decoded'
                    and isinstance(resolved, str) and re.fullmatch('[a-fA-F0-9]{64}', resolved)):
                key = (member['declaring_type'], member['name'], resolved)
                groups.setdefault(key, []).append(fn)
        return groups

    left, right = index(original), index(rebuilt)
    matches, differences, used_left, used_right = [], [], set(), set()
    fields = ('il_sha256', 'max_stack', 'init_locals', 'local_var_sig_token', 'exception_regions')
    for key, candidates in left.items():
        peers = right.get(key, [])
        if len(candidates) != 1 or len(peers) != 1:
            continue
        a, b = candidates[0], peers[0]
        body_a, body_b = a['managed_method']['body'], b['managed_method']['body']
        observed = all(body.get('status') == 'present' and body.get('il_sha256') for body in (body_a, body_b))
        values = [{field: body.get(field) for field in fields} for body in (body_a, body_b)]
        state = 'unobserved' if not observed else 'equal_bytes_and_headers' if values[0] == values[1] else 'different'
        pair = dict(original=a['id'], reimplementation=b['id'], score=1.0,
                    reason='선언 타입·메서드 이름·해석된 시그니처가 양쪽에서 유일하게 일치',
                    source='Recheck / REA 관리 코드 근거', verification_status='identity_only',
                    ambiguous=False, alternatives=[], cil_observation=state)
        matches.append(pair)
        used_left.add(a['id'])
        used_right.add(b['id'])
        if state == 'different':
            differences.append(dict(original=a['id'], reimplementation=b['id'],
                original_name=a['name'], reimplementation_name=b['name'],
                evidence_ids=[item['evidence_id'] for item in (a, b) if item.get('evidence_id')],
                original_cil=values[0], reimplementation_cil=values[1],
                source='REA가 읽은 CIL 원시 바이트 해시·메서드 헤더·예외 영역의 비교'))
    return dict(analysis_kind='managed', matches=matches,
        unmatched_original=[fn['id'] for fn in original['functions'] if fn['id'] not in used_left],
        unmatched_reimplementation=[fn['id'] for fn in rebuilt['functions'] if fn['id'] not in used_right],
        managed_differences=differences, decompile_differences=[],
        score=len(matches) / max(len(original['functions']), len(rebuilt['functions']), 1),
        formula='양쪽에서 유일한 선언 타입·메서드 이름·해석된 시그니처의 대응 수 / 큰 쪽의 메서드 수',
        producer='Recheck 관리 메서드 대응 범위; 의미 동등성 또는 실행 일치율이 아님',
        limitations=['CIL 해시·메타데이터 토큰은 빌드에 종속됩니다. 동일 관측은 모든 입력의 동등성을 증명하지 않습니다.',
                     '본문이 없거나 불완전하면 CIL 비교는 미관측이며, 시그니처가 모호한 메서드는 대응시키지 않습니다.'])


def observation_issues(observation):
    """Reasons a retained raw observation cannot count as measured.

    Older valid observations may omit ``infrastructure_ok``. Missing output is
    never an empty output: successful records must carry both Base64 strings
    and an actual integer exit code (bool is deliberately excluded).
    """
    if not isinstance(observation, dict):
        return ['invalid_observation']
    issues = []
    for flag, category in (
        ('cancelled', 'cancelled'), ('timed_out', 'timed_out'),
        ('oom_killed', 'oom'), ('truncated', 'output_truncated'),
    ):
        if flag in observation and type(observation[flag]) is not bool:
            issues.append('invalid_observation')
        elif observation.get(flag) is True:
            issues.append(category)
    if 'infrastructure_ok' in observation:
        if type(observation['infrastructure_ok']) is not bool:
            issues.append('invalid_observation')
        elif observation['infrastructure_ok'] is False and not issues:
            issues.append('infrastructure_failure')
    status = observation.get('status')
    if status == 'blocked':
        issues.append('unsupported_or_blocked')
    elif status == 'cancelled':
        issues.append('cancelled')
    elif status not in ('success', 'completed', 'exited'):
        if not issues:
            issues.append('infrastructure_failure')
    if status in ('success', 'completed', 'exited'):
        if type(observation.get('exit_code')) is not int:
            issues.append('invalid_observation')
        for field in ('stdout_b64', 'stderr_b64'):
            if not isinstance(observation.get(field), str):
                issues.append('invalid_observation')
                continue
            try:
                base64.b64decode(observation[field], validate=True)
            except (ValueError, TypeError):
                issues.append('invalid_observation')
    return sorted(set(issues))


def compare_behavior(rows, settings):
    def normalize(value):
        if settings.normalize_newlines:
            value = value.replace(b'\r\n', b'\n').replace(b'\r', b'\n')
        if settings.trim_trailing_whitespace:
            value = b'\n'.join(line.rstrip(b' \t') for line in value.split(b'\n'))
        return value

    comparisons = []
    for row in rows:
        a, b = row.get('original', {}), row.get('reimplementation', {})
        reasons = sorted(set(observation_issues(a) + observation_issues(b)))
        eligible = not reasons

        def output(x, key):
            return normalize(base64.b64decode(x[key + '_b64'], validate=True))

        equal = (
            eligible
            and a.get('exit_code') == b.get('exit_code')
            and all(output(a, key) == output(b, key) for key in ('stdout', 'stderr'))
        )
        comparisons.append(
            dict(
                test_id=row['test_id'],
                execution_status='measured' if eligible else 'unmeasured',
                match=equal if eligible else None,
                observation_categories=(
                    sorted(set(reasons)) if reasons else ['matched' if equal else 'mismatched']
                ),
                original_exit_code=a.get('exit_code') if isinstance(a, dict) else None,
                reimplementation_exit_code=b.get('exit_code') if isinstance(b, dict) else None,
            )
        )
    denominator = sum(x['execution_status'] == 'measured' for x in comparisons)
    numerator = sum(x['match'] is True for x in comparisons)
    return dict(
        tests=comparisons,
        matched=numerator,
        denominator=denominator,
        registered=len(rows),
        match_rate=numerator / denominator if denominator else None,
        category_counts={
            category: sum(category in x['observation_categories'] for x in comparisons)
            for category in (
                'matched',
                'mismatched',
                'infrastructure_failure',
                'unsupported_or_blocked',
                'cancelled',
                'timed_out',
                'oom',
                'output_truncated',
                'invalid_observation',
            )
        },
        formula='일치한 유효 테스트 쌍 / 양쪽 모두 정상 수집된 테스트 쌍 (시간초과·취소·OOM·출력잘림·인프라실패 제외)',
        normalization=dict(
            newlines=settings.normalize_newlines,
            trailing_whitespace=settings.trim_trailing_whitespace,
        ),
        scope='등록하여 실행한 입력 범위에서만 확인한 결과',
        producer='Recheck',
    )
