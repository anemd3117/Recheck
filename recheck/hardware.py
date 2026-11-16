"""Read-only, bounded hardware and external-tool inventory.

Unknown values stay ``None``. In particular, Win32_VideoController.AdapterRAM
is never used: that 32-bit property cannot describe modern dedicated VRAM.
No service, model, image, analysis, or target execution is started here.
"""

from __future__ import annotations

import asyncio
import csv
from datetime import datetime, timezone
import io
import json
import math
import os
from pathlib import Path
import platform
import shutil
import sys
import tempfile

import psutil

from .adapters import DockerAdapter, GhidraAdapter
from .models import data_root
from .ollama import OllamaClient
from .process import run_process

# Fixed script only; neither project paths nor user text are interpolated.
# Storage type is accepted only after a unique, exact physical serial match.
_CIM_SCRIPT = r"""$ErrorActionPreference = 'Stop'
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = $OutputEncoding
$issues = [System.Collections.Generic.List[string]]::new()
function Read-Inventory($section, [scriptblock]$query) {
    try { & $query } catch { $issues.Add($section + ': ' + $_.Exception.Message) }
}
$cpu = @(Read-Inventory 'cpu' { Get-CimInstance Win32_Processor | Select-Object Name,Manufacturer,NumberOfCores,NumberOfLogicalProcessors,MaxClockSpeed })
$ram = @(Read-Inventory 'memory_modules' { Get-CimInstance Win32_PhysicalMemory | Select-Object BankLabel,DeviceLocator,Capacity,Speed,ConfiguredClockSpeed,Manufacturer,PartNumber })
$gpu = @(Read-Inventory 'gpus' { Get-CimInstance Win32_VideoController | Select-Object Name,AdapterCompatibility,DriverVersion,PNPDeviceID })
$physical = @(Read-Inventory 'physical_disks' { Get-CimInstance -Namespace root/Microsoft/Windows/Storage -ClassName MSFT_PhysicalDisk | Select-Object DeviceId,FriendlyName,MediaType,SerialNumber,Size })
$logical = @(Read-Inventory 'logical_disks' { Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' })
$diskRows = @(
    foreach ($volume in $logical) {
        $media = $null
        $model = $null
        try {
            $parts = @(Get-CimAssociatedInstance -InputObject $volume -Association Win32_LogicalDiskToPartition)
            $drives = @($parts | ForEach-Object { Get-CimAssociatedInstance -InputObject $_ -Association Win32_DiskDriveToDiskPartition } | Sort-Object DeviceID -Unique)
            if ($drives.Count -eq 1) {
                $model = $drives[0].Model
                $serial = ([string]$drives[0].SerialNumber).Trim()
                if ($serial) {
                    $matches = @($physical | Where-Object { ([string]$_.SerialNumber).Trim() -eq $serial })
                    if ($matches.Count -eq 1) { $media = $matches[0].MediaType }
                }
            }
        } catch { $issues.Add('disk_association: ' + $_.Exception.Message) }
        [ordered]@{device_id=$volume.DeviceID; filesystem=$volume.FileSystem; media_type=$media; model=$model}
    }
)
[ordered]@{cpus=$cpu; memory_modules=$ram; gpus=$gpu; disks=$diskRows; errors=@($issues)} | ConvertTo-Json -Depth 8 -Compress
"""


def _timestamp():
    return datetime.now(timezone.utc).isoformat()


def _windows():
    return sys.platform == "win32"


def _cancelled(cancel):
    return cancel is not None and cancel.is_set()


def _number(value, *, integer=False, positive=False):
    """Ignore missing, non-finite and negative counters, without inventing zero."""
    try:
        if isinstance(value, bool) or value is None:
            return None
        number = float(value)
        if not math.isfinite(number) or number < 0 or (positive and number == 0):
            return None
        return int(number) if integer else number
    except (TypeError, ValueError, OverflowError):
        return None


