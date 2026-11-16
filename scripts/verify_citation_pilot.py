"""검증된 기존 관찰을 보존하고 AI 인용 경계만 새 요청으로 점검한다."""

import argparse
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))
from recheck.engine import Engine
from recheck.models import Settings, atomic_json
from recheck.storage import now
from verify_reference_stability import (
    load_sample,
    independent_record,
    check_observations,
    finish_extra_report,
    assess,
    digest,
)


async def main(args):
    source = json.loads(Path(args.source).read_text(encoding='utf-8'))
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    settings = Settings.model_validate(source['settings'])
    engine = Engine(settings, output / 'data')
    result = dict(
        created=now(),
        status='RUNNING',
        engine_sha256=digest(ROOT / 'recheck/engine.py'),
        scope='새 AI 요청만 수행; 기존 정적·동적 관찰 재사용; 새 전체 실행 아님',
        records=[],
    )
    try:
        for sample_id in ('gcd_algorithms', 'range_overlap'):
            sample = load_sample(Path(source['samples_root']), sample_id)
            rows = [
                row
                for row in source['records']
                if row['sample_id'] == sample_id and not row.get('analysis_reuse')
            ]
            if len(rows) != 1:
                raise ValueError('재사용할 단일 원본 분석을 확인할 수 없습니다: ' + sample_id)
            source_result = Path(rows[0]['result'])
            run = json.loads(source_result.read_text(encoding='utf-8'))
            if not check_observations(run, sample)['passed']:
                raise ValueError('재사용할 동적 관찰 무결성 검사가 실패했습니다: ' + sample_id)
            fresh = independent_record(run, settings.model_dump(), source_result)
            engine.store.save(fresh)
            atomic_json(engine.store.run_dir(fresh['run_id']) / 'tests.json', fresh['tests'])
            atomic_json(engine.store.run_dir(fresh['run_id']) / 'settings.json', fresh['settings'])
            print(sample_id + ' AI만 새 요청', flush=True)
            run = await engine.reinterpret(fresh['run_id'])
            finish_extra_report(engine, run)
            record = assess(engine, run, sample, 'AI 인용 pilot; 정적·동적 관찰 재사용')
            result['records'].append(record)
            atomic_json(output / 'summary.json', result)
            print(sample_id + ' ' + record['status'] + ' ' + record['run_id'], flush=True)
        result['status'] = (
            'VERIFIED'
            if all(row['status'] == 'VERIFIED' for row in result['records'])
            else 'FAILED'
        )
    finally:
        result['finished'] = now()
        atomic_json(output / 'summary.json', result)
    return 0 if result['status'] == 'VERIFIED' else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True)
    parser.add_argument('--output', required=True)
    raise SystemExit(asyncio.run(main(parser.parse_args())))
