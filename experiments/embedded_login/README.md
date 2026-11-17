# QWebEngineView 내장 로그인 실험

상태: **폐기 가능한 브랜치 실험, 운영 미적용** · 2026-11-17

이 문서는 제품 통합 전의 시제품과 당시 관찰을 보존한다. 현재 0.1.6 통합 구현과 검증 상태는 [연결 사용법](../../docs/AI_CONNECTIONS.md), [ADR-0005](../../docs/decisions/0005-integrated-provider-connections.md), [진행 기록](../../docs/decisions/2026-11-17-embedded-login-work-log.md)을 따른다. 아래 시제품이 Recheck 제품의 진입점은 아니다.

`experiment/qwebengine-login`에서 로그인 화면까지 앱 안에서 진행할 수 있는지 확인한다. 현재 배포된 0.1.5의 시스템 브라우저 인증·설정·패키징은 바꾸지 않는다. 선택의 배경과 대안은 [ADR-0001](../../docs/decisions/0001-embedded-login-experiment.md), 팀원의 개발 절차는 [기여 안내](../../CONTRIBUTING.md)에 있다.

## 실행 방법

Windows x64, Python 3.14.8과 Qt 6.12.0을 사용해 실행했다. 저장소 루트의 PowerShell에서 실행한다. 일반 개발용 `.venv`와 분리하며 가상환경을 활성화할 필요는 없다.

```powershell
py -3.14 -m venv verification\embedded-login\.venv
.\verification\embedded-login\.venv\Scripts\python -m pip install -r experiments\embedded_login\requirements.txt
.\verification\embedded-login\.venv\Scripts\python -m experiments.embedded_login
```

