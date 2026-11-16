"""Read current and historical artifacts without changing historical evidence."""

from __future__ import annotations
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import sqlite3
import sys
import xml.etree.ElementTree as ET
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from recheck.models import atomic_json, data_root, inspect_binary
from recheck.storage import now

root = Path(__file__).resolve().parents[1]
out = root / (root / 'verification/active-recovery.txt').read_text(encoding='utf-8-sig').strip()


def identity(path):
    path = Path(path)
    return dict(
        path=str(path.resolve()),
        size=path.stat().st_size,
        modified_ns=path.stat().st_mtime_ns,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def main():
    if (out / 'audit.json').exists():
        raise SystemExit('기존 감사 기록이 있습니다. 새 검증 폴더를 사용하세요.')
    data = dict(
        created=now(),
        status='VERIFIED',
        scope='read-only historical audit; not new execution',
        python=sys.version,
        packages={
            p: importlib.metadata.version(p)
            for p in ['PySide6-Essentials', 'pydantic', 'pytest', 'pyinstaller', 'httpx', 'psutil']
        },
        historical=[],
    )
    for rel in [
        'verification/pytest.xml',
        'verification/real-comparison.json',
        'verification/packaged-actual/scenario.json',
    ]:
        path = root / rel
        item = identity(path)
        if path.suffix == '.json':
            value = json.loads(path.read_text(encoding='utf-8-sig'))
            item['record'] = {
                k: value[k]
                for k in ['run_id', 'attempt_id', 'status', 'model', 'behavior', 'result', 'report']
                if k in value
            }
            if value.get('run_id'):
                result = data_root() / 'runs' / value['run_id'] / 'result.json'
                if result.exists():
                    run = json.loads(result.read_text(encoding='utf-8'))
                    item['stored_result'] = identity(result)
                    item['created'] = run['created']
                    item['llm_findings'] = run.get('llm', {}).get('interpretation', {})
        else:
            item['suites'] = [s.attrib for s in ET.parse(path).iter('testsuite')]
        data['historical'].append(item)
    samples = root.parent / '비교 대상 파일' / 'samples' / 'discount_boundary'
    binaries = samples / 'bin/linux-x64'
    data['samples'] = {p.name: identity(p) for p in samples.iterdir() if p.is_file()}
    data['binaries'] = {
        name: inspect_binary(binaries / name) for name in ['original', 'reimplementation']
    }
    data['distribution'] = [
        dict(relative=p.relative_to(root / 'dist/Recheck').as_posix(), **identity(p))
        for p in sorted((root / 'dist/Recheck').rglob('*'))
        if p.is_file()
    ]
    # SQLite's backup API obtains a consistent snapshot without altering the live DB.
    db_path = data_root() / 'history.sqlite3'
    if db_path.exists():
        source = sqlite3.connect(f'file:{db_path.as_posix()}?mode=ro', uri=True)
        target = sqlite3.connect(out / 'historical-history.sqlite3')
        source.backup(target)
        data['database'] = {
            'path': str(db_path),
            'run_count': source.execute('select count(*) from runs').fetchone()[0],
        }
        source.close()
        target.close()
    texts = []
    with zipfile.ZipFile(root / 'Recheck_Light.pptx') as archive:
        names = sorted(
            (
                n
                for n in archive.namelist()
                if n.startswith('ppt/slides/slide') and n.endswith('.xml')
            ),
            key=lambda n: int(Path(n).stem.removeprefix('slide')),
        )
        for name in names:
            slide = ET.fromstring(archive.read(name))
            texts.append(
                name + '\n' + '\n'.join(n.text or '' for n in slide.iter() if n.tag.endswith('}t'))
            )
    (out / 'reference-slides.txt').write_text('\n\n'.join(texts), encoding='utf-8')
    prompts = list(root.parent.glob('*프롬*.txt')) + list(root.glob('*프롬*.txt'))
    data['previous_prompts'] = [identity(p) for p in prompts]
    for p in prompts:
        (out / ('reference-' + p.name)).write_bytes(p.read_bytes())
    atomic_json(out / 'audit.json', data)
    print(
        json.dumps(
            dict(
                output=str(out),
                historical_runs=[r.get('record', {}).get('run_id') for r in data['historical']],
                samples=data['binaries'],
                distribution_files=len(data['distribution']),
                previous_prompts=data['previous_prompts'],
            ),
            ensure_ascii=False,
        )
    )


if __name__ == '__main__':
    main()
