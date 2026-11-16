"""All-or-nothing proposal preflight and provider-compatible tool identities."""

import asyncio
import copy

import pytest

from ai_providers.contracts import GenerationOptions, ProviderProfile
from recheck.agent import AgentRegistry, AgentRunner


@pytest.mark.parametrize('artifacts_ready', [True, False])
def test_last_shared_request_finishes_the_answer_only_when_required_artifacts_are_ready(
    artifacts_ready,
):
    budget = {'used': 1, 'max_requests': 3}  # One earlier interpretation in this workflow.
    requests = []

    class BudgetClient:
        async def chat(self, model, messages, **kwargs):
            assert budget['used'] < budget['max_requests']
            budget['used'] += 1
            requests.append(copy.deepcopy(kwargs))
            calls = [call(identity='call-' + str(budget['used']))] if kwargs.get('tools') else []
            return {
                'message': {
                    'role': 'assistant',
                    'content': 'Observed facts and limits.',
                    'tool_calls': calls,
                }
            }

    async def handler(**kwargs):
        return {'ready': artifacts_ready}

    runner = AgentRunner(
        BudgetClient(),
        AgentRegistry('r', {'get_status': handler}),
        completion_check=lambda: artifacts_ready,
        request_budget=lambda: dict(budget),
    )
    result = run(runner.run('model', {}))
    assert budget['used'] == 3 and len(requests) == 2
    assert bool(requests[-1]['tools']) is (not artifacts_ready)
    assert result['status'] == ('completed' if artifacts_ready else 'budget_exhausted')


def run(value):
    return asyncio.run(value)


class Client:
    def __init__(self, calls, *, selected=None):
        self.proposals, self.requests = calls, []
        if selected is not None:
            self.profile = selected

    async def chat(self, model, messages, **kwargs):
        self.requests.append((copy.deepcopy(messages), copy.deepcopy(kwargs)))
        return {
            'message': {
                'role': 'assistant',
                'content': '',
                'tool_calls': self.proposals if len(self.requests) == 1 else [],
            }
        }


def call(name='get_status', arguments=None, identity='call-1'):
    return {'id': identity, 'function': {'name': name, 'arguments': arguments or {'run_id': 'r'}}}


@pytest.mark.parametrize(
    'invalid',
    [
        call('shell', identity='call-2'),
        call(arguments={'run_id': 'other'}, identity='call-2'),
        call(arguments={'run_id': 'r', 'command': 'bad'}, identity='call-2'),
        call(identity='call-1'),
        call('run_tests', {'run_id': 'r', 'test_ids': ['unknown']}, 'call-2'),
    ],
)
def test_invalid_later_call_prevents_every_handler_in_the_batch(invalid):
    executed = []

    async def handler(**kwargs):
        executed.append(kwargs)
        return {'ready': True}

    registry = AgentRegistry('r', {'get_status': handler, 'run_tests': handler}, test_ids=['known'])
    result = run(AgentRunner(Client([call(), invalid]), registry).run('model', {}))
    assert result['status'] == 'failed'
    assert executed == [] and registry.calls == []


def test_tool_result_returns_original_call_id_and_name():
    async def handler(**kwargs):
        return {'ready': True}

    client = Client([call(identity='provider-call-9')])
    result = run(AgentRunner(client, AgentRegistry('r', {'get_status': handler})).run('model', {}))
    assert result['status'] == 'completed'
    tool = client.requests[1][0][-1]
    assert tool['tool_call_id'] == 'provider-call-9' and tool['name'] == 'get_status'


def test_profile_options_and_request_budget_replace_agent_fixed_prediction_budget():
    selected = ProviderProfile(
        profile_id='remote',
        provider='openai',
        auth_mode='api_key',
        model='remote',
        enabled=True,
        external_consent=True,
        options=GenerationOptions(
            context_budget=8192, max_output_tokens=512, max_requests=1, temperature=0.6
        ),
    )

    async def handler(**kwargs):
        return {'ready': True}

    client = Client([call()], selected=selected)
    runner = AgentRunner(client, AgentRegistry('r', {'get_status': handler}), max_tokens=2048)
    result = run(
        runner.run(
            'remote',
            {},
            options={
                'num_ctx': 8192,
                'num_predict': 512,
                'temperature': 0.6,
                '_evidence_kinds': (),
            },
        )
    )
    assert result['status'] == 'budget_exhausted' and len(client.requests) == 1
    assert client.requests[0][1]['options']['num_predict'] == 512