Qt 6.12에서는 `PySide6-Addons`만 설치해도 WebEngine이 생기는 것이 아니다. 이 실험은 Qt Project의 별도 패키지 [PySide6-WebEngine 6.12.0.140](https://pypi.org/project/PySide6-WebEngine/6.12.0.140/)을 사용한다. 일반 앱의 고정 의존성과 배포 spec에는 추가하지 않았다.

1. **ChatGPT 내장 로그인 시도**를 누르면 기존 OAuth 서비스가 만든 공식 인증 URL을 내장 화면에서 연다.
2. 실제 계정 시험은 사용자가 직접 로그인·승인한다. 화면이 보이는 것만으로 인증 성공을 판정하지 않는다.
3. OAuth 서비스가 state·PKCE·nonce·토큰·서명 검증과 임시 보안 저장을 마쳐야 인증 완료를 표시한다. AI 응답은 이 실험에서 검사하지 않는다.
4. **시도 중단 · 화면 지우기**로 취소할 수 있다. 창을 닫으면 진행 중인 작업을 취소·정리한 뒤 종료한다.

별도의 **Google 내장 로그인 시도**에는 본인이 등록한 Google Desktop OAuth client ID가 필요하다. 입력 칸 또는 `RECHECK_EXPERIMENT_GOOGLE_CLIENT_ID` 환경 변수를 사용한다. 공개 식별자인 client ID는 API 키나 client secret과 다르다. 이번에는 Google Desktop 앱을 새로 등록하거나 기존 개인 설정에서 식별자를 가져오지 않았다.

## 수행한 일과 확인 결과

| 항목 | 2026-11-17 결과 | 확인 범위 |
| --- | --- | --- |
| 별도 브랜치·가상환경 | 완료 | WebEngine은 실험 환경에만 설치 |
| QWebEngineView 창 실행 | 완료 | Windows 실제 화면·접근성 트리 확인 |
| ChatGPT 공식 로그인 화면 | 표시 확인 | 이메일 입력·Google/Apple/Microsoft 등 선택지 확인, 개인정보 입력 없음 |
| ChatGPT 화면의 Google로 계속하기 | Google 로그인 화면 표시 확인 | OpenAI 연결용 Google 이메일 입력 화면까지, 인증 완료 아님 |
| 별도 Google Desktop OAuth 버튼 | client ID 미입력 안내 확인 | 유효한 client ID를 사용하는 인증은 미실행 |
| 취소·화면 지우기·창 종료 | 확인 | 대기 중 취소 후 버튼 복구·빈 화면·프로세스 종료 확인 |
| 기존 OAuth 자동 검사 | 32개 통과 | 모의 제공자와 실제 loopback 검사, 실계정 검사가 아님 |
| 패키지 의존성·소스 확인 | 통과 | 실험 환경 `pip check`, Python 구문 검사, Ruff 정확성 검사 |
| 사용자 로그인·승인·토큰 교환 | 미실행 | 실계정 인증 성공·실패를 판정할 근거 없음 |
| 실제 추론·다른 PC·frozen EXE | 미실행 | 지원이나 배포 검증으로 표시하지 않음 |

검증 명령:

```powershell
.\verification\embedded-login\.venv\Scripts\python -m pip check
.\verification\embedded-login\.venv\Scripts\python -m py_compile experiments\embedded_login\__main__.py
.\.venv\Scripts\python -m ruff check experiments\embedded_login
.\.venv\Scripts\python -m pytest -q tests\test_oauth.py
```

실제 관찰은 **로그인 화면을 렌더링할 수 있다**는 근거다. Google의 정책상 금지나 OpenAI의 시스템 브라우저 안내가 사라졌다는 근거는 아니다. 이 실행에서 `disallowed_useragent` 오류가 발생했다고 기록하지 않으며, 인증을 완료하지 않았으므로 차단되지 않는다고도 결론 내리지 않는다. 공식 근거와 운영 적용 판단은 ADR에서 구분한다.

이 실행의 종료 코드는 0이었다. Chromium 진단에는 Windows 비디오 인코더의 지원 형식 오류와 STUN 서버 DNS 조회 실패가 나타났다. 로그인 입력 화면은 표시됐지만, 이 진단이 후속 인증에 미치는 영향은 확인하지 않았다. 해당 로그를 숨기기 위해 렌더러·네트워크·인증 보안을 변경하지 않았다.

## 실험의 데이터 취급

브라우저는 이름 없는 off-the-record 프로필을 사용하며 시스템 브라우저의 쿠키를 가져오지 않는다. 저장된 제품 설정·계정을 읽지 않는다. 발급된 토큰이 있다면 PC 임시 디렉터리의 별도 DPAPI 저장소에만 기록하고 해당 시도가 끝날 때 삭제한다. 제품 프로필로 가져오거나 기본 AI로 선택하지 않는다. 로컬 자료 삭제가 제공자 계정의 연결 승인 철회를 뜻하지는 않는다.

전체 인증 URL·콜백 쿼리·토큰·이메일·웹페이지 본문을 로그에 수집하지 않는다. 사이트 표시에는 origin만 사용한다. 사이트에 JavaScript를 주입해 키·비밀번호를 읽거나 쿠키를 추출하지 않는다. 인증서 검증·브라우저 보안을 끄거나 사용자 에이전트를 위장하지 않는다. 다운로드는 중단한다.

## 앞으로 확인할 일

- 사용자의 직접 조작으로 계정별 로그인·승인·콜백 검증 결과를 확인한다. 성공·실패·미실행을 각각 기록한다.
- 인증 성공과 API 응답 성공을 분리해 기록한다. 추론 검사는 별도 범위로 다룬다.
- 제공자 정책과 실제 호환성, 세션 정리, 운영 패키징을 확인한 뒤 채택 여부를 다시 결정한다.
- Google API 키 발급 화면과 GPT·Gemini·Grok·DeepSeek·Claude의 공통 연결 안내는 [제공자별 설계 제안](../../docs/decisions/0004-provider-onboarding.md)에서 다룬다. 현재 실험에 해당 콘솔 UI를 구현한 것은 아니다.

## 검토했지만 이번에 하지 않은 일

| 대안·작업 | 하지 않은 이유 |
| --- | --- |
| WebEngine을 기본 앱·현재 Windows ZIP에 포함 | 실험 요청 범위를 지키고, 정책·실계정·배포 검증을 먼저 확인하기 위해 보류 |
| iframe 구현 | 사용자가 요청한 QWebEngineView의 독립 웹 화면부터 확인. 프레임 허용 정책은 별도 문제 |
| 네이티브 로그인 버튼·상태 UI 개선 완성 | 목표는 채택했으나 렌더링 선택을 확인하는 동안 구현 보류 |
| 비밀번호 입력·키 발급 버튼 자동 조작·결제 설정 | 사용자가 공식 서비스 화면에서 직접 수행할 계정 작업 |
| `.env` 도입·토큰 이전 | 기존 DPAPI 보호를 유지하며 개발 편의안만 검토 ([ADR-0002](../../docs/decisions/0002-development-secrets.md)) |
| `onefile` 전환 | 로그인 실험과 별도로 배포·복구 경로를 검증해야 함 ([ADR-0003](../../docs/decisions/0003-windows-bundle-mode.md)) |

이 문서는 [작업 기록 양식](../../docs/decisions/work-log-template.md)의 실제 예시다. 결정이 바뀌면 기존 관찰을 지우지 않고 새 결과·날짜·근거를 추가한다.
