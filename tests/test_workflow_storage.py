"""Shared workflow persistence and natural language command ownership."""

import asyncio
import copy
import pytest
from recheck.engine import Engine
from recheck.models import Settings
from recheck.storage import Store, new_id


def test_workflow_survives_database_reopen_without_entering_comparison_history(tmp_path):
    store = Store(tmp_path)
    record = dict(
        workflow_id=new_id('workflow'),
        status='blocked',
        source_path='project/main.c',
        tool_calls=[],
        specification={'scope': 'observed inputs'},
        approve_build=False,
    )
    store.save_workflow(record)
    reopened = Store(tmp_path)
    assert reopened.load_workflow(record['workflow_id']) == record
    assert reopened.list_workflows() == [record]
    assert reopened.list_runs() == []


@pytest.mark.parametrize(
    'workflow_id', ['../other', 'workflow_../other', 'run_' + 'a' * 32, 'workflow_' + 'a' * 31]
)
def test_workflow_path_rejects_traversal(tmp_path, workflow_id):
    with pytest.raises(ValueError):
        Store(tmp_path).workflow_dir(workflow_id)


def test_natural_command_uses_validated_agent_and_preserves_measured_metrics(tmp_path, monkeypatch):
    engine = Engine(Settings(model='fixture'), data_dir=tmp_path)
    record = dict(
        run_id=new_id('run'),
        attempt_id=new_id('attempt'),
        status='success',
        behavior={'matched': 5, 'denominator': 6},
        agent={},
        commands=[],
    )
    engine.store.save(record)
    measured = copy.deepcopy(record['behavior'])

    async def agent(run, cancel, instruction=''):
        assert instruction == 'inspect registered function evidence'
        result = {'status': 'success', 'tool_calls': [{'name': 'get_function', 'ok': True}]}
        run['agent'] = result
        return result

    monkeypatch.setattr(engine, '_agent', agent)
    monkeypatch.setattr(engine, 'report', lambda rid: tmp_path / 'report.html')
    response = asyncio.run(engine.command(record['run_id'], 'inspect registered function evidence'))
    stored = Store(tmp_path).load_run(record['run_id'])
    assert stored['behavior'] == measured and stored['commands'][0]['status'] == 'success'
    assert response['agent']['tool_calls'][0]['name'] == 'get_function'
    assert response['run_id'] == record['run_id']
