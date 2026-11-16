"""새 전체 실행과 관찰을 재사용하는 독립 추가 AI 요청을 분리해 보존한다.

원본 샘플/설정/과거 결과를 수정하지 않는다. 실행 결과는 항상 새 폴더에 쓴다.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import copy
import hashlib
import html
from html.parser import HTMLParser
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from recheck.engine import Engine
from recheck.models import Settings, atomic_json, data_root, load_settings, validate_tests
from recheck.storage import new_id, now

DEFAULT_SAMPLES = ('gcd_algorithms', 'range_overlap', 'discount_boundary')
EXTRA_AI_SAMPLES = frozenset(('gcd_algorithms', 'range_overlap'))


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(part)
    return result.hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def confined(root, relative):
    root = Path(root).resolve()
    raw = Path(relative)
    path = (root / raw).resolve()
    if raw.is_absolute() or path == root or root not in path.parents:
        raise ValueError('샘플 경계를 벗어난 경로입니다: ' + str(relative))
    for part in (root / raw, *(root / raw).parents):
        if part == root:
            break
        if part.is_symlink() or getattr(part, 'is_junction', lambda: False)():
            raise ValueError('샘플의 링크 경로는 허용하지 않습니다: ' + str(relative))
    return path


def load_sample(samples_root, sample_id):
    samples_root = Path(samples_root).resolve(strict=True)
    catalog = read(samples_root / 'catalog.json')
    rows = [row for row in catalog['samples'] if row['sample_id'] == sample_id]
    if len(rows) != 1:
        raise ValueError('카탈로그 sample_id가 없거나 중복됩니다: ' + sample_id)
    row = rows[0]
    folder = confined(samples_root, row['sample_directory'])
    paths = {key: confined(samples_root, row['paths'][key]) for key in ('tests', 'oracle')}
    paths.update(
        {
            target: confined(samples_root, row['paths']['binaries'][target])
            for target in ('original', 'reimplementation')
        }
    )
    sums = {}
    for line in (folder / 'SHA256SUMS').read_text(encoding='utf-8-sig').splitlines():
        expected, relative = line.split('  ', 1)
        if relative in sums:
            raise ValueError('SHA256SUMS 경로가 중복됩니다: ' + relative)
        sums[relative] = expected
    identities = {}
    for key, path in paths.items():
        if folder not in path.parents:
            raise ValueError('카탈로그가 다른 샘플을 가리킵니다: ' + key)
        relative = path.relative_to(folder).as_posix()
        actual = digest(path)
        if sums.get(relative) != actual:
            raise ValueError('샘플 SHA-256이 기준과 다릅니다: ' + str(path))
        identities[key] = {'path': str(path), 'sha256': actual, 'size': path.stat().st_size}
    tests = [test.model_dump() for test in validate_tests(read(paths['tests']))]
    oracle_rows = read(paths['oracle'])['cases']
    oracle = {item['test_id']: item for item in oracle_rows}
    if len(oracle) != len(oracle_rows) or set(oracle) != {test['id'] for test in tests}:
        raise ValueError('입력과 oracle ID 집합이 다릅니다: ' + sample_id)
    for item in oracle.values():
        if type(item.get('match')) is not bool or item['match'] != (
            item['original'] == item['reimplementation']
        ):
            raise ValueError('oracle의 일치 판정과 출력이 다릅니다: ' + item['test_id'])
    return {
        'sample_id': sample_id,
        'tests': tests,
        'oracle': oracle,
        'paths': paths,
        'manifest': identities,
        'catalog_sha256': digest(samples_root / 'catalog.json'),
    }


def check_observations(run, sample):
    expected_ids = {test['id'] for test in sample['tests']}
    tests_by_id = {test['id']: test for test in sample['tests']}
    errors = []
    rows = run.get('dynamic', [])
    actual_ids = [row.get('test_id') for row in rows]
    if len(actual_ids) != len(expected_ids) or set(actual_ids) != expected_ids:
        errors.append('동적 관찰의 ID 누락·중복 또는 추가')
    pairs = []
    for row in rows:
        tid = row.get('test_id')
        if tid not in sample['oracle']:
            continue
        expected = sample['oracle'][tid]
        registered = tests_by_id[tid]
        for target in ('original', 'reimplementation'):
            actual = row.get(target, {})
            good = (
                actual.get('status') in ('success', 'completed', 'exited')
                and actual.get('infrastructure_ok') is True
                and not any(
                    actual.get(flag)
                    for flag in ('timed_out', 'oom_killed', 'truncated', 'cancelled')
                )
                and not actual.get('cleanup_errors')
            )
            actual_input = actual.get('actual_input', {})
            good = good and all(
                actual_input.get(key) == registered.get(key, default)
                for key, default in (('args', []), ('stdin', ''), ('env', {}))
            )
            # 세 안정화 사례는 보조 파일이 없다. 자료를 새로 추가하면 별도의
            # 이름·해시 계약을 작성해야 하며 이 도구가 추정해서 통과시키지 않는다.
            good = (
                good
                and not registered.get('aux_files')
                and actual_input.get('auxiliary_files') == []
            )
            for stream in ('stdout', 'stderr'):
                try:
                    raw = base64.b64decode(actual[stream + '_b64'], validate=True)
                    good = good and raw == expected[target][stream].encode('utf-8')
                    good = good and actual.get(stream) == expected[target][stream]
                except (KeyError, TypeError, ValueError):
                    good = False
            good = good and actual.get('target_exit_code') == expected[target]['exit_code']
            pairs.append({'test_id': tid, 'target': target, 'passed': bool(good)})
            if not good:
                errors.append(tid + '/' + target + ': 원시 출력·종료·실행 상태가 oracle와 다름')
    behavior = run.get('behavior', {})
    measured = behavior.get('tests', [])
    ids = [row.get('test_id') for row in measured]
    if len(ids) != len(expected_ids) or set(ids) != expected_ids:
        errors.append('비교 결과의 ID 누락·중복 또는 추가')
    for row in measured:
        tid = row.get('test_id')
        if tid not in sample['oracle'] or row.get('match') is not sample['oracle'][tid]['match']:
            errors.append('원본/후보의 일치 판정이 oracle와 다름: ' + str(tid))
    expected_matched = sum(row['match'] for row in sample['oracle'].values())
    if (
        behavior.get('registered') != len(expected_ids)
        or behavior.get('denominator') != len(expected_ids)
        or behavior.get('matched') != expected_matched
    ):
        errors.append('등록·유효·일치 입력 수가 oracle와 다름')
    return {
        'passed': not errors,
        'errors': errors,
        'pairs': pairs,
        'expected': {
            'registered': len(expected_ids),
            'matched': expected_matched,
            'mismatched': len(expected_ids) - expected_matched,
        },
        'actual': {key: behavior.get(key) for key in ('registered', 'denominator', 'matched')},
    }


def independent_record(run, settings, source_result):
    fresh = copy.deepcopy(run)
    fresh.update(
        run_id=new_id('run'),
        attempt_id=new_id('attempt'),
        status='running',
        created=now(),
        settings=copy.deepcopy(settings),
        llm={},
        attempts=[],
        events=[],
        diagnostics=[],
        agent={},
    )
    for key in ('llm_history', 'questions', 'commands', 'previous_results', 'reproduction'):
        fresh.pop(key, None)
    # 이전 해석의 교정 피드백도 새 독립 요청의 분석 근거에 섞지 않는다.
    # 원문은 source_result 및 그 해시로 계속 보존한다.
    fresh['evidence'] = [
        item for item in fresh.get('evidence', []) if item.get('kind') != 'llm_failure'
    ]
    fresh['stages']['llm'] = {'status': 'pending'}
    fresh['stages']['report'] = {'status': 'pending'}
    fresh['analysis_reuse'] = {
        'kind': '정적·동적 관찰만 재사용; 이전 AI 응답은 입력에서 제외; 새 AI 요청',
        'source_run_id': run['run_id'],
        'source_attempt_id': run['attempt_id'],
        'source_result': str(Path(source_result).resolve()),
        'source_sha256': digest(source_result),
        'evidence_provenance': '근거 ID 및 원래 run/attempt는 원문 그대로 보존',
    }
    return fresh


def llm_usage(llm):
    responses = llm.get('responses', [])
    failures = {item.get('attempt'): item.get('error') for item in llm.get('validation_errors', [])}
    attempts = [
        {
            'attempt': index,
            'done_reason': response.get('done_reason'),
            'input_tokens': response.get('prompt_eval_count'),
            'output_tokens': response.get('eval_count'),
            'validation_error': failures.get(index),
        }
        for index, response in enumerate(responses, 1)
    ]
    accepted = 1 if llm.get('status') == 'success' and responses else 0

    def total(key):
        values = [response.get(key) for response in responses]
        return sum(values) if values and all(type(value) is int for value in values) else None

    return {
        'status': llm.get('status'),
        'model': llm.get('model'),
        'settings': llm.get('settings'),
        'response_count': len(responses),
        'rejected_response_count': len(responses) - accepted,
        'accepted_response_count': accepted,
        'input_tokens': total('prompt_eval_count'),
        'output_tokens': total('eval_count'),
        'attempts': attempts,
        'cost': None,
        'cost_status': '로컬 모델; 금전 비용은 측정하지 않음',
    }


def finish_extra_report(engine, run):
    engine._stage(run, 'report', 'running')
    engine.report(run['run_id'])
    engine._stage(run, 'report', 'success')
    engine.report(run['run_id'])


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids, self.references = set(), set()

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if 'id' in values:
            self.ids.add(values['id'])
        if values.get('href', '').startswith('#'):
            self.references.add(values['href'][1:])


def assess(engine, run, sample, kind):
    destination = engine.store.run_dir(run['run_id'])
    llm = run.get('llm', {})
    atomic_json(destination / 'llm-raw-responses.json', llm.get('responses', []))
    atomic_json(destination / 'llm-context.json', llm.get('context', {}))
    atomic_json(destination / 'llm-validation-errors.json', llm.get('validation_errors', []))
    atomic_json(destination / 'llm-citation-completions.json', llm.get('citation_completions', []))
    atomic_json(destination / 'sample-input-manifest.json', sample['manifest'])
    observations = check_observations(run, sample)
    identity_errors = []
    for target in ('original', 'reimplementation'):
        expected = sample['manifest'].get(target, {})
        recorded = run.get('files', {}).get(target, {})
        if not expected.get('sha256') or recorded.get('sha256') != expected['sha256']:
            identity_errors.append(target + ': 등록 바이너리 SHA-256 불일치')
        snapshot = Path(recorded.get('snapshot', ''))
        if not snapshot.is_file() or digest(snapshot) != expected.get('sha256'):
            identity_errors.append(target + ': 보존한 바이너리 사본 SHA-256 불일치')
        if any(
            row.get(target, {}).get('binary_sha256') != expected.get('sha256')
            for row in run.get('dynamic', [])
        ):
            identity_errors.append(target + ': 실행 바이너리 SHA-256 불일치')
    reference = {'passed': False, 'error': None, 'completion_reconstructed': None}
    if llm.get('status') == 'success' and llm.get('responses'):
        try:
            raw_text = llm['responses'][-1]['message']['content']
            validated_text = raw_text
            if 'validated_text' in llm:
                completion = engine._complete_citations(run, raw_text, llm['evidence_ids'])
                recorded = (
                    llm.get('citation_completions', [])[-1]
                    if llm.get('citation_completions')
                    else {}
                )
                if (
                    completion.get('status') != 'completed'
                    or completion['normalized_text'] != llm['validated_text']
                ):
                    raise ValueError(
                        '저장한 보정 응답을 실제 원문·전달 근거로 재구성할 수 없습니다'
                    )
                if any(
                    recorded.get(key) != completion.get(key)
                    for key in (
                        'raw_text_sha256',
                        'normalized_text_sha256',
                        'normalized_text',
                        'additions',
                        'refusals',
                        'status',
                    )
                ):
                    raise ValueError('인용 보정 감사 기록이 원문 기반 재구성과 다릅니다')
                validated_text = completion['normalized_text']
                reference['completion_reconstructed'] = True
            interpretation = engine._validate_interpretation(
                run, validated_text, llm['evidence_ids']
            )
            if interpretation != llm.get('interpretation'):
                raise ValueError('표시·저장된 해석이 엄격 검증한 응답과 다릅니다')
            reference['passed'] = True
        except (KeyError, ValueError) as exc:
            reference['error'] = str(exc)
    else:
        reference['error'] = llm.get('error') or '유효한 AI 해석 없음'
    report = destination / 'report.html'
    links = Links()
    report_errors = []
    if report.is_file():
        document = report.read_text(encoding='utf-8')
        links.feed(document)
        if html.escape(run['run_id']) not in document:
            report_errors.append('현재 실행 ID 없음')
        behavior = run.get('behavior', {})
        if f"({behavior.get('matched', 0)}/{behavior.get('denominator', 0)})" not in document:
            report_errors.append('현재 일치 입력 수 없음')
        for row in run.get('dynamic', []):
            if html.escape(row['test_id']) not in document:
                report_errors.append(row['test_id'] + ': 입력 ID 없음')
            for target in ('original', 'reimplementation'):
                for stream in ('stdout', 'stderr'):
                    value = row.get(target, {}).get(stream, '')
                    encoded = html.escape(json.dumps(value, ensure_ascii=False))
                    if value and html.escape(value) not in document and encoded not in document:
                        report_errors.append(
                            row['test_id'] + '/' + target + '/' + stream + ': 관찰 출력 없음'
                        )
    report_check = {
        'exists': report.is_file(),
        'broken_links': sorted(links.references - links.ids),
        'errors': report_errors,
        'path': str(report),
        'sha256': digest(report) if report.is_file() else None,
    }
    stages = (
        'metadata',
        'static_original',
        'static_reimplementation',
        'structure',
        'image',
        'dynamic',
        'llm',
        'report',
    )
    passed = (
        run.get('status') == 'success'
        and not identity_errors
        and observations['passed']
        and reference['passed']
        and report_check['exists']
        and not report_check['broken_links']
        and not report_errors
        and all(run.get('stages', {}).get(stage, {}).get('status') == 'success' for stage in stages)
    )
    record = {
        'kind': kind,
        'sample_id': sample['sample_id'],
        'run_id': run['run_id'],
        'attempt_id': run['attempt_id'],
        'status': 'VERIFIED' if passed else 'FAILED',
        'run_status': run['status'],
        'stages': run['stages'],
        'observations': observations,
        'identity': {'passed': not identity_errors, 'errors': identity_errors},
        'references': reference,
        'report': report_check,
        'usage': llm_usage(llm),
        'analysis_reuse': run.get('analysis_reuse'),
        'result': str(destination / 'result.json'),
        'result_sha256': digest(destination / 'result.json'),
    }
    atomic_json(destination / 'reference-verification.json', record)
    return record


async def verify(args):
    samples_root = Path(args.samples_root).resolve(strict=True)
    chosen = args.sample or list(DEFAULT_SAMPLES)
    if len(set(chosen)) != len(chosen):
        raise ValueError('--sample을 중복 지정하지 마세요')
    samples = [load_sample(samples_root, sample_id) for sample_id in chosen]
    output = (
        Path(args.output).resolve()
        if args.output
        else ROOT / 'verification' / new_id('reference-stability')
    )
    output.mkdir(parents=True, exist_ok=False)
    original_settings = load_settings()
    settings_values = original_settings.model_dump()
    for name in ('num_ctx', 'num_predict'):
        if getattr(args, name) is not None:
            settings_values[name] = getattr(args, name)
    settings = Settings.model_validate(settings_values)
    if not settings.model:
        raise ValueError('사용자가 선택한 모델이 없습니다. 앱에서 로컬 모델을 선택하세요')
    settings_file = data_root() / 'settings.json'
    settings_before = digest(settings_file) if settings_file.is_file() else None
    summary = {
        'created': now(),
        'status': 'RUNNING',
        'output': str(output),
        'scope': '새 Engine.analyze 세 사례와 관찰 재사용 독립 추가 AI를 분리',
        'source_root': str(ROOT),
        'samples_root': str(samples_root),
        'engine_sha256': digest(ROOT / 'recheck/engine.py'),
        'settings': settings.model_dump(),
        'original_settings': original_settings.model_dump(),
        'local_overrides': {
            key: settings_values[key]
            for key in ('num_ctx', 'num_predict')
            if settings_values[key] != original_settings.model_dump()[key]
        },
        'settings_file_before_sha256': settings_before,
        'records': [],
    }
    atomic_json(output / 'summary.json', summary)
    engine = Engine(
        settings,
        data_dir=output / 'data',
        on_event=lambda event: (
            print(event.get('stage', '') + ' ' + event.get('status', ''), flush=True)
            if event.get('kind') == 'stage'
            else None
        ),
    )
    try:
        for sample in samples:
            sanitized = [
                {**test, 'description': '', 'source': '등록 샘플 입력'} for test in sample['tests']
            ]
            print('새 전체 분석: ' + sample['sample_id'], flush=True)
            run = await engine.analyze(
                str(sample['paths']['original']),
                str(sample['paths']['reimplementation']),
                sanitized,
            )
            record = assess(engine, run, sample, '새 정적·동적·AI 전체 실행')
            summary['records'].append(record)
            atomic_json(output / 'summary.json', summary)
            print(sample['sample_id'] + ' ' + record['status'] + ' ' + run['run_id'], flush=True)
            if sample['sample_id'] not in EXTRA_AI_SAMPLES:
                continue
            for index in range(args.additional_ai):
                if not check_observations(run, sample)['passed']:
                    summary['records'].append(
                        {
                            'sample_id': sample['sample_id'],
                            'kind': '독립 추가 AI',
                            'status': 'BLOCKED',
                            'reason': '재사용할 정적·동적 기준 관찰 검증 실패',
                        }
                    )
                    break
                source_result = engine.store.run_dir(run['run_id']) / 'result.json'
                fresh = independent_record(run, settings.model_dump(), source_result)
                engine.store.save(fresh)
                atomic_json(engine.store.run_dir(fresh['run_id']) / 'tests.json', fresh['tests'])
                atomic_json(
                    engine.store.run_dir(fresh['run_id']) / 'settings.json', fresh['settings']
                )
                print('독립 추가 AI ' + str(index + 1) + ': ' + sample['sample_id'], flush=True)
                extra = await engine.reinterpret(fresh['run_id'])
                extra['attempts'] = [
                    {
                        'attempt_id': extra['attempt_id'],
                        'finished': now(),
                        'status': extra['status'],
                        'scope': 'AI만 새 요청; 정적·동적 관찰 재사용',
                        'stages': copy.deepcopy(extra['stages']),
                        'conditions': settings.model_dump(),
                    }
                ]
                engine.store.save(extra)
                finish_extra_report(engine, extra)
                record = assess(engine, extra, sample, '독립 추가 AI; 정적·동적 관찰 재사용')
                summary['records'].append(record)
                atomic_json(output / 'summary.json', summary)
                print(
                    sample['sample_id'] + ' 추가 AI ' + record['status'] + ' ' + extra['run_id'],
                    flush=True,
                )
    finally:
        settings_after = digest(settings_file) if settings_file.is_file() else None
        summary['settings_file_after_sha256'] = settings_after
        summary['settings_preserved'] = settings_before == settings_after
        summary['finished'] = now()
        summary['source_preserved'] = all(
            digest(item['path']) == item['sha256']
            for sample in samples
            for item in sample['manifest'].values()
        )
        expected_records = len(samples) + args.additional_ai * sum(
            sample['sample_id'] in EXTRA_AI_SAMPLES for sample in samples
        )
        summary['status'] = (
            'VERIFIED'
            if len(summary['records']) == expected_records
            and all(record['status'] == 'VERIFIED' for record in summary['records'])
            and summary['settings_preserved']
            and summary['source_preserved']
            else 'FAILED'
        )
        atomic_json(output / 'summary.json', summary)
        print(str(output / 'summary.json'), flush=True)
    return 0 if summary['status'] == 'VERIFIED' else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--samples-root', default=str(ROOT.parent / '비교 대상 파일'))
    parser.add_argument(
        '--sample', action='append', help='반복 지정 가능; 기본은 두 문제와 할인 경계 세 사례'
    )
    parser.add_argument(
        '--additional-ai',
        type=int,
        choices=range(4),
        default=1,
        help='두 문제 각각 새 AI 요청 횟수(기본 1)',
    )
    parser.add_argument('--num-ctx', type=int, help='저장하지 않는 지역 컨텍스트 상한')
    parser.add_argument('--num-predict', type=int, help='저장하지 않는 지역 출력 상한')
    parser.add_argument('--output', help='존재하지 않는 새 출력 폴더')
    args = parser.parse_args()
    try:
        return asyncio.run(verify(args))
    except (ValueError, OSError) as exc:
        print('검증 실행 실패: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
