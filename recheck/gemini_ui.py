"""Gemini-specific, bounded guidance without raw provider errors or key material."""


def gemini_error_message(code):
    messages = {
        'authentication': 'Gemini API 키가 승인되지 않았습니다. AI Studio에서 키의 프로젝트와 API 제한을 확인한 뒤 다시 입력해 주세요.',
        'credential_required': 'Gemini 개인 API 키를 입력하거나 이 연결에 저장한 키를 확인해 주세요.',
        'credential': 'Gemini API 키 형식을 확인하지 못했습니다. AI Studio에서 키를 다시 복사해 보안 입력란에 입력해 주세요.',
        'permission': 'Gemini 프로젝트의 API 사용 권한을 확인하지 못했습니다. AI Studio에서 프로젝트 인증·API 사용 설정·키 제한을 확인하세요. 유료 업그레이드를 자동 진행하지 않습니다.',
        'rate_limit': 'Gemini의 무료 한도에 도달했거나 프로젝트의 사용 가능 할당량이 없습니다. AI Studio에서 Free tier·모델 한도·프로젝트 인증 상태를 확인하고 잠시 후 다시 시도하세요.',
        'free_model_required': 'Gemini 무료 후보 모델을 선택해 주세요. 유료 모델로 자동 전환하지 않습니다.',
        'free_model_unavailable': '이 프로젝트에서 Gemini 무료 후보 모델을 확인하지 못했습니다. AI Studio의 무료 모델 제공 여부와 프로젝트 인증 상태를 확인하세요.',
        'model_catalog_empty': 'Gemini 모델 목록이 비어 있습니다. 프로젝트의 API 사용 설정과 무료 모델 제공 범위를 확인하세요.',
        'setup_inference': 'Gemini의 실제 응답과 보호된 검사 결과 저장을 확인하지 못했습니다. API 키·무료 한도·인터넷 연결을 확인한 뒤 다시 연결해 주세요.',
        'external_consent': 'Gemini로 보낼 비교 근거의 범위를 확인하고 전송에 동의해 주세요.',
        'gemini_profile_required': 'Gemini API 키 연결을 선택해 주세요. 다른 제공자의 인증 정보를 사용하지 않습니다.',
        'profile_stale': '다른 화면에서 Gemini 설정이 바뀌었습니다. 연결 목록을 새로 연 뒤 다시 시도해 주세요.',
        'general_settings_unsaved': '바꾼 도구 설정을 먼저 저장한 뒤 Gemini를 다시 연결해 주세요.',
        'secure_store_unavailable': 'Gemini 키를 Windows 사용자 DPAPI 보안 저장소에 저장하지 못했습니다. 현재 Windows 사용자와 저장소 권한을 확인하세요.',
        'cancelled': 'Gemini 연결을 중단했습니다. 기존 기본 AI는 유지하며 준비되면 다시 시작할 수 있습니다.',
        'timeout': 'Gemini 응답 대기 시간이 지났습니다. 인터넷 연결과 무료 한도를 확인한 뒤 다시 시도해 주세요.',
        'network': 'Gemini 서버에 연결하지 못했습니다. 인터넷 연결을 확인한 뒤 다시 시도해 주세요.',
        'malformed_response': 'Gemini 응답을 읽지 못했습니다. 현재 모델의 지원 기능을 다시 확인해 주세요.',
    }
    return messages.get(
        code,
        'Gemini 연결을 마치지 못했습니다. API 키·무료 프로젝트 설정과 한도를 확인하고 다시 시도해 주세요. 기존 기본 AI는 유지합니다.',
    )
