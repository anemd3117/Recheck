"""Fixed compiler operations; generated programs are never executed on the host."""

from __future__ import annotations

import base64
import asyncio
import hashlib
import json
from pathlib import Path
import re

from .adapters import DockerAdapter, _ok
from .models import atomic_json, inspect_binary, resources_root
from .ollama import cancelled
from .storage import new_id


class CompilerAdapter:
    preferred_image = 'recheck-comparison-build:1'
    managed_image = 'recheck-generated-compiler:1'
    # The shell is an application-owned transport wrapper. Source bytes are stdin,
    # and every compiler argument is a separate fixed positional parameter.
    wrapper = '"$1" --version >&2 && "$@" && cat /tmp/recheck-program'

    def __init__(self, settings):
        self.settings = settings
        self.docker = DockerAdapter(context=settings.docker_context, resources=resources_root())

    async def prepare(self, language, cancel=None):
        environment = await self.docker.probe()
        if (
            not environment.get('ready')
            or environment.get('os') != 'linux'
            or environment.get('arch') not in ('amd64', 'x86_64')
            or not environment.get('local_endpoint')
        ):
            return dict(
                status='blocked',
                error='로컬 Linux x86-64 Docker 엔진이 필요합니다.',
                environment=environment,
            )
        definition = resources_root() / 'docker' / 'compiler'
        definition_hash = hashlib.sha256((definition / 'Dockerfile').read_bytes()).hexdigest()
        for name in (self.preferred_image, self.managed_image):
            process = await self.docker.call('image', 'inspect', name, timeout=15, cancel=cancel)
            if not _ok(process):
                continue
            image = json.loads(process['stdout'])[0]
            labels = (image.get('Config') or {}).get('Labels') or {}
            if image.get('Os') != 'linux' or image.get('Architecture') != 'amd64':
                continue
            if language == 'cpp' and labels.get('org.recheck.compiler.cpp') != 'true':
                continue
            if (
                name == self.managed_image
                and labels.get('org.recheck.definition_sha256') != definition_hash
            ):
                continue
            return dict(
                status='success',
                name=name,
                image_id=image['Id'],
                digests=image.get('RepoDigests', []),
                definition_sha256=definition_hash,
                reused=True,
            )
        build = await self.docker.call(
            'build',
            '--label',
            'org.recheck.definition_sha256=' + definition_hash,
            '--tag',
            self.managed_image,
            str(definition),
            timeout=300,
            cancel=cancel,
            output_limit=2 * 1024 * 1024,
        )
        if not _ok(build):
            return dict(
                status='cancelled' if cancelled(cancel) else 'failed',
                error='프로젝트 컴파일러 이미지 준비에 실패했습니다.',
                preparation=build,
            )
        inspected = await self.docker.call(
            'image', 'inspect', self.managed_image, timeout=15, cancel=cancel
        )
        if not _ok(inspected):
            return dict(
                status='failed',
                error='준비한 컴파일러 이미지 정보를 확인하지 못했습니다.',
                preparation=build,
                inspection=inspected,
            )
        image = json.loads(inspected['stdout'])[0]
        return dict(
            status='success',
            name=self.managed_image,
            image_id=image['Id'],
            digests=image.get('RepoDigests', []),
            definition_sha256=definition_hash,
            reused=False,
            preparation=build,
        )

    async def compile(
        self, source_path, output_dir, workflow_id, revision, language='c', cancel=None
    ):
        if language not in ('c', 'cpp') or not re.fullmatch(r'workflow_[a-f0-9]{32}', workflow_id):
            raise ValueError('지원하지 않는 컴파일 요청입니다.')
        source_path = Path(source_path)
        source = source_path.read_bytes()
        if not source or len(source) > 65536 or b'\0' in source:
            raise ValueError('소스는 NUL 없는 UTF-8 텍스트로 최대 64KB여야 합니다.')
        source.decode('utf-8')
        output_dir = Path(output_dir) / new_id('build')
        output_dir.mkdir(parents=True)
        result = dict(
            status='running',
            directory=str(output_dir),
            source_sha256=hashlib.sha256(source).hexdigest(),
            language=language,
            container=None,
            cleanup_errors=[],
            network='none',
            schema_version='1.0',
        )
        created = False
        container = 'recheck-generated-' + new_id('owned')
        try:
            image = await self.prepare(language, cancel)
            result['image'] = image
            if image['status'] != 'success':
                result.update(status=image['status'], error=image['error'])
                return result
            compiler = 'gcc' if language == 'c' else 'g++'
            argv = [
                compiler,
                '-std=c17' if language == 'c' else '-std=c++17',
                '-O0',
                '-g',
                '-fno-inline',
                '-fno-pie',
                '-no-pie',
                '-fno-ident',
                '-Wall',
                '-Wextra',
                '-x',
                'c' if language == 'c' else 'c++',
                '-',
                '-o',
                '/tmp/recheck-program',
            ]
            result.update(compiler_argv=argv, wrapper=self.wrapper, container=container)
            creation = await self.docker.call(
                'create',
                '--name',
                container,
                '--label',
                'org.recheck.managed=true',
                '--label',
                'org.recheck.purpose=generated-build',
                '--label',
                'org.recheck.workflow=' + workflow_id,
                '--label',
                'org.recheck.revision=' + str(revision),
                '--network',
                'none',
                '--read-only',
                '--user',
                '65534:65534',
                '--cap-drop',
                'ALL',
                '--security-opt',
                'no-new-privileges',
                '--memory',
                '512m',
                '--cpus',
                '1',
                '--pids-limit',
                '64',
                '--tmpfs',
                '/tmp:rw,noexec,nosuid,size=128m,mode=1777',
                '--workdir',
                '/tmp',
                '--interactive',
                '--entrypoint',
                '/bin/sh',
                image['image_id'],
                '-c',
                self.wrapper,
                'recheck-compile',
                *argv,
                timeout=20,
                cancel=cancel,
            )
            atomic_json(output_dir / 'create.json', creation)
            if not _ok(creation):
                result.update(
                    status='cancelled' if cancelled(cancel) else 'failed',
                    error='컴파일 컨테이너 생성에 실패했습니다.',
                )
                return result
            created = True
            process = await self.docker.call(
                'start',
                '--attach',
                '--interactive',
                container,
                stdin=source,
                timeout=120,
                cancel=cancel,
                output_limit=16 * 1024 * 1024,
            )
            atomic_json(output_dir / 'process.json', process)
            result['process_artifact'] = str(output_dir / 'process.json')
            result['compiler_log'] = process.get('stderr', '')[:16000]
            inspected = await self.docker.call('inspect', container, timeout=10)
            atomic_json(output_dir / 'inspect.json', inspected)
            state = json.loads(inspected['stdout'])[0]['State'] if _ok(inspected) else {}
            result['container_state'] = state
            if (
                not _ok(process)
                or state.get('ExitCode') != 0
                or state.get('OOMKilled')
                or state.get('Running')
            ):
                result.update(
                    status='cancelled' if cancelled(cancel) else 'failed',
                    error='격리 컴파일에 실패했습니다. 컴파일 로그와 컨테이너 상태를 확인하세요.',
                )
                return result
            binary = base64.b64decode(process.get('stdout_b64', ''), validate=True)
            destination = output_dir / 'program'
            destination.write_bytes(binary)
            metadata = inspect_binary(destination)
            if metadata['format'] != 'ELF' or not metadata['supported']:
                result.update(
                    status='failed',
                    error='컴파일 출력이 지원하는 Linux ELF x86-64 실행 파일이 아닙니다.',
                    binary=metadata,
                )
                return result
            result.update(
                status='success',
                binary=metadata,
                binary_path=str(destination),
                compiler_version=(
                    result['compiler_log'].splitlines()[0] if result['compiler_log'] else '미확인'
                ),
            )
            return result
        except asyncio.CancelledError:
            result.update(status='cancelled', error='컴파일 요청이 취소되었습니다.')
            raise
        except Exception as exc:
            result.update(
                status='cancelled' if cancelled(cancel) else 'failed',
                error='컴파일 처리 오류: ' + str(exc),
            )
            return result
        finally:
            if created:
                try:
                    inspection = await self.docker.call('inspect', container, timeout=10)
                    labels = json.loads(inspection['stdout'])[0]['Config']['Labels']
                    owned = (
                        _ok(inspection)
                        and labels.get('org.recheck.workflow') == workflow_id
                        and labels.get('org.recheck.purpose') == 'generated-build'
                    )
                    if owned:
                        cleanup = await self.docker.call('rm', '--force', container, timeout=15)
                        if not _ok(cleanup):
                            result['cleanup_errors'].append(cleanup)
                    else:
                        result['cleanup_errors'].append(
                            '컴파일 컨테이너의 소유권을 재확인하지 못해 자동 삭제하지 않았습니다.'
                        )
                except Exception as exc:
                    result['cleanup_errors'].append('소유 컴파일 컨테이너 정리 오류: ' + str(exc))
            if result['cleanup_errors'] and result['status'] == 'success':
                result.update(
                    status='partial',
                    error='컴파일은 완료했지만 소유 컨테이너 정리가 완료되지 않았습니다.',
                )
            atomic_json(output_dir / 'result.json', result)
