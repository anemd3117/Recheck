"""Explicit free-model Gemini setup, without changing existing AI selections.

The allowlist reflects Google's pricing on 2026-11-16. It identifies models
offering a free tier, not the billing tier or remaining quota of a user's key.
"""

from __future__ import annotations

from contextlib import nullcontext
import uuid

from ai_providers.contracts import (
    GenerationOptions, ModelDescriptor, ProviderError, ProviderProfile, emit,
)

FREE_MODEL_POLICY_DATE = '2026-11-16'
DEFAULT_FREE_MODEL = 'gemini-3.5-flash-lite'
FREE_MODELS = ('gemini-3.5-flash-lite', 'gemini-4-hydrogen')
ANALYSIS_EVIDENCE = ('runtime', 'static', 'code')
SETUP_PROGRESS = {
    'models': '1/3 · 저장한 키로 사용할 수 있는 Gemini 무료 모델을 확인하고 있습니다.',
    'response': '2/3 · 실제 Gemini 응답과 JSON 설명 기능을 확인하고 있습니다.',
    'activate': '3/3 · 검증한 Gemini 연결을 선택한 AI 역할로 저장하고 있습니다.',
}


def new_free_profile(*, profile_id=None):
    """Build an unconnected draft; callers must obtain consent and save a key."""
    return ProviderProfile(
        profile_id=profile_id or 'gemini_free_' + uuid.uuid4().hex,
        name='Gemini · 무료 기본', provider='gemini', auth_mode='api_key',
        model=DEFAULT_FREE_MODEL, structured_mode='json_prompt',
        options=GenerationOptions(
            context_budget=16384, max_output_tokens=4096, max_requests=5,
            timeout_seconds=300,
        ),
    )


def choose_free_model(models, preferred=''):
    """Intersect exact normalized model IDs with the reviewed free allowlist."""
    visible = set()
    try:
        for value in models:
            descriptor = ModelDescriptor.model_validate(value)
            identifier = descriptor.id.removeprefix('models/')
            if descriptor.metadata.get('visibility', 'list') == 'list':
                visible.add(identifier)
    except (TypeError, ValueError, AttributeError):
        raise ProviderError('malformed_response', 'Gemini 모델 목록을 읽지 못했습니다.') from None
    if preferred in FREE_MODELS and preferred in visible:
        return preferred
    for identifier in FREE_MODELS:
        if identifier in visible:
            return identifier
    raise ProviderError(
        'free_model_unavailable',
        '이 키에서 확인된 무료 모델이 없습니다. 유료 모델로 자동 변경하지 않습니다.',
    )


