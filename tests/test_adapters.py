import asyncio
import base64
import hashlib
import json
import shutil
from pathlib import Path
import pytest
from recheck.adapters import DockerAdapter, GhidraAdapter, validate_static


def process(stdout="", returncode=0, **extra):
    data = dict(
        returncode=returncode,
        stdout=stdout,
        stderr="",
        stdout_b64=base64.b64encode(stdout.encode()).decode(),
        stderr_b64="",
        error=None,
        timed_out=False,
        cancelled=False,
        truncated=False,
        duration=0.01,
    )
    data.update(extra)
    return data


def test_schema_duplicate_and_version():
    function = dict(
        id="001", name="main", address="001", blocks=[], edges=[], calls=[], mnemonics=[]
    )
    data = dict(schema_version="1.0", sha256="a" * 64, functions=[function])
    assert validate_static(data) is data
    with pytest.raises(ValueError):
        validate_static(dict(data, functions=[function, function]))
    with pytest.raises(ValueError):
        validate_static(dict(data, schema_version="2"))


def test_intentional_local_ai_service_is_not_an_abandoned_test_container():
    adapter = DockerAdapter(cli='docker')

    async def call(*args, **kwargs):
        return process(
            '\n'.join(
                json.dumps(row)
                for row in [
                    {
                        'Names': 'recheck-managed-ollama-v1',
                        'Labels': 'org.recheck.managed=true,org.recheck.component=ollama',
                    },
                    {
                        'Names': 'recheck-static-interrupted',
                        'Labels': 'org.recheck.managed=true,org.recheck.component=static',
                    },
                    {
                        'Names': 'recheck-test-interrupted',
                        'Labels': 'org.recheck.managed=true,org.recheck.run=run_example',
                    },
                ]
            )
        )

    adapter.call = call
    result = asyncio.run(adapter.leftovers())
    assert [row['Names'] for row in result['containers']] == [
        'recheck-static-interrupted',
        'recheck-test-interrupted',
    ]


def test_engine_os_blocked_without_execution(tmp_path):
    adapter = DockerAdapter(cli="docker")

    async def probe():
        return dict(ready=True, os="linux", arch="amd64", local_endpoint=True)

    adapter.probe = probe
    result = asyncio.run(
        adapter.execute(tmp_path / "absent.exe", {}, "run", "a", tmp_path, target_os="windows")
    )
    assert result["status"] == "blocked"


def test_container_evidence_before_cleanup_and_nonzero_target(tmp_path):
    binary = tmp_path / "한글 공백"
    binary.write_bytes(b"fixture only, never executed on host")
    adapter = DockerAdapter(cli="docker")
    calls = []

    async def probe():
        return dict(ready=True, os="linux", arch="amd64", local_endpoint=True)

    async def call(*args, **kwargs):
        calls.append(args)
        if args[0] == "create":
            assert "--read-only" in args and "none" in args and "ALL" in args
            return process("container-id\n")
        if args[0] == "start":
            assert kwargs["stdin"] == b"abc"
            return process("answer", returncode=7)
        if args[0] == "inspect":
            return process(
                json.dumps(
                    [
                        dict(
                            State=dict(Status="exited", ExitCode=7, OOMKilled=False, Error=""),
                            Image="sha256:image",
                        )
                    ]
                )
            )
        if args[0] == "logs":
            return process("retained logs")
        return process()

    adapter.probe, adapter.call = probe, call
    result = asyncio.run(
        adapter.execute(
            binary,
            dict(id="t1", args=["; $()"], stdin="abc", timeout_seconds=2),
            "run",
            "a",
            tmp_path / "work",
        )
    )
    assert result["infrastructure_ok"] and result["target_exit_code"] == 7
    assert [c[0] for c in calls] == ["create", "start", "inspect", "logs", "rm"]
    assert result["container_logs"]["stdout"] == "retained logs"
    assert (
        json.loads(next((tmp_path / "work").glob("recheck-*.json")).read_text(encoding="utf-8"))[
            "target_exit_code"
        ]
        == 7
    )


