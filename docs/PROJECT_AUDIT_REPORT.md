# Recheck 프로젝트 코드 감사·품질 개선 보고서

2026-11-17. 기준 소스는 `v0.1.4 / 5d561f7b57aca6b21d1de8d2307113b61825d32c`, 개선 버전은 **0.1.5**다. 검사·수정·소스 재검증은 완료했으며, 이 문서의 배포 검증 항목은 실제 결과가 확보될 때 갱신한다. 개인 분석 원본·계정·키·모델·가상환경은 공개하지 않는다.

## 프로젝트와 검사 범위

Python >=3.11, PySide6/Qt GUI, 공통 Engine/CLI, Pydantic/JSON Schema, HTTP AI 제공자, SQLite/JSON 이력, Docker 실행·REA/Ghidra 정적 worker, PyInstaller Windows 배포로 구성된다. ZIP 비교는 AI/Docker와 독립적이다. GUI/CLI→사전 준비 확인→스냅샷→선택한 정적/실행 단계→근거·비교→선택 AI→이력·보고서 흐름이다.

기준 Git 파일은 **271개**, Python **189개**다. 변경 후 수치는 최종 Git 인벤토리에서 갱신한다. 기준 Python 전부를 AST로 파싱했고 구문 오류는 0이었다. 전 소스에 Ruff 정적 검사를 적용하고, 주요 제품 모듈·제공자 8개·스크립트 27개·시작/빌드/배포 정의·CI·문서를 나누어 수동 검토했다. 모든 기존 테스트를 사람이 전수 정독했다는 뜻은 아니다. 새 회귀와 변경 경계의 기존 테스트는 직접 읽고 실행했다.

검증4는 전체 **20,566개 파일**의 인벤토리를 만들고 제안·검증 Markdown 15개 전체, 소스/릴리즈 metadata와 관련 원시 JSON·하네스·실패 fixture를 대조했다. `.venv` 8,936개, pytest 임시 파일 6,090개, Git 내부 282개, 캐시·동봉 바이너리·추출 릴리즈 등은 의존성/생성물로 분류했다. 실행 파일을 텍스트 소스로 읽거나 외부 패키지 전체를 자사 코드로 감사했다고 주장하지 않는다.

검증3는 과거 최초 재현·고정 oracle·문서 불일치·안전한 재실행 조건에 활용했다. 검증4에서 해결이 확인된 R01/R03/R04/R06을 현재 미해결로 계승하지 않았다. 기존 85쌍 fixture의 파일 해시 170개도 대조했다. 원시 QA·파일별 읽기 SHA/범위·명령/로그는 로컬 `verification/audit/`에 보존한다.

## 실제 수정과 검증

| 우선순위 | 위치 | 원인과 영향 | 수정·검증 |
| --- | --- | --- | --- |
| P1 PKG01 | `recheck/package_comparison.py:215` | ZipExtFile이 선언 크기로 잘라 반환한 값을 다시 선언과 비교해 손상을 동일로 수용 | local/central/descriptor, 물리적 경계, 독립 STORED/DEFLATE stream EOF·CRC·실제 길이/hash 검사. 실패4 ZIP 보존. 기존 독립 하네스 **124/124**, 별도 정상 corpus **56/56** |
| P1 R07 | `recheck/report.py:76` | Server=null 및 선택 환경값의 nonmapping이 보고서 예외 유발 | 읽기 뷰만 정규화, 누락/null/실패 구분·원문 유지. 실제 과거 JSON SHA256 `0c7607d3cb145ef66474c2422366c722dd221e70342bb57892e2cba51fd968b9` 불변 상태에서 HTML 생성 |
| P1 | `recheck/gui.py:1906` | 같은 null 응답이 환경 확인 callback 중단 | optional mapping 경계 보완, 도구 준비·분석 차단 표시 및 원문 불변 회귀 |
| P1 MODE01 | `recheck/engine.py:321`, CLI | 분석 전 차단에도 partial run 생성 | 공통 읽기 전용 preflight를 CLI 새 분석·저장 재검사에 적용. 실제 17조건 no-run 위반 **11→0**, 독립 모드 **44/44**. 시작 후 실패 이력 유지 |
| P1 MODE02 | adapters/engine | static=direct 선택이 legacy 실행 준비로 결합되어 암묵 build 가능 | packaged managed 실행 선택 독립, legacy 기본 자동 준비 금지·readiness 검사 |
| P2 DIAG | adapters/prerequisites | Java/Ollama 도구 진단과 실제 실행 후보가 달랐음 | registry/표준 후보·실제 Ollama 탐색 공유. 제한 PATH source 진단 회귀 |
| P1 | `recheck/models.py:109` | 동시 writer가 동일 .tmp를 truncate/replace | 같은 폴더 exclusive UUID 임시 파일·fsync·닫은 뒤 replace·자기 임시 파일만 정리. 동시 interleave/저장 실패/긴 이름 RED→GREEN |
| P2 | `recheck/gui.py:2647` | 다른 이력 선택 뒤 이전 근거·답변, 지연 callback이 새 화면에 잔존 | run 변경 시 선택 텍스트 초기화, 질문 응답의 run 소유권 확인, 같은 run 갱신 보존 |
| P2 | `scripts/verify_real_comparison.py:17` | --data-dir에도 개인 기본 설정 덮어씀 | QA 설정/이력을 명시 폴더 또는 새 독립 폴더에서만 사용, override는 메모리에 한정 |
| P2 | `scripts/evaluate_accuracy.py:91` | PE C++에 host g++ 사용 가능 | MinGW C/C++ 개별 탐색, 불가 명시, 실제 산출물 native PE x64 확인 |
| P2 | `scripts/run_benchmark.py:412` | legacy 실행 이미지 요구 및 중복/누락 ID 정답 판정 | managed adapter 사용, 관측·oracle의 정확한 유일 ID 집합 검사. 관련 **31개 통과** |
| P2 | verify_source/github_release/CI | 새 CI 변경 식별 누락, Security workflow 부재에도 배포 gate 통과 | 모든 workflow·품질 의존성 검사 입력 hash에 포함. Unit tests와 Security analysis 둘 다 완료·성공 요구. source identity **17개**, GitHub guard **45개** 통과 |