def test_later_precondition_failure_prevents_an_earlier_valid_side_effect():
    executed = []

    async def handler(**kwargs):
        executed.append(kwargs)
        return {'ready': True}

    registry = AgentRegistry(
        'r',
        {'get_status': handler, 'compare_structure': handler},
        state=lambda: {'static_available': False},
    )
    result = run(
        AgentRunner(Client([call(), call('compare_structure', identity='call-2')]), registry).run(
            'model', {}
        )
    )
    assert result['status'] == 'failed' and executed == []


def test_duplicate_proposal_with_different_ids_is_rejected_before_execution():
    executed = []

    async def handler(**kwargs):
        executed.append(kwargs)
        return {'ready': True}

    registry = AgentRegistry('r', {'get_status': handler})
    result = run(AgentRunner(Client([call(), call(identity='call-2')]), registry).run('model', {}))
    assert result['status'] == 'failed' and executed == []


def test_unknown_function_in_later_call_cannot_follow_a_valid_side_effect():
    executed = []

    async def handler(**kwargs):
        executed.append(kwargs)
        return {'ready': True}

    registry = AgentRegistry(
        'r', {'get_status': handler, 'get_function': handler}, function_ids={'original': ['main']}
    )
    calls = [
        call(),
        call(
            'get_function',
            {'run_id': 'r', 'target': 'original', 'function_id': 'unknown'},
            'call-2',
        ),
    ]
    result = run(AgentRunner(Client(calls), registry).run('model', {}))
    assert result['status'] == 'failed' and executed == []


def test_profile_client_requires_provider_call_identity():
    selected = ProviderProfile(model='fixture', enabled=True)

    async def handler(**kwargs):
        return {'ready': True}

    proposed = call()
    proposed.pop('id')
    result = run(
        AgentRunner(
            Client([proposed], selected=selected), AgentRegistry('r', {'get_status': handler})
        ).run('fixture', {})
    )
    assert result['status'] == 'failed' and result['tool_calls'] == []


@pytest.mark.parametrize('malformed', [{}, '', None])
def test_empty_non_list_tool_calls_are_rejected(malformed):
    async def handler(**kwargs):
        return {'ready': True}

    result = run(
        AgentRunner(Client(malformed), AgentRegistry('r', {'get_status': handler})).run('model', {})
    )
    assert result['status'] == 'failed' and result['tool_calls'] == []


def test_static_tool_result_adds_code_scope_before_the_next_request():
    async def handler(**kwargs):
        return {'decompiled': 'int main() { return 0; }'}

    client = Client(
        [call('get_function', {'run_id': 'r', 'target': 'original', 'function_id': 'main'})]
    )
    result = run(
        AgentRunner(client, AgentRegistry('r', {'get_function': handler})).run(
            'model', {}, options={'_evidence_kinds': ('runtime',)}
        )
    )
    assert result['status'] == 'completed'
    assert client.requests[1][1]['options']['_evidence_kinds'] == ('runtime', 'static', 'code')


def test_whole_batch_repetition_budget_is_checked_before_an_unrelated_side_effect():
    executed = []

    async def handler(**kwargs):
        executed.append(kwargs)
        return {'ready': True}

    class Repeated(Client):
        async def chat(self, model, messages, **kwargs):
            self.requests.append(None)
            calls = (
                [call()]
                if len(self.requests) == 1
                else [call('diagnose', identity='call-2'), call(identity='call-3')]
            )
            return {'message': {'role': 'assistant', 'content': '', 'tool_calls': calls}}

    registry = AgentRegistry('r', {'get_status': handler, 'diagnose': handler})
    result = run(AgentRunner(Repeated([]), registry, max_retries=0).run('model', {}))
    assert result['status'] == 'repeated_request' and len(executed) == 1
    assert [item['name'] for item in registry.calls] == ['get_status']


def test_preflight_has_a_time_budget_and_never_reaches_a_handler():
    executed = []

    async def handler(**kwargs):
        executed.append(kwargs)
        return {'ready': True}

    async def slow_state():
        await asyncio.sleep(0.3)
        return {'static_available': True}

    registry = AgentRegistry('r', {'compare_structure': handler}, state=slow_state)
    result = run(
        AgentRunner(Client([call('compare_structure')]), registry, max_seconds=0.03).run(
            'model', {}
        )
    )
    assert executed == [] and result['elapsed_seconds'] < 0.2