def test_timeout_kill_inspect_logs_cleanup(tmp_path):
    binary = tmp_path / "input"
    binary.write_bytes(b"not executable fixture")
    adapter = DockerAdapter(cli="docker")
    calls = []

    async def probe():
        return dict(ready=True, os="linux", arch="amd64", local_endpoint=True)

    async def call(*args, **kwargs):
        calls.append(args[0])
        if args[0] == "start":
            return process("prior output", returncode=-1, timed_out=True)
        if args[0] == "inspect":
            return process(
                json.dumps(
                    [
                        dict(
                            State=dict(Status="exited", ExitCode=137, OOMKilled=False, Error=""),
                            Image="i",
                        )
                    ]
                )
            )
        return process("id")

    adapter.probe, adapter.call = probe, call
    result = asyncio.run(adapter.execute(binary, {}, "run", "a", tmp_path / "w"))
    assert result["timed_out"] and not result["oom_killed"] and not result["infrastructure_ok"]
    assert calls[-4:] == ["kill", "inspect", "logs", "rm"]


def test_auxiliary_evidence_hashes_staged_bytes_when_source_changes(tmp_path, monkeypatch):
    binary = tmp_path / "fixture"
    binary.write_bytes(b"not executable")
    auxiliary = tmp_path / "source.txt"
    auxiliary.write_bytes(b"OLD")
    adapter = DockerAdapter(cli="docker")
    original_copy = shutil.copy2

    def copy_then_change_source(source, destination):
        copied = original_copy(source, destination)
        if Path(source).name == auxiliary.name:
            auxiliary.write_bytes(b"NEW")
        return copied

    async def probe():
        return dict(ready=True, os="linux", arch="amd64", local_endpoint=True)

    async def call(*args, **kwargs):
        if args[0] == "inspect":
            return process(
                json.dumps(
                    [dict(State=dict(Status="exited", ExitCode=0, OOMKilled=False, Error=""))]
                )
            )
        return process("id" if args[0] == "create" else "")

    adapter.probe, adapter.call = probe, call
    monkeypatch.setattr("recheck.adapters.shutil.copy2", copy_then_change_source)
    work = tmp_path / "work"
    result = asyncio.run(
        adapter.execute(
            binary,
            {"auxiliary_files": [{"source": str(auxiliary), "name": "input.txt"}]},
            "run",
            "a",
            work,
        )
    )
    assert result["status"] == "success"
    assert auxiliary.read_bytes() == b"NEW"
    assert (Path(result["input_staging"]["artifact"]) / "input.txt").read_bytes() == b"OLD"
    expected = [
        {
            "name": "input.txt",
            "sha256": "099d90cbee62f89e6478e153eb3240efcbe4ac2231bedc3e84549bbeaaba87e8",
        }
    ]
    assert result["actual_input"]["auxiliary_files"] == expected
    persisted = json.loads(
        (work / (result["container_name"] + ".json")).read_text(encoding="utf-8")
    )
    assert persisted["actual_input"]["auxiliary_files"] == expected


def test_ghidra_java_required_from_installation(tmp_path, monkeypatch):
    root = tmp_path / "Ghidra"
    (root / "support").mkdir(parents=True)
    (root / "Ghidra").mkdir()
    (root / "support/analyzeHeadless.bat").write_text("fixture")
    (root / "support/analyzeHeadless").write_text("fixture")
    (root / "Ghidra/application.properties").write_text(
        "application.version=12.2\napplication.java.min=25\n"
    )

    async def fake(*args, **kwargs):
        return process(stderr='openjdk version "21.0.8"')

    monkeypatch.setattr("recheck.adapters.run_process", fake)
    result = asyncio.run(GhidraAdapter(home=root).probe())
    assert result["java_required_min"] == 25 and not result["ready"]


@pytest.mark.parametrize(
    "malformed",
    [
        {"blocks": [{"id": "a", "start": "a", "end": "b", "size": "3"}]},
        {"edges": [["unknown", "x"]]},
        {"edges": [["x"]]},
        {"calls": [123]},
        {"mnemonics": [{}]},
    ],
)
def test_deep_static_schema_rejects_bad_types_references(malformed):
    function = dict(id="a", name="main", address="a", blocks=[], edges=[], calls=[], mnemonics=[])
    function.update(malformed)
    with pytest.raises(ValueError):
        validate_static(dict(schema_version="1.0", sha256="a" * 64, functions=[function]))


