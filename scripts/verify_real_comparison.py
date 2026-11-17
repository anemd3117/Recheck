"""Run genuine binaries through the shipped Engine. No mocks or fixture substitution."""

import argparse
import asyncio
import json
from pathlib import Path
import signal
import sys
import threading
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from recheck.models import load_settings, atomic_json
from recheck.engine import Engine


async def verify(args):
    data_dir = (
        Path(args.data_dir).resolve()
        if args.data_dir
        else Path(args.summary).resolve().parent / ('real-comparison-data-' + uuid.uuid4().hex)
    )
    settings = load_settings(root=data_dir)
    settings.model = args.model
    settings.active_ai_profile = 'legacy-local'
    settings.ghidra_path = args.ghidra or settings.ghidra_path
    settings.llm_timeout = 300
    cancel = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: cancel.set())

    def progress(e):
        if e.get('kind') in ('stage', 'failure', 'llm_failure', 'llm_validation', 'agent_retry'):
            print(
                json.dumps({k: v for k, v in e.items() if k != 'traceback'}, ensure_ascii=False),
                flush=True,
            )

    engine = Engine(settings, data_dir=data_dir, on_event=progress)
    tests = json.loads(Path(args.tests).read_text(encoding='utf-8-sig'))
    run = await engine.analyze(
        args.original, args.reimplementation, tests, mode=args.mode, cancel=cancel
    )
    summary = dict(
        validation_kind='actual_tool_and_real_C_binaries',
        run_id=run['run_id'],
        attempt_id=run['attempt_id'],
        status=run['status'],
        stages=run['stages'],
        model=settings.model,
        files=run['files'],
        function_counts={t: len(v.get('functions', [])) for t, v in run['static'].items()},
        structure=dict(
            score=run['structure'].get('score'), matched=len(run['structure'].get('matches', []))
        ),
        behavior=run['behavior'],
        llm_status=run['llm'].get('status'),
        llm_errors=run['llm'].get('validation_errors'),
        agent_status=run.get('agent', {}).get('status'),
        diagnostics=run['diagnostics'],
        result=str(engine.store.run_dir(run['run_id']) / 'result.json'),
        report=str(engine.report(run['run_id'])),
        evidence_count=len(run['evidence']),
        no_fixture_substitution=True,
    )
    atomic_json(Path(args.summary), summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0 if run['status'] == 'success' else 1


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--original', required=True)
    p.add_argument('--reimplementation', required=True)
    p.add_argument('--tests', required=True)
    p.add_argument('--ghidra')
    p.add_argument('--model', default='gemma4:e4b')
    p.add_argument('--mode', choices=['automatic', 'agent'], default='automatic')
    p.add_argument('--data-dir')
    p.add_argument('--summary', default='verification/real-comparison.json')
    raise SystemExit(asyncio.run(verify(p.parse_args())))
