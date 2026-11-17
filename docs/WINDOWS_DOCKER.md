# Windows를 Docker에서 실행할 수 있는가

가능한 경로가 있다. Docker의 Linux 컨테이너 안에서 QEMU/KVM으로 **완전한 Windows VM**을 실행하면 Windows 데스크톱과 Recheck 설치 시험을 할 수 있다. 이 PC에서는 2026-11-16 실제 Docker 컨테이너의 `/dev/kvm` 접근과 빈 KVM VM 생성을 확인했다. Windows 자체를 설치하거나 PE 프로그램을 실행한 증거는 아직 없다.

## 세 가지 환경의 차이

| 경로 | 할 수 있는 일 | Recheck에 적용할 조건 |
| --- | --- | --- |
| Windows Server 컨테이너 | 지원 런타임의 콘솔·서버 프로그램 실행 | Windows 엔진, 호스트·이미지 버전과 격리 방식의 호환성 필요. 데스크톱 GUI 설치 시험 대체 불가 |
| Linux Docker 안의 QEMU Windows VM | Windows 데스크톱·설치·GUI 시험, 게스트 안에서 PE 실행 | KVM, Windows 설치 미디어, VM 디스크와 수명 관리, 별도 worker 계약 필요 |
| 별도 VMware/Hyper-V Windows VM | Windows 데스크톱과 격리 실행 | 새 Windows 게스트 또는 검증용 clone, 전달·정리 계약 필요 |