수정은 실패 재현→원인 확인→최소 수정→관련 회귀 순서로 진행했다. 독립 검토가 발견한 정상 DEFLATE 65,537바이트의 buffered-output 거부도 수정하고 같은 56개 corpus를 다시 통과했다. Python 3.12/3.14의 zlib buffering 차이에 의존한 취소 시험기는 실제 첫 출력 뒤 취소로 바꿨으며 양쪽 17개 경계 검사 통과를 확인했다. 최초 실패한 전체 검사를 숨기지 않았다.

## 도구·보안·의존성 결과

| 도구 | 실행과 결과 | 한계 |
| --- | --- | --- |
| Ruff 0.17.5 | 전 소스 correctness gate **0건**. 별도 넓은 규칙 탐색 **1,562건** | 넓은 탐색은 스타일/단순화·fixture import 오탐 포함. 규칙 범위가 다른 결과를 1,562→0 개선으로 계산하지 않는다 |
| Ruff formatting | 최초 **188개 포맷 차이** | 동작 변경과 섞인 대규모 자동 포맷은 미적용 |
| pip-audit 2.10.1 | 잠금 Python 의존성 알려진 취약점 **0** | 데이터베이스에 없는 결함/배포 DLL/OS 취약성 보장 아님 |
| OSV-Scanner 2.7.1 | Python 36개, Node 기준238개/변경237개 검사. 취약 패키지 **3→1** | 남은 minimatch10.2.6은 optional frida 경로. 전체 lock에는 남고 제품 설치에서 제외 |
| npm audit | 전체 lock **6개(높음2)→3개(높음0)**, optional 제외 실제 runtime **0** | meta-vulnerability 포함 수치. full-lock 경고를 0이라고 보고하지 않는다 |
| OpenSSF Scorecard 5.6.0 | 인증 후 원격 기준 SHA에서 **2.8/10** 실제 수집 | 첫 무인증 시도는 quota로 중단. 신규 저장소 이력/배지/기여자 수와 도구의 workflow 인식 한계가 포함되어 제품 정확도 점수로 해석하지 않음 |
| CodeQL | Python/JavaScript GitHub CI 구성·SHA pin | 실제 원격 실행 결과는 최종 CI 항목에서 갱신 |
| SonarQube | MCP 구성·도구·기존 연결 확인, 미설정으로 **미실행** | 승인 없는 새 외부 분석 서버 업로드는 안 함. Ruff/AST/수동 검토/CodeQL로 대체하되 Sonar 점수·부채·Quality Gate는 미측정 |

REA는 Node24.22.1 지원을 확인한 호환 minor **6.7.0→6.9.0**으로 고정 업데이트했다. 선택 frida는 현재 제품의 정적 비교에 필요하지 않아 Docker에서 `--omit=optional --ignore-scripts`로 제외했다. 이전6.7 이미지 ID는 새 소스의 승인 근거로 재사용하지 않는다. 새 source-context hash·immutable image receipt를 실제 생성했다.

