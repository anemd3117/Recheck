"""Recommended defaults and the user-facing sequence for connecting ChatGPT.

The UI starts one workflow. Existing profile, OAuth and capability services remain
responsible for credentials, request limits and committing verified selections.
"""

from __future__ import annotations

import uuid
from ai_providers.contracts import ModelDescriptor, ProviderError, ProviderProfile, emit

RECOMMENDED_MODEL = 'gpt-6.1-sol'
ANALYSIS_EVIDENCE = ('runtime', 'static', 'code')

SETUP_PROGRESS = {
    'login': '1/4 · 브라우저에서 계정과 사용 권한을 확인하고 있습니다.',
    'models': '2/4 · 이 계정에서 사용할 GPT 모델을 찾고 있습니다.',
    'response': '3/4 · 실제 GPT 응답과 사용할 수 있는 기능을 확인하고 있습니다.',
    'activate': '4/4 · GPT를 기본 분석 AI로 저장하고 있습니다.',
}


def activity_message(event):
    """Readable progress; technical identifiers remain in the detailed log."""
    if event.get('kind') == 'quick_setup':
        return SETUP_PROGRESS.get(event.get('step'), '')
    message = event.get('message')
    if isinstance(message, str) and message.strip():
        return message.splitlines()[0][:300]
    if event.get('kind') == 'provider_operation':
        action = {
            'models': 'AI 모델 목록 확인',
            'connection': 'AI 연결 확인',
            'capabilities': 'AI 기능 확인',
        }.get(event.get('operation'), 'AI 연결 작업')
        from .display import status_label

        return action + ' · ' + status_label(event.get('status'))
    if event.get('stage'):
        from .display import stage_label, status_label

        return (
            stage_label(str(event['stage'])) + ' · ' + status_label(event.get('status', 'running'))
        )
    return ''


def recommended_model(models, preferred=''):
    """Choose from the account's visible catalog, retaining an explicit choice."""
    visible = []
    for value in models:
        model = ModelDescriptor.model_validate(value)
        if model.metadata.get('visibility', 'list') == 'list' and model.id:
            visible.append(model.id)
    if preferred in visible:
        return preferred
    if RECOMMENDED_MODEL in visible:
        return RECOMMENDED_MODEL
    if not visible:
        raise ProviderError(
            'model_catalog_empty', '이 계정에서 사용할 수 있는 GPT 모델이 없습니다.'
        )
    return visible[0]


def next_action(*, ai_ready, tools_ready, files_ready, result_ready):
    if result_ready:
        return 'view_results'
    if not ai_ready:
        return 'connect_ai'
    if not tools_ready:
        return 'prepare_tools'
    return 'compare' if files_ready else 'choose_files'


def ai_error_message(code):
    """Only known messages reach the screen; raw network/token errors never do."""
    messages = {
        'oauth_timeout': '로그인 대기 시간이 지났습니다. 다시 연결한 뒤 새로 열린 브라우저에서 승인을 마쳐 주세요.',
        'oauth_browser': '브라우저를 열지 못했습니다. 기본 브라우저를 확인한 뒤 다시 연결해 주세요.',
        'access_denied': '계정 연결이 승인되지 않았습니다. 다시 로그인해 Recheck의 사용 권한을 확인해 주세요.',
        'oauth_network': 'OpenAI 로그인 서버에 연결하지 못했습니다. 인터넷 연결을 확인한 뒤 다시 시도해 주세요.',
        'oauth_http': 'OpenAI가 로그인 요청을 거부했습니다. 다시 연결하거나 잠시 후 시도해 주세요.',
        'invalid_grant': 'OpenAI가 인증 요청을 받아들이지 않았습니다. 새 로그인 화면에서 Recheck 연결을 다시 승인해 주세요.',
        'oauth_reauthentication': '계정에 다시 로그인해야 합니다. ChatGPT로 연결하기를 눌러 주세요.',
        'oauth_id_token': '계정 확인을 마치지 못했습니다. 다시 연결해 주세요. 반복되면 진단 기록을 확인해 주세요.',
        'malformed_response': '계정의 모델 목록 또는 응답을 읽지 못했습니다. 연결을 다시 확인해 주세요.',
        'model_catalog_empty': '이 계정에서 사용할 수 있는 GPT 모델이 없습니다. ChatGPT의 사용 권한과 이용 한도를 확인해 주세요.',
        'setup_inference': '로그인은 됐지만 GPT 응답을 확인하지 못했습니다. 이용 한도와 인터넷 연결을 확인한 뒤 다시 연결해 주세요.',
        'permission': 'ChatGPT 사용 권한이 확인되지 않았습니다. 다시 연결해 Recheck 사용 승인을 확인해 주세요.',
        'rate_limit': 'AI 이용 한도에 도달했습니다. ChatGPT 사용량을 확인하거나 잠시 후 다시 시도해 주세요.',
        'authentication': '저장한 계정 인증을 확인하지 못했습니다. 다시 로그인해 주세요.',
        'credential_required': '먼저 ChatGPT 계정을 연결하거나 다른 AI의 키를 저장해 주세요.',
        'profile_stale': '다른 화면에서 설정이 바뀌었습니다. 연결 목록을 새로 연 뒤 다시 시도해 주세요.',
        'general_settings_unsaved': '바꾼 도구 설정을 먼저 저장한 뒤 다시 연결해 주세요.',
        'cancelled': '연결을 중단했습니다. 준비되면 다시 시작할 수 있습니다.',
        'timeout': 'GPT 응답을 기다리는 시간이 지났습니다. 인터넷 연결을 확인한 뒤 다시 시도해 주세요.',
        'network': 'AI 서버에 연결하지 못했습니다. 인터넷 연결을 확인한 뒤 다시 시도해 주세요.',
    }
    return messages.get(
        code,
        '연결을 마치지 못했습니다. 다시 연결해 주세요. 반복되면 상세 설정에서 계정 상태를 확인해 주세요.',
    )


