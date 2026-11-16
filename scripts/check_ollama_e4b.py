"""Actual local model validation; does not execute a target binary or pull a model."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from recheck.engine import Engine
from recheck.models import load_settings, save_settings, data_root, atomic_json
from recheck.ollama import OllamaClient
from recheck.storage import new_id, now


async def main():
    output = Path('docs/ollama-e4b-live.json')
    data = {
        'validation_kind': 'actual_tool',
        'created': now(),
        'requested_model': 'gemma4:e4b',
        'model_download_performed': False,
        'steps': [],
    }
    client = OllamaClient(timeout=300)

    def checkpoint(step, **values):
        data.update(values)
        data['steps'].append({'step': step, 'at': now()})
        atomic_json(output, data)
        print(step, flush=True)

    try:
        server = await client.probe()
        models = await client.models()
        selected = next(m for m in models if m['name'] == 'gemma4:e4b')
        checkpoint(
            'installed_exact_tag',
            server=server,
            models=models,
            selected=selected,
            metadata=await client.local_metadata('gemma4:e4b'),
        )
        caps = await client.capabilities('gemma4:e4b')
        checkpoint('capabilities_complete', capabilities=caps)
        settings = load_settings()
        settings.model = 'gemma4:e4b'
        settings.num_ctx = 8192
        settings.num_predict = 2048
        settings.llm_timeout = 300
        settings.capabilities = caps
        # Test the production structured interpretation and validator on real server
        # metadata only. No binary behavior or comparison values are fabricated.
        engine = Engine(settings)
        record = dict(
            schema_version='1.0',
            run_id=new_id('run'),
            attempt_id=new_id('attempt'),
            status='partial',
            created=now(),
            mode='environment_validation',
            files={},
            tests=[],
            settings=settings.model_dump(),
            stages={},
            static={},
            structure={},
            dynamic=[],
            behavior={},
            evidence=[],
            diagnostics=[],
            llm={},
            agent={},
            attempts=[],
            events=[],
        )
        engine._evidence(
            record,
            'environment',
            'both',
            {
                'ollama_server': server,
                'selected_model': selected,
                'local_model_metadata': {
                    k: v
                    for k, v in data['metadata'].items()
                    if k in ['details', 'capabilities', 'requires']
                },
                'scope': 'Only Ollama environment connectivity and features; no target binaries or measured behavior available.',
            },
            tool='Ollama native HTTP',
            source='/api/version, /api/tags, /api/show',
        )
        result = await engine._interpret(
            record,
            None,
            question='실제 연결된 Ollama 환경과 선택 모델의 준비 상태를 한국어로 설명하세요. 비교 바이너리나 동작 실측값은 아직 없습니다. 존재하지 않는 함수나 테스트를 제시하지 마세요.',
        )
        engine.store.save(record)
        checkpoint(
            'production_interpretation_complete',
            interpretation=result,
            interpretation_run_id=record['run_id'],
            interpretation_result_path=str(engine.store.run_dir(record['run_id']) / 'result.json'),
        )
        # Refresh the actual local load at the production context size.
        checkpoint(
            'execution_verified',
            execution=client.last_execution,
            production_execution=result.get('execution'),
        )
        latest = load_settings()
        latest.model = settings.model
        latest.num_ctx = settings.num_ctx
        latest.num_predict = settings.num_predict
        latest.llm_timeout = settings.llm_timeout
        latest.capabilities = caps
        save_settings(latest)
        checkpoint(
            'settings_saved',
            settings_path=str(data_root() / 'settings.json'),
            status=(
                'success'
                if caps.get('basic')
                and caps.get('json_schema')
                and caps.get('tools')
                and result.get('status') == 'success'
                else 'partial'
            ),
        )
    except Exception as exc:
        checkpoint(
            'failure',
            status='failed',
            error={'code': getattr(exc, 'code', 'exception'), 'message': str(exc)},
        )
        raise
    finally:
        await client.close()


if __name__ == '__main__':
    asyncio.run(main())