class GeminiSetup:
    def __init__(self, manager):
        self.manager = manager

    @staticmethod
    def _gemini_profile(profile):
        if profile.provider != 'gemini' or profile.auth_mode != 'api_key':
            raise ProviderError(
                'gemini_profile_required', 'Gemini API 키 연결을 선택해 주세요.',
            )
        if profile.model and profile.model not in FREE_MODELS:
            raise ProviderError(
                'free_model_required', '무료 기본 연결에는 지원하는 무료 모델을 선택해 주세요.',
            )

    def prepare_profile(self, profile_id=None, *, consent=False):
        """Only an explicit user action may enable this preset and its evidence."""
        if consent is not True:
            raise ProviderError(
                'external_consent', 'Gemini에 선택한 분석 근거를 전송하는 데 동의해 주세요.',
            )
        profile = self.manager.get_profile(profile_id) if profile_id else new_free_profile()
        self._gemini_profile(profile)
        data = profile.model_dump()
        data.update(
            model=profile.model or DEFAULT_FREE_MODEL, enabled=True, external_consent=True,
            allowed_evidence=ANALYSIS_EVIDENCE, structured_mode='json_prompt',
        )
        return self.manager.save_profile(data)

    @staticmethod
    def _check_cancel(cancel):
        if cancel is not None and cancel.is_set():
            raise ProviderError('cancelled', 'Gemini 연결을 중단했습니다.')

    def _check_current(self, profile, role, previous_selection, cancel):
        self._check_cancel(cancel)
        # ProfileManager owns disk CAS and an RLock. Holding that same lock also
        # closes the check/select gap if another local UI task changes settings.
        check_disk = getattr(self.manager, '_check_disk', None)
        if check_disk is not None:
            try:
                check_disk()
            except (ValueError, OSError):
                raise ProviderError('profile_stale', '저장된 AI 설정이 변경됐습니다.') from None
        try:
            current = self.manager.get_profile(profile.profile_id)
        except ProviderError:
            raise ProviderError('profile_stale', '연결 중 AI 프로필이 변경됐습니다.') from None
        if (
            current.fingerprint() != profile.fingerprint()
            or self.manager.selected_profile_id(role) != previous_selection
        ):
            raise ProviderError(
                'profile_stale', '다른 작업에서 AI 설정이 변경돼 이전 연결을 적용하지 않았습니다.',
            )

    @staticmethod
    def _ready(proof, profile):
        return isinstance(proof, dict) and (
            all(proof.get(key) is True for key in (
                'basic', 'json_schema', 'actual_inference_verified', 'proof_persisted',
            ))
            and proof.get('profile_fingerprint') == profile.fingerprint()
            and proof.get('model') == profile.model
            and proof.get('provider') == 'gemini'
            and proof.get('auth_mode') == 'api_key'
        )

    async def connect(self, profile_id, *, role='analysis', cancel=None, on_event=None,
                      force_probe=False):
        self._check_cancel(cancel)
        profile = self.manager.get_profile(profile_id)
        self._gemini_profile(profile)
        if not profile.enabled or not profile.external_consent:
            raise ProviderError('external_consent', 'Gemini 연결과 분석 근거 전송을 허용해 주세요.')
        previous_selection = self.manager.selected_profile_id(role)
        account = self.manager.account_status(profile_id)
        if (
            not account.get('credential_saved') or not profile.credential_ref
            or profile.credential_version <= 0 or profile.credential_origin != profile.origin
        ):
            raise ProviderError('credential_required', '먼저 Gemini API 키를 보안 입력 칸에 저장해 주세요.')

        async def step(name):
            self._check_current(profile, role, previous_selection, cancel)
            await emit(on_event, {
                'kind': 'gemini_setup', 'step': name, 'status': 'running',
                'message': SETUP_PROGRESS[name],
            })
            self._check_current(profile, role, previous_selection, cancel)

        await step('models')
        models = await self.manager.list_models(profile_id, cancel=cancel, on_event=on_event)
        self._check_current(profile, role, previous_selection, cancel)
        chosen = choose_free_model(models, preferred=profile.model)
        if chosen != profile.model:
            profile = self.manager.save_profile({**profile.model_dump(), 'model': chosen})

        await step('response')
        cached = getattr(self.manager, 'cached_capabilities', lambda pid: None)(profile_id)
        reused = not force_probe and bool(self._ready(cached, profile))
        capabilities = cached if reused else await self.manager.probe_capabilities(
            profile_id, cancel=cancel, on_event=on_event,
        )
        self._check_current(profile, role, previous_selection, cancel)
        if not self._ready(capabilities, profile):
            raise ProviderError(
                'setup_inference',
                'Gemini의 실제 응답·JSON 설명·검사 결과 저장을 모두 확인하지 못했습니다.',
            )
        await step('activate')
        with getattr(self.manager, '_lock', nullcontext()):
            self._check_current(profile, role, previous_selection, cancel)
            selected = self.manager.select_profile(profile_id, role=role)
        return {
            'ready': True, 'role': role, 'profile': selected.model_dump(),
            'account': account, 'capabilities': capabilities, 'capabilities_reused': reused,
            'model': chosen, 'free_model_policy_date': FREE_MODEL_POLICY_DATE,
            'free_tier_independently_verified': False,
        }
