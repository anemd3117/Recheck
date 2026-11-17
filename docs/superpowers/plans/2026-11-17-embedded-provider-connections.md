# AI 연결 통합 구현 계획

이 문서는 구현 순서와 검토 조건을 보존한 계획이다. 실제 완료·실패·최종 검사와 배포 결과는 [작업 기록](../../decisions/2026-11-17-embedded-login-work-log.md)을 기준으로 확인한다. 후속 사용자 지시로 최종 검증 후 PR을 `main`에 병합하고 새 버전을 배포하며, 병합된 작업 브랜치만 정리한다.

> **구현 에이전트 안내:** `superpowers:subagent-driven-development` 또는 `superpowers:executing-plans`를 적용해 단계별로 진행한다. 확인란에는 실제 수행한 항목만 표시한다.

**목표:** Recheck 안의 AI 연결·공식 웹 화면·검증 상태를 통합하고 실제 확인 뒤 Windows 미리보기 ZIP을 배포한다.

**구조:** 기존 OAuth·프로필·DPAPI·기능 검사 기록을 재사용한다. 인증 URL 전용 UI 전달 경로와 수명이 분리된 WebEngine 창을 추가하고, 제공자 목록·키 설정·진행 상태를 기존 ProviderPanel에 연결한다.

**사용 기술:** Python 3.14.8, PySide6 6.12.0, PySide6-WebEngine 6.12.0.140, pytest, PyInstaller 6.22.3.

**설계:** [AI 연결 통합 설계](../specs/2026-11-17-embedded-provider-connections-design.md)

## 공통 제약

- 작업 브랜치에서 구현하고 검토한 정확한 SHA의 검사 뒤 `main`에 병합한다. 새 태그·ZIP을 검증해 게시하고 미병합 브랜치는 보존한다.
- 인증 URL·쿼리·계정 식별정보·원시 예외를 progress/log에 넣지 않는다.
- 토큰/키는 기존 사용자 DPAPI에 저장하고 실제 응답 proof 없이 기본 AI로 선택하지 않는다.
- Google 내장 OAuth를 제품 지원으로 제공하거나 인증 차단·TLS·sandbox를 우회하지 않는다.
- 외부 브라우저는 명시적인 사용자 선택만으로 실행한다.
- 기존 사용자 설정·API 키·OAuth 토큰·선택과 고정된 기존 릴리스는 보존한다.

## 중점 검토 항목

- 취소 뒤 대기열의 URL·진행 신호가 도착해 브라우저를 다시 여는 경우: 3단계의 작업 ID 검사.
- 유효하지 않은 콜백·서명이나 페이지 도착만으로 승인 성공을 표시하는 경우: 1~2단계.
- 종료 시 WebEngine 페이지·프로필 또는 QThread의 파괴 순서가 잘못되는 경우: 2~3단계 및 빌드한 GUI 확인.
- 키 교체·프로필 변경·다른 선택이 검사 중 발생하는 경우: 3단계의 설정 사본·CAS 검사.
- WebEngine DLL·locale·Chromium 고지·대응 소스가 누락되는 경우: 4단계의 실제 파일 목록과 빌드한 실행 파일 확인.

### 1단계: 인증 표시 담당자와 검증 진행 이벤트

**관련 파일:** `ai_providers/oauth.py`, `ai_providers/profiles.py`, `recheck/setup_flow.py`; 관련 `tests/test_oauth.py`, `test_provider_profiles.py`, `test_setup_flow.py`.

**인터페이스:** OAuthService.login의 기존 위치 인수는 보존하고 키워드 전용 인수 `on_event=None`을 추가한다. manager.login과 setup.connect에 선택 인수 `on_url=None`을 전달한다. 새 옵션이 None이면 기존에 주입된 서비스에 불필요한 키워드 인수를 보내지 않는다. 이벤트 상태는 `callback_validated`, `token_exchange`, `identity_verified`이며 manager의 성공 상태는 기존 저장 확정 뒤에만 전송한다.

