"""Run the product's local benchmark engine and save reproducible recommendations."""

import argparse
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from recheck.models import atomic_json, load_settings
from recheck.performance import BenchmarkRunner
from recheck.recommendations import recommend_settings


async def main(args):
    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=False)
    settings = load_settings()
    atomic_json(out / 'settings.json', settings.model_dump())
    result = await BenchmarkRunner(
        settings,
        root=out / 'benchmarks',
        on_event=lambda event: print(event['stage'], event['status'], flush=True),
    ).run(mode=args.mode)
    profiles = {
        profile: recommend_settings(settings, result.get('hardware') or {}, result, profile)
        for profile in ('stability', 'balanced', 'performance')
    }
    atomic_json(out / 'profiles.json', profiles)
    atomic_json(
        out / 'summary.json',
        dict(
            benchmark_id=result['benchmark_id'],
            status=result['status'],
            result_path=result['result_path'],
            stages={
                k: {
                    'status': v['status'],
                    'error': v.get('error'),
                    'duration_seconds': v.get('duration_seconds'),
                }
                for k, v in result['stages'].items()
            },
        ),
    )
    print(
        json.dumps(
            dict(
                benchmark_id=result['benchmark_id'],
                status=result['status'],
                result=result['result_path'],
            ),
            ensure_ascii=False,
        )
    )
    return 0 if result['status'] == 'success' else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    parser.add_argument('--mode', choices=['quick', 'detailed'], default='quick')
    raise SystemExit(asyncio.run(main(parser.parse_args())))
