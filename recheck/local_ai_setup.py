"""The explicit local-AI setup action; settings commit belongs to the GUI."""

from __future__ import annotations

import asyncio
import inspect
import re

from .managed_runtime import ManagedRuntime
from .ollama import OllamaClient, OllamaError, _cloud, cancelled

DEFAULT_LOCAL_MODEL = 'qwen3:4b-instruct'


async def ensure_selected_local_ai(settings, *, cancel=None, on_event=None, runtime=None):
    """Resume only the user's selected managed endpoint, without model work.

    This is an explicit lifecycle action, separate from read-only environment
    probing. Ordinary local servers and GPT connections keep their own lifecycle.
    The reserved IPv4 loopback port is the managed service boundary; profile IDs
    may differ because setup creates a new candidate before proof is committed.
    """
    from ai_providers.profiles import resolve_profile

    profile = resolve_profile(settings)
    required = bool(
        profile.profile_id != 'legacy-local'
        and profile.enabled
        and profile.provider == 'ollama'
        and profile.auth_mode == 'none'
        and profile.endpoint.rstrip('/') == 'http://127.0.0.1:11435'
    )
    if cancelled(cancel):
        return {
            'required': required,
            'ready': False,
            'status': 'cancelled',
            'message': '로컬 AI 연결 준비를 중단했습니다.',
        }
    if not required:
        return {'required': False, 'status': 'skipped'}
    result = await (runtime or ManagedRuntime(settings)).start_ollama(
        cancel=cancel, on_event=on_event
    )
    if cancelled(cancel):
        return {
            'required': True,
            'ready': False,
            'status': 'cancelled',
            'message': '로컬 AI 연결 준비를 중단했습니다.',
        }
    return {**result, 'required': True}


def _check_cancel(cancel):
    if cancelled(cancel):
        raise OllamaError('cancelled', '로컬 AI 준비를 중단했습니다.')


async def _emit(callback, step, message):
    if callback:
        result = callback(
            {
                'kind': 'local_ai_setup',
                'step': step,
                'status': 'success' if step == 'ready' else 'running',
                'message': message,
            }
        )
        if inspect.isawaitable(result):
            await result


def _failure(status, message, code=None, **details):
    return {
        'ready': False,
        'status': status,
        'message': message,
        'code': code or status,
        'url': None,
        'model': None,
        'models': [],
        'capabilities': {},
        'inference_verified': False,
        **details,
    }


