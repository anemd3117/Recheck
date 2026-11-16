"""A requested reinterpretation can fail without destroying valid analysis results."""
import asyncio
import copy
import threading

import pytest
from recheck.engine import Engine
from recheck.models import Settings


@pytest.mark.parametrize('outcome', ['failed', 'success', 'cancelled'])
def test_reinterpretation_records_requested_operation_separately_from_analysis(tmp_path, monkeypatch, outcome):
    engine = Engine(Settings(), tmp_path)
    run = dict(run_id='run_' + 'a' * 32, attempt_id='attempt_original', mode='static',
               analysis_mode='static', ai_mode='off', status='success',
               files={}, tests=[], static={}, structure={}, dynamic=[], behavior={},
               llm={}, diagnostics=[], evidence=[], events=[],
               analysis_status={'static': 'success', 'dynamic': 'not_requested', 'ai': 'not_requested'},
               stages={name: {'status': 'success'} for name in
                       ['metadata', 'static_original', 'static_reimplementation', 'structure']})
    original = copy.deepcopy(run)
    engine.store.save(run)

    async def interpret(record, cancel):
        # External inference boundary only; real _step/storage/reporting stay in use.
        record['llm'] = dict(status=outcome, error_code='evidence_context' if outcome == 'failed' else '')
        return record['llm']
    monkeypatch.setattr(engine, '_interpret', interpret)
    cancel = threading.Event()
    if outcome == 'cancelled':
        cancel.set()
    result = asyncio.run(engine.reinterpret(run['run_id'], cancel))
    assert result['status'] == original['status']
    assert result['analysis_status'] == original['analysis_status']
    assert result['last_operation']['kind'] == 'reinterpret'
    assert result['last_operation']['status'] == outcome
    assert result['last_operation']['analysis_status'] == 'success'
    assert result['operations'][-1] == result['last_operation']
    assert engine.store.load_run(run['run_id'])['last_operation'] == result['last_operation']
