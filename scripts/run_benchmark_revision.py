"""Re-run C after a documented repair while retaining the originally blind A/B."""

from __future__ import annotations

import argparse
import asyncio
import html
import os
from pathlib import Path
import signal
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.run_benchmark import (
    ROOT,
    TARGETS,
    behavior_decisions,
    check_outputs,
    digest,
    evaluate_decisions,
    read_json,
)
from recheck.engine import Engine, PROMPT_VERSION
from recheck.models import Settings, atomic_json
from recheck.storage import new_id, now


def execution_record(llm):
    """A schema failure does not erase the HTTP adapter's /api/ps observation."""
    if llm.get('execution'):
        return dict(
            status='VERIFIED', source='validated interpretation execution', data=llm['execution']
        )
    for index, response in reversed(list(enumerate(llm.get('responses', [])))):
        execution = response.get('_execution')
        if execution and execution.get('kind') == 'local' and execution.get('digest'):
            return dict(
                status='VERIFIED',
                source=f'responses[{index}]._execution',
                data=execution,
                limitation='로컬 실행 응답은 검증되었지만 해석 JSON 검증 성공을 의미하지 않음',
            )
    return dict(status='NOT VERIFIED', source=None, data={})


def function_localization(result, pair):
    findings = result.get('llm', {}).get('interpretation', {}).get('findings', [])
    function_evidence = {
        e['evidence_id']: e for e in result.get('evidence', []) if e['kind'] == 'function'
    }
    linked = set()
    localized = set()
    mismatches = {row['test_id'] for row in pair['ground_truth'] if not row['match']}
    for finding in findings:
        for eid in finding.get('evidence_ids', []):
            ev = function_evidence.get(eid)
            if not ev or ev['data']['id'] not in finding.get('function_ids', []):
                continue
            name = ev['data'].get('name', '')
            linked.add((ev['target'], ev['data']['id'], name, eid))
            if name in pair['candidate_functions']:
                localized.update(mismatches & set(finding.get('test_ids', [])))
    return dict(
        correct_candidate_count=sum(
            name in pair['candidate_functions'] for _, _, name, _ in linked
        ),
        cited_candidate_count=len(linked),
        precision=dict(
            numerator=sum(name in pair['candidate_functions'] for _, _, name, _ in linked),
            denominator=len(linked),
        ),
        linked_functions=[
            dict(target=t, id=fid, name=name, evidence_id=eid)
            for t, fid, name, eid in sorted(linked)
        ],
        mismatch_test_localization=dict(
            numerator=len(localized), denominator=len(mismatches), test_ids=sorted(localized)
        ),
        limitation='작성자가 사전 지정한 관련 함수 이름과 일치하는 근거 연결이며 원인 확정 또는 실행 추적은 아님',
    )


def fairness_checks(result, pair, baseline_b, baseline_a):
    by_id = {row['test_id']: row for row in baseline_b['dynamic']}
    records = result.get('dynamic', [])
    observed_images = sorted(
        {row[target].get('image_id', '') for row in records for target in TARGETS}
    )
    baseline_images = sorted(
        {row[target].get('image_id', '') for row in baseline_b['dynamic'] for target in TARGETS}
    )
    actual_execution = execution_record(result.get('llm', {}))
    expected_digest = baseline_a.get('model_digest', '').removeprefix('sha256:')
    expected_options = baseline_a.get('settings', {})
    return dict(
        binary_hashes_match=all(
            result.get('files', {}).get(target, {}).get('sha256')
            == pair['binary_metadata'][target]['sha256']
            for target in TARGETS
        ),
        image_ids_match=observed_images == baseline_images == [baseline_b['image_id']],
        registered_count_match=len(records) == len(baseline_b['dynamic']) == len(pair['tests']),
        actual_inputs_match=all(
            row[target].get('actual_input')
            == by_id.get(row['test_id'], {}).get(target, {}).get('actual_input')
            for row in records
            for target in TARGETS
        ),
        actual_limits_match=all(
            row[target].get('limits') == by_id.get(row['test_id'], {}).get(target, {}).get('limits')
            for row in records
            for target in TARGETS
        ),
        model_digest_match=bool(expected_digest)
        and actual_execution['data'].get('digest', '').removeprefix('sha256:') == expected_digest,
        model_tag_match=result.get('llm', {}).get('model') == baseline_a.get('model'),
        model_options_match=all(
            result.get('llm', {}).get('settings', {}).get(key) == expected_options.get(key)
            for key in ('num_ctx', 'num_predict')
        ),
        behavior_matches_docker_baseline=behavior_decisions(result.get('behavior', {}))
        == behavior_decisions(baseline_b.get('behavior', {})),
    )


