# ADR-0004 · 여러 AI 제공자의 연결 준비를 공통 앱 화면으로 안내한다

- 상태: 초기 설계 제안 · [ADR-0005](0005-integrated-provider-connections.md)로 구체화
- 날짜: 2026-11-17
- 적용 범위: GPT·Gemini·Grok·DeepSeek·Claude 연결 준비 화면의 후속 설계
- 관련 결정: [로그인 렌더링 실험](0001-embedded-login-experiment.md), [인증 자료 저장](0002-development-secrets.md)

## 배경과 문제

사용자는 Google API 키 발급 페이지도 앱 안에서 열고, 같은 방식을 GPT·Gemini·Grok·DeepSeek·Claude로 확장하기를 요청했다. 제공자마다 콘솔 주소·로그인 방식·키 발급·API 형식이 다르므로 하나의 로그인 성공을 전체 제공자의 지원으로 일반화할 수 없다.

**웹 콘솔에 로그인해 API 키를 직접 발급·입력하는 흐름**과 **ChatGPT OAuth로 앱의 추론 권한을 받는 흐름**은 구분한다. 콘솔 로그인이 Recheck 인증을 자동으로 완료하거나 ChatGPT 구독 로그인이 OpenAI API 키 발급을 대신하는 것으로 표시하지 않는다.

## 판단 기준

- 앱 안에서 연결 준비를 이어가는 사용 경험을 우선한다.
- 제공자별 브라우저 정책·기술 호환성·API 검증 상태를 각각 표시한다.
- 키 발급과 결제는 사용자가 공식 콘솔에서 직접 수행한다.
- 기존 DPAPI와 인증 대상·프로필 결합을 유지한다.
- 기존 어댑터와 향후 지원 후보를 명확히 구분한다.

## 제안하는 결정

공통 네이티브 화면에서 `제공자 선택 → 공식 페이지 열기 → 사용자의 로그인·키 발급 → 키 직접 입력·저장 → 모델·응답 확인` 단계를 안내한다. 단계와 현재 상태를 보이되, 로그인 완료를 페이지 표시만으로 판정하지 않는다. ChatGPT OAuth를 선택한 경우에는 별도 callback·토큰 검증 단계를 사용한다.

제공자별 데이터에 **공식 URL·인증 방식·브라우저 정책과 확인 날짜·내장 표시 시험 상태·연결할 API 어댑터·검증 범위**를 둔다. 내장 웹 화면은 제공자 정책이 허용하는 경우에만 시험한다. 정책 제한이나 기술 실패를 화면에 설명하고 사용자가 재시도·중단·외부 브라우저 대안을 선택하도록 한다. 실패 시 외부 브라우저를 조용히 자동 실행하지 않는다.

이번 기록은 설계 제안이다. 다중 제공자 제품 UI·이름별 프리셋·자동 연결·새 배포 의존성을 구현하거나 운영 지원을 승인한 것은 아니다.

## 제공자별 출발점

아래는 2026-11-17에 공식 문서와 현행 소스를 대조한 목록이다. 키 페이지 링크가 존재한다는 사실은 내장 브라우저 지원이나 실제 API 호출 성공의 증거가 아니다.

