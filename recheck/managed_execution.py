"""Execution images supplied by the verified managed environment distribution."""

from __future__ import annotations

import json
from pathlib import Path

from .adapters import DockerAdapter, _event, _ok
from .static_backend import _IMAGE_ID, _read_json


class ManagedExecutionAdapter(DockerAdapter):
    """Retain the execution sandbox while replacing Linux runtime builds with pins."""

    def __init__(self, settings, *, runtime=None, cli=None, context=None, resources=None):
        value = (
            settings.get
            if isinstance(settings, dict)
            else (lambda key, default=None: getattr(settings, key, default))
        )
        super().__init__(
            cli=cli,
            context=context if context is not None else value('docker_context', ''),
            resources=resources,
        )
        self.settings = settings
        self.images = dict(self.images)
        self.images['windows'] = value('windows_image', self.images['windows'])
        if runtime is None:
            from .managed_runtime import ManagedRuntime

            runtime = ManagedRuntime(
                settings, docker=self, manifest_path=self.resources / 'runtime/environment.json'
            )
        self.runtime = runtime

    async def prepare_image(self, target_os, cancel=None, on_event=None):
        if target_os == 'windows':
            return dict(
                status='blocked',
                managed=True,
                error_code='windows_runtime_unverified',
                error='현재 동봉 환경은 Linux 실행 비교용입니다. Windows EXE 실행 비교에는 검증된 별도 Windows 격리 환경이 필요합니다. Windows 실행 지원은 아직 검증되지 않았습니다.',
            )
        if target_os != 'linux':
            return await super().prepare_image(target_os, cancel=cancel, on_event=on_event)
        if cancel is not None and cancel.is_set():
            return dict(status='cancelled', managed=True)
        environment = None
        try:
            engine = await self.probe()
            if (
                not engine.get('ready')
                or engine.get('local_endpoint') is not True
                or engine.get('os') != 'linux'
                or engine.get('arch') not in ('amd64', 'x86_64')
            ):
                raise ValueError('A local Linux amd64 Docker engine is required')
            resolve = getattr(self.runtime, 'image_specifications', None)
            manifest = resolve() if resolve else _read_json(self.resources / 'runtime/environment.json')
            specification = manifest['images']['execution']
            image_id = specification.get('image_id')
            if (
                not isinstance(image_id, str)
                or not _IMAGE_ID.fullmatch(image_id)
                or specification.get('platform') != 'linux/amd64'
            ):
                raise ValueError(
                    'The packaged execution image is not configured with an immutable ID'
                )
            environment = await self.runtime.probe()
            if cancel is not None and cancel.is_set():
                return dict(status='cancelled', managed=True, environment=environment)
            image = environment.get('images', {}).get('execution', {})
            if not image.get('ready') or image.get('image_id') != image_id:
                raise ValueError('The pinned packaged execution image is unavailable')
            inspected = await self.call('image', 'inspect', image_id, timeout=15, cancel=cancel)
            if inspected.get('cancelled'):
                return dict(status='cancelled', managed=True, environment=environment)
            if not _ok(inspected):
                raise ValueError('Cannot inspect the pinned execution image')
            details = json.loads(inspected['stdout'])[0]
            labels = (details.get('Config') or {}).get('Labels') or {}
            if (
                details.get('Id') != image_id
                or details.get('Os') != 'linux'
                or details.get('Architecture') not in ('amd64', 'x86_64')
                or labels.get('org.recheck.component') != 'execution'
            ):
                raise ValueError('Execution image identity, platform or component mismatch')
            _event(
                on_event,
                stage='image',
                status='ready',
                message='Verified analysis execution image ready',
            )
            return dict(
                status='success',
                managed=True,
                image=image_id,
                image_id=image_id,
                image_ref=specification.get('ref'),
                details=details,
                build=None,
                reused=True,
                environment=environment,
                network_usage='Verified image; target execution uses network=none',
            )
        except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError) as exc:
            return dict(
                status='blocked',
                managed=True,
                environment=environment,
                error=f'{exc}. Prepare the packaged analysis environment in Setup.',
            )

    async def execute(
        self,
        binary,
        test,
        run_id,
        attempt_id,
        workspace,
        limits=None,
        target_os='linux',
        cancel=None,
        image=None,
        on_event=None,
    ):
        prepared = None
        if target_os == 'windows':
            prepared = await self.prepare_image('windows', cancel=cancel, on_event=on_event)
            return dict(
                status=prepared['status'],
                infrastructure_ok=False,
                error=prepared['error'],
                error_code=prepared['error_code'],
                managed_environment=prepared,
            )
        if target_os == 'linux':
            prepared = await self.prepare_image('linux', cancel=cancel, on_event=on_event)
            if prepared['status'] != 'success':
                return dict(
                    status=prepared['status'],
                    infrastructure_ok=False,
                    error=prepared.get('error'),
                    managed_environment=prepared,
                )
            if image is not None and image != prepared['image_id']:
                return dict(
                    status='blocked',
                    infrastructure_ok=False,
                    error='Explicit execution image differs from the pinned managed image',
                    managed_environment=prepared,
                )
            image = prepared['image_id']
        result = await super().execute(
            binary,
            test,
            run_id,
            attempt_id,
            workspace,
            limits=limits,
            target_os=target_os,
            cancel=cancel,
            image=image,
            on_event=on_event,
        )
        if prepared:
            result['managed_environment'] = dict(
                image_id=prepared['image_id'],
                image_ref=prepared.get('image_ref'),
                component='execution',
            )
            if result.get('image_id') != prepared['image_id'] and (
                result.get('infrastructure_ok') or result.get('image_id')
            ):
                result.update(
                    status='failed',
                    infrastructure_ok=False,
                    error='Execution container actual image differs from the managed pin',
                )
            # The parent persists before returning. Persist the final managed identity/verdict too.
            if result.get('container_name'):
                self._persist(Path(workspace) / (result['container_name'] + '.json'), result)
        return result