def summarize(result, pair, baseline_b, baseline_a, duration, engine):
    findings = result.get('llm', {}).get('interpretation', {}).get('findings', [])
    known = {e['evidence_id'] for e in result.get('evidence', [])}
    cited = [eid for f in findings for eid in f.get('evidence_ids', [])]
    valid = sum(eid in known for eid in cited)
    outputs = check_outputs(result.get('dynamic', []), pair['ground_truth'])
    fairness = fairness_checks(result, pair, baseline_b, baseline_a)
    localization = function_localization(result, pair)
    localized = localization['mismatch_test_localization']
    stages = [
        stage
        for key, stage in result['stages'].items()
        if key
        in ('environment', 'static_original', 'static_reimplementation', 'image', 'dynamic', 'llm')
        and stage['status'] not in ('blocked', 'pending')
    ]
    return dict(
        method='C_recheck_repaired',
        pair_id=pair['pair_id'],
        created=now(),
        run_id=result['run_id'],
        attempt_id=result['attempt_id'],
        run_status=result['status'],
        status=(
            'VERIFIED'
            if result['status'] == 'success'
            and outputs['status'] == 'VERIFIED'
            and valid == len(cited)
            and all(fairness.values())
            else 'FAILED'
        ),
        result=str(engine.store.run_dir(result['run_id']) / 'result.json'),
        report=str(engine.report(result['run_id'])),
        behavior=result.get('behavior', {}),
        metrics=evaluate_decisions(
            behavior_decisions(result.get('behavior', {})), pair['ground_truth']
        ),
        ground_truth_validation=outputs,
        fairness_checks=fairness,
        function_identification=localization,
        mismatch_traceability_status=(
            'VERIFIED' if localized['numerator'] == localized['denominator'] else 'PARTIAL'
        ),
        evidence_links=dict(
            valid=valid, total=len(cited), invalid=[eid for eid in cited if eid not in known]
        ),
        reproduction_evidence=dict(
            available=result.get('behavior', {}).get('denominator', 0),
            denominator=len(pair['tests']),
        ),
        duration_seconds=duration,
        stages=result['stages'],
        stage_duration_seconds={
            key: stage.get('duration_seconds') for key, stage in result['stages'].items()
        },
        static_cache={
            target: result.get('static', {}).get(target, {}).get('cache', {}) for target in TARGETS
        },
        structure_score=result.get('structure', {}).get('score'),
        llm_status=result.get('llm', {}).get('status'),
        llm_validation_errors=result.get('llm', {}).get('validation_errors', []),
        llm_responses=len(result.get('llm', {}).get('responses', [])),
        local_execution=execution_record(result.get('llm', {})),
        tool_failure_rate=dict(
            numerator=sum(
                stage['status'] in ('failed', 'partial', 'cancelled') for stage in stages
            ),
            denominator=len(stages),
            unit='attempted external analysis stage',
        ),
        prompt_version=result.get('llm', {}).get('prompt_version'),
    )