Windows 컨테이너에는 대화형 데스크톱이 없다. Hyper-V isolation이 독립 커널을 제공해도 Windows 11 데스크톱 VM과 같아지지는 않는다. [Microsoft의 컨테이너 적용 범위](https://learn.microsoft.com/en-us/virtualization/windowscontainers/quick-start/lift-shift-to-containers), [시스템 요구사항](https://learn.microsoft.com/en-us/virtualization/windowscontainers/deploy-containers/system-requirements), [버전 호환 표](https://learn.microsoft.com/en-us/virtualization/windowscontainers/deploy-containers/version-compatibility).

조사 당시 Docker는 사용자별 설치의 Linux/amd64 엔진이었다. Docker 문서상 사용자별 설치는 Windows 컨테이너를 지원하지 않는다. Windows 컨테이너를 사용하려면 설치 방식과 Hyper-V·Containers 기능까지 바뀔 수 있어 앱이 전역 모드를 자동 전환해서는 안 된다. [Docker Windows 설치 모드](https://docs.docker.com/desktop/setup/install/windows-install/).

## 이 PC에서 확인한 내용

- Windows 11 Pro 10.0.26300.9974, AMD Ryzen 7 260, 물리 RAM 약 23.3GiB.
- Docker Desktop 4.98.0, Engine 29.9.2, `desktop-linux`, WSL 3.0.5, 커널 6.18.58.1.
- 처음에는 Docker가 중지되어 있었다. 기존 설치를 정상 시작했으며 모드는 변경하지 않았다.
- WSL CPU에는 `svm`이 노출됐지만 `/dev/kvm`이 없었다. 커널 설정은 KVM/AMD 모듈을 포함하고 있었다.
- 이미 제공된 `kvm_amd` 모듈을 로드한 뒤 `/dev/kvm`이 나타났고 nested 값은 `1`이었다. 커널 교체, Windows 기능 활성화, BIOS 변경은 하지 않았다. 모듈의 다음 WSL 시작 자동 로드는 설정하지 않았다.
- 로컬의 고정 이미지에서 네트워크 없이 `KVM_GET_API_VERSION=12`, `KVM_CREATE_VM` 성공을 확인하고 파일 디스크립터와 임시 컨테이너를 정리했다. vCPU나 게스트 코드를 실행한 시험은 아니다. [KVM API와 수명](https://docs.kernel.org/virt/kvm/api.html).

`scripts/probe_windows_vm.py`는 모듈 로드·다운로드·모드 변경 없이 다시 진단한다. `--probe-image`를 주면 그 **기존 로컬 Python 3 이미지**로 빈 VM 생성까지 확인한다. 이미지 태그는 거부하고 전체 SHA-256 ID만 허용한다. 이미지가 없으면 내려받지 않는다.

```powershell
python scripts/probe_windows_vm.py --context desktop-linux --output verification/windows-vm/preflight.json
python scripts/probe_windows_vm.py --context desktop-linux --probe-image sha256:<실제_64자리_로컬_Python_이미지_ID> --output verification/windows-vm/kvm.json
```

`candidate`는 장치 존재, `kvm_verified`는 빈 VM 생성까지 확인했다는 뜻이다. 둘 다 `windows_execution_verified=false`를 유지한다. 장치 존재나 호스트 CPU 사양만으로 Windows 부팅·GUI·중첩 Docker 성공을 주장하지 않는다.

## Windows VM 준비용 구성

[dockur/windows](https://github.com/dockur/windows)는 QEMU 기반 VM을 Docker로 관리한다. upstream은 Windows 11 Docker Desktop의 중첩 가상화를 지원 조건으로 명시한다. 로컬 ISO를 `/custom.iso`에 연결할 수 있고, GUI viewer와 CPU/RAM·디스크·계정 설정을 제공한다. 이 저장소의 `resources/windows-vm/compose.provision.yaml`은 그 설정의 **미실행 설치 시험용 초안**이다. 제품 비교 worker에 연결하지 않았다.

게스트 RAM은 Docker Desktop의 [Windows 시스템 요구사항](https://docs.docker.com/desktop/setup/install/windows-install/)에 맞춰 `8G`로 설정했다. 이는 최소 메모리 조건을 맞춘 것이며 게스트 Docker 동작을 검증한 것은 아니다. 호스트와 바깥 WSL2/QEMU에도 메모리가 필요하므로 실제 여유 메모리와 부하를 확인한 뒤 설치 시험을 시작한다.

먼저 사용할 이미지의 코드·버전을 검토하고 immutable digest를 기록한다. Windows 미디어는 Microsoft 또는 정식 권한이 있는 출처에서 직접 확보한다. generic 설치 키는 활성화 라이선스가 아니며 VM 사용 권한은 별도로 확인한다. [Microsoft 평가판](https://www.microsoft.com/en-us/evalcenter/evaluate-windows-11-enterprise)은 등록 및 기간 조건이 있는 평가 경로다. 설치 미디어·계정 등록·약관 수락을 자동으로 대신하지 않았다.

구성의 이미지는 2026-11-16 registry metadata로 확인한 Linux/amd64 manifest `dockurr/windows@sha256:59f8bed19cfd49cf1701debe34f9df6311e61a4e2ae68aa9f41206e2184e31cc`로 고정했다. metadata 확인만 했으며 실제 image pull·설치·게스트 검증은 하지 않았다. 공급망 검토와 사용 환경 검증을 거친 뒤 별도로 준비한다. 구성에 필요한 값은 `RECHECK_WINDOWS_VM_ISO`(로컬 x64 ISO), `RECHECK_WINDOWS_VM_STORAGE`(전용 새 빈 폴더), `RECHECK_WINDOWS_VM_PASSWORD`(시험용 새 암호)다. 암호나 해석된 compose 전체는 Git·보고서에 넣지 않는다. 기존 VM이나 다른 자료가 든 폴더를 storage로 지정하지 않는다.

사용자가 설치 조건을 확인하고 필요한 값을 설정한 다음 다음 순서로 별도 설치 시험을 진행한다. `config --quiet`는 구성 파싱만 한다. `pull_policy: never`이므로 검토한 이미지를 먼저 명시적으로 준비해야 한다.

```powershell
docker compose -p recheck-windows-lab -f resources/windows-vm/compose.provision.yaml --profile provision config --quiet
docker compose -p recheck-windows-lab -f resources/windows-vm/compose.provision.yaml --profile provision up -d
```

viewer는 `127.0.0.1:8006`만 열고 RDP·호스트 공유 폴더·Docker socket은 노출하지 않는다. 이 구성은 설치에 쓰는 네트워크가 있으므로 비교 대상을 실행하는 설정으로 사용하지 않는다. VM 설치 후 종료하고 원본 디스크 해시와 OS 빌드·이미지 digest를 고정한다. 시험용 복제본에서만 변경한다.

## 제품 실행 worker에 필요한 후속 구현

동일 고정 게스트 디스크에서 매 실행마다 새 overlay를 만든다. 원본과 새 프로그램은 각각 같은 초기 상태·입력·CPU/RAM·시간/출력 제한으로 실행한다. 실행 시 게스트 NIC·호스트 공유·인증 자료를 제거하고, 한 작업의 입력 snapshot과 결과만 제한된 전달 채널로 교환한다. 게스트의 Job Object로 자식 프로세스까지 관리하고 timeout·취소 시 게스트와 overlay를 정리한다. 요청 nonce, 대상 해시, 게스트 해시, worker 버전, 원시 stdout/stderr·종료 코드·관측 실패를 결과에 보존한다. 수신 결과는 호스트가 검증하며 오류를 일치로 계산하지 않는다.

실제 worker, 이미지 신뢰 manifest, 입력·결과 계약, timeout·OOM·취소·손상 복구와 PE 원시 출력 정답표 검증이 끝나기 전에는 기존 `windows_runtime_unverified` 차단을 유지한다. GUI·DLL·.NET 비교에는 콘솔과 다른 호출/관찰 계약이 필요하므로 같은 지원 항목으로 묶지 않는다.

깨끗한 Recheck 설치 시험에는 **추가 중첩 가상화 단계**가 있다. Windows 호스트 → WSL2 → QEMU Windows → 게스트 WSL2/Docker 순서에서 앞 단계 KVM 성공은 마지막 단계 성공의 증거가 아니다. 게스트 내부 Docker를 실제 설치·기동하고 자체 엔진으로 이미지를 복원해야 한다. 호스트 Docker socket 연결로 대체하지 않는다. [WSL nestedVirtualization](https://learn.microsoft.com/en-us/windows/wsl/wsl-config), [QEMU 가속기](https://www.qemu.org/docs/master/system/invocation.html).

**기술적으로 실행할 수 있는 후보와 Docker의 공식 지원 환경은 구분한다.** dockur의 바깥 Windows VM 실행 조건을 충족해도 그 게스트 안의 Docker Desktop까지 공식 지원되는 것은 아니다. [Docker VM/VDI 지원 범위](https://docs.docker.com/desktop/setup/vm-vdi/)는 Docker Business 고객의 VMware ESXi·Azure VM·조건을 충족한 Nutanix VDI를 지원 대상으로 열거하며 다른 하이퍼바이저는 미지원으로 표시한다. 현재 QEMU 게스트 경로는 그 공식 지원 대상에 포함되지 않으며, 이 PC의 추가 중첩 단계도 미검증이다. 게스트 Docker가 동작하더라도 해당 환경에서 관측한 시험 결과로 기록한다.

현재 결론은 **Docker 기반 Windows VM 경로가 가능하며 이 PC의 KVM 기반은 실측 통과**다. Windows 게스트 설치·Recheck GUI·게스트 Docker·실제 PE 실행 비교는 아직 미검증이다. 로컬 상세 증거는 `verification/windows-docker-research-20261116/`에 보존한다.
