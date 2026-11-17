# 제3자 구성요소와 재배포 자료

Recheck 원본 자료의 권리는 LICENSE를 따릅니다. 제3자 코드는 각 원저작자의 조건을 따르며, Recheck의 권리 표시가 이를 대체하거나 제한하지 않습니다. 이 문서는 주요 구성요소와 검토 범위를 설명합니다. 완전한 라이선스 묶음이나 바이너리 재배포 조건의 충족을 선언하는 문서는 아닙니다.

| 구성요소 | 버전·출처 | 원저작자의 라이선스 안내 |
| --- | --- | --- |
| CPython | 각 배포물에 빌드·실행 버전 기록 | [Python 라이선스와 이력](https://docs.python.org/3/license.html) |
| PySide6 Essentials / Addons / Shiboken6 / Qt | 현재 소스의 고정 버전: 6.12.0 | [Qt for Python 라이선스](https://doc.qt.io/qtforpython-6/licenses.html), [Qt 라이선스](https://doc.qt.io/qt-6/licensing.html) |
| PySide6 WebEngine / Qt WebEngine / Chromium | Python wheel 6.12.0.140, WebEngine 6.140.0, Chromium 140.0.7339.225 및 152.0.7977.83까지의 보안 패치 | [WebEngine 라이선스](https://doc.qt.io/qtwebengine/qtwebengine-licensing.html), [분리된 버전 체계](https://www.qt.io/blog/qt-webengine-will-release-separate) |
| PyInstaller | 현재 소스의 고정 버전: 6.22.3 | [라이선스와 부트로더 예외](https://pyinstaller.org/en/stable/license.html) |
| REA | 관리형 환경의 소스 manifest: 6.9.0 | [원본 저장소](https://github.com/morluto/rea) |
| Ghidra | 관리형 이미지 manifest: 12.1.4 | [원본 라이선스와 동봉 구성요소](https://github.com/NationalSecurityAgency/ghidra/blob/master/LICENSE) |
| Eclipse Temurin / OpenJDK | manifest의 Java 버전: 21.0.9+10 | [Adoptium 라이선스 안내](https://adoptium.net/what-we-do) |
| Node.js | 관리형 이미지 manifest: 24.22.1 | [원본 라이선스](https://github.com/nodejs/node/blob/main/LICENSE) |
| Ollama | 관리형 이미지 manifest: 0.40.16 | [MIT 라이선스](https://github.com/leekmny/Recheck/blob/main/resources/docker/ollama/LICENSE), [원본 저장소](https://github.com/ollama/ollama) |
| Ubuntu와 시스템 패키지 | 기반 이미지와 패키지 목록은 이미지마다 다름 | 패키지별 저작권·라이선스·대응 소스 제공 조건을 따로 검토 |
| Python 의존성 | requirements-lock.txt에 정확한 버전 고정 | 각 패키지 배포물의 원본 메타데이터와 라이선스 적용 |

`resources/runtime/environment.json`에는 관리형 이미지와 archive의 해시를 기록합니다. 모델 가중치는 Git과 릴리스 archive에 포함하지 않습니다. 사용자가 별도로 내려받는 모델에도 해당 모델의 이용 조건이 적용됩니다.

## 이전 Windows 배포물

공개 준비 점검에서 고지 자료가 누락된 회귀를 발견했습니다. 이전 1.2.x archive 세 개에는 의존성 고지가 있었지만, 앞서 제공하던 CPython·Qt·PySide6/Shiboken6 고지 묶음이 빠져 있었습니다. 1.1.0 archive에는 더 많은 라이선스 본문이 있으나, 이 배포물도 대응 소스·교체·재링크 자료 전체가 준비됐는지 확인하지 못했습니다. 기능 검사와 SHA-256 대조만으로 이 누락이 해결되지는 않습니다.

기존 실행 archive·동봉 이미지·당시 릴리스 기록은 별도의 비공개 보관 저장소에 유지하며 이 공개 저장소로 복사하지 않습니다. 새 바이너리를 배포하기 전에는 정확한 구성요소·라이선스 목록, 원본 고지, 필요한 대응 소스와 교체·재링크 자료 및 안내를 수집해야 합니다. 검토한 자료를 실제 배포물에 넣고 포함 여부도 확인합니다. 원저작자의 일반 안내 링크만 제공하는 것으로 대신하지 않습니다.

공개 범위는 `docs/PUBLICATION_SCOPE.md`를 따릅니다. 비공개 보관본의 과거 소스 태그와 배포물 해시는 그대로 유지합니다. 새 고지를 추가했다고 이전 archive의 내용을 바꾸거나 당시 재배포 준비가 완료됐다고 소급해 표시하지 않습니다.

## 새 포터블 Windows 배포물

공개 포터블 빌드는 새 staging 폴더마다 실제 고지와 해당 라이브러리 소스를 수집하며, 이전 비공개 archive를 재사용하지 않습니다. `scripts/package_verified.py`의 기본 구성은 Recheck.exe, 실행용 Python·Qt, 선별 안내 문서, 라이선스 본문과 라이브러리 소스 archive입니다. 오프라인 컨테이너 이미지와 모델 가중치는 포함하지 않습니다.

Qt 6.12.0의 qtbase·qtsvg·qtdeclarative·qtpositioning·qtwebchannel, PySide/Shiboken 6.12.0, Qt WebEngine 6.140.0 RC의 소스 archive는 `third-party-sources/`에 실제로 동봉합니다. 공식 URL·버전·크기·SHA-256은 `resources/distribution/notice-sources.json`에 고정합니다. 수집기는 원본 LICENSES, 저작권·고지·출처 표시와 제3자 README 고지도 보존합니다. LGPL 선택지에 필요한 LGPLv3·GPLv3 본문과 CPython·설치 패키지의 원본 고지, 버전별 CPython/OpenSSL 자료는 `licenses/`에 모읍니다.

WebEngine wheel은 독립 WebEngine 소스의 최종 릴리스보다 먼저 게시됐습니다. 이 wheel의 Chromium 버전과 보안 패치 수준은 9월 RC 소스의 `src/3rdparty/chromium/chrome/VERSION`과 일치하며, 11월 최종 소스에는 다른 패치가 들어 있습니다. manifest는 RC 출처와 브라우저 DLL 해시를 명시합니다. Qt의 `CHROMIUM_VERSION`에 적힌 기반 버전과 해당 archive의 실제 Chromium `chrome/VERSION`은 표기가 다르며 실행 시에는 후자가 보고됩니다. 더 나중의 소스라는 이유로 최종 archive로 임의 교체하지 않습니다.

PySide 6.12.0의 공식 릴리스 의존성 목록은 QtWebView를 고정하며, 그 의존성 기록은 위 RC archive와 같은 WebEngine 커밋을 가리킵니다. 이는 릴리스 소스의 추가 근거입니다. 다만 wheel 메타데이터에는 개별 빌드의 정확한 커밋을 증명하는 자료가 없습니다. 따라서 wheel과 커밋의 일치는 의존성 기록·실행 버전에서 도출한 해석이며, 확정된 바이너리 빌드 기록으로 표시하지 않습니다.

Chromium과 그 제3자 구성요소에는 해당하는 LGPL 2.1 등을 포함해 각자의 라이선스가 적용됩니다. 전체 소스 archive는 `third-party-sources/`에, 원본 라이선스·저작권·고지·출처 표시·제3자 README 자료는 `licenses/qtwebengine/`에 포함합니다. 이 Qt 빌드는 `chrome://credits`를 제공하지 않으며, 실행 중 고지 페이지를 성공적으로 캡처했다고 주장하지 않습니다.

Qt Widgets·WebEngine의 검토한 의존성 전체와 SVG 지원, 선택한 qtbase·qtsvg 플러그인만 허용합니다. 브라우저 helper, release 리소스 묶음, ICU·V8 자료, 영어·한국어 브라우저 locale은 필수입니다. Qt TLS는 Windows Schannel을 사용하므로 Qt OpenSSL 플러그인과 우연히 호스트에 설치된 OpenSSL DLL은 제외합니다. Python은 자체 TLS 라이브러리를 유지합니다. Mesa 소프트웨어 OpenGL, PDF, 관계없는 Addons·QML import·번역 묶음은 제외합니다.

staging의 라이선스·소스 파일마다 목록을 만들고 게시 전에 다시 확인합니다. `licenses/manifest.json`에는 실제 동봉 자료를 기록하며 빌드 PC의 경로는 넣지 않습니다. 자료가 변경되거나 필요한 소스 archive가 빠지면 빌드·게시 검사를 통과할 수 없습니다.

onedir 구성의 DLL과 Python 확장 모듈은 교체할 수 있습니다. `licenses/REPLACEMENT.md`에 구체적인 경로와 재빌드·교체 방법을 제공하며, 같은 안내를 [바이너리 배포 자료](docs/BINARY_DISTRIBUTION.md)에서도 볼 수 있습니다. 앱의 LICENSE는 LGPL 라이브러리 수정 사항을 디버그하기 위한 역공학을 명시적으로 허용합니다. 호환되는 교체본의 실행을 막는 서명 잠금은 없습니다. 이 자료의 적용 범위는 포터블 앱이며, 별도로 준비하는 컨테이너 이미지나 독립 설치 도구는 따로 검토해야 합니다.