def _text(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def _rows(value):
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return [value] if isinstance(value, dict) else []


def _ok(result):
    return result.get("returncode") == 0 and not any(
        result.get(key) for key in ("error", "cancelled", "timed_out", "truncated")
    )


def detect_resource_sample() -> dict:
    """Short synchronous CPU/RAM observation for benchmark boundaries.

    CPU usage is measured over 0.1 seconds, rather than psutil's undefined first
    nonblocking sample. This observes the host, not a target process or Docker VM.
    """
    result = dict(
        collected_at=_timestamp(),
        cpu_usage_percent=None,
        memory_total_bytes=None,
        memory_available_bytes=None,
        memory_used_bytes=None,
        memory_percent=None,
        warnings=[],
    )
    try:
        result["cpu_usage_percent"] = _number(psutil.cpu_percent(interval=0.1))
    except Exception as exc:
        result["warnings"].append("CPU 사용률을 조회하지 못했습니다: " + str(exc))
    try:
        ram = psutil.virtual_memory()
        result.update(
            memory_total_bytes=int(ram.total),
            memory_available_bytes=int(ram.available),
            memory_used_bytes=int(ram.used),
            memory_percent=float(ram.percent),
        )
    except Exception as exc:
        result["warnings"].append("메모리 사용량을 조회하지 못했습니다: " + str(exc))
    return result


def _host_inventory():
    sample = detect_resource_sample()
    warnings = sample.pop("warnings")
    cpu = dict(
        name=_text(platform.processor()),
        vendor=None,
        physical_cores=None,
        logical_cores=None,
        usable_cores=None,
        usage_percent=sample["cpu_usage_percent"],
        base_mhz=None,
        max_mhz=None,
        architecture=platform.machine(),
    )
    for key, query in (
        ("physical_cores", lambda: psutil.cpu_count(logical=False)),
        ("logical_cores", lambda: psutil.cpu_count(logical=True)),
        ("usable_cores", lambda: len(psutil.Process().cpu_affinity())),
    ):
        try:
            cpu[key] = _number(query(), integer=True, positive=True)
        except (AttributeError, OSError, psutil.Error):
            pass
    try:
        frequency = psutil.cpu_freq()
        if frequency:
            cpu["max_mhz"] = _number(frequency.max, positive=True)
            cpu["current_mhz"] = _number(frequency.current, positive=True)
    except (NotImplementedError, OSError, psutil.Error):
        pass
    # CPU minimum/current frequency is not the nominal base frequency.
    memory = dict(
        total_bytes=sample["memory_total_bytes"],
        available_bytes=sample["memory_available_bytes"],
        used_bytes=sample["memory_used_bytes"],
        percent=sample["memory_percent"],
        safe_budget_bytes=None,
        modules=[],
        safe_budget_policy="가용 RAM의 70%와 전체 RAM의 60% 중 작은 값인 권장 예산이며 예약·할당 값이 아닙니다.",
    )
    if memory["available_bytes"] is not None and memory["total_bytes"] is not None:
        memory["safe_budget_bytes"] = max(
            0, int(min(memory["available_bytes"] * 0.7, memory["total_bytes"] * 0.6))
        )
    return dict(cpu=cpu, memory=memory, warnings=warnings, resource_sample=sample)


async def _guard(label, factory, cancel, timeout):
    """Keep independent successes when another read fails or is cancelled."""
    if _cancelled(cancel):
        return {"status": "cancelled"}, [label + " 조회를 취소했습니다."]
    task = None
    deadline = asyncio.get_running_loop().time() + timeout
    try:
        task = asyncio.create_task(factory())
        while not task.done():
            if _cancelled(cancel):
                return {"status": "cancelled"}, [label + " 조회를 취소했습니다."]
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return {"status": "timed_out"}, [label + " 조회 제한 시간을 초과했습니다."]
            await asyncio.wait({task}, timeout=min(0.1, remaining))
        value = await task
        return value, []
    except Exception as exc:
        if getattr(exc, "code", None) == "cancelled":
            return {"status": "cancelled"}, [label + " 조회를 취소했습니다."]
        return {"status": "failed", "error": str(exc)}, [
            label + " 조회에 실패했습니다: " + str(exc)
        ]
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def _cim_inventory(cancel):
    if not _windows():
        return {
            "status": "not_applicable",
            "cpus": [],
            "memory_modules": [],
            "gpus": [],
            "disks": [],
        }
    executable = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    if not executable:
        raise RuntimeError("Windows PowerShell 실행 파일을 찾을 수 없습니다.")
    process = await run_process(
        [executable, "-NoProfile", "-NonInteractive", "-Command", _CIM_SCRIPT],
        timeout=25,
        cancel=cancel,
        output_limit=1024 * 1024,
    )
    if not _ok(process):
        return dict(
            status="cancelled" if process.get("cancelled") else "failed",
            process=process,
            warnings=["Windows CIM 장치 조회를 완료하지 못했습니다."],
        )
    value = json.loads(process["stdout"].lstrip("\ufeff"))
    if not isinstance(value, dict):
        raise ValueError("Windows CIM 결과가 JSON 객체가 아닙니다.")
    return {
        **value,
        "status": "partial" if value.get("errors") else "success",
        "process": process,
        "warnings": ["Windows CIM 일부 조회 실패: " + str(e) for e in value.get("errors", [])],
    }


def _nvidia_cli():
    found = shutil.which("nvidia-smi") or shutil.which("nvidia-smi.exe")
    if found:
        return found
    if _windows():
        candidates = [
            Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/nvidia-smi.exe",
            Path(os.environ.get("ProgramFiles", "C:/Program Files"))
            / "NVIDIA Corporation/NVSMI/nvidia-smi.exe",
        ]
        return next((str(path) for path in candidates if path.is_file()), None)
    return None


def _parse_nvidia(stdout):
    result = []
    for row in csv.reader(io.StringIO(stdout), skipinitialspace=True):
        if not row or not any(item.strip() for item in row):
            continue
        if len(row) != 7 or not _text(row[0]):
            raise ValueError("nvidia-smi GPU 결과의 열 구성이 올바르지 않습니다.")
        name, uuid, driver, total, used, free, usage = (value.strip() for value in row)

        def mib(value):
            number = _number(value)
            return int(number * 1024 * 1024) if number is not None else None

        result.append(
            dict(
                name=name,
                vendor="NVIDIA",
                uuid=uuid,
                driver=driver,
                vram_total_bytes=mib(total),
                vram_used_bytes=mib(used),
                vram_free_bytes=mib(free),
                usage_percent=_number(usage),
                backend="NVIDIA driver",
                source="nvidia-smi",
            )
        )
    return result


async def _nvidia_inventory(cancel):
    cli = _nvidia_cli()
    if not cli:
        return {"status": "not_installed", "gpus": []}
    process = await run_process(
        [
            cli,
            "--query-gpu=name,uuid,driver_version,memory.total,memory.used,memory.free,utilization.gpu",
            "--format=csv,noheader,nounits",
        ],
        timeout=8,
        cancel=cancel,
        output_limit=256 * 1024,
    )
    if not _ok(process):
        return dict(
            status="cancelled" if process.get("cancelled") else "failed",
            gpus=[],
            process=process,
            warnings=["NVIDIA GPU 실시간 자원을 조회하지 못했습니다."],
        )
    return dict(status="success", gpus=_parse_nvidia(process["stdout"]), process=process)


async def detect_gpu_sample(cancel=None) -> dict:
    """Short read-only GPU observation; no CIM scan or workload is invoked."""
    value, warnings = await _guard("NVIDIA GPU", lambda: _nvidia_inventory(cancel), cancel, 10)
    warnings.extend(value.get("warnings", []))
    gpus = _rows(value.get("gpus"))
    warnings.append("AMD·Intel GPU의 실시간 VRAM은 DXGI 미구현으로 미확인입니다.")
    if not gpus:
        warnings.append(
            "NVIDIA GPU 자원을 확인하지 못했습니다. GPU 부재 또는 0 사용량으로 해석하지 마세요."
        )
    status = value.get("status", "failed")
    if status not in ("cancelled", "timed_out"):
        status = (
            "success"
            if gpus and all(gpu.get("vram_total_bytes") is not None for gpu in gpus)
            else "partial"
        )
    return dict(
        collected_at=_timestamp(), status=status, gpus=gpus, warnings=list(dict.fromkeys(warnings))
    )


def _merge_gpus(cim, nvidia):
    result = [dict(row) for row in _rows(nvidia.get("gpus"))]
    unmatched = list(range(len(result)))
    for device in _rows(cim.get("gpus")):
        name = _text(device.get("Name"))
        if not name:
            continue
        match = next(
            (index for index in unmatched if result[index]["name"].casefold() == name.casefold()),
            None,
        )
        if match is not None:
            result[match]["pnp_device_id"] = device.get("PNPDeviceID")
            unmatched.remove(match)
            continue
        vendor = _text(device.get("AdapterCompatibility"))
        if not vendor:
            vendor = next(
                (v for v in ("NVIDIA", "AMD", "Intel") if v.casefold() in name.casefold()), None
            )
        result.append(
            dict(
                name=name,
                vendor=vendor,
                driver=_text(device.get("DriverVersion")),
                pnp_device_id=device.get("PNPDeviceID"),
                vram_total_bytes=None,
                vram_used_bytes=None,
                vram_free_bytes=None,
                usage_percent=None,
                backend=None,
                source="Windows CIM (identity only)",
            )
        )
    return result


def _disk_inventory(project_path, cim):
    result, warnings = [], []
    system_directory = (
        (os.environ.get("SystemRoot") or os.environ.get("WINDIR")) if _windows() else "/"
    )
    system_path = Path(system_directory).anchor if system_directory else None
    paths = (
        ("project", Path(project_path) if project_path else Path.cwd()),
        ("user_data", data_root()),
        ("temporary", Path(tempfile.gettempdir())),
        ("system", Path(system_path) if system_path else None),
    )
    volumes = {str(item.get("device_id", "")).casefold(): item for item in _rows(cim.get("disks"))}
    for role, requested in paths:
        row = dict(
            role=role,
            path=str(requested) if requested is not None else None,
            total_bytes=None,
            free_bytes=None,
            type=None,
        )
        if requested is None:
            warnings.append("Windows 시스템 디스크 경로를 확인하지 못했습니다.")
            result.append(row)
            continue
        try:
            path = requested.resolve()
            row["path"] = str(path)
            usage_path = path
            while not usage_path.exists() and usage_path != usage_path.parent:
                usage_path = usage_path.parent
            if usage_path.is_file():
                usage_path = usage_path.parent
            usage = psutil.disk_usage(str(usage_path))
            row.update(
                total_bytes=int(usage.total), free_bytes=int(usage.free), usage_path=str(usage_path)
            )
            info = volumes.get(path.drive.casefold(), {})
            row["type"] = {3: "HDD", 4: "SSD", 5: "SCM"}.get(
                _number(info.get("media_type"), integer=True)
            )
            row["filesystem"] = info.get("filesystem")
            row["model"] = info.get("model")
        except (OSError, ValueError, psutil.Error) as exc:
            warnings.append("저장소 여유 공간을 조회하지 못했습니다 (" + role + "): " + str(exc))
        result.append(row)
    return result, warnings


async def _docker_inventory(settings, cancel):
    adapter = DockerAdapter(context=settings.docker_context)
    result = {
        **await adapter.probe(),
        "cpus": None,
        "memory_bytes": None,
        "images": [],
        "resource_scope": "docker_engine",
        "warnings": [],
    }
    if not result.get("ready") or _cancelled(cancel):
        return result
    process = await adapter.call("info", "--format", "{{json .}}", timeout=10, cancel=cancel)
    result["info_process"] = process
    if _ok(process):
        try:
            info = json.loads(process["stdout"])
            result.update(
                cpus=_number(info.get("NCPU"), integer=True, positive=True),
                memory_bytes=_number(info.get("MemTotal"), integer=True, positive=True),
                os=info.get("OSType", result.get("os")),
                operating_system=info.get("OperatingSystem"),
                architecture=info.get("Architecture"),
            )
        except (ValueError, TypeError, AttributeError):
            result["warnings"].append("Docker 할당 자원 응답 형식을 확인하지 못했습니다.")
    else:
        result["warnings"].append("Docker 할당 CPU·메모리를 조회하지 못했습니다.")
    references = list(dict.fromkeys(filter(None, (settings.linux_image, settings.windows_image))))
    for reference in references:
        if _cancelled(cancel):
            break
        inspected = await adapter.call("image", "inspect", reference, timeout=8, cancel=cancel)
        image = dict(
            reference=reference, present=False, id=None, digests=[], os=None, architecture=None
        )
        if _ok(inspected):
            try:
                details = json.loads(inspected["stdout"])[0]
                image.update(
                    present=True,
                    id=details.get("Id"),
                    digests=details.get("RepoDigests") or [],
                    os=details.get("Os"),
                    architecture=details.get("Architecture"),
                    size_bytes=details.get("Size"),
                )
            except (ValueError, TypeError, KeyError, IndexError, AttributeError):
                image["error"] = "Docker 이미지 정보 형식을 확인하지 못했습니다."
        else:
            image["error"] = "이미지가 없거나 조회하지 못했습니다. 자동 빌드는 실행하지 않았습니다."
        result["images"].append(image)
    return result


async def _ollama_inventory(settings, cancel):
    client = OllamaClient(settings.ollama_url, timeout=8)
    try:
        result = {
            **await client.probe(cancel=cancel),
            "models": [],
            "running": [],
            "warnings": [],
            "inference_invoked": False,
            "local_execution_status": "not_probed",
        }
        if not result.get("connected") or _cancelled(cancel):
            return result
        for label, operation in (
            ("models", lambda: client.models(cancel=cancel)),
            ("running", lambda: client._json("GET", "/api/ps", cancel=cancel, timeout=5)),
        ):
            if _cancelled(cancel):
                break
            try:
                value = await operation()
                if label == "running":
                    if not isinstance(value.get("models"), list):
                        raise ValueError("/api/ps models 필드가 올바르지 않습니다.")
                    value = value["models"]
                result[label] = value
            except Exception as exc:
                result["warnings"].append("Ollama " + label + " 조회에 실패했습니다: " + str(exc))
        return result
    finally:
        await client.close()


def _apply_cim(host, cim):
    cpus = _rows(cim.get("cpus"))
    if cpus:
        names = list(dict.fromkeys(filter(None, (_text(row.get("Name")) for row in cpus))))
        vendors = list(
            dict.fromkeys(filter(None, (_text(row.get("Manufacturer")) for row in cpus)))
        )
        host["cpu"].update(
            name=" / ".join(names) or host["cpu"]["name"], vendor=" / ".join(vendors) or None
        )
        for key, field in (
            ("physical_cores", "NumberOfCores"),
            ("logical_cores", "NumberOfLogicalProcessors"),
        ):
            values = [_number(row.get(field), integer=True, positive=True) for row in cpus]
            if host["cpu"][key] is None and all(v is not None for v in values):
                host["cpu"][key] = sum(values)
        speeds = [_number(row.get("MaxClockSpeed"), positive=True) for row in cpus]
        if host["cpu"]["max_mhz"] is None and all(v is not None for v in speeds):
            host["cpu"]["max_mhz"] = max(speeds)
    for module in _rows(cim.get("memory_modules")):
        host["memory"]["modules"].append(
            dict(
                bank=_text(module.get("BankLabel")),
                locator=_text(module.get("DeviceLocator")),
                capacity_bytes=_number(module.get("Capacity"), integer=True, positive=True),
                speed_mhz=_number(module.get("Speed"), positive=True),
                configured_clock_mhz=_number(module.get("ConfiguredClockSpeed"), positive=True),
                manufacturer=_text(module.get("Manufacturer")),
                part_number=_text(module.get("PartNumber")),
                source="Windows CIM",
            )
        )


async def detect_hardware(settings, project_path=None, cancel=None) -> dict:
    """Return a partial-safe inventory without preparing or running workloads."""
    host = await asyncio.to_thread(_host_inventory)
    jobs = (
        ("Windows 장치", lambda: _cim_inventory(cancel), 28),
        ("NVIDIA GPU", lambda: _nvidia_inventory(cancel), 10),
        ("Docker", lambda: _docker_inventory(settings, cancel), 65),
        ("Ollama", lambda: _ollama_inventory(settings, cancel), 25),
        (
            "Ghidra·Java",
            lambda: GhidraAdapter(home=settings.ghidra_path, java_home=settings.java_home).probe(),
            18,
        ),
    )
    observations = await asyncio.gather(
        *(_guard(label, factory, cancel, timeout) for label, factory, timeout in jobs)
    )
    cim, nvidia, docker, ollama, ghidra = (item[0] for item in observations)
    warnings = list(host.pop("warnings"))
    for value, failures in observations:
        warnings.extend(failures)
        warnings.extend(value.get("warnings", []))
    _apply_cim(host, cim)
    gpus = _merge_gpus(cim, nvidia)
    if not gpus:
        warnings.append("GPU 정보를 확인하지 못했습니다. GPU가 없다는 뜻은 아닙니다.")
    elif any(gpu.get("vram_total_bytes") is None for gpu in gpus):
        warnings.append(
            "일부 GPU의 전용 VRAM을 확인하지 못했습니다. WMI AdapterRAM 추정값은 사용하지 않습니다."
        )
    if not docker.get("ready"):
        warnings.append("Docker 엔진 연결 또는 할당 자원이 준비되지 않았습니다.")
    if not ollama.get("connected"):
        warnings.append("Ollama API에 연결하지 못했습니다. 모델 추론은 실행하지 않았습니다.")
    if not ghidra.get("ready"):
        warnings.append("Ghidra·Java 준비 상태를 확인하세요. 정적 분석은 실행하지 않았습니다.")
    disks, disk_warnings = await asyncio.to_thread(_disk_inventory, project_path, cim)
    warnings.extend(disk_warnings)
    warnings = list(dict.fromkeys(warnings))
    return dict(
        schema_version="1.0",
        collected_at=_timestamp(),
        status="cancelled" if _cancelled(cancel) else ("partial" if warnings else "success"),
        **host,
        gpus=gpus,
        disks=disks,
        environment=dict(
            os=dict(
                system=platform.system(),
                release=platform.release(),
                version=platform.version(),
                architecture=platform.machine(),
            ),
            python=dict(
                version=platform.python_version(),
                executable=sys.executable,
                implementation=platform.python_implementation(),
            ),
            java_home=settings.java_home or os.environ.get("JAVA_HOME"),
            java=ghidra.get("java"),
            ghidra=ghidra,
            docker=docker,
            ollama=ollama,
        ),
        probe_status=dict(cim=cim.get("status"), nvidia=nvidia.get("status")),
        probe_details=dict(cim=cim, nvidia=nvidia),
        warnings=warnings,
    )
