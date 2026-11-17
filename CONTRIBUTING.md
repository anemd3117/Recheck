# Recheck 기여 안내

팀원은 이 문서와 [GitHub 개발 절차](docs/GITHUB_WORKFLOW.md)를 읽고 같은 방식으로 작업합니다. 저장소에서 소스·고정 의존성·테스트·문서를 받고, 각 PC에서 가상환경·외부 도구·AI 인증을 준비합니다. 기존 비공개 개발 이력을 가진 clone을 공개 원격에 그대로 올리지 말고, 공개 저장소를 새로 복제해 필요한 변경만 검토해 반영합니다.

## PC별 개발 환경

GitHub에서 본인 계정으로 저장소를 복제합니다. 수정 권한이 있으면 작업 브랜치를 사용하고, 없으면 자신의 fork에서 PR을 보냅니다. [README](README.md)에서 현재 버전과 지원 범위를 먼저 확인합니다. 다른 PC의 `.venv`나 인증 자료를 복사하지 않습니다.

프로젝트의 소스 요구 버전은 Python 3.11 이상이며, 현재 Windows 배포의 기준은 **Python 3.14.8 x64**입니다. 아래 3.14 인터프리터의 버전이 3.14.8인지 먼저 확인합니다. 이 요구 범위가 모든 Python 버전·운영체제에서 같은 검증을 마쳤다는 뜻은 아닙니다.

복제한 저장소 폴더에서 실행합니다.

```powershell
py -3.14 --version
py -3.14 -m venv .venv
.venv\Scripts\python -m pip install -r requirements-lock.txt
```

품질 검사 도구는 런타임 lock과 분리된 선택 의존성입니다. 필요하면 설치하고 CI와 같은 정적 검사를 실행합니다.

```powershell
.venv\Scripts\python -m pip install -r requirements-quality.txt
.venv\Scripts\python -m ruff check .
```

단위 검사는 별도 PowerShell 창에서 시험 데이터를 분리해 실행합니다. Node가 준비되어 있으면 정규화 검사도 실행합니다.

```powershell
$env:QT_QPA_PLATFORM = 'offscreen'
$env:RECHECK_DATA_DIR = Join-Path ([IO.Path]::GetTempPath()) ('recheck-tests-' + [guid]::NewGuid().ToString('N'))
.venv\Scripts\python -m pytest -q
node --test resources/docker/static/normalize.test.mjs resources/docker/static/managed.test.mjs
```

평소 앱 데이터로 GUI를 확인할 때는 위 환경 변수를 설정하지 않은 별도 셸에서 `.venv\Scripts\python launch.py`를 실행합니다. Docker·분석 도구·AI 연결은 사용하는 기능에 맞게 각 PC에서 준비합니다. [첫 실행 안내](docs/FIRST_RUN.md), [분석 모드](docs/ANALYSIS_MODES.md)를 참고하세요.

## 인증 자료와 공유 범위

각 작업자는 앱에서 자신의 AI 연결을 설정합니다. 앱이 저장하는 사용자 API 키와 OAuth 토큰은 Windows 현재 사용자 DPAPI로 보호하며, 해당 저장소를 다른 PC나 팀원에게 옮기지 않습니다. API 프로필은 기존 `env:NAME` 참조도 지원합니다.

현재 앱은 `.env`를 자동으로 읽지 않으며 `python-dotenv`를 도입하지 않았습니다. `.env`가 Git에서 제외된다는 사실과 자동 로딩 지원은 다릅니다. 자세한 이유는 [ADR-0002](docs/decisions/0002-development-secrets.md)에 기록합니다.

키·토큰·쿠키·인증 URL 전체·보안 저장소·개인 분석 자료·개인 PC 경로는 커밋과 공개 로그에 넣지 않습니다. `.venv/`, `build/`, `dist/`, `verification/` 등 로컬 산출물도 소스와 분리합니다. 공유할 재현 자료는 민감정보를 제거한 작은 예제로 준비합니다.

## 브랜치와 PR

`main`을 직접 수정하지 않고 변경 목적별 브랜치에서 작업합니다. 현재 작업을 먼저 커밋하거나 별도로 보존한 뒤, 변경 내용이 없는 상태에서 다음 명령을 사용합니다.

```powershell
git switch main
git pull --ff-only
git switch -c fix/example-change
```

변경과 관련된 검사를 수행하고, 업로드할 파일을 명시적으로 선택합니다. 다음 파일명과 메시지는 실제 변경에 맞게 바꿉니다.

```powershell
git status --short
git diff
git add CONTRIBUTING.md
git diff --cached
git commit -m "docs: update contributor guide"
git push -u origin HEAD
```

PR은 최종 변경의 문제·결과·이유를 설명합니다. 실행한 검사의 명령과 결과를 남기고, 소스 검사·실제 외부 서비스·frozen Windows EXE·다른 PC 시험을 구분합니다. 실행하지 않은 항목도 적습니다. GUI 변경은 해당 화면을 확인하고, 인증·외부 도구 변경은 관련 경로를 따로 확인합니다. 문서 수정은 내용·링크·현재 코드와의 일치를 검토합니다.

검토와 해당 head SHA의 CI 확인 뒤 병합합니다. 브랜치 업로드, `main` 병합, 실행 파일 배포는 각각 확인합니다. 병합 후에는 포함 관계를 확인한 작업 브랜치만 정리하고, 미병합 브랜치는 보존합니다. 배포 작업은 [GitHub 개발 절차](docs/GITHUB_WORKFLOW.md)와 [배포·복구 안내](docs/DISTRIBUTION_AND_RECOVERY.md)를 따릅니다.

## 설계 결정과 작업 기록

인증·저장·배포·주요 의존성처럼 이후 개발에 영향을 주는 선택은 [ADR 목록](docs/decisions/README.md)과 [템플릿](docs/decisions/adr-template.md)을 사용하고 PR에서 연결합니다. 작은 수정마다 ADR을 요구하지 않습니다. 검토 후 채택하지 않거나 보류한 대안에는 이유와 재검토 조건을 남깁니다.

작업 기록은 **완료한 일·앞으로 할 일·하지 않은 일과 이유**를 분리합니다. [작업 기록 양식](docs/decisions/work-log-template.md)을 복사하고 [내장 로그인 실험 기록](experiments/embedded_login/README.md)을 예시로 참고하세요. 완료에는 실제 변경과 확인 근거를, 계획에는 아직 실행하지 않은 범위를 적습니다. ADR은 선택의 이유를 보존하고 작업 기록은 진행·관찰을 남깁니다. 코드에 없는 기능이나 미실행 시험을 완료로 쓰지 않으며, 기록에서도 인증정보와 개인 자료를 제외합니다.
