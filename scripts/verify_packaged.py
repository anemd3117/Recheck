"""Serial real Windows GUI checks through the packaged application's handlers."""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from recheck.models import atomic_json, load_settings
from recheck.process import run_process


async def main(args):
    release = Path(args.release).resolve(strict=True)
    exe = release / 'staged-dist' / 'Recheck' / 'Recheck.exe'
    root = Path(__file__).resolve().parents[1]
    settings = load_settings()
    data = release / 'runtime-data'
    data.mkdir(exist_ok=False)
    atomic_json(data / 'settings.json', settings.model_dump())
    env = {
        **os.environ,
        'RECHECK_DATA_DIR': str(data),
        'RECHECK_SMOKE_DIR': str(release / 'packaged-smoke'),
        'PYTHONUTF8': '1',
    }
    # A real Windows platform plugin is required; offscreen pytest settings do not leak here.
    env.pop('QT_QPA_PLATFORM', None)
    samples = json.loads(
        (root / 'verification' / 'ui-refresh-20261113-124024' / 'scenario-config.json').read_text(
            encoding='utf-8'
        )
    )
    samples['output_dir'] = str(release / 'packaged-scenario')
    atomic_json(release / 'scenario-config.json', samples)
    commands = [
        ('smoke', ['--smoke-test'], 20),
        ('setup', ['--verify-setup', str(release / 'packaged-setup')], 150),
        ('scenario', ['--verify-scenario', str(release / 'scenario-config.json')], 650),
        (
            'performance',
            ['--verify-performance', 'quick', str(release / 'packaged-performance')],
            240,
        ),
        (
            'cancel',
            [
                '--verify-performance',
                'quick',
                str(release / 'packaged-cancel'),
                '--cancel-performance-after-ms',
                '1500',
            ],
            60,
        ),
    ]
    results = []
    for name, arguments, timeout in commands:
        print(name + ' start', flush=True)
        outcome = await run_process(
            [str(exe), *arguments], timeout=timeout, env=env, output_limit=32768
        )
        record = dict(
            check=name, executable_sha256=hashlib.sha256(exe.read_bytes()).hexdigest(), **outcome
        )
        atomic_json(release / (name + '-process.json'), record)
        results.append(record)
        if outcome.get('returncode') != 0 or outcome.get('timed_out') or outcome.get('cancelled'):
            raise RuntimeError('패키지 검증 실패: ' + name)
        print(name + ' completed', flush=True)
    after = json.loads((data / 'settings.json').read_text(encoding='utf-8'))
    if after != settings.model_dump():
        raise RuntimeError('성능 측정 중 사용자 승인 없이 설정값이 바뀌었습니다.')
    atomic_json(
        release / 'packaged-summary.json',
        dict(
            status='VERIFIED',
            settings_unchanged=True,
            executable=str(exe),
            sha256=hashlib.sha256(exe.read_bytes()).hexdigest(),
            checks=[r['check'] for r in results],
        ),
    )


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--release', required=True)
    asyncio.run(main(parser.parse_args()))
