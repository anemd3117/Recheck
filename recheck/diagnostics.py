from .storage import new_id

RULES = [
    (
        'cancelled',
        ('cancelled', '취소'),
        '사용자가 작업을 취소했거나 상위 작업이 중단됨',
        ['취소 시점까지 수집한 결과를 확인하세요', '필요한 경우 새 시도로 양쪽을 다시 실행하세요'],
    ),
    (
        'output_limit',
        ('output_limit', '출력 크기 제한'),
        '출력 수집 한도 초과',
        [
            '저장된 잘림 표시와 원본 출력 크기를 확인하세요',
            '출력 제한 조정 후 양쪽을 같은 조건에서 재실행하세요',
        ],
    ),
    (
        'memory',
        ('out of memory', 'oom', 'memory allocation', '메모리'),
        '메모리 부족 후보',
        [
            '컨테이너 OOMKilled와 모델 로드 로그를 확인하세요',
            '모델/컨텍스트를 줄이거나 메모리 제한을 조정하고 양쪽을 재실행하세요',
        ],
    ),
    (
        'loader',
        (
            'no such file',
            'not found',
            'shared libr',
            'dll',
            'permission denied',
            'exec format',
            'input/output error',
            '대상 프로그램을 시작하지 못했습니다',
        ),
        '실행 파일·로더·라이브러리·권한 문제 후보',
        [
            '파일 형식과 이미지 OS/아키텍처를 확인하세요',
            '필요한 라이브러리와 보조 파일을 등록하세요',
        ],
    ),
    (
        'ollama',
        ('ollama', 'connection refused', 'connecterror', 'port_conflict'),
        'Ollama 연결 또는 서버 준비 실패',
        [
            '서버 주소·버전·포트 점유를 확인하세요',
            '설치된 로컬 서버를 시작한 뒤 연결을 다시 확인하세요',
        ],
    ),
    (
        'model',
        ('model', '모델'),
        '모델 누락·로드·지원 기능 문제 후보',
        [
            '정확한 설치 태그와 로컬 모델 여부를 확인하세요',
            '기본/스키마/도구 응답 테스트를 다시 실행하세요',
        ],
    ),
    (
        'docker',
        ('docker', 'container', 'image', 'wsl', 'virtual'),
        'Docker 엔진·이미지·실행 호환성 문제 후보',
        [
            '선택한 Docker context와 엔진 OS/아키텍처를 확인하세요',
            'Desktop·WSL·가상화·권한과 필요한 이미지 준비 결과를 확인하세요',
        ],
    ),
    (
        'ghidra',
        ('ghidra', 'java', 'extract', 'script'),
        'Ghidra·Java·추출 문제 후보',
        [
            'Ghidra application.properties의 Java 요구 버전을 확인하세요',
            'Headless와 스크립트 로그, JSON 완료 표시를 확인하세요',
        ],
    ),
    (
        'timeout',
        ('timeout', 'timed_out', '시간'),
        '작업 시간 제한',
        [
            '실제 프로세스 상태와 완료 표시를 확인하세요',
            '자원·컨텍스트·시간 제한 조정 후 재시도하세요',
        ],
    ),
    (
        'response',
        ('json', 'evidence', 'schema', 'stream', 'context', 'budget', 'repeat'),
        'LLM 응답·근거·에이전트 예산 오류',
        [
            '원문 응답과 검증 오류를 확인하세요',
            '검증된 근거로 제한된 재요청 또는 LLM만 재실행하세요',
        ],
    ),
    (
        'storage',
        ('sqlite', 'disk', 'save', 'permission', '저장'),
        '파일·DB 저장 실패 후보',
        [
            '사용자 데이터 폴더 쓰기 권한과 디스크 여유를 확인하세요',
            '최초 오류와 정리 오류를 모두 보존하세요',
        ],
    ),
]


def diagnose(stage, error, evidence_ids=None, target='both'):
    text = str(error)
    lower = text.lower()
    rule = next(
        (x for x in RULES if any(k in lower for k in x[1])),
        (
            'internal',
            (),
            'Recheck 내부 오류 또는 추가 근거 필요',
            ['이벤트 로그와 스택 정보를 확인하세요', '마스킹된 진단 ZIP을 생성하세요'],
        ),
    )
    return dict(
        diagnostic_id=new_id('diag'),
        stage=stage,
        target=target,
        summary=rule[2],
        error=text,
        code=rule[0],
        certainty='candidate',
        evidence_ids=evidence_ids or [],
        log_location='events.jsonl: event_id로 조회',
        additional_checks=rule[3],
        resolution=rule[3],
        auto_recoverable=rule[0] in ('ollama', 'docker', 'ghidra', 'timeout', 'response'),
        verification_status='needs_verification',
        producer='Recheck 규칙 기반',
    )
