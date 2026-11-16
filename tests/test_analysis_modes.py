"""Independent analysis scope and AI requests, using inert binary headers only."""

import base64
import json
import struct

import pytest

from recheck import cli
from recheck.engine import Engine
from recheck.models import Settings
from ai_providers.profiles import resolve_profile
from ai_providers.contracts import ProviderError
from test_engine_llm import FakeClient


@pytest.fixture
def configured(tmp_path, monkeypatch):
    artifact = tmp_path / 'header-only.bin'
    header = bytearray(64)
    header[:6] = b'\x7fELF\x02\x01'
    struct.pack_into('<HH', header, 16, 2, 62)
    artifact.write_bytes(header)
    engine = Engine(Settings(model='fixture-selected-model'), tmp_path / 'results')

    async def environment(cancel=None, **kwargs):
        return {'fixture': True}

    class Static:
        async def analyze(self, *args, **kwargs):
            return dict(status='success', complete=True, functions=[dict(
                id='main', name='main', blocks=[{'id': 'b'}], edges=[], calls=[],
                mnemonics=['RET'],
            )])

    class Runtime:
        async def prepare_image(self, *args, **kwargs):
            return dict(status='success', image='fixture', image_id='sha256:fixture')

        async def execute(self, *args, **kwargs):
            return dict(status='success', exit_code=0,
                        stdout_b64=base64.b64encode(b'observation\n').decode(), stderr_b64='')

    monkeypatch.setattr(engine, 'environment', environment)
    monkeypatch.setattr(engine, '_ghidra', Static)
    monkeypatch.setattr(engine, '_execution_adapter', lambda run: Runtime())
    return engine, artifact


@pytest.mark.asyncio
@pytest.mark.parametrize('scope,static_status,dynamic_status', [
    ('static', 'success', 'not_requested'),
    ('dynamic', 'not_requested', 'success'),
    ('both', 'success', 'success'),
])
async def test_requested_scope_controls_observed_outputs_and_reproduction(
    configured, monkeypatch, scope, static_status, dynamic_status,
):
    engine, artifact = configured

    def forbidden():
        pytest.fail('unrequested analysis or AI was invoked')

    if scope == 'static':
        monkeypatch.setattr(engine, '_execution_adapter', lambda run: forbidden())
    if scope == 'dynamic':
        monkeypatch.setattr(engine, '_ghidra', forbidden)
    monkeypatch.setattr(engine, '_client', forbidden)
    result = await engine.analyze(str(artifact), str(artifact), [{'id': 'baseline'}],
                                  analysis_mode=scope, ai_mode='off')
    assert result['status'] == 'success'
    assert result['analysis_mode'] == scope and result['ai_mode'] == 'off'
    assert result['analysis_status'] == dict(static=static_status, dynamic=dynamic_status,
                                             ai='not_requested')
    assert bool(result['static']) == (scope != 'dynamic')
    assert bool(result['dynamic']) == (scope != 'static')
    assert ('match_rate' in result['behavior']) == (scope != 'static')
    assert result['reproduction']['argv'][-4:] == ['--analysis-mode', scope, '--ai-mode', 'off']
    saved = engine.store.load_run(result['run_id'])
    assert saved['analysis_mode'] == scope and saved['analysis_status'] == result['analysis_status']


@pytest.mark.asyncio
async def test_both_scope_preserves_blocked_dynamic_instead_of_reporting_static_only_success(
    configured, monkeypatch,
):
    engine, artifact = configured

    class MissingRuntime:
        async def prepare_image(self, *args, **kwargs):
            return dict(status='blocked', error='격리 환경을 준비하세요')

    monkeypatch.setattr(engine, '_execution_adapter', lambda run: MissingRuntime())
    result = await engine.analyze(str(artifact), str(artifact), [{'id': 'baseline'}],
                                  analysis_mode='both', ai_mode='off')
    assert result['status'] == 'partial'
    assert result['analysis_status'] == dict(static='success', dynamic='blocked', ai='not_requested')
    assert result['behavior'] == {} and result['analysis_mode'] == 'both'
    assert 'dynamic' in result['reproduction']['missing_stages']


