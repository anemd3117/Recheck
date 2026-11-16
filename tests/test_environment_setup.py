import asyncio
import threading

from recheck.models import Settings


class Runtime:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def prepare(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def run(runtime_result, mode='auto'):
    from recheck.environment_setup import prepare_analysis_environment

    runtime = Runtime(runtime_result)
    calls = []

    async def discover(*args, **kwargs):
        calls.append('discover')
        return {'status': 'partial', 'patch': {'java_home': 'fixture'}, 'warnings': []}

    result = asyncio.run(
        prepare_analysis_environment(
            Settings(static_backend=mode), runtime=runtime, discover=discover
        )
    )
    return result, runtime.calls, calls


def test_prepared_environment_needs_no_host_paths_or_ollama():
    result, calls, discovered = run({'ready': True, 'status': 'ready'})
    assert result['status'] == 'ready' and not result['patch']
    assert not discovered and calls[0]['include_ollama'] is False
    assert result['components']['analysis_environment']['status'] == 'ready'


def test_legacy_source_install_keeps_host_discovery_and_runtime_diagnosis():
    result, _, calls = run({'ready': False, 'status': 'missing', 'message': 'bundle missing'})
    assert calls == ['discover'] and result['patch']['java_home'] == 'fixture'
    assert result['managed_runtime']['message'] == 'bundle missing'
    assert 'bundle missing' in result['warnings']


def test_explicit_rea_and_cancel_never_fall_back_or_change_settings():
    for state in ({'status': 'cancelled'}, {'status': 'failed'}):
        result, _, calls = run(state, mode='rea')
        assert not calls and not result['patch']
    result, _, calls = run({'status': 'cancelled'})
    assert not calls and result['status'] == 'cancelled'


def test_direct_mode_does_not_import_or_prepare_containers():
    result, calls, discovered = run({'ready': True}, mode='direct')
    assert not calls and discovered == ['discover']


def test_pre_cancelled_setup_has_no_side_effects():
    from recheck.environment_setup import prepare_analysis_environment

    event = threading.Event()
    event.set()
    runtime = Runtime({'ready': True})
    result = asyncio.run(prepare_analysis_environment(Settings(), cancel=event, runtime=runtime))
    assert result['status'] == 'cancelled' and not runtime.calls
