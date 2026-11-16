"""Actual Ollama + fixed registry harmless get_status round trip."""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from recheck.agent import AgentRegistry
from recheck.models import atomic_json
from recheck.ollama import OllamaClient
from recheck.storage import now, new_id


async def main():
    path = Path('docs/ollama-e4b-live.json')
    data = json.loads(path.read_text(encoding='utf-8'))
    client = OllamaClient(timeout=300)
    run_id = new_id('run')

    async def status(**kwargs):
        return {
            'status': 'success',
            'server': await client.probe(),
            'observed_at': now(),
            'scope': 'Only live environment; no binary execution requested.',
        }

    registry = AgentRegistry(run_id, {'get_status': status})
    smoke = {'created': now(), 'run_id': run_id, 'tool_manifest': registry.manifest()}
    try:
        messages = [
            {
                'role': 'system',
                'content': 'You are testing the Recheck bounded agent adapter. You MUST call the single registered get_status tool using the exact run_id. This tool only reads the running local Ollama environment. After its tool result, explain the verified status briefly in Korean. No binary execution.',
            },
            {
                'role': 'user',
                'content': json.dumps({'run_id': run_id, 'request': 'Call get_status now.'}),
            },
        ]
        response = await client.chat(
            'gemma4:e4b',
            messages,
            tools=registry.definitions(),
            options={'num_ctx': 4096, 'num_predict': 256},
        )
        smoke['proposal'] = response
        message = response['message']
        messages.append(message)
        calls = message.get('tool_calls', [])
        if len(calls) != 1:
            raise ValueError('Expected one actual get_status call')
        call = calls[0]['function']
        result = await registry.execute(call['name'], call['arguments'])
        smoke['tool_result'] = result
        smoke['tool_history'] = registry.calls
        if not result['ok']:
            raise ValueError('Actual tool request rejected: ' + str(result))
        messages.append(
            {
                'role': 'tool',
                'tool_name': call['name'],
                'content': json.dumps(result, ensure_ascii=False),
            }
        )
        smoke['answer'] = await client.chat(
            'gemma4:e4b', messages, options={'num_ctx': 4096, 'num_predict': 256}
        )
        smoke['status'] = 'success'
        print(
            json.dumps(
                {
                    'status': 'success',
                    'tool': call['name'],
                    'model': 'gemma4:e4b',
                    'answer': smoke['answer']['message']['content'],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    except Exception as exc:
        smoke.update(status='failed', error=str(exc))
        raise
    finally:
        data['registry_agent_smoke'] = smoke
        atomic_json(path, data)
        await client.close()


if __name__ == '__main__':
    asyncio.run(main())
