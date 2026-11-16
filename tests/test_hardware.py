"""Hardware inventory fixtures. No live services or workloads are started."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
import threading

import pytest

from recheck import hardware
from recheck.models import Settings


def process(stdout="", **extra):
    return {
        **dict(
            returncode=0,
            stdout=stdout,
            stderr="",
            error=None,
            timed_out=False,
            cancelled=False,
            truncated=False,
        ),
        **extra,
    }


def host():
    return dict(
        cpu=dict(
            name="fixture",
            vendor=None,
            physical_cores=4,
            logical_cores=8,
            usable_cores=8,
            usage_percent=12.0,
            base_mhz=None,
            max_mhz=4200.0,
            architecture="AMD64",
        ),
        memory=dict(
            total_bytes=16000,
            available_bytes=8000,
            used_bytes=8000,
            percent=50.0,
            safe_budget_bytes=5600,
            modules=[],
        ),
        warnings=[],
        resource_sample={},
    )


def stub_inventory(monkeypatch):
    monkeypatch.setattr(hardware, "_host_inventory", host)
    monkeypatch.setattr(hardware, "_disk_inventory", lambda *args: ([], []))

    async def cim(cancel):
        return dict(status="success", cpus=[], memory_modules=[], gpus=[], disks=[])

    async def nvidia(cancel):
        return dict(status="not_installed", gpus=[])

    async def docker(settings, cancel):
        return dict(ready=True, cpus=2, memory_bytes=4000, images=[])

    async def ollama(settings, cancel):
        return dict(connected=True, version="fixture", models=[], running=[])

    class Ghidra:
        def __init__(self, **kwargs):
            pass

        async def probe(self):
            return dict(ready=True, java={"stderr": "fixture Java"})

    for key, value in (
        ("_cim_inventory", cim),
        ("_nvidia_inventory", nvidia),
        ("_docker_inventory", docker),
        ("_ollama_inventory", ollama),
        ("GhidraAdapter", Ghidra),
    ):
        monkeypatch.setattr(hardware, key, value)


def test_nvidia_mib_conversion_unknown_metrics_and_multiple_cards():
    gpus = hardware._parse_nvidia(
        '"NVIDIA fixture, one", GPU-1, 570.1, 24576, 1024.5, 23551.5, 17\n'
        'NVIDIA fixture two, GPU-2, 570.1, N/A, [Not Supported], N/A, N/A\n'
    )
    assert gpus[0]["name"] == "NVIDIA fixture, one"
    assert gpus[0]["vram_total_bytes"] == 24 * 1024**3
    assert gpus[0]["vram_used_bytes"] == int(1024.5 * 1024**2)
    assert gpus[0]["usage_percent"] == 17
    assert gpus[1]["vram_total_bytes"] is None
    assert gpus[1]["vram_used_bytes"] is None
    assert gpus[1]["usage_percent"] is None


@pytest.mark.parametrize("value", [None, True, "N/A", "NaN", "inf", -1, "-0.5"])
def test_unknown_or_invalid_counter_is_not_zero(value):
    assert hardware._number(value) is None


def test_gpu_identity_does_not_use_unreliable_adapter_ram():
    cim = dict(
        gpus=[
            dict(
                Name="AMD fixture",
                AdapterCompatibility="AMD",
                DriverVersion="1",
                AdapterRAM=4294967295,
            ),
            dict(Name="Intel fixture", AdapterCompatibility="Intel", AdapterRAM=1024),
        ]
    )
    gpus = hardware._merge_gpus(cim, {})
    assert len(gpus) == 2
    assert all(gpu["vram_total_bytes"] is None for gpu in gpus)
    assert all(gpu["vram_free_bytes"] is None for gpu in gpus)
    # The Windows query must not request that property in the first place.
    assert "AdapterRAM" not in hardware._CIM_SCRIPT


def test_gpu_merge_preserves_identical_multiple_devices_without_duplicates():
    nvidia = dict(
        gpus=hardware._parse_nvidia(
            'NVIDIA fixture, GPU-1, 1, 4096, 1, 4095, 0\n'
            'NVIDIA fixture, GPU-2, 1, 4096, 2, 4094, 0\n'
        )
    )
    cim = dict(
        gpus=[
            dict(Name="NVIDIA fixture", PNPDeviceID="id-1"),
            dict(Name="NVIDIA fixture", PNPDeviceID="id-2"),
        ]
    )
    gpus = hardware._merge_gpus(cim, nvidia)
    assert len(gpus) == 2
    assert [g["pnp_device_id"] for g in gpus] == ["id-1", "id-2"]
    assert gpus[1]["vram_used_bytes"] == 2 * 1024**2


def test_cpu_and_module_metadata_do_not_invent_nominal_frequency():
    value = host()
    cim = dict(
        cpus=[dict(Name="실제 CPU 이름", Manufacturer="fixture vendor", MaxClockSpeed=5000)],
        memory_modules=[
            dict(
                Capacity="17179869184",
                Speed=5600,
                ConfiguredClockSpeed=5200,
                DeviceLocator="한글 슬롯",
                Manufacturer="fixture",
            )
        ],
    )
    hardware._apply_cim(value, cim)
    assert value["cpu"]["name"] == "실제 CPU 이름"
    assert value["cpu"]["base_mhz"] is None
    assert value["cpu"]["max_mhz"] == 4200  # Already measured host value stays intact.
    assert value["memory"]["modules"][0]["capacity_bytes"] == 16 * 1024**3
    assert value["memory"]["modules"][0]["configured_clock_mhz"] == 5200


def test_resource_sample_measures_cpu_and_isolates_ram_failure(monkeypatch):
    intervals = []
    monkeypatch.setattr(
        hardware.psutil, "cpu_percent", lambda interval: intervals.append(interval) or 25.0
    )

    def fail():
        raise OSError("fixture denied")

    monkeypatch.setattr(hardware.psutil, "virtual_memory", fail)
    result = hardware.detect_resource_sample()
    assert intervals == [0.1]
    assert result["cpu_usage_percent"] == 25.0
    assert result["memory_available_bytes"] is None
    assert "메모리" in result["warnings"][0]


def test_ram_budget_is_explicit_recommendation_and_affinity_is_separate(monkeypatch):
    monkeypatch.setattr(
        hardware,
        "detect_resource_sample",
        lambda: dict(
            cpu_usage_percent=0.0,
            memory_total_bytes=10000,
            memory_available_bytes=9000,
            memory_used_bytes=1000,
            memory_percent=10.0,
            warnings=[],
        ),
    )
    monkeypatch.setattr(hardware.psutil, "cpu_count", lambda logical: 16 if logical else 8)
    monkeypatch.setattr(
        hardware.psutil, "Process", lambda: SimpleNamespace(cpu_affinity=lambda: [0, 1])
    )
    monkeypatch.setattr(
        hardware.psutil, "cpu_freq", lambda: SimpleNamespace(current=3000, min=800, max=4200)
    )
    result = hardware._host_inventory()
    assert result["cpu"]["usable_cores"] == 2
    assert result["cpu"]["logical_cores"] == 16
    assert result["cpu"]["base_mhz"] is None
    assert result["memory"]["safe_budget_bytes"] == 6000
    assert "권장" in result["memory"]["safe_budget_policy"]


def test_cim_uses_utf8_fixed_script_and_bounded_structured_argv(monkeypatch):
    monkeypatch.setattr(hardware, "_windows", lambda: True)
    monkeypatch.setattr(hardware.shutil, "which", lambda name: "powershell.exe")

    async def execute(argv, **kwargs):
        assert argv[:4] == ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command"]
        assert argv[4].startswith("$ErrorActionPreference = 'Stop'")
        assert "UTF8Encoding" in argv[4]
        assert "Get-CimInstance" in argv[4]
        assert "한글 경로" not in argv[4]
        assert kwargs["timeout"] == 25
        return process('\ufeff{"cpus":[{"Name":"한글 CPU"}],"errors":[]}')

    monkeypatch.setattr(hardware, "run_process", execute)
    result = asyncio.run(hardware._cim_inventory(None))
    assert result["status"] == "success"
    assert result["cpus"][0]["Name"] == "한글 CPU"


def test_docker_uses_engine_resources_and_never_prepares_images(monkeypatch):
    calls = []

    class Docker:
        def __init__(self, context):
            assert context == "fixture-context"

        async def probe(self):
            return dict(ready=True, context="fixture-context", os="linux", local_endpoint=True)

        async def call(self, *args, **kwargs):
            calls.append(args)
            assert args[0] in ("info", "image")
            if args[0] == "info":
                return process('{"NCPU":2,"MemTotal":1073741824,"OSType":"linux"}')
            if args[-1] == "linux:fixture":
                return process(
                    '[{"Id":"sha256:fixture","RepoDigests":[],"Os":"linux","Architecture":"amd64"}]'
                )
            return process("", returncode=1)  # One optional image is missing.

    monkeypatch.setattr(hardware, "DockerAdapter", Docker)
    settings = Settings(
        docker_context="fixture-context",
        linux_image="linux:fixture",
        windows_image="windows:fixture",
    )
    result = asyncio.run(hardware._docker_inventory(settings, None))
    assert result["cpus"] == 2
    assert result["memory_bytes"] == 1024**3
    assert result["resource_scope"] == "docker_engine"
    assert result["images"][0]["present"]
    assert not result["images"][1]["present"]
    assert len(calls) == 3


def test_docker_invalid_info_remains_unknown(monkeypatch):
    class Docker:
        def __init__(self, **kwargs):
            pass

        async def probe(self):
            return dict(ready=True)

        async def call(self, *args, **kwargs):
            return process("not json")

    monkeypatch.setattr(hardware, "DockerAdapter", Docker)
    result = asyncio.run(
        hardware._docker_inventory(Settings(linux_image="", windows_image=""), None)
    )
    assert result["cpus"] is None and result["memory_bytes"] is None
    assert result["warnings"]


def test_ollama_inventory_only_queries_readonly_endpoints_and_retains_partial_results(monkeypatch):
    calls = []

    class Ollama:
        def __init__(self, *args, **kwargs):
            pass

        async def probe(self, **kwargs):
            calls.append("version")
            return dict(connected=True, version="fixture", url="http://127.0.0.1:11434")

        async def models(self, **kwargs):
            calls.append("tags")
            return [{"name": "fixture:local", "local_verified": False}]

        async def _json(self, method, path, **kwargs):
            calls.append((method, path))
            assert (method, path) == ("GET", "/api/ps")
            raise RuntimeError("fixture unsupported ps")

        async def close(self):
            calls.append("close")

    monkeypatch.setattr(hardware, "OllamaClient", Ollama)
    result = asyncio.run(hardware._ollama_inventory(Settings(), None))
    assert result["connected"]
    assert result["models"][0]["name"] == "fixture:local"
    assert result["running"] == [] and result["warnings"]
    assert result["local_execution_status"] == "not_probed"
    assert result["inference_invoked"] is False
    assert calls == ["version", "tags", ("GET", "/api/ps"), "close"]


def test_component_failure_preserves_other_inventory(monkeypatch):
    stub_inventory(monkeypatch)

    async def broken(cancel):
        raise ValueError("fixture bad CIM JSON")

    monkeypatch.setattr(hardware, "_cim_inventory", broken)
    result = asyncio.run(hardware.detect_hardware(Settings()))
    assert result["schema_version"] == "1.0"
    assert result["cpu"]["logical_cores"] == 8
    assert result["environment"]["docker"]["cpus"] == 2
    assert result["environment"]["ollama"]["connected"]
    assert result["environment"]["ghidra"]["ready"]
    assert result["status"] == "partial"
    assert any("fixture bad CIM JSON" in warning for warning in result["warnings"])


def test_pre_cancelled_inventory_never_calls_external_probes(monkeypatch):
    stub_inventory(monkeypatch)

    async def forbidden(*args, **kwargs):
        raise AssertionError("cancelled probe must not start")

    monkeypatch.setattr(hardware, "_cim_inventory", forbidden)
    monkeypatch.setattr(hardware, "_nvidia_inventory", forbidden)
    monkeypatch.setattr(hardware, "_docker_inventory", forbidden)
    monkeypatch.setattr(hardware, "_ollama_inventory", forbidden)
    cancel = threading.Event()
    cancel.set()
    result = asyncio.run(hardware.detect_hardware(Settings(), cancel=cancel))
    assert result["status"] == "cancelled"
    assert result["cpu"]["logical_cores"] == 8
    assert result["environment"]["docker"]["status"] == "cancelled"


def test_guard_cancels_timed_out_work_and_retains_other_success():
    cleaned_up = []

    async def hangs():
        try:
            await asyncio.Event().wait()
        finally:
            cleaned_up.append(True)

    async def ready():
        return {"ready": True}

    async def scenario():
        return await asyncio.gather(
            hardware._guard("fixture", hangs, None, 0.02),
            hardware._guard("ready", ready, None, 0.1),
        )

    bad, good = asyncio.run(scenario())
    assert bad[0]["status"] == "timed_out"
    assert good[0]["ready"]
    assert cleaned_up == [True]


def test_guard_user_cancellation_stops_pending_work():
    cancel = threading.Event()
    cleaned_up = []

    async def pending():
        try:
            cancel.set()
            await asyncio.Event().wait()
        finally:
            cleaned_up.append(True)

    result, warnings = asyncio.run(hardware._guard("fixture", pending, cancel, 1))
    assert result["status"] == "cancelled" and warnings
    assert cleaned_up == [True]


def test_guard_also_isolates_synchronous_adapter_setup_failure():
    def broken_factory():
        raise OSError("fixture cannot inspect installation directory")

    value, warnings = asyncio.run(hardware._guard("fixture", broken_factory, None, 1))
    assert value["status"] == "failed"
    assert "fixture cannot inspect" in value["error"]
    assert warnings


def test_disk_uses_existing_parent_without_creating_project_directory(tmp_path, monkeypatch):
    requested = tmp_path / "한글 경로" / "not-created"
    monkeypatch.setattr(hardware, "data_root", lambda: requested / "user-data")
    rows, warnings = hardware._disk_inventory(requested, {})
    assert rows[0]["path"] == str(requested)
    assert rows[0]["usage_path"] == str(tmp_path)
    assert rows[0]["total_bytes"] > 0 and rows[0]["free_bytes"] > 0
    assert rows[0]["type"] is None
    assert not requested.exists()
    assert not warnings


def test_disk_inventory_includes_system_volume_independent_of_project(tmp_path, monkeypatch):
    monkeypatch.setattr(hardware, '_windows', lambda: True)
    monkeypatch.setenv('SystemRoot', str(tmp_path / 'Windows'))
    rows, warnings = hardware._disk_inventory(tmp_path / 'project', {})
    system = next(row for row in rows if row['role'] == 'system')
    assert system['path'] == str(Path(tmp_path.anchor))
    assert system['total_bytes'] > 0 and system['free_bytes'] > 0
    assert not warnings


def test_unknown_system_volume_stays_unknown_without_guessing_project_drive(tmp_path, monkeypatch):
    monkeypatch.setattr(hardware, '_windows', lambda: True)
    monkeypatch.delenv('SystemRoot', raising=False)
    monkeypatch.delenv('WINDIR', raising=False)
    rows, warnings = hardware._disk_inventory(tmp_path, {})
    system = next(row for row in rows if row['role'] == 'system')
    assert system['path'] is None and system['total_bytes'] is None and system['free_bytes'] is None
    assert any('시스템 디스크' in warning for warning in warnings)


def test_gpu_snapshot_preserves_measured_counters_and_declares_dxgi_limit(monkeypatch):
    async def nvidia(cancel):
        return dict(
            status="success",
            gpus=hardware._parse_nvidia("NVIDIA fixture, GPU-1, 1, 4096, 256, 3840, 3\n"),
        )

    monkeypatch.setattr(hardware, "_nvidia_inventory", nvidia)
    result = asyncio.run(hardware.detect_gpu_sample())
    assert result["status"] == "success"
    assert result["gpus"][0]["vram_used_bytes"] == 256 * 1024**2
    assert result["collected_at"]
    assert any("DXGI" in warning for warning in result["warnings"])


def test_missing_nvidia_snapshot_is_unknown_not_zero(monkeypatch):
    async def nvidia(cancel):
        return dict(status="not_installed", gpus=[])

    monkeypatch.setattr(hardware, "_nvidia_inventory", nvidia)
    result = asyncio.run(hardware.detect_gpu_sample())
    assert result["status"] == "partial"
    assert result["gpus"] == []
    assert any("0 사용량" in warning for warning in result["warnings"])
