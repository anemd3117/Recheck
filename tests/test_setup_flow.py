"""User outcomes for one-button ChatGPT setup and clear next-step guidance."""

import asyncio
import importlib
import importlib.util
import threading
import pytest
from ai_providers.contracts import ModelDescriptor, ProviderError, ProviderProfile


def flow_module():
    assert (
        importlib.util.find_spec('recheck.setup_flow') is not None
    ), 'One-button setup flow is missing'
    return importlib.import_module('recheck.setup_flow')


class SetupManager:
    def __init__(self, *, connected=False, models=None, basic=True):
        self.profiles = {}
        self.selected = 'legacy-local'
        self.connected = connected
        self.models = (
            models
            if models is not None
            else [ModelDescriptor(id='gpt-6-astra'), ModelDescriptor(id='gpt-6.1-sol')]
        )
        self.basic = basic
        self.calls = []

    def list_profiles(self):
        return list(self.profiles.values())

    def selected_profile_id(self, role='analysis'):
        return self.selected

    def get_profile(self, pid):
        return self.profiles[pid]

    def save_profile(self, value):
        value = ProviderProfile.model_validate(value)
        self.profiles[value.profile_id] = value
        return value

    def account_status(self, pid):
        return {'authenticated': self.connected, 'inference_authorized': self.connected}

    async def login(self, pid, **kwargs):
        self.calls.append('login')
        self.connected = True
        return self.account_status(pid)

    async def list_models(self, pid, **kwargs):
        self.calls.append('models')
        return self.models

    async def probe_capabilities(self, pid, **kwargs):
        self.calls.append('capabilities')
        return {
            'basic': self.basic,
            'actual_inference_verified': self.basic,
            'proof_persisted': self.basic,
            'tools': True,
            'tool_roundtrip': True,
            'model': self.get_profile(pid).model,
        }

    def select_profile(self, pid, role='analysis'):
        self.calls.append('select')
        self.selected = pid
        return self.get_profile(pid)


@pytest.mark.parametrize('connected', [False, True])
async def test_setup_forwards_dedicated_url_presenter_only_when_login_is_needed(connected):
    module = flow_module()
    manager = SetupManager(connected=connected)
    presented = []

    async def present(url):
        presented.append(url)

    original_login = manager.login

    async def login(pid, *, cancel=None, on_event=None, on_url=None):
        assert on_url is present
        await on_url('https://auth.openai.com/fixture')
        return await original_login(pid, cancel=cancel, on_event=on_event)

    manager.login = login
    setup = module.ChatGPTSetup(manager)
    profile = setup.prepare_profile()
    result = await setup.connect(profile.profile_id, on_url=present)
    assert result['ready'] is True
    assert presented == ([] if connected else ['https://auth.openai.com/fixture'])


async def test_setup_absent_presenter_preserves_original_manager_login_signature():
    module = flow_module()
    manager = SetupManager()
    original_login = manager.login

    async def login(pid, *, cancel=None, on_event=None):
        return await original_login(pid, cancel=cancel, on_event=on_event)

    manager.login = login
    setup = module.ChatGPTSetup(manager)
    result = await setup.connect(setup.prepare_profile().profile_id)
    assert result['ready'] is True


def test_recommended_model_is_visible_and_explicit_existing_choice_is_preserved():
    module = flow_module()
    models = [ModelDescriptor(id='gpt-6-astra'), ModelDescriptor(id='gpt-6.1-sol')]
    assert module.recommended_model(models) == 'gpt-6.1-sol'
    assert module.recommended_model(models, preferred='gpt-6-astra') == 'gpt-6-astra'
    assert module.recommended_model([ModelDescriptor(id='account-model')]) == 'account-model'
    with pytest.raises(ProviderError):
        module.recommended_model([])


def test_new_chatgpt_profile_has_complete_analysis_defaults_and_does_not_change_active_ai():
    module = flow_module()
    manager = SetupManager()
    profile = module.ChatGPTSetup(manager).prepare_profile()
    assert profile.provider == 'openai' and profile.auth_mode == 'chatgpt'
    assert profile.model == 'gpt-6.1-sol' and profile.enabled and profile.external_consent
    assert set(profile.allowed_evidence) == {'runtime', 'static', 'code'}
    assert profile.structured_mode == 'json_prompt' and not profile.credential_ref
    assert (
        profile.options.max_requests == 5
    ), 'A full explanation plus follow-up tools needs a usable request budget'
    assert manager.selected == 'legacy-local'


