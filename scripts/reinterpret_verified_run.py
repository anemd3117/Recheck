"""Reinterpret an existing real run; preserve prior model output and compare evidence."""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from recheck.engine import Engine
from recheck.models import load_settings, atomic_json
from recheck.comparison import match_functions
from recheck.storage import now


async def main():
    run_id = sys.argv[1]
    engine = Engine(load_settings())
    run = engine.store.load_run(run_id)
    path = Path('docs/llm-semantic-revalidation.json')
    prior = run.get('llm', {})
    record = {
        'run_id': run_id,
        'created': now(),
        'previous_response': prior,
        'review': {
            'status': 'rejected_after_semantic_review',
            'observations': 'Previous response was English and confused normal-101-stdin input 101 with input 99; schema validity alone was insufficient.',
        },
    }
    try:
        engine._validate_interpretation(run, prior['responses'][-1]['message']['content'])
        record['old_response_rejected_by_new_validator'] = False
    except Exception as exc:
        record['old_response_rejected_by_new_validator'] = True
        record['rejection_reason'] = str(exc)
    run['structure'] = match_functions(
        run['static']['original']['functions'], run['static']['reimplementation']['functions']
    )
    engine._evidence(
        run,
        'structure',
        'both',
        run['structure'],
        tool='Recheck comparison',
        source='recomputed actual saved Ghidra extraction',
    )
    engine.store.save(run)
    atomic_json(path, record)
    new = await engine.reinterpret(run_id)
    record['new_response'] = new['llm']
    record['run_result_path'] = str(engine.store.run_dir(run_id) / 'result.json')
    record['status'] = new['llm']['status']
    atomic_json(path, record)
    print(
        json.dumps(
            {
                'status': record['status'],
                'summary': new['llm'].get('interpretation', {}).get('summary'),
                'validation_errors': new['llm'].get('validation_errors'),
                'prompt_characters': new['llm'].get('prompt_data_characters'),
                'token_statistics': [
                    {k: r.get(k) for k in ['prompt_eval_count', 'eval_count', 'done_reason']}
                    for r in new['llm'].get('responses', [])
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )


if __name__ == '__main__':
    asyncio.run(main())
