"""Per-comparison choice uses bound saved proof and never changes billing defaults."""

import asyncio

import pytest

from ai_providers import profiles
from recheck.provider_onboarding import preset_profile
from test_gui import app, window, settle


@pytest.fixture
def connections(window, monkeypatch):
    manager = window.provider_manager
    calls = []

    class Remote:
        def __init__(self, profile):
            self.profile = profile

        async def close(self):
            pass

    async def probe(provider, **kwargs):
        calls.append(provider.profile.profile_id)
        p = provider.profile
        return dict(basic=True, tools=p.provider == 'gemini', tool_roundtrip=p.provider == 'gemini',
                    json_schema=True,
                    actual_inference_verified=True, model=p.model, provider=p.provider,
                    profile_fingerprint=p.fingerprint())

    manager.provider_factory = Remote
    monkeypatch.setattr(profiles, 'probe_provider_capabilities', probe)
    saved = []
    for preset in ('gemini', 'claude'):
        p = preset_profile(preset).model_copy(update=dict(
            model='fixture-model', enabled=True, external_consent=True,
            allowed_evidence=('runtime',)))
        p = manager.save_profile(p)
        p = manager.save_secret(p.profile_id, 'test-only-key-' + preset)
        asyncio.run(manager.probe_capabilities(p.profile_id))
        saved.append(p)
    manager.select_profile(saved[0].profile_id)
    window.settings = manager.settings.model_copy(deep=True)
    window.refresh_selected_provider()
    calls.clear()
    return saved, calls


def choose(window, pid):
    assert hasattr(window, 'comparison_ai'), 'Visible per-comparison AI selector is required'
    index = window.comparison_ai.findData(pid)
    assert index >= 0
    window.comparison_ai.setCurrentIndex(index)


def test_choice_is_read_only_and_survives_another_default_connection(window, connections):
    saved, calls = connections
    before = (window.provider_manager.root / 'settings.json').read_bytes()
    choose(window, saved[1].profile_id)
    assert window.provider_manager.selected_profile_id() == saved[0].profile_id
    assert (window.provider_manager.root / 'settings.json').read_bytes() == before
    window.refresh_selected_provider()
    assert window.comparison_ai.currentData() == saved[1].profile_id
    assert not window.mode.model().item(1).isEnabled()
    assert calls == []


def test_request_freezes_one_profile_without_mutating_saved_roles(window, app, connections, monkeypatch):
    saved, calls = connections
    choose(window, saved[1].profile_id)
    window.original.path.setText('original')
    window.reimplementation.path.setText('changed')
    window.analysis_mode.setCurrentIndex(window.analysis_mode.findData('static'))
    captured = []
    scheduled = []
    monkeypatch.setattr(window, 'persist_settings', lambda: True)
    monkeypatch.setattr(window, 'launch_task', lambda title, factory, done: scheduled.append(factory))

    async def ready(settings, *args):
        captured.append(('preflight', profiles.resolve_profile(settings).profile_id))
        return {'can_analyze': True}

    class Engine:
        async def analyze(self, *args, **kwargs):
            captured.append(('analyze', kwargs['ai_mode']))
            return {}

    def engine(settings, emit):
        type(settings).model_validate(settings.model_dump())
        assert settings.ai_role_profiles == window.settings.ai_role_profiles
        captured.append(('engine', profiles.resolve_profile(settings).profile_id))
        return Engine()

    monkeypatch.setattr(window, 'analysis_preflight', ready)
    monkeypatch.setattr(window, 'engine_for', engine)
    window.start_analysis()
    choose(window, saved[0].profile_id)
    import threading
    asyncio.run(scheduled[0](window.settings.model_copy(deep=True), threading.Event(), lambda e: None))
    assert captured == [('preflight', saved[1].profile_id), ('engine', saved[1].profile_id),
                        ('analyze', 'explain')]
    assert window.settings.active_ai_profile == saved[0].profile_id
    assert calls == []


def test_lost_proof_does_not_fall_back_to_another_connected_ai(window, connections, monkeypatch):
    saved, calls = connections
    choose(window, saved[1].profile_id)
    window.provider_manager.delete_secret(saved[1].profile_id)
    window.settings = window.provider_manager.settings.model_copy(deep=True)
    window.refresh_selected_provider()
    assert window.comparison_ai.currentData() is None
    assert not window._ai_ready
    window.original.path.setText('original')
    window.reimplementation.path.setText('changed')
    errors = []
    monkeypatch.setattr(window, 'show_error', errors.append)
    monkeypatch.setattr(window, 'persist_settings', lambda: pytest.fail('invalid choice must stop early'))
    window.start_analysis()
    assert errors and 'AI' in errors[0]
    assert window.provider_manager.selected_profile_id() == saved[0].profile_id
    assert calls == []


def test_ai_off_and_busy_disable_picker(window, connections, app, monkeypatch):
    import threading
    saved, _ = connections
    choose(window, saved[0].profile_id)
    window.ai_explanation.setChecked(False)
    assert not window.comparison_ai.isEnabled()
    window.ai_explanation.setChecked(True)
    gate = threading.Event()

    async def wait(settings, cancel, emit):
        await asyncio.to_thread(gate.wait, 2)

    try:
        window.launch_task('fixture', wait)
        assert not window.comparison_ai.isEnabled()
    finally:
        gate.set()
        settle(app, lambda: not window._task_join_pending)
    assert window.comparison_ai.isEnabled()


def test_no_ready_default_does_not_capture_a_future_choice(window, connections):
    saved, calls = connections
    window._comparison_profile_id = None
    window._comparison_choice_initialized = False
    window.settings = window.provider_manager.settings.model_copy(
        deep=True, update={'active_ai_profile': 'legacy-local'})
    window.refresh_selected_provider()
    assert window.comparison_ai.currentData() is None
    assert not window._ai_ready
    # A later successful onboarding may select its own saved default. Until the
    # user has chosen a comparison AI, the first ready default can be adopted.
    window.provider_manager.select_profile(saved[1].profile_id)
    window.settings = window.provider_manager.settings.model_copy(deep=True)
    window.refresh_selected_provider()
    assert window.comparison_ai.currentData() == saved[1].profile_id
    assert calls == []


def test_choosing_saved_local_ai_uses_its_own_tool_proof(window, connections):
    from ai_providers.contracts import ModelDescriptor
    saved, calls = connections
    manager = window.provider_manager
    manager.save_profile(manager.get_profile('legacy-local').model_copy(
        update={'model': 'local-fixture', 'enabled': True}))

    class Local:
        def __init__(self, profile):
            self.profile = profile

        async def list_models(self, **kwargs):
            return [ModelDescriptor(id=self.profile.model, digest='sha256:' + 'a' * 64)]

        async def close(self):
            pass

    manager.provider_factory = Local
    asyncio.run(manager.probe_capabilities('legacy-local'))
    window.settings = manager.settings.model_copy(deep=True)
    window.refresh_selected_provider()
    choose(window, saved[0].profile_id)
    assert window.mode.model().item(1).isEnabled()
    calls.clear()
    choose(window, 'legacy-local')
    assert window._ai_ready
    assert not window.mode.model().item(1).isEnabled()
    assert window.mode.currentData() == 'automatic'
    assert calls == []
