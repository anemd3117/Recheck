"""Checkpoint registration and compiler-adapter fixtures; no external tools run."""

import asyncio
import base64
import json
import struct
import threading

import pytest

from recheck.build_adapter import CompilerAdapter
from recheck.models import Settings
from recheck.reconstruction import ReconstructionService


def elf():
    data = bytearray(64)
    data[:7] = b'\x7fELF\x02\x01\x01'
    struct.pack_into('<HH', data, 16, 2, 62)
    return bytes(data)


@pytest.fixture
def original(tmp_path):
    path = tmp_path / 'original'
    path.write_bytes(elf())
    return path


def test_checkpoint_preserves_contract_and_explicitly_blocks_unimplemented_pipeline(
    tmp_path, original
):
    service = ReconstructionService(Settings(), tmp_path / 'data')
    result = asyncio.run(service.run(original, [{'id': 'smoke'}], '<script>requirement</script>'))
    assert result['status'] == 'blocked' and result['implementation_status'] == 'NOT IMPLEMENTED'
    assert (
        result['comparison_run_id'] is None
        and result['builds'] == []
        and result['tool_calls'] == []
    )
    assert (
        service.store.load_workflow(result['workflow_id'])['original']['sha256']
        == result['original']['sha256']
    )
    from pathlib import Path

    report = Path(result['report']).read_text(encoding='utf8')
    assert '&lt;script&gt;' in report and '<script>requirement</script>' not in report
    assert original.read_bytes() == elf()


def test_modernization_copies_source_and_never_changes_selected_file(tmp_path, original):
    source = tmp_path / 'user.c'
    source.write_text('int main(void){return 0;}')
    service = ReconstructionService(Settings(), tmp_path / 'data')
    result = asyncio.run(
        service.run(
            original,
            [{'id': 'smoke'}],
            '출력 보존',
            mode='modernization',
            source_path=source,
            approve_build=True,
        )
    )
    from pathlib import Path

    assert source.read_text() == 'int main(void){return 0;}'
    assert Path(result['source_path']).read_bytes() == source.read_bytes()
    assert Path(result['source_path']) != source and result['status'] == 'blocked'
    assert result['selected_source']['status'] == 'user_copy_not_generated'


def test_checkpoint_cancellation_is_saved_without_external_work(tmp_path, original):
    event = threading.Event()
    event.set()
    service = ReconstructionService(Settings(), tmp_path / 'data')
    result = asyncio.run(service.run(original, [{'id': 'smoke'}], '', cancel=event))
    assert result['status'] == 'cancelled' and 'original' not in result
    assert service.store.load_workflow(result['workflow_id'])['status'] == 'cancelled'


@pytest.mark.parametrize(
    'kwargs',
    [{'max_repairs': 3}, {'approve_build': 'yes'}, {'mode': 'shell'}, {'language': 'python'}],
)
def test_checkpoint_rejects_invalid_execution_policy(tmp_path, original, kwargs):
    result = asyncio.run(
        ReconstructionService(Settings(), tmp_path / 'data').run(
            original, [{'id': 'smoke'}], '', **kwargs
        )
    )
    assert result['status'] == 'failed' and result['builds'] == []


def process(stdout='', returncode=0, **kwargs):
    return dict(
        stdout=stdout,
        stderr='',
        returncode=returncode,
        error=None,
        timed_out=False,
        cancelled=False,
        truncated=False,
        **kwargs,
    )


class DockerFixture:
    def __init__(
        self, workflow_id, compiler_failure=False, wrong_owner=False, cleanup_failure=False
    ):
        self.workflow_id = workflow_id
        self.calls = []
        self.compiler_failure = compiler_failure
        self.wrong_owner = wrong_owner
        self.cleanup_failure = cleanup_failure

    async def probe(self):
        return dict(ready=True, os='linux', arch='amd64', local_endpoint=True)

    async def call(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if args[:2] == ('image', 'inspect'):
            return process(
                json.dumps(
                    [
                        dict(
                            Id='sha256:fixture',
                            Os='linux',
                            Architecture='amd64',
                            Config={'Labels': {}},
                            RepoDigests=[],
                        )
                    ]
                )
            )
        if args[0] == 'create':
            return process('f' * 64)
        if args[0] == 'start':
            result = process(
                returncode=1 if self.compiler_failure else 0,
                stdout_b64=base64.b64encode(elf()).decode(),
            )
            result['stderr'] = (
                'gcc fixture\nintentional compile error'
                if self.compiler_failure
                else 'gcc fixture\n'
            )
            return result
        if args[0] == 'inspect':
            return process(
                json.dumps(
                    [
                        dict(
                            State=dict(
                                ExitCode=1 if self.compiler_failure else 0,
                                OOMKilled=False,
                                Running=False,
                            ),
                            Config={
                                'Labels': {
                                    'org.recheck.workflow': (
                                        'other' if self.wrong_owner else self.workflow_id
                                    ),
                                    'org.recheck.purpose': 'generated-build',
                                }
                            },
                        )
                    ]
                )
            )
        if args[0] == 'rm':
            return process(returncode=1 if self.cleanup_failure else 0)
        raise AssertionError(args)


def test_compiler_fixed_argv_and_isolation_are_fixture_tested(tmp_path):
    workflow = 'workflow_' + 'a' * 32
    source = tmp_path / 'main.c'
    source.write_text('int main(void){return 0;} /* $(never-shell) */')
    adapter = CompilerAdapter(Settings())
    adapter.docker = DockerFixture(workflow)
    result = asyncio.run(adapter.compile(source, tmp_path / 'output', workflow, 1))
    assert result['status'] == 'success' and result['binary']['format'] == 'ELF'
    creation = next(args for args, kw in adapter.docker.calls if args[0] == 'create')
    assert '--read-only' in creation and '--cap-drop' in creation and '--mount' not in creation
    assert creation[creation.index('--network') + 1] == 'none'
    assert creation[creation.index('--user') + 1] == '65534:65534'
    assert all('$(never-shell)' not in value for value in creation)
    start = next(kw for args, kw in adapter.docker.calls if args[0] == 'start')
    assert start['stdin'] == source.read_bytes()


@pytest.mark.parametrize(
    'kwargs,expected',
    [
        ({'compiler_failure': True, 'cleanup_failure': True}, 'failed'),
        ({'wrong_owner': True}, 'partial'),
    ],
)
def test_compiler_failure_and_cleanup_are_separate_and_foreign_resources_preserved(
    tmp_path, kwargs, expected
):
    workflow = 'workflow_' + 'b' * 32
    source = tmp_path / 'main.c'
    source.write_text('int main(void){return 0;}')
    adapter = CompilerAdapter(Settings())
    adapter.docker = DockerFixture(workflow, **kwargs)
    result = asyncio.run(adapter.compile(source, tmp_path / 'output', workflow, 1))
    assert result['status'] == expected and result['cleanup_errors']
    if kwargs.get('compiler_failure'):
        assert '컴파일에 실패' in result['error']
    if kwargs.get('wrong_owner'):
        assert not any(args[0] == 'rm' for args, kw in adapter.docker.calls)
