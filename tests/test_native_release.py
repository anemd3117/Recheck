import importlib
import pytest


def test_unmeasured_or_wrong_verdicts_cannot_satisfy_native_release_gate():
    assert importlib.util.find_spec('scripts.verify_native_release'), 'native verifier missing'
    m = importlib.import_module('scripts.verify_native_release')
    with pytest.raises(ValueError):
        m.validate_dynamic({'behavior': {'matched': 1, 'denominator': 1},
                            'stages': {'dynamic': {'status': 'success'}}}, [True, False])


def test_stage_failure_cannot_pass_even_with_expected_match_count():
    assert importlib.util.find_spec('scripts.verify_native_release'), 'native verifier missing'
    m = importlib.import_module('scripts.verify_native_release')
    with pytest.raises(ValueError):
        m.validate_dynamic({'behavior': {'matched': 1, 'denominator': 2},
                            'stages': {'dynamic': {'status': 'failed'}}}, [True, False])


def test_gate_compares_each_input_verdict_even_when_totals_are_identical():
    m = importlib.import_module('scripts.verify_native_release')
    run = {'behavior': {'matched': 1, 'denominator': 2, 'tests': [
        {'match': True, 'execution_status': 'measured'},
        {'match': False, 'execution_status': 'measured'}]},
        'stages': {'dynamic': {'status': 'success'}}}
    m.validate_dynamic(run, [True, False])
    with pytest.raises(ValueError):
        m.validate_dynamic(run, [False, True])


@pytest.mark.parametrize('damage', ['stdout', 'stderr', 'exit', 'image', 'image_id', 'test_id'])
def test_native_raw_observation_gate_rejects_hidden_wrong_outputs(damage):
    m = importlib.import_module('scripts.verify_native_release')
    assert hasattr(m, 'validate_observations'), 'literal observation gate missing'
    execution = dict(stdout_b64='b2sK', stderr_b64='', exit_code=0, image='sha256:' + '1' * 64,
                     image_id='sha256:' + '1' * 64)
    run = {'dynamic': [dict(test_id='case-0', original=dict(execution), reimplementation=dict(execution))]}
    oracle = [dict(test_id='case-0', original=dict(stdout='ok\n', stderr='', exit_code=0),
                   reimplementation=dict(stdout='ok\n', stderr='', exit_code=0))]
    m.validate_observations(run, oracle, 'sha256:' + '1' * 64)
    if damage == 'test_id':
        run['dynamic'][0]['test_id'] = 'other-input'
    else:
        key, value = {'stdout': ('stdout_b64', 'YmFkCg=='), 'stderr': ('stderr_b64', 'ZXJy'),
                      'exit': ('exit_code', 2), 'image': ('image', 'sha256:' + '2' * 64),
                      'image_id': ('image_id', 'sha256:' + '2' * 64)}[damage]
        run['dynamic'][0]['reimplementation'][key] = value
    with pytest.raises(ValueError):
        m.validate_observations(run, oracle, 'sha256:' + '1' * 64)