- [ ] 실패 테스트: 전용 URL 담당자가 기본 브라우저를 대체하며, 취소/표시 실패 때 fallback이 없고 민감정보가 이벤트에 없음을 검사.
- [ ] 실패 테스트: 위조 state/서명은 검증 성공 이벤트를 보내지 않고 정상 콜백은 올바른 순서로 진행함을 검사.
- [ ] 최소 구현 후 OAuth·프로필·setup 관련 검사를 통과시킨다. 저장 뒤 관측자 실패가 새 참조를 잃게 하지 않는다.
- [ ] 변경 diff와 검증 결과를 독립 검토하고 기록한다.

### 2단계: 수명이 분리된 공식 사이트 창

**관련 파일:** 새 `recheck/provider_browser.py`, `tests/test_provider_browser.py`.

**인터페이스:** `ProviderBrowserDialog(parent=None, *, external_opener=None, browser_factory=None)`; `open_console(provider,url,title)->bool`, `open_authorization(url,*,provider,attempt_id)->bool`, `finish_attempt(attempt_id)`, `cancel_attempt(attempt_id=None)`, `shutdown()`; 신호 `cancelled(object)`, `notice(str)`, `external_opened(object)`.

- [ ] 실패 테스트: HTTPS/userinfo/port/scheme/정확한 loopback·중복 redirect 파라미터와 Google 정책 제한을 표로 검사한다.
- [ ] 실패 테스트: 명시적 버튼만 외부 실행, 원래 URL 사용, 페이지 로드만으로 인증 성공 없음, 수명·팝업·정리·민감정보 비노출을 검사한다.
- [ ] 새로운 off-the-record 프로필과 팝업 공유, certificate/download/permission 거부, lazy WebEngine 생성·주입 가능한 테스트 경계를 구현한다.
- [ ] 오프라인 단위 검사와 실제 창 기본 동작을 각각 확인하고 독립 검토한다.

### 3단계: 공통 제공자 화면과 검증된 연결

**관련 파일:** 새 `recheck/provider_onboarding.py`; `recheck/provider_ui.py`, `recheck/gui.py`; 새로 추가하거나 기존에 있던 UI·설정 흐름 검사.

**인터페이스:** `CONNECTION_PRESETS`는 ChatGPT, OpenAI API, Gemini, Grok, DeepSeek, Claude의 이름과 공식 `key_url`·`provider`·`auth`·`endpoint`를 제공한다. `APIKeySetup`은 프로필 준비와 실제 모델·기능 검사 기록 확인 뒤에만 선택한다. ProviderPanel은 비공개 URL 전달 신호와 고유 작업 ID로 2단계의 창을 제어한다.

- [ ] 실패 테스트: 제공자 선택이 올바른 endpoint·auth를 사용하며 기존 프로필을 덮어쓰지 않는다.
- [ ] 실패 테스트: 키 저장·페이지 표시만으로 선택하지 않고, 실제 proof 부족·취소·stale 선택에서 이전 기본 AI를 보존한다.
- [ ] 실패 테스트: worker URL은 UI thread에서만 창을 열고 뒤늦은 신호는 무시하며 모든 종료 경로가 브라우저를 정리한다.
- [ ] 눈에 띄는 연결 선택과 로그인 대기/승인 확인/응답 검사/연결 완료 상태를 통합한다. Gemini 무료 동의는 보존하고 native 마스킹 입력을 재사용한다.
- [ ] 메인 이벤트 전달과 QApplication 초기화·취소·닫기를 연결하고 관련 UI/회귀 검사를 통과시킨다.

### 4단계: WebEngine을 포함한 Windows 빌드

**관련 파일:** `pyproject.toml`, `requirements-lock.txt`, `Recheck.spec`, `scripts/collect_distribution_notices.py`, 해당 라이선스·소스 manifest와 검사, 버전 파일 및 배포 안내.

- [ ] 기존 source identity·binary allowlist·license 검사를 보존하면서 실제 WebEngine PE closure와 locale/pak를 허용하는 실패 검사를 작성한다.
- [ ] Qt WebEngine 6.140.0과 Qt 6.12.0 보조 모듈의 정확한 대응 소스·hash·Chromium 고지를 확보한다.
- [ ] 의존성·spec·inventory·고지·교체 안내를 함께 갱신하고 기존 onedir 빌드를 수행한다.
- [ ] frozen EXE에서 실제 공식 사이트 창·실패/취소·정상 종료를 확인하고 정상 다운로드 가능한 ZIP을 검증한다.