class ChatGPTSetup:
    def __init__(self, manager):
        self.manager = manager

    def prepare_profile(self, profile_id=None):
        """An explicit Connect click applies the complete recommended preset."""
        existing = None
        if profile_id:
            candidate = self.manager.get_profile(profile_id)
            if candidate.provider == 'openai' and candidate.auth_mode == 'chatgpt':
                existing = candidate
        if existing is None:
            candidates = [
                p
                for p in self.manager.list_profiles()
                if p.provider == 'openai' and p.auth_mode == 'chatgpt'
            ]
            if len(candidates) == 1:
                existing = candidates[0]
        data = (
            existing.model_dump()
            if existing
            else {
                'profile_id': 'chatgpt_' + uuid.uuid4().hex,
                'name': 'ChatGPT · 추천',
                'provider': 'openai',
                'auth_mode': 'chatgpt',
            }
        )
        options = dict(data.get('options', {}))
        if not existing:
            options.update(
                context_budget=32768, max_output_tokens=4096, max_requests=5, timeout_seconds=300
            )
        data.update(
            model=data.get('model') or RECOMMENDED_MODEL,
            enabled=True,
            external_consent=True,
            allowed_evidence=ANALYSIS_EVIDENCE,
            structured_mode='json_prompt',
            options=options,
        )
        return self.manager.save_profile(data)

    @staticmethod
    def _check_cancel(cancel):
        if cancel is not None and cancel.is_set():
            raise ProviderError('cancelled', '계정 연결을 중단했습니다.')

    async def connect(self, profile_id, *, cancel=None, on_event=None, on_url=None):
        self._check_cancel(cancel)
        profile = self.manager.get_profile(profile_id)
        if profile.provider != 'openai' or profile.auth_mode != 'chatgpt':
            raise ProviderError('oauth_unsupported', 'ChatGPT 로그인 연결을 선택해 주세요.')

        async def step(name):
            self._check_cancel(cancel)
            await emit(on_event, {'kind': 'quick_setup', 'step': name, 'status': 'running'})

        await step('login')
        account = self.manager.account_status(profile_id)
        if not (account.get('authenticated') and account.get('inference_authorized')):
            hooks = {'on_url': on_url} if on_url is not None else {}
            account = await self.manager.login(
                profile_id, cancel=cancel, on_event=on_event, **hooks
            )
        if not account.get('authenticated') or not account.get('inference_authorized'):
            raise ProviderError('permission', 'ChatGPT 추론 사용 승인을 확인해 주세요.')

        await step('models')
        models = await self.manager.list_models(profile_id, cancel=cancel, on_event=on_event)
        profile = self.manager.get_profile(profile_id)
        chosen = recommended_model(models, preferred=profile.model)
        profile = self.manager.save_profile({**profile.model_dump(), 'model': chosen})

        await step('response')
        cached = getattr(self.manager, 'cached_capabilities', lambda pid: None)(profile_id)
        reused = bool(
            cached
            and cached.get('profile_fingerprint') == profile.fingerprint()
            and cached.get('model') == chosen
            and cached.get('actual_inference_verified') is True
        )
        capabilities = (
            cached
            if reused
            else await self.manager.probe_capabilities(profile_id, cancel=cancel, on_event=on_event)
        )
        if not (
            capabilities.get('basic') is True
            and capabilities.get('actual_inference_verified') is True
            and capabilities.get('proof_persisted') is True
        ):
            raise ProviderError(
                'setup_inference', 'GPT의 실제 응답과 연결 저장을 확인하지 못했습니다.'
            )
        await step('activate')
        profile = self.manager.select_profile(profile_id, role='analysis')
        return {
            'ready': True,
            'profile': profile.model_dump(),
            'account': account,
            'capabilities': capabilities,
            'capabilities_reused': reused,
            'model': chosen,
        }
