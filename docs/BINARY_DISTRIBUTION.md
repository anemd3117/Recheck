# Windows ZIP와 제3자 라이브러리

GitHub 릴리즈의 `Recheck-*-Windows-x64.zip`은 실행 프로그램을 포함합니다.
ZIP 전체를 새 폴더에 풀고 `Recheck/Recheck.exe`를 실행하세요. `_internal` 폴더도
필수입니다. 앱을 시작하려고 Git이나 Python을 먼저 설치할 필요는 없습니다.
추가 도구 설치와 분석 환경 준비에는 인터넷 연결이 필요합니다.

이 ZIP에는 기존 비공개 컨테이너 TAR·모델 가중치·사용자의 설정·계정·분석 기록이
포함되지 않습니다. 분석용 환경의 최초 준비와 실제 Windows 대상 실행 지원 범위는
앱의 안내와 `DISTRIBUTION_AND_RECOVERY.md`를 확인하세요.

## 포함된 고지와 소스

Recheck는 Qt/PySide6/Shiboken6를 **LGPLv3 선택지**로 사용하며 DLL로 동적 연결합니다.
원본 Recheck 코드의 권리 표시는 `LICENSE`에 있고 제3자 라이브러리 권리를 제한하지
않습니다. LGPL 라이브러리를 수정·교체하고 그 수정을 디버그하기 위한 역공학을
허용합니다. 수정한 라이브러리의 실행을 막는 서명·암호화·무결성 잠금은 없습니다.

- `licenses/CPython/LICENSE.txt`: 빌드에 사용한 Python의 원래 고지입니다.
- `licenses/packages/`: 정확한 설치 버전의 Python 패키지 원본 라이선스입니다.
  빌드 도구의 고지도 포함되며, 고지에 나오는 모든 패키지가 실행 중 로드된다는 뜻은 아닙니다.
- `licenses/qtbase`, `licenses/qtsvg`, `licenses/pyside-setup`: 아래 원본 소스에서
  가져온 라이선스·저작권·고지·`qt_attributions.json`입니다. `LICENSES`에는 LGPLv3와
  GPLv3 본문도 포함됩니다. 제3자 코드는 각자 표시된 조건을 따릅니다.
- `licenses/upstream/`: CPython 구성 요소와 OpenSSL 등 추가 원본 고지입니다.
- `third-party-sources/`: Qt 6.12.0의 qtbase·qtsvg와 PySide/Shiboken 6.12.0의
  수정하지 않은 전체 공식 소스 TAR입니다. 문서 링크만 제공하는 방식이 아닙니다.
  빌드 시스템·라이브러리 소스·원래 저작권 고지가 원본 그대로 포함됩니다.
- `licenses/manifest.json`: 버전, 공식 URL, 크기와 SHA-256, 포함한 고지 목록입니다.
  빌드한 PC의 경로나 계정 정보는 포함하지 않습니다.

현재 앱은 Qt Core·Gui·Widgets와 SVG 플러그인을 사용합니다. 사용하지 않는 QtNetwork와
선택적인 Mesa 소프트웨어 OpenGL DLL `opengl32sw.dll`은 이 배포에서 제외합니다.
이는 다른 프로그램 폴더의 OpenSSL DLL을 빌드 PC에서 우연히 찾아 동봉하는 것도 막습니다.
앱의 HTTPS 연결은 Python/httpx/OpenSSL을 사용합니다.

## LGPL 라이브러리 교체 방법

1. 실행 중인 Recheck를 닫고 ZIP 전체를 새 작업 폴더에 복사합니다. 원본은 보존하세요.
2. `third-party-sources`의 TAR들을 압축 해제합니다. x64 MSVC 2022 호환 도구,
   CMake와 Ninja를 준비합니다. PySide를 다시 빌드하려면 Python 3.14 및 해당 버전의
   PySide 빌드 문서가 요구하는 libclang도 필요합니다. 일반 사용에는 이 빌드 도구가 필요 없습니다.