@pytest.mark.parametrize(
    "analysis_timeout,wrong_hash,expected",
    [(False, False, "success"), (True, False, "partial"), (False, True, "failed")],
)
def test_ghidra_postscript_completion_and_hash(
    tmp_path, monkeypatch, analysis_timeout, wrong_hash, expected
):
    binary = tmp_path / "fixture"
    binary.write_bytes(b"non-executable fixture")
    adapter = GhidraAdapter()

    async def probe():
        return dict(ready=True, launcher="fake-ghidra")

    async def fake(argv, **kwargs):
        index = argv.index("RecheckExport.java")
        digest = "a" * 64 if wrong_hash else hashlib.sha256(binary.read_bytes()).hexdigest()
        Path(argv[index + 1]).write_text(
            json.dumps(
                dict(
                    schema_version="1.0",
                    sha256=digest,
                    functions=[],
                    analysis_timed_out=analysis_timeout,
                )
            ),
            encoding="utf-8",
        )
        Path(argv[index + 2]).write_text("RECHECK_COMPLETE_1", encoding="utf-8")
        return process()

    adapter.probe = probe
    monkeypatch.setattr("recheck.adapters.run_process", fake)
    result = asyncio.run(adapter.analyze(binary, tmp_path / "w"))
    assert result["status"] == expected
    if analysis_timeout:
        assert result["complete"] and result["analysis_timed_out"]


def test_storage_failure_still_cleans_container(tmp_path, monkeypatch):
    binary = tmp_path / "fixture"
    binary.write_bytes(b"not executable")
    adapter = DockerAdapter(cli="docker")
    calls = []

    async def probe():
        return dict(ready=True, os="linux", arch="amd64", local_endpoint=True)

    async def call(*args, **kwargs):
        calls.append(args[0])
        if args[0] == "inspect":
            return process(
                json.dumps(
                    [
                        dict(
                            State=dict(Status="exited", ExitCode=0, OOMKilled=False, Error=""),
                            Image="i",
                        )
                    ]
                )
            )
        return process("id")

    def replace(*args):
        raise OSError("fixture disk full")

    adapter.probe, adapter.call = probe, call
    monkeypatch.setattr("recheck.adapters.os.replace", replace)
    result = asyncio.run(adapter.execute(binary, {}, "run", "a", tmp_path / "w"))
    assert calls[-1] == "rm" and result["storage_errors"] and result["status"] == "failed"


@pytest.mark.parametrize("cancel_stage", ["create", "start"])
def test_task_cancel_kills_container_and_collects_before_removal(tmp_path, cancel_stage):
    binary = tmp_path / "fixture"
    binary.write_bytes(b"not executable")
    adapter = DockerAdapter(cli="docker")
    calls = []

    async def probe():
        return dict(ready=True, os="linux", arch="amd64", local_endpoint=True)

    async def call(*args, **kwargs):
        calls.append(args[0])
        if args[0] == cancel_stage:
            raise asyncio.CancelledError()
        return process("id")

    adapter.probe, adapter.call = probe, call
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(adapter.execute(binary, {}, "run", "a", tmp_path / "w"))
    expected = (
        ["create"]
        + (["start"] if cancel_stage == "start" else [])
        + ["kill", "inspect", "logs", "rm"]
    )
    assert calls == expected


def test_image_reuse_requires_definition_hash(tmp_path):
    folder = tmp_path / "docker/linux"
    folder.mkdir(parents=True)
    (folder / "Dockerfile").write_text("FROM ubuntu:24.04\n")
    adapter = DockerAdapter(cli="docker", resources=tmp_path, auto_prepare=True)
    calls = []

    async def probe():
        return dict(ready=True, os="linux", arch="amd64")

    async def call(*args, **kwargs):
        calls.append(args)
        if args[0] == "image":
            return process(
                json.dumps(
                    [dict(Id="sha256:x", Os="linux", Architecture="amd64", Config=dict(Labels={}))]
                )
            )
        return process()

    adapter.probe, adapter.call = probe, call
    result = asyncio.run(adapter.prepare_image("linux"))
    assert result["status"] == "success" and not result["reused"]
    assert any(
        args[0] == "build" and any("definition_sha256=" in a for a in args) for args in calls
    )
