"""Bounded native integration checks; unique output directory required."""

from __future__ import annotations
import argparse
import asyncio
import json
from pathlib import Path
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from recheck.engine import Engine
from recheck.models import atomic_json, load_settings
from recheck.ollama import OllamaClient
from recheck.storage import now


async def main(out):
    out.mkdir(parents=True, exist_ok=True)
    settings = load_settings()
    client = OllamaClient(settings.ollama_url, timeout=300)
    record = {
        'created': now(),
        'validation_kind': 'actual native local HTTP and installed tools',
        'steps': {},
    }

    def save(name, value):
        record['steps'][name] = value
        atomic_json(out / 'live-probe.json', record)
        print(name, flush=True)

    try:
        save('server', await client.ensure_server(timeout=30))
        models = await client.models()
        save('models', models)
        selected = next(m for m in models if m['name'] == 'gemma4:e4b')
        save('selected', selected)
        save('metadata', await client.local_metadata(selected['name']))
        save('capabilities', await client.capabilities(selected['name']))
        # Exercise production cancellation on a real HTTP stream, without touching a service.
        cancel = threading.Event()
        seen = []

        def on_event(event):
            seen.append(event)
            if event.get('message', {}).get('content'):
                cancel.set()

        started = time.monotonic()
        try:
            response = await client.chat(
                selected['name'],
                [{'role': 'user', 'content': 'List the integers from 1 to 1000, one per line.'}],
                options={'num_ctx': 2048, 'num_predict': 2048},
                cancel=cancel,
                on_event=on_event,
            )
            save(
                'stream_cancellation',
                {
                    'status': 'FAILED',
                    'reason': 'request completed before cancellation',
                    'response': response,
                },
            )
        except Exception as exc:
            save(
                'stream_cancellation',
                {
                    'status': (
                        'VERIFIED'
                        if getattr(exc, 'code', None) == 'cancelled' and seen
                        else 'FAILED'
                    ),
                    'code': getattr(exc, 'code', None),
                    'error': str(exc),
                    'chunks_received': len(seen),
                    'seconds': time.monotonic() - started,
                },
            )
        save('execution', client.last_execution)
        save('environment', await Engine(settings, data_dir=out / 'probe-data').environment())
        record['status'] = 'VERIFIED' if record['steps']['capabilities'].get('basic') else 'FAILED'
        atomic_json(out / 'live-probe.json', record)
    except Exception as exc:
        save('failure', {'code': getattr(exc, 'code', None), 'error': str(exc)})
        raise
    finally:
        await client.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    asyncio.run(main(Path(parser.parse_args().output).resolve()))
