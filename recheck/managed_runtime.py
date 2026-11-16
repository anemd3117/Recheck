"""Pinned Recheck workers; source builds require a separate explicit setup action."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from pathlib import Path
import re
import sys
import time

from .adapters import DockerAdapter, _ok
from .models import data_root, resources_root

_DIGEST = re.compile(r'sha256:[0-9a-f]{64}\Z')
_DISTRIBUTION = re.compile(r'[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}\Z')
_COMPONENTS = ('static', 'execution', 'ollama')
_OLLAMA_CONTAINER = 'recheck-managed-ollama-v1'
_OLLAMA_VOLUME = 'recheck-managed-ollama-models-v1'
_OLLAMA_LABELS = {
    'org.recheck.managed': 'true',
    'org.recheck.component': 'ollama',
    'org.recheck.owner': 'managed-runtime-v1',
}
_OLLAMA_URL = 'http://127.0.0.1:11435'
_OLLAMA_READINESS_SECONDS = 45


class _Cancelled(Exception):
    pass


def _check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise _Cancelled()


class ManagedRuntime:
    """Manage only manifest-pinned images and the optional owned Ollama service.

    ``probe`` never downloads or creates resources. ``prepare`` is the explicit
    distribution import action, using a verified adjacent ``environment`` folder
    before any publisher-authorized immutable registry reference.
    """

    def __init__(self, settings, *, docker=None, manifest_path=None, bundle_dir=None,
                 resources_dir=None, receipt_path=None):
        self.settings = settings
        context = (
            settings.get('docker_context', '')
            if isinstance(settings, dict)
            else getattr(settings, 'docker_context', '')
        )
        self.docker = docker or DockerAdapter(context=context)
        self.manifest_path = (
            Path(manifest_path)
            if manifest_path
            else (resources_root() / 'runtime' / 'environment.json')
        )
        base = (
            Path(sys.executable).resolve().parent
            if getattr(sys, 'frozen', False)
            else Path(__file__).resolve().parent.parent
        )
        self.bundle_dir = Path(bundle_dir) if bundle_dir else base / 'environment'
        self.resources_dir = Path(resources_dir) if resources_dir else self.manifest_path.parent.parent
        self.receipt_path = Path(receipt_path) if receipt_path else data_root() / 'runtime/local-builds.json'
        self.ollama_url = None

    def _manifest(self):
        value = json.loads(self.manifest_path.read_text(encoding='utf-8'))
        if not isinstance(value, dict) or value.get('version') != 1:
            raise ValueError('Unsupported managed environment manifest version')
        images = value.get('images')
        if not isinstance(images, dict):
            raise ValueError('Missing managed image definitions')
        for component in _COMPONENTS:
            spec = images.get(component)
            if not isinstance(spec, dict) or spec.get('platform') != 'linux/amd64':
                raise ValueError('Managed workers require linux/amd64 image definitions')
            image_id = spec.get('image_id')
            if image_id is not None and not self._valid_id(image_id):
                raise ValueError('Invalid immutable image ID')
            archive = self._archive_spec(spec)
            if archive:
                name, digest = archive
                if (
                    not isinstance(name, str)
                    or not name
                    or name in ('.', '..')
                    or any(c in name for c in '/\\:\0\r\n')
                    or not isinstance(digest, str)
                    or not re.fullmatch('[0-9a-f]{64}', digest)
                ):
                    raise ValueError('Invalid environment archive filename or checksum')
        return value

    @staticmethod
    def _valid_id(value):
        return (
            isinstance(value, str)
            and bool(_DIGEST.fullmatch(value))
            and value != 'sha256:' + '0' * 64
        )

    def image_specifications(self):
        """Return validated published IDs or context-bound IDs built on this PC."""
        from .runtime_source import local_image_receipts

        manifest = self._manifest()
        local = local_image_receipts(self.resources_dir, self.manifest_path, self.receipt_path)
        for component, receipt in local.items():
            manifest['images'][component] = {
                **manifest['images'][component], **receipt, 'origin': 'local_source_build',
                'ref': receipt['image_id'], 'trusted_distribution': False,
            }
            manifest['images'][component].pop('archive', None)
            manifest['images'][component].pop('archive_sha256', None)
        return manifest

    async def build_from_sources(self, *, cancel=None, on_event=None, include_ollama=False):
        from .runtime_source import build_runtime_images

        return await build_runtime_images(self, cancel=cancel, on_event=on_event,
                                          include_ollama=include_ollama)

    @staticmethod
    def _archive_spec(spec):
        archive = spec.get('archive')
        if isinstance(archive, dict):
            return archive.get('filename'), archive.get('sha256')
        if archive is not None:
            return archive, spec.get('archive_sha256')
        return None

    async def _emit(self, callback, component, status, message):
        if callback:
            value = callback(
                dict(stage='environment', component=component, status=status, message=message)
            )
            if inspect.isawaitable(value):
                await value

    def _result(self, status, message, *, images=None, environment=None, **details):
        images = images or {}
        result = dict(
            status=status,
            ready=status in ('ready', 'success'),
            message=message,
            images=images,
            environment=environment or {},
            details=details,
        )
        for component, record in images.items():
            if record.get('ready'):
                result[component + '_image_id'] = record['image_id']
        if self.ollama_url:
            result['ollama_url'] = self.ollama_url
        return result

    async def _environment(self):
        environment = await self.docker.probe()
        if not environment.get('ready'):
            return environment, 'Docker Desktop을 실행한 뒤 환경 준비를 다시 눌러 주세요.'
        if not environment.get('local_endpoint'):
            return (
                environment,
                '관리 환경에는 로컬 Docker 연결이 필요합니다. Docker context를 확인해 주세요.',
            )
        if environment.get('os') != 'linux' or environment.get('arch') not in ('amd64', 'x86_64'):
            return (
                environment,
                '관리 환경에는 Linux amd64 Docker가 필요합니다. Docker Desktop의 실행 모드를 확인해 주세요.',
            )
        return environment, None

    async def _image(self, component, spec, cancel=None):
        record = dict(
            ready=False,
            status='unavailable',
            image_id=spec.get('image_id'),
            ref=spec.get('ref', ''),
            platform=spec['platform'],
        )
        if not self._valid_id(spec.get('image_id')):
            record['message'] = '이 개발 배포본에는 검증된 이미지가 등록되지 않았습니다.'
            return record
        _check_cancel(cancel)
        response = await self.docker.call(
            'image', 'inspect', spec['image_id'], timeout=15, cancel=cancel
        )
        if response.get('cancelled'):
            raise _Cancelled()
        if not _ok(response):
            record.update(status='missing', message='배포 이미지 가져오기가 필요합니다.')
            return record
        try:
            data = json.loads(response['stdout'])[0]
            label = (data.get('Config') or {}).get('Labels') or {}
            valid = (
                data.get('Id') == spec['image_id']
                and data.get('Os') == 'linux'
                and data.get('Architecture') == 'amd64'
                and label.get('org.recheck.component') == component
            )
            if valid and spec.get('origin') == 'local_source_build':
                from .runtime_source import valid_built_image

                valid = valid_built_image(data, component, spec['image_id'], spec['context_sha256'])
        except (ValueError, IndexError, TypeError, AttributeError, KeyError):
            data, valid = {}, False
        record.update(ready=valid, status='ready' if valid else 'invalid', details=data)
        if not valid:
            record['message'] = (
                '이미지 식별자, 플랫폼 또는 Recheck 구성 요소 표식이 일치하지 않습니다.'
            )
        return record

    async def probe(self):
        try:
            manifest = self.image_specifications()
        except (OSError, ValueError, TypeError) as exc:
            return self._result(
                'manifest_invalid', '관리 환경 배포 정보를 확인할 수 없습니다.', reason=str(exc)
            )
        environment, problem = await self._environment()
        if problem:
            return self._result('blocked', problem, environment=environment)
        images = {c: await self._image(c, manifest['images'][c]) for c in _COMPONENTS}
        if self.ollama_url:
            container = await self._inspect_resource('container', _OLLAMA_CONTAINER)
            healthy = (
                container is not None
                and self._owned(container)
                and self._safe_ollama(container, images['ollama']['image_id'])
                and bool((container.get('State') or {}).get('Running'))
            )
            if healthy:
                health = await self.docker.call(
                    'exec',
                    '--env',
                    'OLLAMA_HOST=127.0.0.1:11434',
                    _OLLAMA_CONTAINER,
                    '/bin/ollama',
                    'list',
                    timeout=5,
                    output_limit=1024 * 1024,
                )
                healthy = _ok(health)
            if not healthy:
                self.ollama_url = None
        ready = all(images[c]['ready'] for c in ('static', 'execution'))
        return self._result(
            'ready' if ready else 'not_ready',
            (
                '분석 환경이 준비되었습니다.'
                if ready
                else '환경 준비를 눌러 배포된 분석 도구를 가져와 주세요.'
            ),
            images=images,
            environment=environment,
            versions=manifest.get('versions', {}),
            ollama_compute='cpu',
        )

    def _hash_archive(self, path, cancel):
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                _check_cancel(cancel)
                digest.update(chunk)
        _check_cancel(cancel)
        return digest.hexdigest()

    async def _import(self, component, spec, cancel, on_event):
        archive = self._archive_spec(spec)
        if archive:
            filename, expected = archive
            candidate = self.bundle_dir / filename
            if candidate.is_symlink() or candidate.resolve().parent != self.bundle_dir.resolve():
                return 'integrity_failed', '환경 배포 파일의 경로가 올바르지 않습니다.', None
            if candidate.is_file():
                await self._emit(
                    on_event, component, 'running', '배포 파일의 무결성을 확인하고 있습니다.'
                )
                digest = await asyncio.to_thread(self._hash_archive, candidate, cancel)
                if digest != expected:
                    return (
                        'integrity_failed',
                        '환경 배포 파일이 손상되었습니다. 검증된 배포본을 다시 받아 주세요.',
                        None,
                    )
                await self._emit(
                    on_event, component, 'running', '검증된 환경 파일을 Docker에 가져오고 있습니다.'
                )
                response = await self.docker.call(
                    'load',
                    '--input',
                    str(candidate.resolve()),
                    timeout=1200,
                    cancel=cancel,
                    output_limit=4 * 1024 * 1024,
                )
                return (
                    self._operation_status(response),
                    '환경 파일 가져오기를 마치지 못했습니다.',
                    response,
                )
        ref = spec.get('ref', '')
        if (
            spec.get('trusted_distribution') is True
            and isinstance(ref, str)
            and _DISTRIBUTION.fullmatch(ref)
            and not ref.endswith('sha256:' + '0' * 64)
        ):
            await self._emit(
                on_event, component, 'running', '고정된 공식 환경 이미지를 내려받고 있습니다.'
            )
            response = await self.docker.call(
                'pull',
                '--platform',
                'linux/amd64',
                ref,
                timeout=1200,
                cancel=cancel,
                output_limit=4 * 1024 * 1024,
            )
            return (
                self._operation_status(response),
                '환경 이미지 다운로드를 마치지 못했습니다.',
                response,
            )
        return (
            'distribution_missing',
            '실행 파일 옆 environment 폴더에 검증된 환경 배포 파일을 넣고 다시 준비해 주세요.',
            None,
        )

    @staticmethod
    def _operation_status(response):
        if response.get('cancelled'):
            return 'cancelled'
        return 'success' if _ok(response) else 'failed'

    async def _prepare(self, components, cancel, on_event):
        images = {}
        environment = {}
        try:
            _check_cancel(cancel)
            try:
                manifest = self.image_specifications()
            except (OSError, ValueError, TypeError) as exc:
                return self._result(
                    'manifest_invalid', '관리 환경 배포 정보가 올바르지 않습니다.', reason=str(exc)
                )
            environment, problem = await self._environment()
            _check_cancel(cancel)
            if problem:
                return self._result('blocked', problem, environment=environment)
            for component in components:
                _check_cancel(cancel)
                spec = manifest['images'][component]
                record = images[component] = await self._image(component, spec, cancel)
                if record['ready']:
                    continue
                if record['status'] == 'invalid':
                    return self._result(
                        'integrity_failed',
                        record['message'],
                        images=images,
                        environment=environment,
                    )
                if record['status'] == 'unavailable':
                    return self._result(
                        'distribution_missing',
                        '이 배포본에는 검증된 environment 이미지가 없습니다. 완성된 환경 배포본을 사용해 주세요.',
                        images=images,
                        environment=environment,
                    )
                status, message, operation = await self._import(component, spec, cancel, on_event)
                if status != 'success':
                    return self._result(
                        status, message, images=images, environment=environment, operation=operation
                    )
                record = images[component] = await self._image(component, spec, cancel)
                if not record['ready']:
                    return self._result(
                        'integrity_failed',
                        '가져온 환경 이미지 검증을 통과하지 못했습니다.',
                        images=images,
                        environment=environment,
                    )
            await self._emit(on_event, 'environment', 'success', '분석 환경이 준비되었습니다.')
            return self._result(
                'success',
                '분석 환경이 준비되었습니다.',
                images=images,
                environment=environment,
                versions=manifest.get('versions', {}),
            )
        except _Cancelled:
            return self._result(
                'cancelled',
                '환경 준비를 중단했습니다. 다시 준비할 수 있습니다.',
                images=images,
                environment=environment,
            )
        except OSError as exc:
            return self._result(
                'failed',
                '환경 배포 파일을 읽을 수 없습니다.',
                images=images,
                environment=environment,
                reason=str(exc),
            )

    async def prepare(self, cancel=None, on_event=None, include_ollama=False):
        components = _COMPONENTS if include_ollama else ('static', 'execution')
        return await self._prepare(components, cancel, on_event)

    async def _inspect_resource(self, kind, name, cancel=None):
        _check_cancel(cancel)
        response = await self.docker.call(kind, 'inspect', name, timeout=10, cancel=cancel)
        if response.get('cancelled'):
            raise _Cancelled()
        if not _ok(response):
            return None
        try:
            data = json.loads(response['stdout'])[0]
            return data if isinstance(data, dict) else {'invalid': True}
        except (ValueError, IndexError, TypeError, KeyError):
            return {'invalid': True}

    @staticmethod
    def _owned(resource, kind='container'):
        labels = (
            (resource.get('Config') or {}).get('Labels')
            if kind == 'container'
            else resource.get('Labels')
        ) or {}
        return isinstance(labels, dict) and all(
            labels.get(k) == v for k, v in _OLLAMA_LABELS.items()
        )

    @staticmethod
    def _safe_ollama(resource, image_id):
        config = resource.get('Config') or {}
        host = resource.get('HostConfig') or {}
        mounts = resource.get('Mounts') or []
        ports = host.get('PortBindings') or {}
        expected_port = {'11434/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '11435'}]}
        return (
            resource.get('Image') == image_id
            and 'OLLAMA_NO_CLOUD=1' in (config.get('Env') or [])
            and 'OLLAMA_HOST=0.0.0.0:11434' in (config.get('Env') or [])
            and ports == expected_port
            and len(mounts) == 1
            and mounts[0].get('Type') == 'volume'
            and mounts[0].get('Name') == _OLLAMA_VOLUME
            and mounts[0].get('Destination') == '/root/.ollama'
            and not any(
                host.get(k) for k in ('Privileged', 'Binds', 'Devices', 'CapAdd', 'DeviceRequests')
            )
            and host.get('NetworkMode') != 'host'
            and host.get('PidMode') != 'host'
            and host.get('IpcMode') != 'host'
        )

    async def _remove_owned_ollama(self, cancel=None):
        """Inspect ownership immediately before cleanup; model volumes survive."""
        self.ollama_url = None
        container = await self._inspect_resource('container', _OLLAMA_CONTAINER, cancel)
        if container is None:
            return self._result('success', '관리 로컬 AI가 종료되어 있습니다.')
        if not self._owned(container):
            return self._result(
                'ownership_conflict',
                '같은 이름의 다른 컨테이너가 있어 관리 로컬 AI 종료를 중단했습니다.',
            )
        if (container.get('State') or {}).get('Running'):
            response = await self.docker.call(
                'stop', '--time', '10', _OLLAMA_CONTAINER, timeout=20, cancel=cancel
            )
            status = self._operation_status(response)
            if status != 'success':
                return self._result(
                    status, '관리 로컬 AI 종료를 마치지 못했습니다.', operation=response
                )
        # Re-inspect prevents an unrelated replacement from being removed.
        container = await self._inspect_resource('container', _OLLAMA_CONTAINER, cancel)
        if container is None:
            return self._result('success', '관리 로컬 AI가 종료되었습니다. 모델은 보관됩니다.')
        if not self._owned(container):
            return self._result('ownership_conflict', '컨테이너 소유권이 달라 제거를 중단했습니다.')
        response = await self.docker.call('rm', _OLLAMA_CONTAINER, timeout=15, cancel=cancel)
        return self._result(
            self._operation_status(response),
            (
                '관리 로컬 AI가 종료되었습니다. 내려받은 모델은 보관됩니다.'
                if _ok(response)
                else '관리 로컬 AI 컨테이너 제거를 마치지 못했습니다.'
            ),
            operation=response,
        )

    async def stop_ollama(self, cancel=None):
        self.ollama_url = None
        try:
            _check_cancel(cancel)
            environment, problem = await self._environment()
            if problem:
                return self._result('blocked', problem, environment=environment)
            return await self._remove_owned_ollama(cancel)
        except _Cancelled:
            return self._result('cancelled', '관리 로컬 AI 종료를 중단했습니다.')

    async def start_ollama(self, cancel=None, on_event=None):
        """Start the optional local model service, on a separate loopback port.

        CPU is the portable default. No host credentials, Docker socket, GPU
        devices or host directories are mounted; only an owned named volume is
        retained for model downloads. A GPT setup never calls this method.
        """
        self.ollama_url = None
        prepared = await self._prepare(('ollama',), cancel, on_event)
        if not prepared['ready']:
            return prepared
        touched = False
        images = prepared['images']
        image_id = images['ollama']['image_id']
        try:
            _check_cancel(cancel)
            container = await self._inspect_resource('container', _OLLAMA_CONTAINER, cancel)
            if container is not None:
                if not self._owned(container):
                    return self._result(
                        'ownership_conflict',
                        '관리 로컬 AI와 같은 이름의 다른 컨테이너가 있습니다. Docker에서 이름을 확인해 주세요.',
                    )
                if not self._safe_ollama(container, image_id):
                    return self._result(
                        'configuration_conflict',
                        '기존 관리 로컬 AI의 이미지 또는 격리 설정이 달라 시작하지 않았습니다.',
                    )
            volume = await self._inspect_resource('volume', _OLLAMA_VOLUME, cancel)
            if volume is not None and not self._owned(volume, 'volume'):
                return self._result(
                    'ownership_conflict',
                    '관리 모델 저장소와 같은 이름의 다른 볼륨이 있습니다. Docker에서 이름을 확인해 주세요.',
                )
            if volume is None:
                args = ['volume', 'create']
                for key, value in _OLLAMA_LABELS.items():
                    args += ['--label', key + '=' + value]
                response = await self.docker.call(*args, _OLLAMA_VOLUME, timeout=15, cancel=cancel)
                if not _ok(response):
                    return self._result(
                        self._operation_status(response),
                        '관리 모델 저장소를 만들지 못했습니다.',
                        operation=response,
                    )
                volume = await self._inspect_resource('volume', _OLLAMA_VOLUME, cancel)
                if not volume or not self._owned(volume, 'volume'):
                    return self._result(
                        'ownership_conflict', '만들어진 모델 저장소의 소유권을 확인하지 못했습니다.'
                    )
            if container is None:
                args = ['create', '--name', _OLLAMA_CONTAINER]
                for key, value in _OLLAMA_LABELS.items():
                    args += ['--label', key + '=' + value]
                args += [
                    '--publish',
                    '127.0.0.1:11435:11434',
                    '--mount',
                    'type=volume,src=' + _OLLAMA_VOLUME + ',dst=/root/.ollama',
                    '--env',
                    'OLLAMA_HOST=0.0.0.0:11434',
                    '--env',
                    'OLLAMA_NO_CLOUD=1',
                    '--cap-drop',
                    'ALL',
                    '--security-opt',
                    'no-new-privileges',
                    '--pids-limit',
                    '256',
                    '--log-driver',
                    'json-file',
                    '--log-opt',
                    'max-size=5m',
                    '--log-opt',
                    'max-file=2',
                    image_id,
                    'serve',
                ]
                touched = True  # A cancelled CLI may still have created the owned container.
                response = await self.docker.call(*args, timeout=30, cancel=cancel)
                if not _ok(response):
                    cleanup = await self._remove_owned_ollama()
                    return self._result(
                        self._operation_status(response),
                        '관리 로컬 AI 컨테이너를 만들지 못했습니다.',
                        operation=response,
                        cleanup=cleanup,
                    )
                container = await self._inspect_resource('container', _OLLAMA_CONTAINER, cancel)
                if (
                    not container
                    or not self._owned(container)
                    or not self._safe_ollama(container, image_id)
                ):
                    cleanup = await self._remove_owned_ollama()
                    return self._result(
                        'configuration_conflict',
                        '생성된 관리 로컬 AI 설정을 확인하지 못했습니다.',
                        cleanup=cleanup,
                    )
            if not (container.get('State') or {}).get('Running'):
                touched = True
                response = await self.docker.call(
                    'start', _OLLAMA_CONTAINER, timeout=30, cancel=cancel
                )
                if not _ok(response):
                    cleanup = await self._remove_owned_ollama()
                    return self._result(
                        self._operation_status(response),
                        '관리 로컬 AI를 시작하지 못했습니다. 11435 포트 사용 여부를 확인해 주세요.',
                        operation=response,
                        cleanup=cleanup,
                    )
            await self._emit(
                on_event, 'ollama', 'running', '관리 로컬 AI의 응답을 확인하고 있습니다.'
            )
            deadline = time.monotonic() + _OLLAMA_READINESS_SECONDS
            while time.monotonic() < deadline:
                _check_cancel(cancel)
                response = await self.docker.call(
                    'exec',
                    '--env',
                    'OLLAMA_HOST=127.0.0.1:11434',
                    _OLLAMA_CONTAINER,
                    '/bin/ollama',
                    'list',
                    timeout=5,
                    cancel=cancel,
                    output_limit=1024 * 1024,
                )
                if response.get('cancelled'):
                    raise _Cancelled()
                if _ok(response):
                    self.ollama_url = _OLLAMA_URL
                    await self._emit(
                        on_event, 'ollama', 'success', '관리 로컬 AI가 준비되었습니다.'
                    )
                    return self._result(
                        'success',
                        '관리 로컬 AI가 준비되었습니다.',
                        images=images,
                        container=_OLLAMA_CONTAINER,
                        models_volume=_OLLAMA_VOLUME,
                        compute='cpu',
                    )
                await asyncio.sleep(0.25)
            cleanup = await self._remove_owned_ollama() if touched else None
            return self._result(
                'readiness_timeout',
                '관리 로컬 AI가 제한 시간 안에 응답하지 않았습니다.',
                operation=response,
                cleanup=cleanup,
            )
        except _Cancelled:
            cleanup = await self._remove_owned_ollama() if touched else None
            return self._result('cancelled', '관리 로컬 AI 시작을 중단했습니다.', cleanup=cleanup)
        except asyncio.CancelledError:
            if touched:
                await asyncio.shield(self._remove_owned_ollama())
            raise