| 제공자 | 공식 키 페이지·인증 흐름 | 현행 API 어댑터 | 내장 브라우저·실행 검증 상태 |
| --- | --- | --- | --- |
| GPT / OpenAI | [OpenAI API 키](https://platform.openai.com/api-keys): 콘솔 로그인 후 수동 키. 별도 선택으로 ChatGPT OAuth | `openai`의 `api_key` / `chatgpt` | 키 콘솔 내장 흐름 미검증. ChatGPT 로그인 초입 표시만 이번 실험에서 확인. API 키 실제 추론은 미검증 |
| Gemini | [Google AI Studio 키](https://aistudio.google.com/apikey): Google 로그인 후 수동 키 | `gemini`의 `api_key`; `google_oauth`는 별도 인증 방식 | 키 콘솔 내장 흐름 미검증. Google 로그인 초입 표시와 OAuth 정책 제한을 별개로 취급. 기존 API 키 응답 확인 범위는 인증 상태 문서 참고 |
| Grok / xAI | [xAI API Keys](https://console.x.ai/team/default/api-keys): 콘솔 로그인 후 수동 키 | `compatible` 경로의 검토 후보. 전용 제공자 항목 없음 | 내장 콘솔·키 발급·Recheck API 호출 모두 미검증 |
| DeepSeek | [DeepSeek API keys](https://platform.deepseek.com/api_keys): 콘솔 로그인 후 수동 키 | `compatible` 경로의 검토 후보. 전용 제공자 항목 없음 | 내장 콘솔·키 발급·Recheck API 호출 모두 미검증 |
| Claude | [Claude API keys](https://platform.claude.com/settings/keys): 콘솔 로그인 후 수동 키 | `anthropic`의 `api_key` | 내장 콘솔·키 발급 미검증. API 어댑터·모의 회귀 구현과 실제 사용자 키 추론은 구분하며 후자는 미검증 |

Gemini의 현행 공식 키 문서는 `/apikey`를 연결한다. 요청된 [AI Studio `/api-keys` 경로](https://aistudio.google.com/api-keys)도 조사 도구에서는 Google 로그인으로 이동했으나 로그인 후 도착지는 확인하지 않았다. 구현 전에 실제 진입점과 리디렉션을 다시 확인한다.

Grok·DeepSeek 공식 API 문서는 OpenAI 호환 형식을 안내한다. Recheck의 `compatible` 구현이 두 서비스의 모델 목록·스트리밍·구조화 출력·도구 호출까지 검증됐다는 뜻은 아니다. 현재 제공자 종류와 인증 조합은 [계약](../../ai_providers/contracts.py), 생성 경로는 [factory](../../ai_providers/factory.py), API 분기는 [HTTP 어댑터](../../ai_providers/http_provider.py)에서 확인한다.

## 검토한 대안

| 대안 | 판단과 이유 |
| --- | --- |
| 공통 화면과 제공자별 연결 정보 | 제안한다. 단계는 통일하고 정책·인증·API 차이를 데이터로 드러낼 수 있다. |
| 모든 제공자를 같은 내장 로그인으로 처리 | 채택하지 않는다. 브라우저 정책과 인증 계약이 다르고 검증도 없다. |
| 모든 연결을 외부 브라우저로만 제공 | 이번 요청의 기본 설계로 채택하지 않는다. 내장 경험을 우선 검토하되 제한 시 사용자가 선택할 대안으로 둔다. |
| 콘솔 DOM에서 키 수집·키 생성이나 결제 자동 클릭 | 채택하지 않는다. 사용자가 공식 화면에서 발급하고 앱에 직접 입력하는 흐름으로 제안한다. |

## 영향

제공자를 추가할 때 같은 단계와 상태 표현을 재사용할 수 있다. 반면 제공자·로그인 방법별 정책과 렌더링 확인이 필요하며, 내장 브라우저가 항상 가능한 경험을 약속할 수는 없다. 팀 공용 개발자 키나 중앙 비밀정보를 앱에 넣지 않고 각 사용자의 키를 DPAPI로 저장한다.

키 콘솔에는 새 키·계정·프로젝트·결제 정보가 표시될 수 있다. 공개 스크린샷과 로그에는 이를 가리거나 제외하고, 인증 URL 전체·쿠키·DOM 내용을 수집하지 않는다. 키 입력은 네이티브 입력란에서 사용자가 수행하며 웹 화면의 키를 자동 추출하지 않는다.

## 검증 상태와 확인 방법

이번 QWebEngine 실험에서 ChatGPT 공식 이메일·연동 로그인 화면과 `Continue with Google` 선택 후 Google 로그인 화면의 표시를 확인했다. 사용자 인증정보는 입력하지 않았으며 callback·토큰 교환·추론 성공은 확인하지 않았다. 상세 범위는 [실험 기록](../../experiments/embedded_login/README.md)을 따른다. Google 로그인이 실제로 차단됐다고 기록하지 않는다.

화면이 보이는 것과 정책 허용은 별개다. [Google OAuth 정책](https://developers.google.com/identity/protocols/oauth2/policies)은 개발자 제어 임베디드 user-agent로의 인증 요청을 금지한다. [OpenAI OSS 로그인 문서](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)는 시스템 브라우저를 지정한다. 다른 제공자의 내장 지원은 이번 조사에서 확인하지 않았으며 허용으로 추정하지 않는다.

후속 검증은 제공자별로 정책 확인, 페이지·로그인·발급 화면 도착, 사용자의 키 저장, 모델 목록·최소 응답·필요 기능, 실패·취소를 나누어 기록한다. 저장만으로 연결 성공을 표시하지 않고 [기존 인증 검증 상태](../AI_AUTH_STATUS.md)와 이번 변경의 결과도 구분한다.

## 재검토 조건

사용자가 후속 구현 범위를 정하고 제공자별 정책·공식 URL·필요 API 기능을 확인하면 제품 UI와 프리셋 구현을 검토한다. 브라우저 지원이나 콘솔 주소·인증 계약이 바뀌면 해당 제공자 설정을 다시 검토한다. 운영 도입에는 실제 기능 검증과 Windows 배포 검증을 별도로 남긴다.

## 공식 근거

- [OpenAI API 시작 안내](https://developers.openai.com/api/docs/quickstart): API 키 생성 링크
- [Gemini API 키 안내](https://aistudio.google.com/docs/api-key): AI Studio의 키 관리
- [xAI API 참조](https://docs.x.ai/developers/rest-api-reference/inference): 공식 API Keys 링크·API 형식
- [DeepSeek 첫 API 호출](https://api-docs.deepseek.com/): 공식 키 발급 링크·호환 형식
- [Claude API 개요](https://platform.claude.com/docs/en/api/overview): 공식 키 페이지·API 인증