@pytest.mark.asyncio
@pytest.mark.parametrize('scope', ['static', 'dynamic'])
@pytest.mark.parametrize('failure', [False, True])
async def test_ai_explanation_is_an_independent_requested_stage(configured, monkeypatch, scope, failure):
    engine, artifact = configured
    client = FakeClient([
        ProviderError('quota_exceeded', '요청 한도에 도달했습니다') if failure
        else {'summary': '선택한 분석 근거를 설명합니다.', 'findings': []},
    ])
    monkeypatch.setattr(engine, '_client', lambda: client)
    result = await engine.analyze(str(artifact), str(artifact), [{'id': 'baseline'}],
                                  mode='static', analysis_mode=scope, ai_mode='explain')
    assert result['status'] == ('partial' if failure else 'success')
    assert result['analysis_status']['ai'] == ('failed' if failure else 'success')
    assert result['analysis_status'][scope] == 'success'
    assert result['analysis_status']['dynamic' if scope == 'static' else 'static'] == 'not_requested'
    assert result['llm']['status'] == ('failed' if failure else 'success')
    assert bool(result['llm']['context']['observations']) == (scope == 'dynamic')
    assert client.closed


@pytest.mark.asyncio
@pytest.mark.parametrize('scope,ai', [('typo', 'off'), ('static', 'typo')])
async def test_unknown_scope_or_ai_is_rejected_before_creating_an_analysis(configured, scope, ai):
    engine, artifact = configured
    with pytest.raises(ValueError):
        await engine.analyze(str(artifact), str(artifact), [{'id': 'baseline'}],
                             analysis_mode=scope, ai_mode=ai)
    assert engine.store.list_runs() == []


@pytest.mark.asyncio
async def test_static_recovery_does_not_start_runtime_or_unrequested_ai(configured, monkeypatch):
    engine, artifact = configured

    def forbidden():
        pytest.fail('static recovery touched an unrequested service')

    monkeypatch.setattr(engine, '_docker', forbidden)
    monkeypatch.setattr(engine, '_client', forbidden)
    result = await engine.analyze(str(artifact), str(artifact), [{'id': 'baseline'}],
                                  analysis_mode='static', ai_mode='off')
    recovered = await engine.recover(result['run_id'])
    assert recovered['status'] == 'success' and recovered['analysis_mode'] == 'static'
    assert len(recovered['attempts']) == 2 and recovered['dynamic'] == []


@pytest.mark.asyncio
async def test_environment_excludes_ai_and_static_probes_for_dynamic_without_ai(tmp_path, monkeypatch):
    engine = Engine(Settings(), tmp_path)

    def forbidden():
        pytest.fail('an unrequested provider or static probe was constructed')

    class Runtime:
        async def probe(self):
            return dict(ready=True)

        async def leftovers(self):
            return []

    monkeypatch.setattr(engine, '_docker', Runtime)
    monkeypatch.setattr(engine, '_client', forbidden)
    monkeypatch.setattr(engine, '_ghidra', forbidden)
    result = await engine.environment(analysis_mode='dynamic', include_ai=False)
    assert result['docker']['ready'] is True
    assert result['ai']['status'] == 'not_requested'
    assert result['ghidra']['status'] == 'not_requested'


def test_cli_separate_options_execute_static_without_ai_and_preserve_legacy_static(
    configured, monkeypatch, capsys,
):
    engine, artifact = configured
    monkeypatch.setattr(cli, 'Engine', lambda *args, **kwargs: engine)
    monkeypatch.setattr(cli, '_selected_settings', lambda args: engine.settings)
    for options in (['--analysis-mode', 'static', '--ai-mode', 'off'], ['--mode', 'static']):
        code = cli.main(['analyze', '--original', str(artifact), '--reimplementation', str(artifact),
                         *options])
        result = json.loads(capsys.readouterr().out)
        assert code == 0 and result['status'] == 'success'
        assert result['analysis_mode'] == 'static' and result['dynamic'] == []
        assert result['analysis_status']['ai'] == 'not_requested'


@pytest.mark.asyncio
async def test_static_scope_accepts_no_execution_tests(configured):
    engine, artifact = configured
    result = await engine.analyze(str(artifact), str(artifact), [],
                                  analysis_mode='static', ai_mode='off')
    assert result['status'] == 'success' and result['tests'] == []
    assert result['dynamic'] == [] and result['analysis_status']['dynamic'] == 'not_requested'


