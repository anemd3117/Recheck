"""Explicit first-run Docker builds from shipped, pinned source contexts.

No compared program is executed here. Probing never calls this module's builder;
only the separate setup action may download upstream components and build images.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile

from .adapters import _ok
from .models import atomic_json

COMPONENTS = ('static', 'execution', 'ollama')
BUILD_CONTRACT = 'source-v1'
_DIGEST = re.compile(r'[0-9a-f]{64}\Z')
_IMAGE_ID = re.compile(r'sha256:[0-9a-f]{64}\Z')


class BuildCancelled(Exception):
    pass


def _check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise BuildCancelled()


def _context_files(context):
    """Admit only bounded, ordinary files from the selected shipped context."""
    context = Path(context)
    if context.is_symlink() or getattr(context, 'is_junction', lambda: False)() or not context.is_dir():
        raise ValueError('Missing or linked analysis source context')
    paths = sorted(context.rglob('*'))
    if len(paths) > 10000:
        raise ValueError('Analysis source context exceeds its file limit')
    if any(path.is_symlink() or getattr(path, 'is_junction', lambda: False)() for path in paths):
        raise ValueError('Linked analysis source context member')
    files = [path for path in paths if path.is_file()]
    if not (context / 'Dockerfile').is_file():
        raise ValueError('Missing analysis Dockerfile')
    if sum(path.stat().st_size for path in files) > 64 * 1024 * 1024:
        raise ValueError('Analysis source context exceeds its size limit')
    return files


def context_digest(resources, component, manifest_path, *, context=None):
    if component not in COMPONENTS:
        raise ValueError('Unknown analysis component')
    folder = Path(context) if context is not None else Path(resources) / 'docker' / component
    identity = {'manifest': hashlib.sha256(Path(manifest_path).read_bytes()).hexdigest()}
    identity['context'] = {
        path.relative_to(folder).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in _context_files(folder)
    }
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def local_image_receipts(resources, manifest_path, receipt_path):
    """Read-only trust selection; a stale/malformed receipt grants no image trust."""
    try:
        path = Path(receipt_path)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
            return {}
        value = json.loads(path.read_text(encoding='utf-8'))
        if value.get('version') != 1 or value.get('build_contract') != BUILD_CONTRACT:
            return {}
        images = value.get('images')
        if not isinstance(images, dict):
            return {}
        selected = {}
        for component in COMPONENTS:
            record = images.get(component)
            if (
                not isinstance(record, dict)
                or not isinstance(record.get('image_id'), str)
                or not _IMAGE_ID.fullmatch(record['image_id'])
                or record['image_id'] == 'sha256:' + '0' * 64
                or record.get('platform') != 'linux/amd64'
                or not isinstance(record.get('context_sha256'), str)
                or not _DIGEST.fullmatch(record['context_sha256'])
            ):
                continue
            try:
                digest = context_digest(resources, component, manifest_path)
            except (OSError, ValueError):
                continue
            if record['context_sha256'] == digest:
                selected[component] = record
        return selected
    except (OSError, ValueError, AttributeError, TypeError):
        return {}


def build_labels(component, digest):
    return {
        'org.recheck.managed': 'true',
        'org.recheck.component': component,
        'org.recheck.source-context': digest,
        'org.recheck.build-contract': BUILD_CONTRACT,
    }


def valid_built_image(data, component, image_id, digest):
    labels = (data.get('Config') or {}).get('Labels') or {}
    return (
        data.get('Id') == image_id
        and data.get('Os') == 'linux'
        and data.get('Architecture') == 'amd64'
        and isinstance(labels, dict)
        and all(labels.get(key) == value for key, value in build_labels(component, digest).items())
        and (component != 'static' or labels.get('org.recheck.worker-contract') == 'rea-static-1.0')
    )


async def build_runtime_images(runtime, *, cancel=None, on_event=None, include_ollama=False):
    """Build only selected shipped contexts, then atomically register inspected IDs.

    The Docker build receives a copied context and writes its immutable ID into a
    unique iidfile. No mutable image tag is used for admission. A failed/cancelled
    setup leaves any previous receipt unchanged and never starts containers.
    """
    from .managed_runtime import _Cancelled

    components = COMPONENTS if include_ollama else ('static', 'execution')
    images, environment = {}, {}
    try:
        _check_cancel(cancel)
        runtime._manifest()  # Validate the shipped manifest before external work.
        environment, problem = await runtime._environment()
        _check_cancel(cancel)
        if problem:
            return runtime._result('blocked', problem, environment=environment)
        identities = {
            component: context_digest(runtime.resources_dir, component, runtime.manifest_path)
            for component in components
        }
        receipt_path = Path(runtime.receipt_path)
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        if (receipt_path.is_symlink() or receipt_path.parent.is_symlink()
                or getattr(receipt_path.parent, 'is_junction', lambda: False)()):
            raise ValueError('Linked local runtime receipt path')
        previous = local_image_receipts(runtime.resources_dir, runtime.manifest_path, receipt_path)
        receipts = dict(previous)
        with tempfile.TemporaryDirectory(prefix='source-build-', dir=receipt_path.parent) as temporary:
            staging = Path(temporary)
            # Snapshot and verify every source before invoking the first build.
            for component in components:
                source = Path(runtime.resources_dir) / 'docker' / component
                shutil.copytree(source, staging / component)
                if context_digest(runtime.resources_dir, component, runtime.manifest_path,
                                  context=staging / component) != identities[component]:
                    raise ValueError('Analysis context changed while preparing build')
            for component in components:
                _check_cancel(cancel)
                digest = identities[component]
                await runtime._emit(on_event, component, 'running',
                                    '공식 구성요소를 내려받아 분석 환경을 만들고 있습니다. 처음에는 수 GB와 수 분이 필요할 수 있습니다.')
                iid = staging / (component + '.iid')
                arguments = ['build', '--platform', 'linux/amd64', '--provenance=false',
                             '--progress=plain', '--iidfile', str(iid)]
                for key, value in build_labels(component, digest).items():
                    arguments += ['--label', key + '=' + value]
                arguments.append(str(staging / component))

                async def progress(channel, chunk):
                    text = chunk.decode('utf-8', errors='replace').strip()
                    if text:
                        await runtime._emit(on_event, component, 'running', text[-800:])

                operation = await runtime.docker.call(*arguments, timeout=1800, cancel=cancel,
                                                      output_limit=16 * 1024 * 1024, on_output=progress)
                if operation.get('cancelled'):
                    raise BuildCancelled()
                _check_cancel(cancel)
                if not _ok(operation):
                    return runtime._result('failed', '분석 환경 만들기를 마치지 못했습니다. 인터넷 연결과 Docker 상태를 확인한 뒤 다시 시도해 주세요.',
                                           images=images, environment=environment, operation=operation)
                image_id = iid.read_text(encoding='ascii').strip()
                if not _IMAGE_ID.fullmatch(image_id) or image_id == 'sha256:' + '0' * 64:
                    raise ValueError('Docker build did not return an immutable image ID')
                inspected = await runtime.docker.call('image', 'inspect', image_id, timeout=15, cancel=cancel)
                if inspected.get('cancelled'):
                    raise BuildCancelled()
                if not _ok(inspected):
                    raise ValueError('Cannot inspect the built analysis image')
                data = json.loads(inspected['stdout'])[0]
                if not valid_built_image(data, component, image_id, digest):
                    raise ValueError('Built analysis image identity, platform or labels differ')
                if context_digest(runtime.resources_dir, component, runtime.manifest_path) != digest:
                    raise ValueError('Shipped analysis context changed during the build')
                images[component] = dict(ready=True, status='ready', image_id=image_id,
                                         platform='linux/amd64', origin='local_source_build',
                                         context_sha256=digest, details=data)
                receipts[component] = dict(image_id=image_id, platform='linux/amd64', context_sha256=digest)
            _check_cancel(cancel)
            # Detect edits to an earlier component while a later build ran.
            if any(context_digest(runtime.resources_dir, c, runtime.manifest_path) != identities[c]
                   for c in components):
                raise ValueError('Shipped analysis source changed before registration')
            atomic_json(receipt_path, dict(version=1, build_contract=BUILD_CONTRACT, images=receipts,
                                          created_utc=datetime.now(timezone.utc).isoformat()))
        await runtime._emit(on_event, 'environment', 'success', '분석 환경이 준비되었습니다.')
        return runtime._result('success', '분석 환경이 준비되었습니다.', images=images, environment=environment,
                               origin='local_source_build')
    except (BuildCancelled, _Cancelled):
        return runtime._result('cancelled', '분석 환경 만들기를 중단했습니다. 다시 시작할 수 있습니다.',
                               images=images, environment=environment)
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
        return runtime._result('failed', '분석 환경을 만들지 못했습니다. 배포 파일과 Docker 상태를 확인해 주세요.',
                               images=images, environment=environment, reason=str(exc))


async def build_analysis_environment(settings, *, cancel=None, on_event=None, runtime=None, include_ollama=False):
    from .managed_runtime import ManagedRuntime

    return await (runtime or ManagedRuntime(settings)).build_from_sources(
        cancel=cancel, on_event=on_event, include_ollama=include_ollama
    )
