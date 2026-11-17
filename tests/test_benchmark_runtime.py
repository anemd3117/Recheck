"""Benchmark startup reuses the installed managed execution image."""

import asyncio
import json
from types import SimpleNamespace

from recheck.adapters import DockerAdapter
from recheck import managed_runtime
from recheck.models import Settings
from scripts import run_benchmark


def test_benchmark_run_uses_managed_image_when_legacy_image_is_not_installed(
    tmp_path, monkeypatch
):
    image_id = 'sha256:' + '7' * 64
    settings = Settings(model='benchmark:model', linux_image='legacy-image-not-installed')

    class PreparedRuntime:
        def __init__(self, *args, **kwargs):
            pass

        def image_specifications(self):
            return {'images': {'execution': {
                'image_id': image_id, 'platform': 'linux/amd64', 'ref': 'managed:1'
            }}}

        async def probe(self):
            return {'images': {'execution': {'ready': True, 'image_id': image_id}}}

    async def local_engine(self):
        return dict(ready=True, local_endpoint=True, os='linux', arch='amd64')

    async def inspect_image(self, *arguments, **kwargs):
        if arguments[:2] != ('image', 'inspect'):
            raise AssertionError('Benchmark startup must not build or download an image')
        if arguments[2] != image_id:
            return dict(returncode=1, stdout='', stderr='legacy image is absent')
        return dict(returncode=0, stdout=json.dumps([{
            'Id': image_id, 'Os': 'linux', 'Architecture': 'amd64',
            'Config': {'Labels': {'org.recheck.component': 'execution'}},
        }]))

    class LocalModel:
        def __init__(self, *args):
            pass

        async def probe(self, cancel):
            return {'ready': True}

        async def models(self, cancel):
            return [{'name': 'benchmark:model', 'digest': 'model-digest'}]

        async def local_metadata(self, model, cancel):
            return {}

        async def close(self):
            pass

    # Exercise the real run orchestration and real execution adapters. Only
    # Docker, installed-runtime inventory, model HTTP and corpus preparation are
    # controlled; this startup case deliberately performs no target execution.
    monkeypatch.setattr(managed_runtime, 'ManagedRuntime', PreparedRuntime)
    monkeypatch.setattr(DockerAdapter, 'probe', local_engine)
    monkeypatch.setattr(DockerAdapter, 'call', inspect_image)
    monkeypatch.setattr(run_benchmark, 'load_settings', lambda: settings.model_copy(deep=True))
    monkeypatch.setattr(run_benchmark, 'prepare', lambda output, args, current: {
        'benchmark_id': 'controlled-startup', 'pairs': [], 'settings': current.model_dump()
    })
    monkeypatch.setattr(run_benchmark, 'OllamaClient', LocalModel)
    monkeypatch.setattr(run_benchmark.signal, 'signal', lambda *args: None)
    args = SimpleNamespace(
        output=str(tmp_path / 'benchmark'), resume=False, model='benchmark:model',
        ghidra=None, llm_timeout=30, num_ctx=4096, num_predict=256, prepare_only=False,
    )

    asyncio.run(run_benchmark.run(args))

    receipt = json.loads((tmp_path / 'benchmark/runtime-image.json').read_text(encoding='utf-8'))
    assert receipt['status'] == 'success'
    assert receipt['managed'] is True
    assert receipt['image_id'] == image_id
    assert receipt['build'] is None
    assert json.loads((tmp_path / 'benchmark/results.json').read_text(encoding='utf-8'))[
        'status'
    ] == 'PARTIAL'