@pytest.mark.parametrize('scope', ['dynamic', 'both'])
def test_cli_requires_registered_tests_for_execution_scopes(configured, monkeypatch, capsys, scope):
    engine, artifact = configured
    monkeypatch.setattr(cli, 'Engine', lambda *args, **kwargs: engine)
    monkeypatch.setattr(cli, '_selected_settings', lambda args: engine.settings)
    code = cli.main(['analyze', '--original', str(artifact), '--reimplementation', str(artifact),
                     '--analysis-mode', scope, '--ai-mode', 'off'])
    result = json.loads(capsys.readouterr().out)
    assert code == 2 and result['status'] == 'failed'
    assert engine.store.list_runs() == []


@pytest.mark.parametrize('mode', ['automatic', 'agent', 'dynamic'])
def test_legacy_cli_execution_modes_keep_the_explicit_empty_input_baseline(
    configured, monkeypatch, capsys, mode,
):
    engine, artifact = configured
    monkeypatch.setattr(cli, 'Engine', lambda *args, **kwargs: engine)
    monkeypatch.setattr(cli, '_selected_settings', lambda args: engine.settings)
    code = cli.main(['analyze', '--original', str(artifact), '--reimplementation', str(artifact),
                     '--mode', mode, '--ai-mode', 'off'])
    result = json.loads(capsys.readouterr().out)
    assert code == 0 and result['status'] == 'success'
    assert len(result['tests']) == 1
    assert result['tests'][0]['id'] == 'baseline'
    assert result['tests'][0]['args'] == [] and result['tests'][0]['stdin'] == ''
    assert result['dynamic'][0]['input']['id'] == 'baseline'


@pytest.mark.asyncio
@pytest.mark.parametrize('scope,forbidden_tool,arguments', [
    ('static', 'run_tests', {'test_ids': ['baseline']}),
    ('dynamic', 'start_static', {'target': 'original'}),
    ('static', 'recover', {'action': 'prepare_image'}),
    ('dynamic', 'retry', {'stage': 'static', 'reason': '선택 범위 밖 요청'}),
])
async def test_ai_agent_cannot_expand_the_selected_scope(
    configured, monkeypatch, scope, forbidden_tool, arguments,
):
    engine, artifact = configured
    result = await engine.analyze(str(artifact), str(artifact), [{'id': 'baseline'}],
                                  analysis_mode=scope, ai_mode='off')
    profile = resolve_profile(engine.settings)
    engine.settings.capabilities = dict(
        basic=True, tools=True, model=profile.model, profile_fingerprint=profile.fingerprint(),
        actual_inference_verified=True, local_digest='fixture-digest',
    )
    client = FakeClient([
        {'message': {'role': 'assistant', 'content': '도구를 요청합니다.', 'tool_calls': [{
            'function': {'name': forbidden_tool,
                         'arguments': dict(run_id=result['run_id'], **arguments)},
        }]}},
        {'message': {'role': 'assistant', 'content': '선택한 분석 범위의 결과를 확인했습니다.'}},
    ])
    # FakeClient models must identify the exact installed fixture chosen above.
    async def models(**kwargs):
        return [dict(name=profile.model, digest='fixture-digest')]
    client.models = models
    monkeypatch.setattr(engine, '_client', lambda: client)

    def forbidden():
        pytest.fail('agent expanded the user-selected analysis scope')
    if scope == 'static':
        monkeypatch.setattr(engine, '_execution_adapter', lambda run: forbidden())
    else:
        monkeypatch.setattr(engine, '_ghidra', forbidden)
    agent_result = await engine._agent(result, None)
    tool_messages = [message for messages in client.calls for message in messages
                     if message.get('role') == 'tool']
    if forbidden_tool in ('run_tests', 'start_static'):
        assert forbidden_tool not in {row['name'] for row in result['agent_manifest']}
        assert agent_result['status'] == 'partial' and agent_result['status_detail'] == 'failed'
        assert any(row['kind'] == 'error' for row in agent_result['history'])
    else:
        assert tool_messages and any('error' in message['content'] for message in tool_messages)
        assert agent_result['status'] == 'success'
    assert bool(result['dynamic']) == (scope != 'static')
    assert bool(result['static']) == (scope != 'dynamic')
