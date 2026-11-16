"""Beginner setup: import the shipped tools, preserving direct-install compatibility."""

from .managed_runtime import ManagedRuntime
from .setup_discovery import discover_setup


async def prepare_analysis_environment(
    settings, *, cancel=None, on_event=None, runtime=None, discover=None
):
    if cancel is not None and cancel.is_set():
        return dict(status='cancelled', patch={}, components={}, warnings=[])
    discover = discover or discover_setup

    async def legacy():
        return await discover(
            settings,
            cancel=cancel,
            on_progress=lambda message: on_event
            and on_event(dict(kind='setup_discovery', message=message)),
        )

    if settings.static_backend == 'direct':
        return await legacy()
    runtime = runtime or ManagedRuntime(settings)
    prepared = await runtime.prepare(cancel=cancel, on_event=on_event, include_ollama=False)
    message = (
        prepared.get('message')
        or prepared.get('error')
        or ('배포 환경 파일을 확인한 뒤 분석 환경 자동 준비를 다시 눌러 주세요.')
    )
    component = dict(
        status='ready' if prepared.get('ready') else prepared.get('status', 'blocked'),
        proposed='Recheck 내장 분석 환경',
        reason=message,
    )
    result = dict(
        status='ready' if prepared.get('ready') else prepared.get('status', 'blocked'),
        patch={},
        components={'analysis_environment': component},
        managed_runtime=prepared,
        warnings=[],
    )
    if prepared.get('ready') or prepared.get('status') == 'cancelled':
        return result
    result['warnings'].append(message)
    if settings.static_backend == 'rea':
        return result
    found = await legacy()
    found['managed_runtime'] = prepared
    found.setdefault('warnings', []).append(message)
    found.setdefault('components', {})['analysis_environment'] = component
    return found
