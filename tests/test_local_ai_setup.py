"""Explicit local setup must finish download and real capability verification."""

import asyncio
import importlib
import threading

import pytest

from recheck.models import Settings
from recheck.ollama import OllamaError

MODEL = 'qwen3:4b-instruct'
URL = 'http://127.0.0.1:11435'


class Runtime:
    def __init__(self, result=None):
        self.result = (
            result
            if result is not None
            else {'ready': True, 'status': 'success', 'ollama_url': URL}
        )
        self.started = 0

    async def start_ollama(self, cancel=None, on_event=None):
        self.started += 1
        return self.result


class Client:
    def __init__(self, present=True):
        self.present = present
        self.closed = False
        self.pulls = []
        self.metadata = {
            'details': {'format': 'gguf'},
            'capabilities': ['completion'],
            'model_info': {'qwen3.context_length': 40960},
        }
        self.verified = {
            'basic': True,
            'tools': False,
            'json_schema': True,
            'execution': {
                'kind': 'local',
                'model': MODEL,
                'size': 2500000000,
                'digest': 'a' * 64,
                'size_vram': 0,
            },
        }
        self.failure = None
        self.cancel_after = None
        self.catalog_count = 0

    async def models(self, cancel=None):
        self.catalog_count += 1
        return (
            [{'name': MODEL, 'execution_kind': 'local_candidate', 'digest': 'a' * 64}]
            if self.present
            else []
        )

    async def pull(self, model, cancel=None, on_event=None):
        self.pulls.append(model)
        if self.failure:
            raise self.failure
        self.present = True
        if self.cancel_after:
            self.cancel_after.set()
        return {'status': 'success'}

    async def local_metadata(self, model, cancel=None):
        if self.failure:
            raise self.failure
        return self.metadata

    async def capabilities(self, model, cancel=None):
        return self.verified

    async def close(self):
        self.closed = True


def setup(settings, runtime, client, **options):
    assert importlib.util.find_spec(
        'recheck.local_ai_setup'
    ), 'Explicit local AI setup is not implemented'
    module = importlib.import_module('recheck.local_ai_setup')

    def factory(url, timeout):
        assert url == URL and timeout == settings.llm_timeout
        return client

    return asyncio.run(
        module.prepare_local_ai(settings, runtime=runtime, client_factory=factory, **options)
    )


def test_existing_local_model_is_verified_without_pull_or_settings_commit():
    settings = Settings()
    before = settings.model_dump()
    runtime, client = Runtime(), Client()
    result = setup(settings, runtime, client)
    assert result['ready'] and result['url'] == URL and result['model'] == MODEL
    assert result['capabilities']['basic'] is True and result['models'][0]['name'] == MODEL
    assert result['inference_verified'] is True
    assert not client.pulls and client.closed and runtime.started == 1
    assert settings.model_dump() == before


def test_verified_selection_reports_the_actual_catalog_digest():
    result = setup(Settings(), Runtime(), Client())
    assert result['ready'] and result['model_digest'] == 'a' * 64


def test_model_without_catalog_digest_never_reports_verified_ready():
    client = Client()

    async def catalog(cancel=None):
        return [{'name': MODEL, 'execution_kind': 'local_candidate'}]

    client.models = catalog
    result = setup(Settings(), Runtime(), client)
    assert not result['ready'] and result['code'] == 'local_unverified' and client.closed


def test_changed_execution_digest_cannot_be_returned_as_verified_selection():
    client = Client()
    client.verified['execution']['digest'] = 'b' * 64
    result = setup(Settings(), Runtime(), client)
    assert not result['ready'] and result['code'] == 'local_execution_unverified'
    assert client.closed


def test_gui_can_prepare_weights_then_run_its_own_single_inference_check():
    client = Client()

    async def should_not_run(*args, **kwargs):
        raise AssertionError('GUI performs the actual profile inference check once')

    client.capabilities = should_not_run
    settings = Settings()
    before = settings.model_dump()
    result = setup(settings, Runtime(), client, verify_inference=False)
    assert result['ready'] and result['model_digest'] == 'a' * 64
    assert result['inference_verified'] is False and result['capabilities'] == {}
    assert settings.model_dump() == before and client.closed


def test_explicit_local_choice_pulls_only_missing_selected_model():
    runtime, client = Runtime(), Client(present=False)
    result = setup(Settings(), runtime, client)
    assert result['ready'] and client.pulls == ['qwen3:4b-instruct'] and client.closed
    assert client.catalog_count == 2


def test_runtime_failure_does_not_construct_client_or_report_ready():
    result = setup(
        Settings(),
        Runtime(
            {
                'ready': False,
                'status': 'distribution_missing',
                'message': 'Missing environment pack',
            }
        ),
        Client(),
    )
    assert not result['ready'] and result['status'] == 'distribution_missing'
    assert not result.get('url')


def test_cancel_before_start_never_starts_runtime():
    event = threading.Event()
    event.set()
    runtime = Runtime()
    result = setup(Settings(), runtime, Client(), cancel=event)
    assert not result['ready'] and result['status'] == 'cancelled' and runtime.started == 0