### 5단계: 실제 계정 검증과 조건부 배포

- [ ] 전체 pytest·Node·Ruff·pip check·문서 링크 검사와 독립 전체 변경 검토를 완료한다.
- [ ] 사용자가 완성된 화면에서 직접 로그인하여 승인·실제 응답·재실행 상태·로그아웃을 확인한다. 계정 입력 중 화면/DOM/로그를 수집하지 않는다.
- [ ] ADR·인증 상태·작업 기록·README·릴리스 노트를 실제 관측과 맞춘다.
- [ ] 해당 head SHA의 GitHub 단위·보안 검사와 release guard 통과 뒤 새 태그·미리보기 ZIP을 게시한다. 기존 자산은 변경하지 않는다.
- [ ] 공개 파일 크기·hash·다운로드와 브랜치·배포 상태를 읽어 확인한다.

### 6단계: 실제 점검에서 발견한 재승인 초기화 수정

- [x] 실패 테스트: 같은 연결 작업의 새 승인에서 임시 웹 프로필·쿠키는 유지하고 이전 페이지·팝업·callback 담당자는 정리한다. 다른 작업과 종료에서는 모두 파기한다.
- [x] 실제 합성 WebEngine 검사로 쿠키 유지·새 페이지·이전 신호 거부·새 정확한 callback·최종 파기를 확인한다. 계정 화면이나 인증 자료는 읽지 않는다.
- [x] 재승인 안내와 승인 확인 단계를 유지하고 콜백 수신을 최종 인증 저장과 구분한다.
- [ ] 독립 검토·새 소스 검사·새 frozen 빌드 뒤 실제 계정·응답·재실행을 다시 확인한다. 이전 실행본 결과를 새 버전으로 일반화하지 않는다.

### 7단계: 외부 기본값·화면 순서·콜백 안내 디자인

- [x] 로그인·키 발급은 외부 기본값, 모든 제공자의 내장 선택은 기본 체크 해제로 유지한다. 공통·기존 Gemini 발급 버튼에 같은 선택을 적용한다.
- [x] 추천 ChatGPT → Gemini 무료(API) → AI 계정 연결 순서로 배치하고 안내를 맞춘다.
- [x] 로컬 콜백 안내를 반응형 정적 HTML로 꾸미고 결과 수신과 실제 연결 완료를 구분한다. 외부 자료·스크립트·요청 정보 반영 없이 제한된 CSP를 유지하고 데스크톱·작은 화면을 확인한다.
- [x] [ADR-0006](../../decisions/0006-external-browser-default.md)에 Google 제한의 공식 근거·앱 자체 차단·관측과 추정·보류한 대안과 재검토 조건을 기록한다.
- [ ] 새 변경의 전체 소스·실행본 검사 뒤 실험 브랜치·PR을 갱신한다. 실제 인증·응답과 배포 판정은 별도로 기록한다.

### 8단계: 연결 상태 확인과 분석별 AI 선택

- [ ] 모든 연결 제공자의 주 버튼은 읽기 전용 **연결 상태 확인**으로 표시한다. 미연결 상태의 로그인·키 발급과 구분한다.
- [ ] 진행 중 작업을 취소하는 **연결 중단**과 연결된 상태의 **연결 해제**를 구분하고 기존 계정·다른 연결을 보존한다.
- [ ] 프로그램 비교에서 준비된 연결·모델 하나를 고른다. 저장된 기본 프로필·역할 설정은 바꾸지 않고 해당 실행의 설정 사본에 선택을 고정한다.
- [ ] AI 끔·ZIP 비교·준비되지 않은 연결·실행 중 설정 변경을 검사한다. 여러 AI에 자동 요청하거나 다른 계정으로 자동 대체하지 않는다.
- [ ] 정적 콜백은 외부 앱 사용 권한의 일반 조건만 설명하며 실제 요금제를 추정하지 않는다. 확인된 서버 권한 오류만 정확한 단계에 연결한다.
- [ ] [ADR-0007](../../decisions/0007-single-ai-per-analysis.md)에 단일 AI 선택과 병렬 비교 보류 이유·재검토 조건을 남기고 최종 전체 검사·새 실행본·정확한 SHA의 CI를 확인한다.
