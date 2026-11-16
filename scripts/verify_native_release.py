"""Verify a frozen Windows release against a known deterministic comparison."""
from __future__ import annotations
import argparse
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from recheck.models import Settings, atomic_json
from scripts.release_distribution import inventory, sha


def validate_dynamic(run, expected):
    behavior = run.get('behavior', {})
    tests = behavior.get('tests', [])
    if (run.get('stages', {}).get('dynamic', {}).get('status') != 'success'
            or behavior.get('denominator') != len(expected)
            or behavior.get('matched') != sum(expected)
            or [row.get('match') for row in tests] != expected
            or any(row.get('execution_status') != 'measured' for row in tests)):
        raise ValueError('Frozen dynamic comparison did not reproduce every expected verdict')


def validate_observations(run, oracle, image_id):
    observations = run.get('dynamic', [])
    if [row.get('test_id') for row in observations] != [row['test_id'] for row in oracle]:
        raise ValueError('Frozen observations do not match registered test identities')
    for actual, expected in zip(observations, oracle):
        for side in ('original', 'reimplementation'):
            observation, truth = actual[side], expected[side]
            if (observation.get('image') != image_id or observation.get('image_id') != image_id
                    or observation.get('exit_code') != truth['exit_code']):
                raise ValueError('Frozen observation exit code or immutable image differs')
            for channel in ('stdout', 'stderr'):
                if base64.b64decode(observation.get(channel + '_b64', ''), validate=True) != truth[channel].encode('utf-8'):
                    raise ValueError('Frozen raw output differs from precommitted ground truth')


def verify(stage, output, original, changed, tests, expected, oracle):
    stage, output = Path(stage).resolve(strict=True), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    before = inventory(stage)
    exe = stage / 'Recheck.exe'
    env = {**os.environ, 'RECHECK_DATA_DIR': str(output / 'data'),
           'RECHECK_SMOKE_DIR': str(output / 'smoke'), 'PYTHONUTF8': '1'}
    env.pop('QT_QPA_PLATFORM', None)
    atomic_json(output / 'data/settings.json', Settings().model_dump())
    records = []

    def execute(name, arguments, timeout):
        started = time.monotonic()
        argv = [str(exe), *map(str, arguments)]
        with (output / (name + '-process.log')).open('wb') as stream:
            proc = subprocess.run(argv, env=env, cwd=stage, stdout=stream,
                                  stderr=subprocess.STDOUT, timeout=timeout,
                                  creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        record = dict(argv=argv, returncode=proc.returncode, seconds=time.monotonic() - started)
        atomic_json(output / (name + '-process.json'), record)
        records.append(record)
        return proc.returncode

    if execute('smoke', ['--smoke-test'], 40) != 0:
        raise ValueError('Native GUI smoke process failed')
    smoke = json.loads((output / 'smoke/gui-smoke.json').read_text(encoding='utf-8'))
    if not smoke.get('visible') or not (output / 'smoke/gui-smoke.png').is_file():
        raise ValueError('Native GUI did not become visible')
    code = execute('dynamic', ['--cli', '--data-dir', output / 'data', '--json-output',
                   output / 'dynamic-public.json', 'analyze', '--original', Path(original).resolve(),
                   '--reimplementation', Path(changed).resolve(), '--tests', Path(tests).resolve(),
                   '--mode', 'dynamic'], 300)
    public = json.loads((output / 'dynamic-public.json').read_text(encoding='utf-8'))
    run = json.loads((output / 'data/runs' / public['run_id'] / 'result.json').read_text(encoding='utf-8'))
    if code != 0:
        raise ValueError('Frozen dynamic process failed; see native process record')
    validate_dynamic(run, expected)
    manifest = json.loads((stage / '_internal/resources/runtime/environment.json').read_text(encoding='utf-8'))
    validate_observations(run, oracle, manifest['images']['execution']['image_id'])
    if before != inventory(stage):
        raise ValueError('Release payload changed during native verification')
    embedded = json.loads((stage / '_internal/RECHECK_SOURCE_IDENTITY.json').read_text(encoding='utf-8'))
    proof = dict(status='verified', inventory=before,
                 source_aggregate=embedded['aggregate_sha256'], executable_sha256=sha(exe),
                 checks={'native_smoke': True, 'frozen_dynamic': True, 'literal_observations': True},
                 processes=records, run_id=run['run_id'], behavior=run['behavior'],
                 scope='Actual native Windows GUI smoke and frozen Linux dynamic comparison; no VM claim')
    atomic_json(output / 'native-proof.json', proof)
    return proof


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('stage', 'output', 'original', 'changed', 'tests', 'expected', 'oracle'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    expected = json.loads(Path(args.expected).read_text(encoding='utf-8'))
    if not isinstance(expected, list) or not expected or any(type(x) is not bool for x in expected):
        raise ValueError('Expected verdicts must be a precommitted list of booleans')
    oracle = json.loads(Path(args.oracle).read_text(encoding='utf-8'))
    result = verify(args.stage, args.output, args.original, args.changed, args.tests, expected, oracle)
    print(json.dumps({key: value for key, value in result.items() if key != 'inventory'}, indent=2))
