import asyncio

from recheck.models import Settings


class Adapter:
    def __init__(self, ready):
        self.ready = ready
        self.analyses = 0

    async def probe(self):
        return {'ready': self.ready, 'status': 'ready' if self.ready else 'missing'}

    async def analyze(self, *args, **kwargs):
        self.analyses += 1
        return {'status': 'failed' if not self.ready else 'success', 'functions': []}


def router(mode='auto', *, managed_ready=True, direct_ready=True):
    from recheck.analysis_environment import StaticRouter

    settings = Settings.model_validate({'static_backend': mode})
    direct, managed = Adapter(direct_ready), Adapter(managed_ready)
    return StaticRouter(settings, direct=direct, managed=managed), direct, managed


def test_auto_uses_ready_managed_worker():
    selected, direct, managed = router()
    assert asyncio.run(selected.probe())['backend'] == 'rea'
    result = asyncio.run(selected.analyze('binary', 'output'))
    assert result['backend'] == 'rea'
    assert managed.analyses == 1 and direct.analyses == 0


def test_auto_preserves_direct_ghidra_without_prepared_environment():
    selected, direct, managed = router(managed_ready=False)
    assert asyncio.run(selected.probe())['backend'] == 'direct'
    assert asyncio.run(selected.analyze('binary', 'output'))['backend'] == 'direct'
    assert direct.analyses == 1 and managed.analyses == 0


def test_explicit_rea_never_falls_back_when_unavailable():
    selected, direct, managed = router('rea', managed_ready=False)
    assert not asyncio.run(selected.probe())['ready']
    assert asyncio.run(selected.analyze('binary', 'output'))['status'] == 'failed'
    assert managed.analyses == 1 and direct.analyses == 0


def test_selected_backend_does_not_change_after_analysis_failure():
    selected, direct, managed = router()
    asyncio.run(selected.probe())
    managed.ready = False
    result = asyncio.run(selected.analyze('binary', 'output'))
    assert result['backend'] == 'rea' and result['status'] == 'failed'
    assert direct.analyses == 0


def test_explicit_direct_does_not_probe_or_start_managed_services():
    selected, direct, managed = router('direct')

    async def forbidden():
        raise AssertionError('Direct mode must not probe managed worker')

    managed.probe = forbidden
    assert asyncio.run(selected.probe())['backend'] == 'direct'


def test_unknown_backend_rejected():
    import pytest

    with pytest.raises(ValueError):
        Settings.model_validate({'static_backend': 'some-provider-text'})


def test_engine_uses_packaged_execution_without_runtime_build_for_rea(tmp_path, monkeypatch):
    from recheck.engine import Engine

    engine = Engine(Settings(), tmp_path)
    calls = []

    class Packaged:
        async def prepare_image(self, *args, **kwargs):
            calls.append('packaged')
            return {'status': 'success', 'managed': True, 'image_id': 'sha256:fixture'}

    monkeypatch.setattr(engine, '_managed_docker', lambda: Packaged(), raising=False)
    monkeypatch.setattr(
        engine,
        '_docker',
        lambda: (_ for _ in ()).throw(
            AssertionError('REA execution must not use a runtime Dockerfile build')
        ),
    )
    monkeypatch.setattr(engine, '_evidence', lambda *args, **kwargs: 'fixture')
    run = {
        'files': {'original': {'os': 'linux'}},
        'environment': {'ghidra': {'backend': 'rea'}},
        'diagnostics': [],
    }
    assert asyncio.run(engine._image(run, None))['managed']
    assert calls == ['packaged']


def test_auto_dynamic_mode_uses_packaged_execution_independently_of_static_tools(tmp_path):
    from recheck.engine import Engine
    from recheck.managed_execution import ManagedExecutionAdapter

    engine = Engine(Settings(), tmp_path)
    run = {'mode': 'dynamic', 'static': {}, 'environment': {'ghidra': {'backend': 'direct'}}}
    assert isinstance(engine._execution_adapter(run), ManagedExecutionAdapter)
    assert not isinstance(
        Engine(Settings(static_backend='direct'), tmp_path)._docker(), ManagedExecutionAdapter
    )
