"""Pinned, isolated REA/Ghidra static analysis through the managed Docker image.

The target is data: only the trusted image entrypoint is started. Worker output,
container identity, input identity and cached evidence are independently checked.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import uuid

from .adapters import DockerAdapter, _event, _ok, resource_root, validate_static
from .models import Settings, atomic_json, inspect_binary
from .paths import filesystem_path, plain_local_path

WORKER_CONTRACT = 'rea-static-1.0'
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_EVIDENCE_BYTES = 64 * 1024 * 1024
_IMAGE_ID = re.compile(r'^sha256:[a-f0-9]{64}$')


def _digest(path):
    digest = hashlib.sha256()
    with filesystem_path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _checksum(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode('utf-8')).hexdigest()


def _safe_path(path):
    path = plain_local_path(path).absolute()
    if any(char in str(path) for char in ('\x00', '\r', '\n', ',')):
        raise ValueError('Unsafe Docker mount path')
    for component in (path, *path.parents):
        try:
            info = filesystem_path(component).lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Symlink or reparse-point paths are not accepted')
    return path


def _relative(name):
    if not isinstance(name, str) or not name or '\\' in name or ':' in name:
        raise ValueError('Invalid raw evidence path')
    value = PurePosixPath(name)
    if (
        value.is_absolute()
        or any(part in ('..', '.') for part in name.split('/'))
        or any(char in name for char in ('\x00', '\r', '\n'))
        or str(value) != name
    ):
        raise ValueError('Raw evidence escapes the output workspace')
    return value


def _read_json(path, limit=MAX_ARTIFACT_BYTES):
    path = _safe_path(path)
    if not filesystem_path(path).is_file() or filesystem_path(path).stat().st_size > limit:
        raise ValueError('Missing or oversized static analysis artifact')
    return json.loads(filesystem_path(path).read_text(encoding='utf-8'))


def _validate_artifact(data, expected_hash):
    validate_static(data)
    if data['sha256'] != expected_hash:
        raise ValueError('Static artifact hash mismatch')
    expected_tool = ('REA MCP / Managed metadata' if data.get('analysis_kind') == 'managed'
                     else 'REA MCP / Ghidra')
    if data.get('backend') != 'rea' or data.get('tool') != expected_tool:
        raise ValueError('Unexpected static analysis backend')
    source = data.get('source')
    if (
        not isinstance(source, dict)
        or source.get('container_path') != '/input/target'
        or source.get('sha256') != expected_hash
    ):
        raise ValueError('Static source hash or path mismatch')
    versions = data.get('backend_versions')
    if (
        not isinstance(versions, dict)
        or not versions.get('rea')
        or not versions.get('ghidra')
        or any(not isinstance(value, str) for value in versions.values())
    ):
        raise ValueError('Missing backend versions')
    if versions.get('worker_contract') != WORKER_CONTRACT:
        raise ValueError('Unsupported static worker contract')
    if data.get('status', 'success') not in (
        'success',
        'partial',
        'failed',
        'cancelled',
        'blocked',
    ):
        raise ValueError('Invalid static analysis status')
    for field in ('partial', 'analysis_timed_out', 'functions_truncated', 'complete'):
        if field in data and not isinstance(data[field], bool):
            raise ValueError('Invalid static completion flag')
    raw = data.get('raw_evidence')
    if not isinstance(raw, dict) or not isinstance(raw.get('files'), list):
        raise ValueError('Missing raw static evidence inventory')
    files = raw['files']
    if len(files) > 10000 or len(set(files)) != len(files):
        raise ValueError('Invalid raw evidence inventory')
    for name in files:
        _relative(name)
    return files


def _incomplete(data):
    raw = data.get('raw_evidence') or {}
    return bool(
        data.get('partial')
        or data.get('partial_reasons')
        or data.get('analysis_timed_out')
        or data.get('functions_truncated')
        or data.get('complete') is False
        or raw.get('truncated')
        or raw.get('partial_reasons')
    )


class ContainerStaticAdapter:
    def __init__(self, context='', resources=None, cache_dir=None, runtime=None, docker=None):
        self.resources = Path(resources) if resources else resource_root()
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.docker = docker or DockerAdapter(context=context, resources=self.resources)
        if runtime is None:
            from .managed_runtime import ManagedRuntime

            runtime = ManagedRuntime(
                Settings(docker_context=context),
                docker=self.docker,
                manifest_path=self.resources / 'runtime/environment.json',
            )
        self.runtime = runtime

    async def probe(self):
        result = dict(ready=False, status='blocked', backend='rea', tool='REA MCP / Ghidra')
        try:
            engine = await self.docker.probe()
            result['engine'] = engine
            if not engine.get('ready') or engine.get('local_endpoint') is not True:
                raise ValueError('A local Docker engine is required for static analysis')
            if engine.get('os') != 'linux' or engine.get('arch') not in ('amd64', 'x86_64'):
                raise ValueError('Static analysis requires a Linux amd64 Docker engine')
            resolve = getattr(self.runtime, 'image_specifications', None)
            manifest = resolve() if resolve else _read_json(self.resources / 'runtime/environment.json')
            specification = manifest['images']['static']
            image_id = specification['image_id']
            if not isinstance(image_id, str) or not _IMAGE_ID.fullmatch(image_id):
                raise ValueError('Managed static image must have an immutable image ID')
            if specification.get('platform') != 'linux/amd64':
                raise ValueError('Managed static image platform mismatch')
            environment = await self.runtime.probe()
            result['runtime'] = environment
            image = environment.get('images', {}).get('static', {})
            if not image.get('ready') or image.get('image_id') != image_id:
                raise ValueError('The pinned managed static image is unavailable')
            inspected = await self.docker.call('image', 'inspect', image_id, timeout=15)
            if not _ok(inspected):
                raise ValueError('Cannot inspect the managed static image')
            details = json.loads(inspected['stdout'])[0]
            if (
                details.get('Id') != image_id
                or details.get('Os') != 'linux'
                or details.get('Architecture') not in ('amd64', 'x86_64')
                or (details.get('Config') or {}).get('Labels', {}).get('org.recheck.component')
                != 'static'
            ):
                raise ValueError('Managed static image identity or component mismatch')
            result.update(
                ready=True,
                status='ready',
                image_id=image_id,
                image=specification.get('ref'),
                details=details,
            )
        except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
            result['error'] = str(exc)
            result['reason'] = str(exc)
        return result

    async def _cleanup(self, name):
        errors = []
        try:
            inspected = await self.docker.call('inspect', name, timeout=15)
            if not _ok(inspected):
                if re.search(
                    r'No such (?:container|object)\b', inspected.get('stderr', ''), re.IGNORECASE
                ):
                    return []
                return ['Could not verify container ownership for cleanup']
            details = json.loads(inspected['stdout'])[0]
            labels = (details.get('Config') or {}).get('Labels') or {}
            if (
                labels.get('org.recheck.managed') != 'true'
                or labels.get('org.recheck.component') != 'static'
                or labels.get('org.recheck.owner') != name
            ):
                return ['Container ownership mismatch; container was not removed']
            # Docker may have created the container even if the create CLI was killed.
            await self.docker.call('kill', name, timeout=15)
            removed = await self.docker.call('rm', '-f', name, timeout=15)
            if not _ok(removed):
                errors.append('Could not remove owned static container')
        except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
            errors.append(str(exc))
        return errors

    def _decorate(self, data, output, provenance):
        result = {
            key: value
            for key, value in data.items()
            if key
            not in (
                'process',
                'environment',
                'container',
                'create_process',
                'cache',
                'logs',
                'cleanup_errors',
                'worker_receipt',
            )
        }
        result['backend_provenance'] = provenance
        result['backend_source'] = dict(
            result=str(output / 'result.json'),
            raw_evidence=[
                str(output.joinpath(*_relative(name).parts))
                for name in data['raw_evidence']['files']
            ],
        )
        return result

    def _read_evidence(self, data, output):
        files, total = {}, 0
        for name in data['raw_evidence']['files']:
            path = _safe_path(output.joinpath(*_relative(name).parts))
            if not path.is_relative_to(output) or not filesystem_path(path).is_file():
                raise ValueError('Missing raw static evidence file')
            total += filesystem_path(path).stat().st_size
            if total > MAX_EVIDENCE_BYTES:
                raise ValueError('Raw static evidence exceeds its size budget')
            content = filesystem_path(path).read_bytes()
            files[name] = dict(
                sha256=hashlib.sha256(content).hexdigest(),
                base64=base64.b64encode(content).decode('ascii'),
            )
        return files

    def _restore_cache(self, cache_path, signature, output):
        cached = _read_json(cache_path, 2 * (MAX_ARTIFACT_BYTES + MAX_EVIDENCE_BYTES))
        artifact = cached['artifact']
        _validate_artifact(artifact, signature['binary_sha256'])
        if (
            cached['signature'] != signature
            or cached['artifact_sha256'] != _checksum(artifact)
            or artifact.get('backend_provenance') != signature
            or artifact.get('status') != 'success'
            or artifact.get('complete') is not True
            or _incomplete(artifact)
        ):
            raise ValueError('Cached artifact identity or completion mismatch')
        files = cached['files']
        if not isinstance(files, dict) or set(files) != set(artifact['raw_evidence']['files']):
            raise ValueError('Cached evidence inventory mismatch')
        decoded, total = {}, 0
        for name, entry in files.items():
            _relative(name)
            content = base64.b64decode(entry['base64'], validate=True)
            total += len(content)
            if total > MAX_EVIDENCE_BYTES or hashlib.sha256(content).hexdigest() != entry['sha256']:
                raise ValueError('Cached evidence integrity mismatch')
            decoded[name] = content
        # Nothing is written until the complete envelope and evidence have passed.
        for name, content in decoded.items():
            path = _safe_path(output.joinpath(*_relative(name).parts))
            filesystem_path(path.parent).mkdir(parents=True, exist_ok=True)
            filesystem_path(path).write_bytes(content)
        atomic_json(output / 'result.json', artifact)
        return artifact

    def _publish_evidence(self, data, evidence, output):
        """Publish only checked artifacts, never the worker's whole writable project tree."""
        for name, entry in evidence.items():
            path = _safe_path(output.joinpath(*_relative(name).parts))
            filesystem_path(path.parent).mkdir(parents=True, exist_ok=True)
            _safe_path(path)
            content = base64.b64decode(entry['base64'], validate=True)
            if hashlib.sha256(content).hexdigest() != entry['sha256']:
                raise ValueError('Static evidence changed before publication')
            filesystem_path(path).write_bytes(content)
        atomic_json(output / 'result.json', data)

    @staticmethod
    def _remove_short_stage(stage, staging_root):
        """Delete only this invocation beneath its checked temporary staging root."""
        checked = _safe_path(stage).resolve(strict=True)
        root = _safe_path(staging_root).resolve(strict=True)
        checked.relative_to(root)
        if checked.parent != root or not re.fullmatch(r'static-[A-Za-z0-9_-]+', checked.name):
            raise ValueError('Short static staging ownership mismatch')
        shutil.rmtree(checked)

    async def analyze(self, binary, workspace, *, timeout=300, cancel=None, on_event=None):
        result = dict(
            schema_version='1.0',
            status='failed',
            complete=False,
            functions=[],
            backend='rea',
            tool='REA MCP / Ghidra',
            cache=dict(hit=False),
        )
        if cancel is not None and cancel.is_set():
            result['status'] = 'cancelled'
            return result
        environment = await self.probe()
        result['environment'] = environment
        if cancel is not None and cancel.is_set():
            result['status'] = 'cancelled'
            return result
        if not environment['ready']:
            result.update(
                status='blocked', error=environment.get('error', 'Static image unavailable')
            )
            return result
        cache_error, cache_path, cached, evidence = None, None, False, None
        short_stage, staging_root = None, None
        try:
            if (
                isinstance(timeout, bool)
                or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout)
                or not 1 <= timeout <= 3600
            ):
                raise ValueError('Invalid static analysis timeout')
            binary, workspace = _safe_path(binary), _safe_path(workspace)
            if not filesystem_path(binary).is_file():
                raise ValueError('Static analysis requires a regular input file')
            expected_hash = _digest(binary)
            result['sha256'] = expected_hash
            filesystem_path(workspace).mkdir(parents=True, exist_ok=True)
            _safe_path(workspace)
            invocation = workspace / ('static-' + uuid.uuid4().hex)
            input_dir, output = invocation / 'input', invocation / 'output'
            filesystem_path(input_dir).mkdir(parents=True)
            filesystem_path(output).mkdir()
            filesystem_path(output).chmod(0o777)
            snapshot = input_dir / 'target'
            shutil.copyfile(filesystem_path(binary), filesystem_path(snapshot))
            filesystem_path(snapshot).chmod(0o644)
            if _digest(snapshot) != expected_hash or _digest(binary) != expected_hash:
                raise ValueError('Input snapshot hash mismatch')
            signature = dict(
                binary_sha256=expected_hash,
                image_id=environment['image_id'],
                worker_contract=WORKER_CONTRACT,
            )
            if self.cache_dir:
                cache_dir = _safe_path(self.cache_dir)
                filesystem_path(cache_dir).mkdir(parents=True, exist_ok=True)
                cache_key = _checksum(signature)
                cache_path = cache_dir / (cache_key + '.json')
                result['cache']['key'] = cache_key
                if filesystem_path(cache_path).exists():
                    try:
                        data = self._restore_cache(cache_path, signature, output)
                        if _digest(binary) != expected_hash or _digest(snapshot) != expected_hash:
                            raise ValueError('Input hash changed during cache validation')
                        result.update(self._decorate(data, output, signature))
                        result['cache'] = dict(
                            hit=True,
                            key=cache_key,
                            source=str(cache_path),
                            verified_signature=signature,
                        )
                        _event(
                            on_event,
                            stage='static',
                            status='cached',
                            message='Verified managed static artifact reused',
                        )
                        cached = True
                    except (ValueError, TypeError, KeyError, OSError) as exc:
                        cache_error = str(exc)
            if cached:
                return result
            mount_input, worker_output, mounted_snapshot = input_dir, output, snapshot
            if os.name == 'nt' and max(len(str(input_dir)), len(str(output))) >= 220:
                staging_root = _safe_path(Path(tempfile.gettempdir()) / 'Recheck')
                staging_root.mkdir(parents=True, exist_ok=True)
                short_stage = Path(tempfile.mkdtemp(prefix='static-', dir=staging_root))
                result['short_staging'] = dict(path=str(short_stage), removed=False)
                mount_input, worker_output = short_stage / 'input', short_stage / 'output'
                mount_input.mkdir()
                worker_output.mkdir()
                worker_output.chmod(0o777)
                mounted_snapshot = mount_input / 'target'
                shutil.copyfile(filesystem_path(snapshot), mounted_snapshot)
                mounted_snapshot.chmod(0o644)
                if _digest(mounted_snapshot) != expected_hash:
                    raise ValueError('Short input snapshot hash mismatch')
            _event(
                on_event,
                stage='static',
                status='running',
                message='REA MCP / Ghidra isolated static analysis',
            )
            name = 'recheck-static-' + uuid.uuid4().hex
            command = (
                'create',
                '--name',
                name,
                '--label',
                'org.recheck.managed=true',
                '--label',
                'org.recheck.component=static',
                '--label',
                'org.recheck.owner=' + name,
                '--network',
                'none',
                '--read-only',
                '--cap-drop',
                'ALL',
                '--security-opt',
                'no-new-privileges:true',
                '--user',
                '10001:10001',
                '--cpus',
                '2',
                '--memory',
                '4096m',
                '--pids-limit',
                '512',
                '--tmpfs',
                '/tmp:rw,nosuid,nodev,size=1073741824,mode=1777',
                '--mount',
                f'type=bind,src={mount_input},dst=/input,readonly',
                '--mount',
                f'type=bind,src={worker_output},dst=/output',
                '--env',
                'HOME=/output',
                '--env',
                'TMPDIR=/tmp',
                '-i',
                environment['image_id'],
            )
            try:
                created = await self.docker.call(*command, timeout=30, cancel=cancel)
                result['create_process'] = created
                if not _ok(created):
                    result['status'] = 'cancelled' if created.get('cancelled') else 'failed'
                    raise ValueError('Managed static container creation failed')
                request = dict(
                    operation='analyze',
                    input='/input/target',
                    output='/output',
                    timeout_seconds=int(timeout),
                    max_functions=10000,
                    max_decompile=100,
                )
                if inspect_binary(mounted_snapshot).get('managed'):
                    request['analysis_kind'] = 'managed'
                worker = await self.docker.call(
                    'start',
                    '-a',
                    '-i',
                    name,
                    stdin=json.dumps(request).encode('utf-8'),
                    timeout=timeout + 120,
                    cancel=cancel,
                    output_limit=4 * 1024 * 1024,
                )
                result['process'] = worker
                if worker.get('cancelled'):
                    result['status'] = 'cancelled'
                elif worker.get('timed_out'):
                    result.update(status='partial', external_timeout=True)
                inspected = await self.docker.call('inspect', name, timeout=15)
                if not _ok(inspected):
                    raise ValueError('Cannot verify the static analysis container image')
                details = json.loads(inspected['stdout'])[0]
                result['container'] = details
                if details.get('Image') != environment['image_id']:
                    raise ValueError('Static container actual image mismatch')
                result['backend_provenance'] = signature
                if not worker.get('stdout', '').strip() and (
                    worker.get('cancelled') or worker.get('timed_out')
                ):
                    result['error'] = (
                        'Static analysis cancelled before worker output completed'
                        if worker.get('cancelled')
                        else 'Static analysis timed out before worker output completed'
                    )
                    result['cache'] = dict(
                        hit=False, key=_checksum(signature), rejected=cache_error
                    )
                    return result
                try:
                    receipt = json.loads(worker.get('stdout', ''))
                    if not isinstance(receipt, dict):
                        raise ValueError('Worker receipt must be a JSON object')
                except (ValueError, TypeError) as exc:
                    # Preserve the transport and parser evidence, but do not expose a
                    # parser exception as the explanation for an interrupted worker.
                    state = details.get('State') or {}
                    stopped = not _ok(worker) or (
                        state.get('OOMKilled') or state.get('Error') or state.get('ExitCode')
                    )
                    result['error_code'] = (
                        'static_worker_stopped' if stopped else 'static_worker_receipt_invalid'
                    )
                    result['error_details'] = dict(type=type(exc).__name__, message=str(exc))
                    raise ValueError(
                        '분석 작업이 완료되기 전에 중단되었습니다. 환경 상태를 확인한 뒤 다시 비교해 주세요.'
                        if stopped
                        else '분석 결과를 정상적으로 받지 못했습니다. 다시 비교하고 문제가 반복되면 진단 정보를 확인해 주세요.'
                    ) from exc
                result['worker_receipt'] = receipt
                if (
                    receipt.get('ok') is not True
                    or receipt.get('operation') != 'analyze'
                    or receipt.get('result_path') != '/output/result.json'
                    or receipt.get('sha256') != expected_hash
                ):
                    raise ValueError('Worker receipt identity, hash or result path mismatch')
                data = _read_json(worker_output / 'result.json')
                _validate_artifact(data, expected_hash)
                if receipt.get('backend_versions') != data['backend_versions']:
                    raise ValueError('Worker receipt backend versions mismatch')
                if (
                    _digest(snapshot) != expected_hash
                    or _digest(binary) != expected_hash
                    or _digest(mounted_snapshot) != expected_hash
                ):
                    raise ValueError('Input hash changed during static analysis')
                evidence = self._read_evidence(data, worker_output)
                if short_stage:
                    self._publish_evidence(data, evidence, output)
                status = data.get('status', 'success')
                partial = _incomplete(data) or receipt.get('partial')
                if worker.get('cancelled'):
                    status = 'cancelled'
                elif worker.get('timed_out'):
                    status = 'partial'
                elif not _ok(worker):
                    status = 'failed'
                elif status == 'success' and partial:
                    status = 'partial'
                state = details.get('State') or {}
                if (
                    state.get('Status') != 'exited'
                    or state.get('OOMKilled')
                    or state.get('Error')
                    or state.get('ExitCode') != 0
                ):
                    status = 'failed' if status not in ('cancelled', 'partial') else status
                data.update(
                    status=status, complete=status == 'success', backend_provenance=signature
                )
                result.update(self._decorate(data, output, signature))
                # The raw file remains the unmodified worker artifact; host verdict is explicit.
            finally:
                errors = await asyncio.shield(self._cleanup(name))
                result['cleanup_errors'] = errors
                if errors:
                    result.update(status='failed', complete=False)
            result['cache'] = dict(hit=False, key=_checksum(signature), rejected=cache_error)
        except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
            if result['status'] not in ('cancelled', 'partial'):
                result['status'] = 'failed'
            result.update(complete=False, error=str(exc))
            result['cache']['rejected'] = cache_error
        finally:
            if short_stage:
                try:
                    self._remove_short_stage(short_stage, staging_root)
                    result['short_staging']['removed'] = True
                except (OSError, ValueError) as exc:
                    result.setdefault('cleanup_errors', []).append(str(exc))
                    result.update(status='failed', complete=False)
        # Both the owned container and any short bind staging must be removed before promotion.
        if cache_path and not cached and result['status'] == 'success':
            try:
                artifact = {
                    key: value
                    for key, value in data.items()
                    if key
                    not in (
                        'backend_source',
                        'cache',
                        'process',
                        'environment',
                        'container',
                        'create_process',
                        'logs',
                        'cleanup_errors',
                    )
                }
                atomic_json(
                    cache_path,
                    dict(
                        signature=signature,
                        artifact=artifact,
                        artifact_sha256=_checksum(artifact),
                        files=evidence,
                    ),
                )
            except OSError as exc:
                result['cache']['write_error'] = str(exc)
        return result
