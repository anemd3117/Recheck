"""Storage integrity failures must not present unreadable results as complete."""

import json
import pytest

from recheck.storage import Store, new_id


def completed_run():
    return dict(run_id=new_id('run'), attempt_id=new_id('attempt'), status='success',
                stages={}, tests=[{'id': 'known', 'stdin': '100\n'}], evidence=[], events=[])


@pytest.mark.parametrize('damage', ['truncated', 'missing', 'wrong_identity', 'not_object'])
def test_unreadable_result_is_partial_in_history_without_rewriting_saved_data(tmp_path, damage):
    store = Store(tmp_path)
    run = completed_run()
    store.save(run)
    path = store.run_dir(run['run_id']) / 'result.json'
    if damage == 'truncated':
        path.write_bytes(b'{unfinished')
    elif damage == 'missing':
        path.unlink()
    elif damage == 'wrong_identity':
        path.write_text(json.dumps({**run, 'run_id': new_id('run')}), encoding='utf-8')
    else:
        path.write_text('[]', encoding='utf-8')
    before = path.read_bytes() if path.exists() else None

    listed = store.list_runs()[0]

    assert listed['status'] == 'partial'
    assert listed['recorded_status'] == 'success'
    assert listed['storage_error']['code'] == 'result_unreadable'
    assert listed['tests'] == [{'id': 'known', 'stdin': '100\n'}]
    assert (path.read_bytes() if path.exists() else None) == before
    with store.connect() as db:
        assert db.execute('SELECT status FROM runs').fetchone()[0] == 'success'


def test_loading_corrupt_result_gives_recovery_message_and_keeps_bytes(tmp_path):
    store = Store(tmp_path)
    run = completed_run()
    store.save(run)
    path = store.run_dir(run['run_id']) / 'result.json'
    path.write_bytes(b'{unfinished')

    with pytest.raises(ValueError, match='분석 결과.*보존'):
        store.load_run(run['run_id'])

    assert path.read_bytes() == b'{unfinished'


def test_valid_history_keeps_completed_status_and_saved_payload(tmp_path):
    store = Store(tmp_path)
    run = completed_run()
    store.save(run)
    assert store.list_runs() == [run]
    assert store.load_run(run['run_id']) == run