@pytest.mark.asyncio
@pytest.mark.parametrize('connected', [False, True])
async def test_one_button_selects_the_verified_model_without_a_second_user_action(connected):
    module = flow_module()
    manager = SetupManager(connected=connected)
    setup = module.ChatGPTSetup(manager)
    profile = setup.prepare_profile()
    result = await setup.connect(profile.profile_id)
    assert manager.calls == (['login'] if not connected else []) + [
        'models',
        'capabilities',
        'select',
    ]
    assert manager.selected == profile.profile_id
    assert manager.get_profile(profile.profile_id).model == 'gpt-6.1-sol'
    assert result['ready'] and result['capabilities']['actual_inference_verified']


@pytest.mark.asyncio
async def test_failed_model_response_keeps_the_previously_active_connection():
    module = flow_module()
    manager = SetupManager(basic=False)
    setup = module.ChatGPTSetup(manager)
    profile = setup.prepare_profile()
    with pytest.raises(ProviderError) as error:
        await setup.connect(profile.profile_id)
    assert error.value.code == 'setup_inference'
    assert manager.selected == 'legacy-local' and 'select' not in manager.calls


@pytest.mark.asyncio
async def test_cancelled_setup_never_starts_login_or_selects_a_connection():
    module = flow_module()
    manager = SetupManager()
    setup = module.ChatGPTSetup(manager)
    profile = setup.prepare_profile()
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ProviderError) as error:
        await setup.connect(profile.profile_id, cancel=cancel)
    assert (
        error.value.code == 'cancelled' and not manager.calls and manager.selected == 'legacy-local'
    )


@pytest.mark.asyncio
async def test_reconnecting_reuses_bound_verified_capabilities_without_four_more_paid_calls():
    module = flow_module()
    manager = SetupManager(connected=True)
    setup = module.ChatGPTSetup(manager)
    profile = setup.prepare_profile()
    manager.cached_capabilities = lambda pid: {
        'basic': True,
        'actual_inference_verified': True,
        'proof_persisted': True,
        'profile_fingerprint': manager.get_profile(pid).fingerprint(),
        'model': manager.get_profile(pid).model,
    }
    result = await setup.connect(profile.profile_id)
    assert manager.calls == ['models', 'select']
    assert result['capabilities_reused'] is True


def test_next_action_follows_the_persons_task_instead_of_exposing_technical_settings():
    module = flow_module()
    assert (
        module.next_action(ai_ready=False, tools_ready=False, files_ready=False, result_ready=False)
        == 'connect_ai'
    )
    assert (
        module.next_action(ai_ready=True, tools_ready=False, files_ready=False, result_ready=False)
        == 'prepare_tools'
    )
    assert (
        module.next_action(ai_ready=True, tools_ready=True, files_ready=False, result_ready=False)
        == 'choose_files'
    )
    assert (
        module.next_action(ai_ready=True, tools_ready=True, files_ready=True, result_ready=False)
        == 'compare'
    )
    assert (
        module.next_action(ai_ready=True, tools_ready=True, files_ready=True, result_ready=True)
        == 'view_results'
    )


def test_known_login_failures_offer_a_specific_recovery_without_echoing_raw_errors():
    module = flow_module()
    assert '다시' in module.ai_error_message('oauth_timeout')
    assert '모델 목록' in module.ai_error_message('malformed_response')
    assert 'private-token' not in module.ai_error_message('private-token')


def test_invalid_grant_reports_provider_rejection_without_assuming_expiry_or_storage():
    message = flow_module().ai_error_message('invalid_grant')
    assert 'OpenAI가 인증 요청을 받아들이지 않았습니다.' in message
    assert '새 로그인 화면' in message and 'Recheck 연결을 다시 승인' in message
    assert not any(claim in message for claim in ('만료', '저장', '삭제'))
