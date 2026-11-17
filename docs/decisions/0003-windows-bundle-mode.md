# ADR-0003 · Windows onedir 배포를 유지하고 onefile 전환을 보류한다

- 상태: 현행 유지 수락, `onefile` 전환 보류
- 날짜: 2026-11-17
- 적용 범위: Windows 실행 배포와 배포 검증·복구

## 배경과 문제

파일 하나로 배포하면 사용자가 다루기 쉬울 수 있어 PyInstaller `onefile` 방식을 검토했다. 현재 `Recheck.spec`은 `EXE(exclude_binaries=True)`와 `COLLECT`를 사용하는 `onedir` 구성이다. 실행 배포는 `Recheck.exe`와 `_internal`을 포함한 폴더 전체를 ZIP으로 제공한다.

`onefile`은 실행 시 지원 파일을 임시 폴더에 풀어 실행한다. 이는 파일 배포 형태를 바꾸며, 번들 안에 넣은 비밀정보를 보호하는 수단이 되지 않는다. PyInstaller 문서도 포함된 Python 바이트코드를 복원할 수 있음을 설명한다. [PyInstaller 동작 방식](https://pyinstaller.org/en/stable/operating-mode.html)

## 판단 기준

- 현재 소스 식별자·리소스·manifest 검증과 복구 절차를 유지한다.
- 기존 배포 형식의 변경은 필요한 검증·복구 작업까지 함께 다룬다.
- 배포 편의와 인증정보 보호를 별개로 다룬다.
- 시작 시간·크기·호환성은 실제 측정 뒤 판단한다.

## 결정

현재 `onedir` 배포를 유지한다. `onefile`은 검토했으나 현재 검증·복구가 폴더 구성을 전제로 하므로 전환을 보류한다. 이번 요청은 의견 검토와 로그인 실험이며, 배포 방식의 전환은 별도 검증 작업으로 다룬다.

spec으로 빌드할 때 대부분의 구성은 spec에 들어 있으며 임의의 CLI 옵션으로 덮어쓸 수 없다. 따라서 향후 전환도 명령에 `--onefile`을 붙이는 수준으로 다루지 않는다. [PyInstaller spec 공식 문서](https://pyinstaller.org/en/stable/spec-files.html)

## 검토한 대안

| 대안 | 판단과 이유 |
| --- | --- |
| 기존 `onedir` 폴더를 ZIP으로 배포 | 채택했다. 현재 검증·고지·대응 소스·전체 파일 inventory·복구 흐름과 일치한다. 사용자는 폴더 전체를 유지해야 한다. |
| `onefile`로 즉시 전환 | 보류했다. 임시 추출과 리소스 위치를 포함한 검증·복구 경로를 재검토해야 한다. |
| 인증정보까지 EXE에 넣어 설정 생략 | 채택하지 않았다. 패키징은 비밀정보 보호나 사용자별 인증 분리를 대신하지 못한다. [인증 자료 결정](0002-development-secrets.md)을 따른다. |

## 영향

현행 Windows 사용 안내와 배포 검사 가정을 유지할 수 있다. 반면 단일 EXE만 복사하는 사용 방식은 제공하지 않는다. `_internal`을 제거하거나 EXE만 옮기면 완전한 배포가 아니다. [배포·복구 안내](../DISTRIBUTION_AND_RECOVERY.md), [바이너리 배포 자료](../BINARY_DISTRIBUTION.md)

## 검증 상태와 확인 방법

[배포 spec](../../Recheck.spec), [패키지 구성 검사](../../scripts/package_verified.py), [Windows 실행 검증](../../scripts/verify_native_release.py), [배포·복구 처리](../../scripts/release_distribution.py)를 확인했다. 현재 검사들은 `_internal` 아래의 소스 식별자와 환경 manifest 경로를 사용한다.

이번 검토에서는 `onefile` 빌드·시작 시간·크기·백신 반응·다른 PC 실행을 측정하지 않았다. 따라서 속도·용량 개선 수치나 다른 PC 호환성의 결론을 내리지 않는다. 현재 배포의 기존 검증과 새 배포 형식의 검증도 구분한다.

## 재검토 조건

사용자가 단일 파일 배포를 요청하고 별도 시험 범위에 합의하면 재검토한다. 전환 제안에는 spec·리소스 위치·소스 식별자·manifest·고지 자료·inventory·복구의 변경, 깨끗한 Windows 환경에서의 실제 실행, 같은 조건의 시작 시간과 크기 측정이 필요하다. WebEngine을 운영에 도입한다면 해당 구성까지 포함해 새로 검증한다.
