"""Run the complete source checks, then record the unchanged build inputs."""
from __future__ import annotations
import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from recheck.models import atomic_json

ROOT = Path(__file__).resolve().parents[1]


def snapshot(root):
    paths = [root / name for name in ('launch.py', 'pyproject.toml', 'Recheck.spec', 'requirements-lock.txt')]
    for directory in ('recheck', 'ai_providers', 'resources', 'scripts'):
        paths += [p for p in (root / directory).rglob('*') if p.is_file()
                  and '__pycache__' not in p.parts and p.suffix not in ('.pyc', '.log')]
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(paths) if p.exists()}


def verify(root, output, runner=subprocess.run):
    root, output = Path(root).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    before = snapshot(root)
    commands = [(sys.executable, '-m', 'pytest', '-q'),
                ('node', '--test', 'resources/docker/static/normalize.test.mjs',
                 'resources/docker/static/managed.test.mjs')]
    for name, command in zip(('pytest', 'node'), commands):
        with (output / (name + '.log')).open('w', encoding='utf-8') as stream:
            process = runner(command, cwd=root, stdout=stream, stderr=subprocess.STDOUT,
                             env={**os.environ, 'PYTHONUTF8': '1', 'QT_QPA_PLATFORM': 'offscreen',
                                  'RECHECK_DATA_DIR': str(output / 'test-data')})
        if process.returncode:
            raise RuntimeError(name + ' verification failed; see ' + str(output))
    if before != snapshot(root):
        raise ValueError('Source changed during verification; identity was not updated')
    module = ast.parse((root / 'recheck/__init__.py').read_text(encoding='utf-8'))
    display = next(ast.literal_eval(node.value) for node in module.body
                   if isinstance(node, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == '__version__' for t in node.targets))
    import tomllib
    metadata = tomllib.loads((root / 'pyproject.toml').read_text(encoding='utf-8'))['project']['version']
    numeric = [int(value) for value in re.search(r'filevers=\(([^)]+)\)',
                (root / 'resources/version_info.txt').read_text(encoding='utf-8')).group(1).split(',')]
    aggregate = hashlib.sha256(json.dumps(before, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    result = dict(product='Recheck', display_version=display, python_metadata_version=metadata,
                  windows_numeric_version=numeric, created_utc=datetime.now(timezone.utc).isoformat(),
                  runtime_files=len(before), runtime_identity=before, aggregate_sha256=aggregate,
                  aggregate_algorithm='sha256 of UTF-8 sorted compact JSON runtime_identity',
                  verification={'record': str(output.relative_to(root)) if output.is_relative_to(root) else str(output),
                                'pytest': 'exit 0', 'node': 'exit 0'})
    atomic_json(root / 'RECHECK_SOURCE_IDENTITY.json', result)
    atomic_json(output / 'verified-source.json', result)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    print(json.dumps(verify(ROOT, args.output), ensure_ascii=False, indent=2))