def test_cancel_after_download_never_returns_ready_and_closes_client():
    event = threading.Event()
    client = Client(present=False)
    client.cancel_after = event
    result = setup(Settings(), Runtime(), client, cancel=event)
    assert not result['ready'] and result['status'] == 'cancelled' and client.closed


@pytest.mark.parametrize(
    'error',
    [
        OllamaError('cancelled', 'Cancelled'),
        OllamaError('connection_failed', 'Offline'),
        OllamaError('model_memory', 'Insufficient RAM'),
    ],
)
def test_download_failure_never_reports_ready_and_closes_client(error):
    client = Client(present=False)
    client.failure = error
    result = setup(Settings(), Runtime(), client)
    assert not result['ready'] and not result.get('url') and client.closed
    assert result['status'] == ('cancelled' if error.code == 'cancelled' else 'failed')
    assert result['code'] == error.code


@pytest.mark.parametrize(
    'metadata',
    [
        {'remote_host': 'https://ollama.com', 'details': {'format': 'gguf'}},
        {'details': {'format': 'unknown'}},
    ],
)
def test_unverified_or_cloud_metadata_cannot_activate_local_ai(metadata):
    client = Client()
    client.metadata = metadata
    result = setup(Settings(), Runtime(), client)
    assert not result['ready'] and client.closed


def test_basic_inference_failure_cannot_activate_local_ai():
    client = Client()
    client.verified = {'basic': False, 'errors': {'basic': {'code': 'model_memory'}}}
    result = setup(Settings(), Runtime(), client)
    assert not result['ready'] and client.closed and not result.get('url')


@pytest.mark.parametrize('model', ['remote:cloud', '../model', '', 'bad model'])
def test_invalid_or_cloud_model_rejected_before_start(model):
    runtime = Runtime()
    result = setup(Settings(), runtime, Client(), model=model)
    assert not result['ready'] and runtime.started == 0


def test_progress_reports_model_download_then_verification():
    events = []
    result = setup(Settings(), Runtime(), Client(present=False), on_event=events.append)
    assert result['ready']
    stages = [event.get('step') for event in events if event.get('kind') == 'local_ai_setup']
    assert stages == ['server', 'models', 'download', 'verify', 'ready']


def local_settings(**changes):
    from ai_providers.contracts import ProviderProfile

    profile = ProviderProfile(
        profile_id='saved-user-choice',
        provider='ollama',
        auth_mode='none',
        endpoint=URL,
        model=MODEL,
        enabled=True,
    )
    profile = profile.model_copy(update=changes)
    return Settings(ai_profiles=[profile], active_ai_profile=profile.profile_id)


def ensure(settings, runtime, **options):
    module = importlib.import_module('recheck.local_ai_setup')
    return asyncio.run(module.ensure_selected_local_ai(settings, runtime=runtime, **options))


def test_previously_selected_managed_loopback_ai_is_started_without_model_calls(monkeypatch):
    settings = local_settings()
    before = settings.model_dump()
    runtime = Runtime()
    module = importlib.import_module('recheck.local_ai_setup')

    def no_model_client(*args, **kwargs):
        raise AssertionError('Resume starts only owned server, never models or inference')

    monkeypatch.setattr(module, 'OllamaClient', no_model_client)
    result = ensure(settings, runtime)
    assert result['required'] is True and result['ready'] and runtime.started == 1
    assert settings.model_dump() == before


@pytest.mark.parametrize(
    'settings',
    [
        Settings(),
        Settings(ollama_url=URL, model=MODEL),
        local_settings(endpoint='http://127.0.0.1:11434'),
        local_settings(endpoint='http://localhost:11435'),
        local_settings(endpoint='http://127.0.0.1:11436'),
        local_settings(enabled=False),
        local_settings(
            provider='openai', auth_mode='api_key', endpoint='https://api.openai.com/v1'
        ),
    ],
)
def test_resume_skips_gpt_legacy_existing_and_custom_endpoints(settings):
    runtime = Runtime()
    result = ensure(settings, runtime)
    assert result['status'] == 'skipped' and result['required'] is False
    assert runtime.started == 0


def test_managed_resume_failure_is_actionable_without_changing_selection():
    settings = local_settings()
    before = settings.model_dump()
    runtime = Runtime({'ready': False, 'status': 'ownership_conflict', 'message': 'Unowned name'})
    result = ensure(settings, runtime)
    assert result['required'] is True and result['ready'] is False
    assert result['status'] == 'ownership_conflict' and result['message'] == 'Unowned name'
    assert settings.model_dump() == before


def test_cancelled_managed_resume_never_starts_runtime():
    cancel = threading.Event()
    cancel.set()
    runtime = Runtime()
    result = ensure(local_settings(), runtime, cancel=cancel)
    assert result['status'] == 'cancelled' and result['ready'] is False and not runtime.started


def test_cancellation_arriving_during_resume_never_reports_ready():
    cancel = threading.Event()
    runtime = Runtime()

    async def start(cancel=None, on_event=None):
        cancel.set()
        return {'ready': True, 'status': 'success', 'ollama_url': URL}

    runtime.start_ollama = start
    result = ensure(local_settings(), runtime, cancel=cancel)
    assert result['required'] and result['status'] == 'cancelled' and not result['ready']
