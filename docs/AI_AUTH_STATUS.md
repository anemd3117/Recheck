# Recheck 다중 AI 인증 구현과 검증 상태

Recheck 1.1.0-prototype.2, 2026-11-15 기준입니다. 이번 버전은 실제 ChatGPT 사용자 로그인과 GPT-6.1 Sol 추론을 확인했습니다. 이전 기록에 적힌 “원격 추론 0회”는 이전 버전의 상태입니다.

| 연결 | 실제 확인 범위 |
| --- | --- |
| ChatGPT | 공식 앱 자체 OAuth 로그인·추론 권한·모델 목록·기본 응답·JSON·도구 호출과 왕복·보호된 검사 결과 저장 확인. 실제 할인 예제의 비교·GPT 설명·추가 분석·보고서 확인. |
| Ollama | 이전 배포본에서 gemma4:e4b와 Linux 예제 3종 비교 실측. 이번 변경에서는 기존 경로 회귀 검사. |
| OpenAI API 키 | 구현·모의 HTTP 회귀 검사. 실제 사용자 API 키 추론은 미검증. |
| Claude API | 구현·모의 HTTP 회귀 검사. 실제 키 추론은 미검증. Claude.ai 구독 로그인 미지원. |
| Gemini | API 키 및 등록한 Desktop OAuth 경로 구현·회귀 검사. 실제 계정 추론 미검증. |
| OpenAI 호환 서버 | 구현·모의 HTTP 회귀 검사. 실제 개별 서버와 모델은 미검증. |

OAuth는 공식 동적 앱 등록·PKCE·loopback callback·JWT/JWKS와 nonce 검증을 사용합니다. 브라우저 승인은 사용자가 완료했고, 이후 앱 자체 계정 저장과 실제 추론을 확인했습니다. 다른 앱의 로그인 자료를 가져오지 않았습니다.

실제 서비스 응답으로 두 가지 호환 문제를 재현했습니다. ChatGPT 모델 목록의 `models/slug/visibility` 형식을 읽도록 수정했고, 완료된 Responses 응답의 `incomplete_details: null`을 정상 처리합니다. 숨김 모델·잘못된 목록·불완전 응답은 정상 성공으로 처리하지 않습니다.

설정에는 보안 자료 참조만 저장합니다. Windows 현재 사용자 DPAPI 보호와 프로필·origin·인증 버전 결합을 유지합니다. 도구 경로 또는 PC 자원 설정 변경은 무관한 GPT 검증 자료를 지우지 않습니다. AI 프로필·모델·인증 결합이 달라지면 재사용하지 않습니다.

모델 목록 확인과 응답 생성 검사는 다릅니다. 기능 확인은 별도 최대 4회 요청이며, 비교 업무는 해석과 추가 분석 전체에서 최대 요청 예산을 공유합니다. 자동 HTTP 재전송은 없습니다. 연결 성공이 모든 분석의 성공 또는 AI 설명의 사실성을 보장하지는 않습니다.

공식 자료: [앱 로그인 계약](https://developers.openai.com/siwc/token-sharing-open-source/sign-in), [ChatGPT 모델·추론 계약](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference), [GPT-6.1 Sol](https://developers.openai.com/api/docs/models/gpt-6.1-sol).

연결 순서는 [AI 연결 안내](AI_CONNECTIONS.md), 실제 검증 범위와 남은 조건은 같은 폴더의 검증범위.txt 또는 프로젝트의 UX_UPGRADE_20261115.md를 참고하세요.
