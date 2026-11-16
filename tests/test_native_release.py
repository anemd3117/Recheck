import importlib
import pytest


@pytest.mark.parametrize('damage', [None, 'context', 'receipt'])
def test_native_gate_uses_only_context_bound_local_execution_identity(tmp_path, damage):
    import json
    from recheck.runtime_source import context_digest

    m = importlib.import_module('scripts.verify_native_release')
    resources = tmp_path / 'stage/_internal/resources'
    (resources / 'runtime').mkdir(parents=True)
    manifest = resources / 'runtime/environment.json'
    manifest.write_text(json.dumps({'version': 1, 'images': {
        c: {'image_id': 'sha256:' + str(n) * 64, 'platform': 'linux/amd64'}
        for n, c in enumerate(('static', 'execution', 'ollama'), 1)}}))
    for c in ('static', 'execution', 'ollama'):
        (resources / 'docker' / c).mkdir(parents=True)
        (resources / 'docker' / c / 'Dockerfile').write_text('FROM scratch\n')
    receipt = tmp_path / 'data/runtime/local-builds.json'
    receipt.parent.mkdir(parents=True)
    receipt.write_text(json.dumps({'version': 1, 'build_contract': 'source-v1', 'images': {
        'execution': {'image_id': 'sha256:' + '9' * 64, 'platform': 'linux/amd64',
                      'context_sha256': context_digest(resources, 'execution', manifest)}}}))
    if damage == 'context':
        (resources / 'docker/execution/Dockerfile').write_text('FROM changed\n')
    elif damage == 'receipt':
        receipt.write_text('{broken receipt')
    assert hasattr(m, 'expected_execution_image'), 'Native gate cannot resolve local builds'
    expected = 'sha256:' + ('9' if damage is None else '2') * 64
    assert m.expected_execution_image(tmp_path / 'stage', tmp_path / 'data') == expected


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