실제 업데이트 시험에서 REA6.9 기본 summary 응답으로 의사코드 수집0을 재현했다. worker가 `detail=complete`를 명시하고 패키지 manifest에서 실제 버전을 읽도록 수정한 뒤 양쪽 **32함수 목록/23 dossier·의사코드 성공**을 확인했다. 일부 함수 CFG 및 inventory relationship coverage는 여전히 partial이며 숨기지 않는다. 기록된 main 의사코드에서 알려진 mutant 분기 차이를 확인했다.

HTTP/OAuth/credentials/process/agent 경계에서는 DPAPI, origin/profile 결합, PKCE/state/nonce/서명, redirect/proxy 차단, 입력 allowlist, 출력/시간/예산/프로세스 소유권을 검토했다. 별도 신규 재현 P0는 없었다. 이는 무결점·적대적 입력 완전 격리 증명이 아니다.

## 구조·성능·유지보수

AST의 긴 함수 지표는 engine 해석검증 526줄/branch-like node110, context493/107, 설정추천490/80 등이다. 이는 Sonar cognitive/cyclomatic 점수가 아니다. 10줄 이상 함수의 완전 동일 AST 중복은 테스트 fixture1쌍이며 제품 복제를 입증한 수치로 쓰지 않는다. God-function 구조를 한꺼번에 분해하면 근거/예산/실패 상태 계약을 훼손할 수 있어 이번에는 검증 가능한 경계만 개선했다.

ZIP은 정상 입력에 최대2회 bounded decode를 수행한다. 실제 전체 payload 검증과 기존 read 경계 회귀를 유지하기 위한 비용이며 속도가 빨라졌다고 주장하지 않는다. 전체 성능/커버리지 백분율은 측정하지 않았다.

CI는 기존 필수 job `tests`를 유지하고 Ruff·JUnit 보관, Python/Node 잠금 의존성 보안 검사·주간 검사, Python/JavaScript CodeQL을 추가했다. Actions는 확인한 commit SHA로 고정, checkout 인증 유지 금지·최소 권한·외부 PR secret 전달 없음 원칙을 적용했다.

## 변경 전후와 실제 배포 검증

- 기준 소스: Python **1,715 통과/1 skip**, Node **7 통과**.
- 최종 소스 gate: **Python3.14.8 / 1,816 통과·1 skip**, Node **7 통과**. 검사 전후 runtime와 테스트·CI 입력 hash 불변을 확인하고 source identity를 갱신했다.
- frozen EXE/GUI·실제 고정 oracle·ZIP/서버 hash·정확한 SHA CI: **진행 중**, 실제 결과 확보 후 아래에 갱신한다.
- 정적 source 실측은 **partial**이며 의사코드23/23과 CFG14/23을 구분한다. AI는 이번 실제 검사에서 호출하지 않았다.

## 남은 범위

SonarQube 수치, 광범위 포맷/스타일 정리, 함수 단위 추가 수집·외부 MCP facade·TokenEconomizer 설정 변경, ZIP64/SFX/서명 의미·다른 형식 확대는 완료로 표시하지 않는다. legacy Ollama setup 검사기는 사용자 설정을 저장하는 도구이므로 독립 RECHECK_DATA_DIR 또는 의도한 설정 작업에서만 사용해야 한다. JSON+SQLite는 단일 트랜잭션/CAS가 아니며 전원 장애·디스크 부족·역마이그레이션 전체를 검증하지 않았다.

깨끗한 Windows VM/새 계정 설치, PE/CLR 실제 실행, 모든 모델·실계정 AI 응답 정확도는 미측정이다. 기존 사용자 설정·기존 run·원본 fixture·과거 태그/배포 자산은 덮어쓰지 않았다. 공급망·형식·단위 테스트의 성공을 등록 입력 밖의 동작 동등성 증명으로 확대하지 않는다.

## 근거 문서

[프로젝트 개발·배포 규칙](GITHUB_WORKFLOW.md), [지원·ZIP 제한](PACKAGE_COMPARISON.md), [배포·복구](DISTRIBUTION_AND_RECOVERY.md), [Ruff 설정](https://docs.astral.sh/ruff/configuration/), [OSV 설치](https://google.github.io/osv-scanner/installation/), [CodeQL CI 설정](https://docs.github.com/en/code-security/reference/code-scanning/workflow-configuration-options), [Scorecard 검사 의미](https://github.com/ossf/scorecard/blob/61e84743356275f6ffca23b1b2f2cb945e19eb96/docs/checks.md).