def write_report(output, revision):
    def link(path, label):
        relative = Path(os.path.relpath(path, output)).as_posix()
        return '<a href="' + html.escape(relative, quote=True) + '">' + html.escape(label) + '</a>'

    rows = []
    current = {r['pair_id']: r for r in revision['repaired_c']}
    for old in revision['initial_c']:
        pair_id = old['pair_id']
        new = current.get(pair_id)
        a = revision['baselines'][pair_id]['A_llm_only']
        b = revision['baselines'][pair_id]['B_docker_only']
        parts = [html.escape(pair_id)]
        for method in (a, b):
            m = method['metrics']
            parts.append(
                f"검출 {m['true_positive']}/{m['actual_mismatches']}, 오탐 {m['false_positive']}, 판정불가 {m['inconclusive']}"
            )
        parts.append(
            html.escape(old['status'])
            + f" · 해석 응답 {old['responses']}회, 검증 오류 {old['validation_errors']}회 · "
            + link(old['artifact'], '초기 결과')
        )
        if new:
            m = new['metrics']
            f = new['function_identification']['mismatch_test_localization']
            parts.append(
                html.escape(new['status'])
                + f" · 검출 {m['true_positive']}/{m['actual_mismatches']} · 함수연결 {f['numerator']}/{f['denominator']} ({new['mismatch_traceability_status']}) · "
                + link(new['report'], '새 분석 보고서')
            )
        else:
            parts.append('미실행')
        rows.append('<tr>' + ''.join('<td>' + part + '</td>' for part in parts) + '</tr>')
    page = (
        '<!doctype html><html lang="ko"><meta charset="utf-8"><title>Recheck 수정 전후 비교 실험</title><style>body{font:16px sans-serif;max-width:1400px;margin:40px auto}table{border-collapse:collapse}td,th{padding:10px;border:1px solid #aaa;vertical-align:top}</style><h1>Recheck 수정 전후 비교 실험</h1><p>최초 LLM 단독 예측과 Docker 단독 관찰은 그대로 보존했습니다. 초기 전체 분석의 실패도 아래에 함께 표시하고, 수정된 전체 엔진만 새 실행 ID로 다시 검증했습니다.</p><table><tr><th>비교 쌍</th><th>A · LLM 단독</th><th>B · Docker 단독</th><th>C · 초기 전체 분석</th><th>C · 수정 후 전체 분석</th></tr>'
        + ''.join(rows)
        + '</table><p>각 3쌍, 등록 입력 20개, 각 평가판별 1회입니다. Docker와 전체 분석의 실제 검출 수는 동일하게 기록하며, 함수 후보의 근거 연결을 별도로 평가합니다.</p><p>'
        + html.escape(revision['timing_limitations'])
        + '</p><p>조건 식별 정확도는 미검증, 프로세스별 최대 메모리는 미측정입니다. 작은 예제의 결과를 범용 우위나 동등성 증명으로 일반화하지 않습니다.</p></html>'
    )
    (output / 'report.html').write_text(page, encoding='utf-8')


