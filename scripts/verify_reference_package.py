"""안정화 후보의 실제 Windows GUI·세 사례를 별도 데이터에서 검증한다."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))

from recheck.engine import Engine
from recheck.models import Settings, atomic_json, data_root
from recheck.process import run_process
from verify_reference_stability import load_sample, assess


def check_execution_settings(run, settings):
    expected = settings.model_dump()
    fields = (
        'model',
        'ollama_url',
        'num_ctx',
        'num_predict',
        'llm_timeout',
        'linux_image',
        'memory_mb',
        'cpu',
        'output_limit',
        'static_timeout',
        'normalize_newlines',
        'trim_trailing_whitespace',
    )
    actual = {key: run.get('settings', {}).get(key) for key in fields}
    wanted = {key: expected[key] for key in fields}
    llm = run.get('llm', {})
    passed = (
        actual == wanted
        and llm.get('model') == settings.model
        and all(
            llm.get('settings', {}).get(key) == expected[key] for key in ('num_ctx', 'num_predict')
        )
    )
    return dict(
        passed=passed,
        requested=wanted,
        actual=actual,
        ai_model=llm.get('model'),
        ai_options=llm.get('settings'),
    )


async def main(args):
    release = Path(args.release).resolve(strict=True)
    executable = release / 'staged-dist' / 'Recheck' / 'Recheck.exe'
    if not executable.is_file():
        raise ValueError('검증할 안정화 패키지가 없습니다.')
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    settings = Settings.model_validate_json(Path(args.settings).read_text(encoding='utf-8'))
    original_settings = data_root() / 'settings.json'
    settings_before = original_settings.read_bytes() if original_settings.exists() else None
    runtime = output / 'runtime-data'
    atomic_json(runtime / 'settings.json', settings.model_dump())
    environment = {
        **os.environ,
        'RECHECK_DATA_DIR': str(runtime),
        'RECHECK_SMOKE_DIR': str(output / 'smoke'),
        'PYTHONUTF8': '1',
    }
    environment.pop('QT_QPA_PLATFORM', None)
    summary = dict(
        status='RUNNING',
        executable=str(executable),
        executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
        settings=settings.model_dump(),
        checks=[],
    )
    try:
        smoke = await run_process(
            [str(executable), '--smoke-test'], timeout=30, env=environment, output_limit=32768
        )
        atomic_json(output / 'smoke-process.json', smoke)
        if smoke.get('returncode') != 0 or smoke.get('timed_out') or smoke.get('cancelled'):
            raise RuntimeError('패키지 시작 검사가 실패했습니다.')
        summary['checks'].append(dict(kind='smoke', status='VERIFIED'))
        for sample_id in ('gcd_algorithms', 'range_overlap', 'discount_boundary'):
            sample = load_sample(Path(args.samples_root).resolve(strict=True), sample_id)
            folder = output / sample_id
            folder.mkdir()
            config = dict(
                original=str(sample['paths']['original']),
                reimplementation=str(sample['paths']['reimplementation']),
                tests=sample['tests'],
                output_dir=str(folder / 'gui'),
            )
            atomic_json(folder / 'scenario-config.json', config)
            print(sample_id + ' GUI 시작', flush=True)
            process = await run_process(
                [str(executable), '--verify-scenario', str(folder / 'scenario-config.json')],
                timeout=650,
                env=environment,
                output_limit=32768,
            )
            atomic_json(folder / 'process.json', process)
            scenario_file = folder / 'gui' / 'scenario.json'
            scenario = (
                json.loads(scenario_file.read_text(encoding='utf-8'))
                if scenario_file.exists()
                else {}
            )
            if not scenario.get('run_id'):
                raise RuntimeError(sample_id + ' GUI 분석 결과가 없습니다.')
            engine = Engine(settings, data_dir=runtime)
            result = engine.store.load_run(scenario['run_id'])
            assessment = assess(engine, result, sample, 'actual_packaged_gui')
            execution_settings = check_execution_settings(result, settings)
            assessment.update(
                sample_id=sample_id,
                scenario=str(scenario_file),
                execution_settings=execution_settings,
                gui_artifacts=scenario.get('artifact_checks'),
                gui_exit_code=scenario.get('exit_code'),
                process_returncode=process.get('returncode'),
            )
            passed = (
                assessment['status'] == 'VERIFIED'
                and execution_settings['passed']
                and scenario.get('exit_code') == 0
                and process.get('returncode') == 0
                and not process.get('timed_out')
                and not process.get('cancelled')
            )
            if not passed:
                assessment['status'] = 'FAILED'
            summary['checks'].append(assessment)
            atomic_json(output / 'summary.json', summary)
            print(sample_id + ' ' + assessment['status'], flush=True)
            if not passed:
                raise RuntimeError(sample_id + ' 패키지 검증 실패; 원시 자료를 보존했습니다.')
        summary['status'] = 'VERIFIED'
    except Exception as exc:
        summary.update(status='FAILED', error=str(exc))
        raise
    finally:
        after = original_settings.read_bytes() if original_settings.exists() else None
        summary['user_settings_unchanged'] = settings_before == after
        if after != settings_before:
            summary.update(status='FAILED', error='원래 사용자 설정이 변경되었습니다.')
        atomic_json(output / 'summary.json', summary)
    if summary['status'] != 'VERIFIED':
        raise RuntimeError(summary.get('error', '안정화 패키지 검증 실패'))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--release', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--settings', required=True)
    parser.add_argument('--samples-root')
    args = parser.parse_args()
    if args.samples_root is None:
        samples = next(
            (p for p in ROOT.parent.iterdir() if p.is_dir() and (p / 'catalog.json').is_file()),
            None,
        )
        if samples is None:
            parser.error('샘플 catalog.json 폴더를 --samples-root로 지정하세요.')
        args.samples_root = str(samples)
    asyncio.run(main(args))