async def prepare_local_ai(
    settings,
    *,
    runtime=None,
    client_factory=None,
    cancel=None,
    on_event=None,
    model=DEFAULT_LOCAL_MODEL,
    verify_inference=True,
):
    """Start the private service, pull a missing model, verify local inference.

    Call only from an explicit local-AI choice. Importing this module and GPT
    setup perform no server start or model download. The worker returns verified
    selections for the caller to commit; it never edits settings or AI profiles.
    The GUI may set ``verify_inference=False`` to run its trusted profile test
    once after preparation. That result explicitly leaves inference unverified.
    """
    client = None
    try:
        _check_cancel(cancel)
        if (
            not isinstance(model, str)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/:-]{0,199}', model)
            or any(part in ('.', '..') for part in model.split('/'))
            or _cloud({'name': model})
        ):
            return _failure(
                'failed', '로컬 가중치가 있는 올바른 모델 이름을 선택해 주세요.', 'invalid_model'
            )
        await _emit(on_event, 'server', '관리 로컬 AI를 시작하고 있습니다.')
        service = await (runtime or ManagedRuntime(settings)).start_ollama(
            cancel=cancel, on_event=on_event
        )
        _check_cancel(cancel)
        if not service.get('ready') or not service.get('ollama_url'):
            return _failure(
                service.get('status', 'failed'),
                service.get('message') or '관리 로컬 AI를 시작하지 못했습니다.',
                runtime=service,
            )
        url = service['ollama_url']
        timeout = (
            settings.get('llm_timeout', 120)
            if isinstance(settings, dict)
            else getattr(settings, 'llm_timeout', 120)
        )
        client = (client_factory or OllamaClient)(url, timeout=timeout)
        await _emit(on_event, 'models', '설치된 로컬 모델을 확인하고 있습니다.')
        models = await client.models(cancel=cancel)
        _check_cancel(cancel)
        selected = next((item for item in models if item.get('name') == model), None)
        if selected is not None and (_cloud(selected) or selected.get('execution_kind') == 'cloud'):
            raise OllamaError('cloud_model_blocked', '로컬 모델이 아닌 항목은 사용할 수 없습니다.')
        if selected is None:
            await _emit(
                on_event,
                'download',
                '선택한 로컬 모델을 처음 내려받고 있습니다. 완료 후에는 보관된 모델을 사용합니다.',
            )
            result = await client.pull(model, cancel=cancel, on_event=on_event)
            _check_cancel(cancel)
            if result.get('status') != 'success':
                raise OllamaError('model_download', '모델 다운로드 완료를 확인하지 못했습니다.')
            models = await client.models(cancel=cancel)
            if not any(item.get('name') == model for item in models):
                raise OllamaError('model_missing', '내려받은 로컬 모델이 목록에 없습니다.')
        _check_cancel(cancel)
        await _emit(on_event, 'verify', '로컬 모델의 가중치와 실제 응답을 확인하고 있습니다.')
        metadata = await client.local_metadata(model, cancel=cancel)
        if _cloud({**metadata, 'name': model}) or (
            (metadata.get('details') or {}).get('format') != 'gguf'
            and not metadata.get('model_info')
        ):
            raise OllamaError('local_unverified', '로컬 모델 가중치를 확인하지 못했습니다.')
        _check_cancel(cancel)
        selected = next(item for item in models if item.get('name') == model)
        if not re.fullmatch(r'(?:sha256:)?[0-9a-f]{64}', str(selected.get('digest', ''))):
            raise OllamaError(
                'local_unverified', '선택한 모델의 가중치 식별자를 확인하지 못했습니다.'
            )
        capabilities = {}
        if verify_inference:
            capabilities = await client.capabilities(model, cancel=cancel)
            _check_cancel(cancel)
            if capabilities.get('basic') is not True:
                return _failure(
                    'failed',
                    '모델의 실제 응답을 확인하지 못했습니다. 메모리와 모델 상태를 확인해 주세요.',
                    'inference_failed',
                    capabilities=capabilities,
                )
            execution = capabilities.get('execution') or {}
            digest = str(selected['digest']).removeprefix('sha256:')
            if (
                execution.get('kind') != 'local'
                or str(execution.get('digest', '')).removeprefix('sha256:') != digest
            ):
                raise OllamaError(
                    'local_execution_unverified',
                    '실제 실행한 모델의 가중치 식별자가 일치하지 않습니다.',
                )
        message = (
            '로컬 AI가 준비되었습니다.'
            if verify_inference
            else '로컬 모델이 준비되었습니다. 실제 응답을 확인합니다.'
        )
        await _emit(on_event, 'ready', message)
        _check_cancel(cancel)
        return {
            'ready': True,
            'status': 'success',
            'message': message,
            'url': url,
            'model': model,
            'models': models,
            'capabilities': capabilities,
            'metadata': metadata,
            'model_digest': selected.get('digest'),
            'compute': 'cpu',
            'inference_verified': bool(verify_inference),
        }
    except OllamaError as exc:
        messages = {
            'cancelled': '로컬 AI 준비를 중단했습니다. 내려받은 모델 파일은 보관됩니다.',
            'model_memory': '모델 실행에 사용할 메모리가 부족합니다. 더 작은 모델을 선택해 주세요.',
            'connection_failed': '관리 로컬 AI에 연결할 수 없습니다. 환경 준비를 다시 실행해 주세요.',
            'response_timeout': '로컬 AI 응답 대기 시간이 지났습니다. 모델 상태를 확인해 주세요.',
            'cloud_model_blocked': '로컬 가중치가 있는 모델을 선택해 주세요.',
        }
        return _failure(
            'cancelled' if exc.code == 'cancelled' else 'failed',
            messages.get(
                exc.code, '로컬 AI 준비를 마치지 못했습니다. 연결과 모델 상태를 확인해 주세요.'
            ),
            exc.code,
        )
    except asyncio.CancelledError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return _failure(
            'failed',
            '로컬 AI 준비를 마치지 못했습니다. 환경과 모델 상태를 확인해 주세요.',
            'local_setup_failed',
            error_type=type(exc).__name__,
        )
    finally:
        if client is not None:
            await client.close()