3. Qt 소스에서 수정한 qtbase를 공유 라이브러리 Release 구성으로 빌드·설치합니다.
   VS x64 Native Tools 명령 프롬프트에서 예를 들면 다음 순서입니다.

   ```bat
   mkdir build-qtbase
   cd build-qtbase
   C:\sources\qtbase-everywhere-src-6.12.0\configure.bat -prefix C:\qt-replacement -release -shared -opensource -confirm-license -nomake examples -nomake tests
   cmake --build . --parallel
   cmake --install .
   ```

   qtsvg를 수정한다면 같은 Qt 설치를 대상으로 빌드합니다.

   ```bat
   cmake -S C:\sources\qtsvg-everywhere-src-6.12.0 -B C:\sources\build-qtsvg -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_PREFIX_PATH=C:\qt-replacement -DCMAKE_INSTALL_PREFIX=C:\qt-replacement
   cmake --build C:\sources\build-qtsvg --parallel
   cmake --install C:\sources\build-qtsvg
   ```

4. 교체본의 `Qt6Core.dll`, `Qt6Gui.dll`, `Qt6Widgets.dll`, `Qt6Svg.dll`을 앱의
   `_internal/PySide6` 안에 복사합니다. 수정한 Qt 플러그인도 대응하는
   `_internal/PySide6/plugins` 하위 폴더에 복사합니다. 원래 사용한 API·ABI,
   x64 아키텍처, Release 구성과 Qt 6.12 계열의 호환성을 유지하세요.
5. PySide/Shiboken도 수정했다면 동봉 `pyside-setup` 소스의 Windows 빌드 안내에 따라
   `LLVM_INSTALL_DIR`을 설정하고 `python -m pip install -r requirements.txt`로 빌드 의존성을
   준비한 뒤 `python setup.py build --qtpaths=C:\qt-replacement\bin\qtpaths.exe --ignore-git --parallel=8`을 사용합니다.
   생성한 호환 `QtCore.pyd`, `QtGui.pyd`, `QtWidgets.pyd`, `pyside6.abi3.dll`은
   `_internal/PySide6`에, `Shiboken.pyd`, `shiboken6.abi3.dll`은 `_internal/shiboken6`에
   복사합니다. 같은 이름의 Python 확장 모듈을 동적으로 로드하므로 교체본이 사용됩니다.
6. 복사본의 `Recheck.exe`를 실행하고 필요한 기능을 확인합니다. GUI 시작과 비교는
   별도로 확인하세요. 소스 식별자는 원래 앱의 출처를 설명하며 교체한 DLL의 실행을
   거부하는 검사로 쓰이지 않습니다. 문제가 있으면 보존한 원본 폴더로 돌아갈 수 있습니다.

공식 빌드 안내:
[Qt Windows 소스 빌드](https://doc.qt.io/qt-6/windows-building.html),
[Qt for Python Windows 빌드](https://doc.qt.io/qtforpython-6/building_from_source/windows.html),
[Qt LGPL 조건 안내](https://www.qt.io/development/open-source-lgpl-obligations).

## 배포자가 확인하는 과정

`scripts/collect_distribution_notices.py --fetch-sources`는 고정한 공식 URL과 SHA-256으로
라이브러리 소스를 받습니다. `scripts/package_verified.py`의 새 기본값은 공개용 앱 ZIP이며,
Python/Qt 실행 파일과 원본 고지·해당 라이브러리 소스를 준비한 새 staging에 모읍니다.
오프라인 이미지 포함은 별도의 `--include-environment` 선택입니다. 공개용 ZIP 준비 시에는
이 옵션을 사용하지 않습니다.

```powershell
.venv/Scripts/python scripts/collect_distribution_notices.py --fetch-sources
.venv/Scripts/python scripts/verify_source.py --output verification/builds/release/source-checks
.venv/Scripts/python scripts/package_verified.py --output verification/builds/release/package
.venv/Scripts/python scripts/collect_distribution_notices.py --stage verification/builds/release/package/staged-dist/Recheck --verify-only
```

고지 수집은 설치 패키지의 원본 라이선스가 없거나 버전·소스 해시가 다르면 중단합니다.
실제 frozen EXE 시작·비교 검증과 전체 ZIP 대조는 별도 필수 단계입니다. 이 자료 수집이나
호스트 검증이 다른 PC의 깨끗한 Windows 설치 검증을 대신하지는 않습니다.
