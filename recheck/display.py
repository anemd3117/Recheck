"""Korean presentation labels; stored identifiers and observations stay unchanged."""

STATUS_LABELS = {
    "pending": "시작 전",
    "waiting": "준비를 기다리는 중",
    "blocked": "진행할 수 없음",
    "running": "실행 중",
    "success": "성공",
    "succeeded": "성공",
    "completed": "완료",
    "partial": "부분 완료",
    "failed": "실패",
    "cancelled": "취소",
    "cached": "이전 분석 결과 다시 사용",
    "measured": "결과 확인됨",
    "unmeasured": "결과 확인 못함",
    "ready": "준비 완료",
    "exited": "프로그램 실행이 끝남",
    "unsupported": "지원하지 않음",
    "inconclusive": "결론을 내릴 수 없음",
    "timed_out": "시간 초과",
    "hypothesis": "아직 확인되지 않은 추정",
    "supported": "근거가 있는 추정",
    "needs_verification": "추가 확인 필요",
    "not_installed": "설치되어 있지 않음",
    "daemon_unavailable": "실행 도구에 연결할 수 없음",
    "stopped": "설치됨 · 꺼져 있음",
    "connected": "연결됨",
    "local": "이 PC에서 실행",
    "cloud": "외부 서버에서 실행",
    "budget_exhausted": "작업 단계 수 제한에 도달",
    "time_budget": "정해진 작업 시간이 지남",
    "context_budget": "한 번에 살펴볼 자료의 양을 넘음",
    "retry_budget": "다시 시도할 횟수를 모두 사용함",
    "repeated_request": "같은 작업이 반복되어 멈춤",
    "build_approval_required": "실행 파일 만들기 허가 필요",
    "generation_pipeline_not_implemented": "아직 만들지 않은 기능이라 진행할 수 없음",
    "input_or_storage_failure": "파일 확인 또는 저장 중 문제 발생",
}

STAGE_LABELS = {
    "metadata": "선택한 파일 확인",
    "environment": "실행 준비 상태 확인",
    "static_original": "원본 프로그램 구조 살펴보기",
    "static_reimplementation": "비교할 프로그램 구조 살펴보기",
    "static": "프로그램 구조 살펴보기",
    "structure": "두 프로그램의 구성 비교",
    "image": "분리된 실행 공간 준비",
    "dynamic": "같은 입력으로 실행해 비교",
    "llm": "AI가 비교 결과 설명",
    "agent": "AI의 추가 확인",
    "report": "결과 보고서 만들기",
    "ghidra": "프로그램 구조 확인 중",
    "docker": "분리된 공간에서 실행 중",
    "llm_stream": "AI 설명 받는 중",
    "failure": "문제가 생긴 이유 확인",
    "llm_failure": "AI 설명을 받지 못한 이유 확인",
    "llm_validation": "AI 설명과 실제 결과 대조",
    "agent_retry": "AI 추가 확인 다시 시도",
    "agent_recovery": "AI 추가 확인을 이어갈 준비",
    "recovery": "실행 준비 다시 확인",
    "interrupted_recovery": "도중에 멈춘 작업 확인",
    "recovery_docker_check": "실행 공간 연결 다시 확인",
    "recovery_docker_start": "프로그램 실행 도구 켜기",
    "recovery_service_error": "필요한 도구를 준비하지 못함",
    "stage": "진행 상태",
    "proposal": "AI가 제안한 다음 작업",
    "tool_result": "요청한 작업의 실제 결과",
    "agent_end": "AI 추가 확인 종료",
    "original_analysis": "원본의 구조와 실행 결과 모으기",
    "specification": "확인된 동작 정리",
    "generation": "새 소스 코드 초안 만들기",
    "build": "소스를 실행 파일로 만들기",
    "comparison": "만든 프로그램과 원본 비교",
    "repair": "소스 수정 후 다시 비교",
    "workflow": "프로그램 다시 만들기·고치기",
    "workflow_stage": "만들기·고치기 진행 상태",
    "reconstruction": "프로그램 다시 만들기·고치기",
    "modernization": "기존 소스 개선",
    "workflow_checkpoint": "작업 등록 내용 저장",
}

TARGET_LABELS = {"original": "원본", "reimplementation": "비교할 프로그램", "both": "두 프로그램"}


def status_label(value):
    return STATUS_LABELS.get(
        str(value), "미확인" if value in (None, "") else f"기타 상태 ({value})"
    )


def stage_label(value):
    return STAGE_LABELS.get(str(value), f"작업 ({value})")


def target_label(value):
    return TARGET_LABELS.get(str(value), f"대상 ({value})")