async def run(args):
    source = Path(args.source).resolve(strict=True)
    output = Path(args.output).resolve()
    if output.exists():
        raise ValueError('이전 평가 보존: 새 --output 폴더를 지정하세요.')
    output.mkdir(parents=True)
    manifest = read_json(source / 'manifest.json')
    settings = Settings.model_validate(manifest['settings'])
    if args.ghidra:
        settings.ghidra_path = args.ghidra
    revision = dict(
        schema_version='1.0',
        revision_id=new_id('evaluation'),
        created=now(),
        status='RUNNING',
        source_benchmark=str(source),
        source_benchmark_id=manifest['benchmark_id'],
        source_results=dict(
            path=str(source / 'results.json'), sha256=digest(source / 'results.json')
        ),
        prompt_version=PROMPT_VERSION,
        settings=settings.model_dump(),
        source_hashes={
            name: digest(ROOT / 'recheck' / name)
            for name in (
                'engine.py',
                'adapters.py',
                'process.py',
                'ollama.py',
                'agent.py',
                'gui.py',
                'models.py',
                'comparison.py',
                'report.py',
            )
        },
        harness_sha256=digest(__file__),
        baselines={},
        initial_c=[],
        repaired_c=[],
        repetitions=1,
        tests_total=sum(len(pair['tests']) for pair in manifest['pairs']),
        condition_identification=dict(
            status='NOT VERIFIED',
            reason='조건의 의미 정확도를 자동 판단하지 않음; 원문 및 디컴파일 수동 대조 필요',
        ),
        resource_observations=dict(
            status='NOT MEASURED',
            reason='프로세스별 peak memory 미측정; 제한값·OOM·호스트 메모리는 실제 산출물에 기록',
        ),
        timing_limitations='수정 후 전체 분석은 호환 검증된 초기 정적 캐시를 재사용합니다. A/B 측정 시간은 최초 실험 값입니다. 콜드/웜 조건이 달라 속도 우위를 주장하지 않습니다.',
    )
    for pair in manifest['pairs']:
        pair_id = pair['pair_id']
        revision['baselines'][pair_id] = {}
        for method in ('A_llm_only', 'B_docker_only'):
            path = source / pair_id / (method + '.json')
            data = read_json(path)
            revision['baselines'][pair_id][method] = dict(
                artifact=str(path),
                sha256=digest(path),
                status=data['status'],
                metrics=data['metrics'],
                duration_seconds=data['duration_seconds'],
                function_identification=data.get('function_identification'),
                originally_blind=method == 'A_llm_only',
            )
        path = source / pair_id / 'C_recheck.json'
        old = read_json(path)
        old_run = read_json(old['result'])
        revision['initial_c'].append(
            dict(
                pair_id=pair_id,
                artifact=str(path),
                sha256=digest(path),
                status=old['status'],
                run_id=old['run_id'],
                result=old['result'],
                report=old['report'],
                metrics=old['metrics'],
                validation_errors=len(old_run.get('llm', {}).get('validation_errors', [])),
                responses=len(old_run.get('llm', {}).get('responses', [])),
                local_execution=execution_record(old_run.get('llm', {})),
            )
        )
    atomic_json(output / 'results.json', revision)
    write_report(output, revision)
    cancel = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: cancel.set())
    for pair in manifest['pairs']:
        if cancel.is_set():
            break
        print(pair['pair_id'] + ' repaired C start', flush=True)

        def progress(event):
            if event.get('kind') in ('stage', 'failure', 'llm_validation', 'llm_failure'):
                print({k: v for k, v in event.items() if k != 'traceback'}, flush=True)

        engine = Engine(settings, data_dir=source / 'recheck-data', on_event=progress)
        tests = [
            dict(test, description='등록 입력 비교', source='benchmark registered input')
            for test in pair['tests']
        ]
        start = time.monotonic()
        result = await engine.analyze(
            pair['files']['original'], pair['files']['reimplementation'], tests, cancel=cancel
        )
        a = read_json(source / pair['pair_id'] / 'A_llm_only.json')
        b = read_json(source / pair['pair_id'] / 'B_docker_only.json')
        summary = summarize(result, pair, b, a, time.monotonic() - start, engine)
        atomic_json(output / pair['pair_id'] / 'C_recheck.json', summary)
        revision['repaired_c'].append(summary)
        atomic_json(output / 'results.json', revision)
        write_report(output, revision)
    revision['status'] = (
        'VERIFIED'
        if len(revision['repaired_c']) == 3
        and all(r['status'] == 'VERIFIED' for r in revision['repaired_c'])
        else 'PARTIAL'
    )
    revision['initial_failure_count'] = sum(
        r['status'] != 'VERIFIED' for r in revision['initial_c']
    )
    revision['initial_validation_errors'] = sum(
        r['validation_errors'] for r in revision['initial_c']
    )
    revision['repaired_validation_errors'] = sum(
        len(r['llm_validation_errors']) for r in revision['repaired_c']
    )
    revision['repaired_llm_responses'] = sum(r['llm_responses'] for r in revision['repaired_c'])
    revision['aggregate'] = []
    for method in ('A_llm_only', 'B_docker_only', 'C_recheck_repaired'):
        rows = (
            revision['repaired_c']
            if method == 'C_recheck_repaired'
            else [value[method] for value in revision['baselines'].values()]
        )
        totals = {
            key: sum(row['metrics'].get(key, 0) for row in rows)
            for key in (
                'true_positive',
                'false_negative',
                'false_positive',
                'true_negative',
                'inconclusive',
                'tests',
                'actual_mismatches',
                'missed_mismatches',
            )
        }
        totals['detection_rate'] = dict(
            numerator=totals['true_positive'], denominator=totals['actual_mismatches']
        )
        totals['false_positive_rate'] = dict(
            numerator=totals['false_positive'],
            denominator=totals['tests'] - totals['actual_mismatches'],
        )
        totals['function_candidate_precision'] = dict(
            numerator=sum(
                (row.get('function_identification') or {}).get('correct_candidate_count', 0)
                for row in rows
            ),
            denominator=sum(
                (row.get('function_identification') or {}).get('cited_candidate_count', 0)
                for row in rows
            ),
        )
        if method == 'C_recheck_repaired':
            totals['mismatch_test_localization'] = dict(
                numerator=sum(
                    row['function_identification']['mismatch_test_localization']['numerator']
                    for row in rows
                ),
                denominator=sum(
                    row['function_identification']['mismatch_test_localization']['denominator']
                    for row in rows
                ),
            )
        revision['aggregate'].append(
            dict(
                method=method,
                completed_pairs=len(rows),
                metrics=totals,
                duration_seconds=sum(row['duration_seconds'] for row in rows),
            )
        )
    revision['completed'] = now()
    atomic_json(output / 'results.json', revision)
    write_report(output, revision)
    print(
        dict(
            status=revision['status'],
            output=str(output),
            run_ids=[r['run_id'] for r in revision['repaired_c']],
        ),
        flush=True,
    )


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='초기 A/B와 실패 C를 보존하는 Recheck 수정 후 평가'
    )
    parser.add_argument('--source', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--ghidra')
    asyncio.run(run(parser.parse_args()))
