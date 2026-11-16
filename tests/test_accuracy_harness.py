"""The accuracy report must expose wrong verdicts and unmeasured inputs."""

import importlib.util
import pytest


def harness():
    spec = importlib.util.find_spec('scripts.evaluate_accuracy')
    assert spec is not None, 'A reproducible accuracy evaluation harness is required'
    from scripts.evaluate_accuracy import grade_pair
    return grade_pair


def observation(stdout='5\n', code=0, **changes):
    import base64
    row = dict(status='success', infrastructure_ok=True, stdout=stdout, stderr='',
               stdout_b64=base64.b64encode(stdout.encode()).decode(), stderr_b64='',
               exit_code=code)
    row.update(changes)
    return row


def test_known_mutation_is_detected_and_oracle_outputs_are_checked():
    grade = harness()
    row = grade({'stdout': '5\n', 'stderr': '', 'exit_code': 0},
                {'stdout': '6\n', 'stderr': '', 'exit_code': 0},
                observation(), observation('6\n'))
    assert row['expected_match'] is False
    assert row['actual_match'] is False
    assert row['oracle_correct'] is True
    assert row['false_positive'] is False and row['false_negative'] is False


def test_unmeasured_timeout_is_not_scored_as_a_detected_mutation():
    grade = harness()
    row = grade({'stdout': '5\n', 'stderr': '', 'exit_code': 0},
                {'stdout': '6\n', 'stderr': '', 'exit_code': 0},
                observation(), observation('', timed_out=True, status='failed'))
    assert row['actual_match'] is None
    assert row['measured'] is False
    assert row['oracle_correct'] is False
    assert row['false_positive'] is False and row['false_negative'] is False


def test_equal_but_wrong_outputs_are_an_oracle_failure():
    grade = harness()
    expected = {'stdout': '5\n', 'stderr': '', 'exit_code': 0}
    row = grade(expected, expected, observation('0\n'), observation('0\n'))
    assert row['actual_match'] is True
    assert row['oracle_correct'] is False


def test_wrong_difference_is_counted_as_a_false_positive():
    grade = harness()
    expected = {'stdout': '5\n', 'stderr': '', 'exit_code': 0}
    row = grade(expected, expected, observation(), observation('6\n'))
    assert row['false_positive'] is True
    assert row['false_negative'] is False
    assert row['oracle_correct'] is False


def test_missed_difference_is_counted_as_a_false_negative():
    grade = harness()
    row = grade({'stdout': '5\n', 'stderr': '', 'exit_code': 0},
                {'stdout': '6\n', 'stderr': '', 'exit_code': 0},
                observation(), observation())
    assert row['false_negative'] is True
    assert row['false_positive'] is False
    assert row['oracle_correct'] is False


def test_reuse_build_refuses_to_overwrite_an_existing_measurement(tmp_path, monkeypatch):
    from scripts import evaluate_accuracy as module
    (tmp_path / 'observations').mkdir()
    marker = tmp_path / 'oracle-identity.json'
    marker.write_bytes(b'previous immutable evidence')
    async def no_external_execution(*args):
        return {'oracle_failures': 0, 'unmeasured_pairs': 0}
    monkeypatch.setattr(module, 'measure', no_external_execution)
    monkeypatch.setattr(module.sys, 'argv', ['evaluate_accuracy.py', '--output', str(tmp_path), '--reuse-build'])
    with pytest.raises(ValueError, match='Existing measurement'):
        module.main()
    assert marker.read_bytes() == b'previous immutable evidence'


def test_returned_failure_is_retained_separately_from_a_stale_success_snapshot(tmp_path):
    import json
    from scripts.evaluate_accuracy import retain_observation

    snapshot = tmp_path / 'recheck-exec-owned.json'
    old = observation()
    snapshot.write_text(json.dumps(old), encoding='utf-8')
    original = snapshot.read_bytes()
    current = observation(status='failed', infrastructure_ok=False,
                          container_name='recheck-exec-owned', storage_errors=['disk write failed'])

    receipt = retain_observation(tmp_path, current)

    assert receipt['returned'] == current
    assert receipt['adapter_snapshot']['matches_returned'] is False
    assert receipt['adapter_snapshot']['status'] == 'success'
    assert receipt['adapter_snapshot']['sha256']
    assert snapshot.read_bytes() == original
    retained = json.loads((tmp_path / 'returned-observation.json').read_text(encoding='utf-8'))
    assert retained['returned']['storage_errors'] == ['disk write failed']
    assert retained['returned']['status'] == 'failed'


@pytest.mark.parametrize('contents', [None, b'{incomplete'])
def test_returned_observation_survives_missing_or_damaged_adapter_snapshot(tmp_path, contents):
    import json
    from scripts.evaluate_accuracy import retain_observation

    snapshot = tmp_path / 'recheck-exec-owned.json'
    if contents is not None:
        snapshot.write_bytes(contents)
    current = observation(status='failed', infrastructure_ok=False,
                          container_name='recheck-exec-owned', storage_errors=['storage unavailable'])
    receipt = retain_observation(tmp_path, current)
    assert receipt['adapter_snapshot']['matches_returned'] is False
    assert receipt['adapter_snapshot']['error']
    retained = json.loads((tmp_path / 'returned-observation.json').read_text(encoding='utf-8'))
    assert retained['returned'] == current
    if contents is not None:
        assert snapshot.read_bytes() == contents
