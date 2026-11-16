"""Build the Recheck-owned probe in an existing compiler image, preserving evidence."""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from recheck.adapters import DockerAdapter
from recheck.models import atomic_json, load_settings, resources_root
from recheck.storage import now


async def main(output, image):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    docker = DockerAdapter(context=load_settings().docker_context)
    log = []

    async def call(*args, check=True):
        result = await docker.call(*args, timeout=60, output_limit=32768)
        log.append(result)
        atomic_json(output / 'build-log.json', log)
        if check and (
            result.get('returncode') != 0
            or any(result.get(k) for k in ('error', 'timed_out', 'cancelled'))
        ):
            raise RuntimeError('성능 측정용 예제 빌드 실패: ' + result.get('stderr', ''))
        return result

    details = json.loads((await call('image', 'inspect', image))['stdout'])[0]
    if details.get('Os') != 'linux' or details.get('Architecture') != 'amd64':
        raise RuntimeError('Linux amd64 컴파일러 이미지가 필요합니다.')
    source = resources_root() / 'benchmarks' / 'sample.c'
    sample = source.with_suffix('.elf')
    manifest = source.with_suffix('.json')
    if sample.exists() or manifest.exists():
        raise RuntimeError('기존 예제 산출물을 덮어쓰지 않습니다.')
    name = 'recheck-probe-build-' + uuid.uuid4().hex
    try:
        await call(
            'create',
            '--name',
            name,
            '--label',
            'org.recheck.managed=true',
            '--label',
            'org.recheck.purpose=performance-probe-build',
            '--network',
            'none',
            '--memory',
            '256m',
            '--cpus',
            '1',
            '--pids-limit',
            '64',
            '--cap-drop',
            'ALL',
            '--security-opt',
            'no-new-privileges',
            '--entrypoint',
            '/bin/sh',
            details['Id'],
            '-c',
            'gcc -O0 -g -fno-inline -no-pie /tmp/sample.c -o /tmp/sample.elf && gcc --version',
        )
        await call('cp', str(source), name + ':/tmp/sample.c')
        execution = await call('start', '--attach', name)
        await call('cp', name + ':/tmp/sample.elf', str(sample))
        record = dict(
            schema_version='1.0',
            created_at=now(),
            provenance='Recheck-owned fixed C benchmark probe',
            source='sample.c',
            source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            sha256=hashlib.sha256(sample.read_bytes()).hexdigest(),
            bytes=sample.stat().st_size,
            image_id=details['Id'],
            flags=['-O0', '-g', '-fno-inline', '-no-pie'],
            compiler=execution.get('stdout'),
            build_log=str(output / 'build-log.json'),
            host_execution=False,
        )
        atomic_json(manifest, record)
        atomic_json(output / 'manifest.json', record)
    finally:
        await call('rm', '--force', name, check=False)
    print(json.dumps(record, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    parser.add_argument('--image', required=True)
    args = parser.parse_args()
    asyncio.run(main(args.output, args.image))
