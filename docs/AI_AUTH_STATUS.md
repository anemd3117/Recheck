# Recheck 다중 AI 인증 구현과 검증 상태

**현재 소스는 Recheck 0.1.6 개발 브랜치입니다.** 아래 기존 실제 계정 검증은 공개 0.1.3 단계의 2026-11-16 기록입니다. 버전 이름 대응은 [공개 이력](PUBLIC_HISTORY.md#공개-버전-이름-정리)에 남깁니다. 기존 계정 검증을 새 내장 인증의 성공으로 해석하지 않습니다.

## 0.1.6 연결 화면 통합

- 공통 제공자 선택·키 발급 화면·내장 ChatGPT 표시·명시적 외부 브라우저 선택·보안 키 입력·실제 응답 검사 흐름을 구현했습니다.
- 로그인·키 발급은 외부 브라우저가 기본입니다. **로그인·키 발급 화면을 앱 안에서 열기**는 기본 해제이며, 체크하면 모든 제공자의 내장 표시를 선택할 수 있습니다. 최초 버튼은 **ChatGPT 로그인**, 저장된 연결이 있으면 **연결 상태 확인**으로 표시하며 새 로그인을 시작하지 않습니다. 다른 제공자는 **제공자 이름 + API 키 발급**으로 안내합니다.
- ChatGPT 계정·모델에 결합된 실제 검사 기록으로 재실행 시 상태를 복원하고 완료 화면을 유지합니다. 응답 확인·재로그인·로그아웃·다음 단계는 별도 동작으로 제공합니다.
- 실제 응답 proof 없이 기본 AI를 바꾸지 않으며 새 키 검사 실패 시 기존 키·기본 AI를 유지합니다. 취소·뒤늦은 신호·프로필 변경·브라우저 종료를 회귀 검사합니다.
- Google 내장 인증은 정책 제한을 설명하고 사용자가 기본 브라우저를 선택하도록 합니다. 내장 ChatGPT의 확인 가능한 로그인 화면에서는 Google 선택지를 숨기며, 계정 선택·다른 로그인 수단·외부 브라우저는 바꾸지 않습니다. ChatGPT 공식 시스템 브라우저 안내와 내장 호환 표시를 구분합니다.
- 새 소스의 실제 계정 인증·추론 및 frozen EXE 검증은 진행 중입니다. 완료 전에는 새 경로의 실제 연결 성공이나 배포 완료를 주장하지 않습니다.

실제 화면 점검에서 로그인 반복 시 초기화되는 UI와 완료 후 자동 이동을 확인해 수정하고 회귀 검사를 마쳤습니다. 해당 점검의 앱 저장소에는 ChatGPT 인증·검사 proof가 없고, 화면에는 `invalid_grant` 안내가 남았습니다. 공식 절차에 맞춰 발급된 등록 ID를 유지한 새 인증을 한 번만 진행하는 복구도 추가했습니다. 웹 계정 로그인 성공과 Recheck의 인증 저장·추론 성공을 구분하며, 새 실행본의 점검에서는 민감정보 없는 단계·오류 코드만 확인합니다.

무료 계정의 새 내장 연결·실제 추론은 미검증입니다. 현재 [공식 도움말](https://help.openai.com/en/articles/20001542-using-your-chatgpt-plan-in-other-apps-and-sites)은 지원되는 오픈소스 도구를 모든 ChatGPT 사용자가 이용할 수 있다고 안내하지만, [개발자 빠른 시작](https://developers.openai.com/siwc/quickstart)은 적격 Plus·Pro 사용자를 명시합니다. 이 안내 차이나 `invalid_grant`만으로 무료 계정 불가를 단정하거나 앱에서 요금제를 임의로 차단하지 않습니다. 실제 서버의 권한·모델·응답 확인 결과로 판단합니다.

후속 사용자 보고에서는 현재 계정·워크스페이스·플랜으로 필수 권한을 사용할 수 없다는 공식 화면과, 이후 외부 웹 로그인 자체가 성공했다는 관측이 모두 있었다. 점검한 앱 저장소에는 새 인증·실제 응답 proof가 확인되지 않았으므로 이를 Recheck의 실제 추론 성공이나 모든 Free 계정 미지원으로 일반화하지 않는다. Google 내장 진행 실패도 앱의 선제 정책 차단과 Google 서버 오류 실측을 구분한다. 상세 원인·공식 근거·재검토 조건은 [외부 기본값 결정](https://github.com/leekmny/Recheck/blob/experiment/qwebengine-login/docs/decisions/0006-external-browser-default.md)에 기록했다.

## 이전 버전에서 확인한 실제 계정 범위

| 연결 | 실제 확인 범위 |
| --- | --- |
| ChatGPT | 공식 앱 자체 OAuth 로그인·추론 권한·모델 목록·기본 응답·JSON·도구 호출과 왕복·보호된 검사 결과 저장 확인. 실제 할인 예제의 비교·GPT 설명·추가 분석·보고서 확인. |
| Ollama | 이전 배포본에서 gemma4:e4b와 Linux 예제 3종 비교 실측. 이번 변경에서는 기존 경로 회귀 검사. |
| OpenAI API 키 | 구현·모의 HTTP 회귀 검사. 실제 사용자 API 키 추론은 미검증. |
| Claude API | 구현·모의 HTTP 회귀 검사. 실제 키 추론은 미검증. Claude.ai 구독 로그인 미지원. |
| Gemini 3.5 Flash-Lite API 키 | 사용자 제공 키로 모델 목록·기본 응답·JSON·도구 호출 왕복·보호된 검사 결과 저장 확인. ELF 고정3입력의 비교 결과 설명 확인: 2일치·1차이, 입력100에서 원본900/변경본1000 차이를 근거와 대조 |
| Gemini 4 Hydrogen API 키 | 작은 기본 응답과 JSON·도구 왕복 확인. 최초 긴 nonce 기능검사는 출력256토큰에서 길이 초과로 실패하여 증거 저장·기본 선택을 적용하지 않음. 별도 후속 검증 필요 |
| Gemini Google Desktop OAuth | 구현·회귀 검사. 실제 Google OAuth 계정 추론 미검증 |
| OpenAI 호환 서버 | 구현·모의 HTTP 회귀 검사. 실제 개별 서버와 모델은 미검증. |

OAuth는 공식 동적 앱 등록·PKCE·loopback callback·JWT/JWKS와 nonce 검증을 사용합니다. 브라우저 승인은 사용자가 완료했고, 이후 앱 자체 계정 저장과 실제 추론을 확인했습니다. 다른 앱의 로그인 자료를 가져오지 않았습니다.

Gemini 설명의 첫 응답은 Markdown 코드 울타리를 포함해 JSON 검증에서 거절됐습니다. 허용된 업무 요청 예산 안에서 수정 응답을 받아 앱의 설명 스키마·근거 참조·실측 지표 불변 검사를 통과했습니다. 최초 실패와 수정 후 성공을 구분하며 모든 프로그램·입력·모델의 정확도로 일반화하지 않습니다. 깨끗한 다른 PC 설치나 Windows PE 실행 검증도 아닙니다.

무료 등급은 사용자가 프로젝트 화면에서 확인했습니다. API200·모델 목록·응답 성공만으로 무료 결제 상태나 남은 할당량을 독립 증명한 것은 아닙니다. 앱은 유료 프로젝트·모델로 자동 전환하지 않습니다. [Gemini 무료 연결 안내](GEMINI_FREE_SETUP.md)를 따르세요.

Gemini 스트림의 시작 이벤트에 이미 포함된 첫 텍스트가 후속 조립에서 사라지던 결함도 수정했습니다. JSON 앞부분·초기 응답·거절 상태를 보존하며 불완전 응답을 성공으로 바꾸지 않습니다.

실제 서비스 응답으로 두 가지 호환 문제를 재현했습니다. ChatGPT 모델 목록의 `models/slug/visibility` 형식을 읽도록 수정했고, 완료된 Responses 응답의 `incomplete_details: null`을 정상 처리합니다. 숨김 모델·잘못된 목록·불완전 응답은 정상 성공으로 처리하지 않습니다.

설정에는 보안 자료 참조만 저장합니다. Windows 현재 사용자 DPAPI 보호와 프로필·origin·인증 버전 결합을 유지합니다. 도구 경로 또는 PC 자원 설정 변경은 무관한 GPT 검증 자료를 지우지 않습니다. AI 프로필·모델·인증 결합이 달라지면 재사용하지 않습니다.

모델 목록 확인과 응답 생성 검사는 다릅니다. 기능 확인은 별도 최대 4회 요청이며, 비교 업무는 해석과 추가 분석 전체에서 최대 요청 예산을 공유합니다. 자동 HTTP 재전송은 없습니다. 연결 성공이 모든 분석의 성공 또는 AI 설명의 사실성을 보장하지는 않습니다.

공식 자료: [앱 로그인 계약](https://developers.openai.com/siwc/token-sharing-open-source/sign-in), [ChatGPT 모델·추론 계약](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference), [GPT-6.1 Sol](https://developers.openai.com/api/docs/models/gpt-6.1-sol).

Gemini 공식 자료: [Interactions](https://aistudio.google.com/docs/interactions-overview), [구조화 출력](https://aistudio.google.com/docs/structured-output), [함수 호출](https://aistudio.google.com/docs/function-calling), [무료 모델 가격·데이터 조건](https://aistudio.google.com/docs/pricing), [프로젝트별 한도](https://aistudio.google.com/docs/rate-limits).

연결 순서는 [AI 연결 안내](AI_CONNECTIONS.md), 실제 검증 범위와 남은 조건은 같은 폴더의 검증범위.txt 또는 프로젝트의 UX_UPGRADE_20261115.md를 참고하세요.
