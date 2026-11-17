"""Official connection presets and verified API-key onboarding.

Opening a console, storing a key and completing inference are separate actions.
No browser cookies or web-page contents participate in API authentication.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass

from ai_providers.contracts import ModelDescriptor, ProviderError, ProviderProfile, emit
from .setup_flow import ANALYSIS_EVIDENCE


@dataclass(frozen=True)
class ConnectionPreset:
    id: str
    title: str
    provider: str
    auth_mode: str
    endpoint: str
    key_url: str


CONNECTION_PRESETS = (
    ConnectionPreset('chatgpt', 'ChatGPT 로그인', 'openai', 'chatgpt',
                     'https://api.openai.com/v1', ''),
    ConnectionPreset('gemini', 'Gemini · 무료 시작', 'gemini', 'api_key',
                     'https://generativelanguage.googleapis.com/v1beta',
                     'https://aistudio.google.com/apikey'),
    ConnectionPreset('openai', 'OpenAI API', 'openai', 'api_key',
                     'https://api.openai.com/v1', 'https://platform.openai.com/api-keys'),
    ConnectionPreset('grok', 'Grok · xAI API', 'compatible', 'api_key',
                     'https://api.x.ai/v1', 'https://console.x.ai/team/default/api-keys'),
    ConnectionPreset('deepseek', 'DeepSeek API', 'compatible', 'api_key',
                     'https://api.deepseek.com/v1', 'https://platform.deepseek.com/api_keys'),
    ConnectionPreset('claude', 'Claude API', 'anthropic', 'api_key',
                     'https://api.anthropic.com/v1', 'https://platform.claude.com/settings/keys'),
)


def connection_preset(preset_id):
    for preset in CONNECTION_PRESETS:
        if preset.id == preset_id:
            return preset
    raise ProviderError('provider_preset', '연결할 AI 제공자를 선택해 주세요.')


def matches_preset(profile, preset_id):
    preset = connection_preset(preset_id)
    return (profile.provider == preset.provider and profile.auth_mode == preset.auth_mode
            and profile.endpoint.rstrip('/') == preset.endpoint)


def preset_profile(preset_id):
    preset = connection_preset(preset_id)
    return ProviderProfile(
        name=preset.title, provider=preset.provider, auth_mode=preset.auth_mode,
        endpoint=preset.endpoint, structured_mode='json_prompt',
        options=dict(context_budget=16384, max_output_tokens=4096,
                     max_requests=5, timeout_seconds=300),
    )


def chatgpt_connection_state(manager, profile_id=None):
    """Read saved account/proof state; never prepare, select or mutate a profile.

    A saved proof describes a previously verified response, not a live request.
    Expired access tokens remain saved connections; the normal request path owns
    refresh. Multiple accounts require an explicit selection rather than a new
    profile or an arbitrary account choice.
    """
    result = {'status': 'disconnected', 'profile': None, 'account': {}, 'proof': None}
    try:
        candidates = [p for p in manager.list_profiles()
                      if p.provider == 'openai' and p.auth_mode == 'chatgpt']
        by_id = {p.profile_id: p for p in candidates}
        profile = by_id.get(profile_id) or by_id.get(manager.selected_profile_id())
        if profile is None:
            if len(candidates) > 1:
                return {**result, 'status': 'choose_profile'}
            profile = candidates[0] if candidates else None
        if profile is None:
            return result
        result['profile'] = profile
        if not profile.credential_ref:
            return result
        account = manager.account_status(profile.profile_id)
        result['account'] = account
        result['status'] = 'needs_attention'
        if not (account.get('authenticated') is True and account.get('inference_authorized') is True):
            return result
        proof = manager.cached_capabilities(profile.profile_id)
        result['proof'] = proof
        if not profile.enabled or not profile.external_consent or not profile.allowed_evidence:
            result['status'] = 'disabled'
        elif account.get('expired') is True:
            result['status'] = 'expired'
        elif (isinstance(proof, dict)
              and all(proof.get(key) is True for key in (
                  'basic', 'actual_inference_verified', 'proof_persisted'))
              and proof.get('profile_fingerprint') == profile.fingerprint()
              and proof.get('model') == profile.model
              and proof.get('provider') == profile.provider
              and proof.get('auth_mode') == profile.auth_mode):
            result['status'] = 'ready'
        else:
            result['status'] = 'account_saved'
        return result
    except Exception:
        # A missing/corrupt protected record is not a disconnected success. Never
        # expose filesystem, account or provider exception text in the card.
        return {**result, 'status': 'needs_attention'}


class APIKeySetup:
    """Connect a named official API preset without selecting an unverified key."""

    def __init__(self, manager):
        self.manager = manager

    def prepare_profile(self, preset_id, profile_id=None, *, consent=False):
        if consent is not True:
            raise ProviderError('external_consent', '분석 근거 전송에 동의해 주세요.')
        if preset_id in ('chatgpt', 'gemini'):
            raise ProviderError('provider_preset', '이 제공자의 전용 연결 안내를 사용해 주세요.')
        profile = self.manager.get_profile(profile_id) if profile_id else preset_profile(preset_id)
        if not matches_preset(profile, preset_id):
            raise ProviderError('provider_preset', '선택한 연결의 제공자와 API 주소를 확인해 주세요.')
        return self.manager.save_profile({
            **profile.model_dump(), 'enabled': True, 'external_consent': True,
            'allowed_evidence': ANALYSIS_EVIDENCE,
        })

    def _check_current(self, profile, role, previous_selection, cancel):
        if cancel is not None and cancel.is_set():
            raise ProviderError('cancelled', 'AI 연결을 중단했습니다.')
        check_disk = getattr(self.manager, '_check_disk', None)
        if check_disk is not None:
            try:
                check_disk()
            except (ValueError, OSError):
                raise ProviderError('profile_stale', '저장된 AI 설정이 변경됐습니다.') from None
        try:
            current = self.manager.get_profile(profile.profile_id)
        except ProviderError:
            raise ProviderError('profile_stale', '연결 중 AI 설정이 변경됐습니다.') from None
        if (current.fingerprint() != profile.fingerprint()
                or self.manager.selected_profile_id(role) != previous_selection):
            raise ProviderError('profile_stale', '설정이 변경돼 이전 연결 결과를 적용하지 않았습니다.')

    @staticmethod
    def _ready(proof, profile):
        return isinstance(proof, dict) and (
            all(proof.get(key) is True for key in (
                'basic', 'json_schema', 'actual_inference_verified', 'proof_persisted'))
            and proof.get('profile_fingerprint') == profile.fingerprint()
            and proof.get('model') == profile.model
            and proof.get('provider') == profile.provider
            and proof.get('auth_mode') == profile.auth_mode
        )

    async def connect(self, profile_id, *, role='analysis', cancel=None, on_event=None):
        profile = self.manager.get_profile(profile_id)
        if (profile.auth_mode != 'api_key' or not profile.enabled
                or not profile.external_consent):
            raise ProviderError('external_consent', 'API 연결과 분석 근거 전송을 허용해 주세요.')
        previous_selection = self.manager.selected_profile_id(role)
        self._check_current(profile, role, previous_selection, cancel)
        account = self.manager.account_status(profile_id)
        if (not account.get('credential_saved') or not profile.credential_ref
                or profile.credential_version <= 0 or profile.credential_origin != profile.origin):
            raise ProviderError('credential_required', '먼저 개인 API 키를 보안 저장해 주세요.')

        async def step(name):
            self._check_current(profile, role, previous_selection, cancel)
            await emit(on_event, {'kind': 'key_setup', 'step': name, 'status': 'running'})
            self._check_current(profile, role, previous_selection, cancel)

        await step('models')
        models = await self.manager.list_models(profile_id, cancel=cancel, on_event=on_event)
        self._check_current(profile, role, previous_selection, cancel)
        visible = [m.id for item in models if (m := ModelDescriptor.model_validate(item)).id
                   and m.metadata.get('visibility', 'list') == 'list']
        if not visible:
            raise ProviderError('model_catalog_empty', '사용 가능한 AI 모델을 찾지 못했습니다.')
        chosen = profile.model if profile.model in visible else visible[0]
        if chosen != profile.model:
            profile = self.manager.save_profile({**profile.model_dump(), 'model': chosen})
        await step('response')
        cached = getattr(self.manager, 'cached_capabilities', lambda pid: None)(profile_id)
        reused = self._ready(cached, profile)
        proof = cached if reused else await self.manager.probe_capabilities(
            profile_id, cancel=cancel, on_event=on_event,
        )
        self._check_current(profile, role, previous_selection, cancel)
        if not self._ready(proof, profile):
            raise ProviderError('setup_inference', '실제 응답·JSON 설명·검사 기록을 확인하지 못했습니다.')
        await step('activate')
        with getattr(self.manager, '_lock', nullcontext()):
            self._check_current(profile, role, previous_selection, cancel)
            selected = self.manager.select_profile(profile_id, role=role)
        return {'ready': True, 'role': role, 'profile': selected.model_dump(),
                'account': account, 'capabilities': proof, 'capabilities_reused': reused,
                'model': chosen}
