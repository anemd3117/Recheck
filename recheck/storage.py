from __future__ import annotations
import json
import re
import sqlite3
import threading
import uuid
import zipfile
import psutil
from datetime import datetime, timezone
from pathlib import Path
from .models import atomic_json, data_root


def now():
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix):
    return prefix + '_' + uuid.uuid4().hex


class Store:
    def __init__(self, root=None):
        self.root = Path(root or data_root())
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self.connect() as db:
            db.execute(
                'CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, status TEXT NOT NULL, updated TEXT NOT NULL, payload TEXT NOT NULL)'
            )
            db.execute(
                'CREATE TABLE IF NOT EXISTS workflows (workflow_id TEXT PRIMARY KEY, status TEXT NOT NULL, updated TEXT NOT NULL, payload TEXT NOT NULL)'
            )

    def connect(self):
        db = sqlite3.connect(self.root / 'history.sqlite3', timeout=10)
        db.execute('PRAGMA journal_mode=WAL')
        return db

    def run_dir(self, run_id):
        if not re.fullmatch(r'run_[a-f0-9]{32}', run_id):
            raise ValueError('잘못된 run_id')
        path = self.root / 'runs' / run_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def save(self, run):
        with self.lock:
            run['updated'] = now()
            atomic_json(self.run_dir(run['run_id']) / 'result.json', run)
            with self.connect() as db:
                db.execute(
                    'INSERT INTO runs VALUES(?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET status=excluded.status,updated=excluded.updated,payload=excluded.payload',
                    (
                        run['run_id'],
                        run['status'],
                        run['updated'],
                        json.dumps(run, ensure_ascii=False, default=str),
                    ),
                )

    def load_run(self, run_id):
        path = self.run_dir(run_id) / 'result.json'
        try:
            run = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(run, dict) or run.get('run_id') != run_id:
                raise ValueError('Result identity mismatch')
            return run
        except (OSError, ValueError):
            raise ValueError(
                '저장된 분석 결과를 읽을 수 없습니다. 기존 이력과 파일은 보존했습니다. '
                '이 분석을 다시 실행하거나 보관된 보고서를 확인하세요.'
            ) from None

    def list_runs(self):
        with self.connect() as db:
            runs = [
                json.loads(row[0])
                for row in db.execute('SELECT payload FROM runs ORDER BY updated DESC LIMIT 100')
            ]
        for run in runs:
            try:
                self.load_run(run['run_id'])
            except ValueError as exc:
                # The database copy is still useful for diagnosis, but its old
                # success state must not hide an unreadable result on disk.
                # Leave both originals untouched for recovery or a new attempt.
                run['recorded_status'] = run['status']
                run['status'] = 'partial'
                run['storage_error'] = dict(code='result_unreadable', message=str(exc))
        return runs

    def workflow_dir(self, workflow_id):
        if not re.fullmatch(r'workflow_[a-f0-9]{32}', workflow_id):
            raise ValueError('잘못된 workflow_id')
        path = self.root / 'workflows' / workflow_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def save_workflow(self, record):
        with self.lock:
            record['updated'] = now()
            atomic_json(self.workflow_dir(record['workflow_id']) / 'result.json', record)
            with self.connect() as db:
                db.execute(
                    'INSERT INTO workflows VALUES(?,?,?,?) ON CONFLICT(workflow_id) DO UPDATE SET status=excluded.status,updated=excluded.updated,payload=excluded.payload',
                    (
                        record['workflow_id'],
                        record['status'],
                        record['updated'],
                        json.dumps(record, ensure_ascii=False, default=str),
                    ),
                )

    def load_workflow(self, workflow_id):
        return json.loads(
            (self.workflow_dir(workflow_id) / 'result.json').read_text(encoding='utf-8')
        )

    def list_workflows(self):
        with self.connect() as db:
            return [
                json.loads(row[0])
                for row in db.execute(
                    'SELECT payload FROM workflows ORDER BY updated DESC LIMIT 100'
                )
            ]

    def event(self, run, kind, **payload):
        event = dict(
            event_id=new_id('event'),
            time=now(),
            run_id=run['run_id'],
            attempt_id=run['attempt_id'],
            kind=kind,
            **payload,
        )
        with self.lock:
            run.setdefault('events', []).append(event)
            path = self.run_dir(run['run_id'])
            with (path / 'events.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(event, ensure_ascii=False, default=str) + '\n')
            with (path / 'recheck.log').open('a', encoding='utf-8') as stream:
                stream.write(
                    f"{event['time']} [{event['event_id']}] {kind} {json.dumps(payload, ensure_ascii=False, default=str)}\n"
                )
        return event

    def evidence(self, run, kind, target, data, *, tool='Recheck', source='', sha256=''):
        record = dict(
            evidence_id=new_id('ev'),
            run_id=run['run_id'],
            attempt_id=run['attempt_id'],
            kind=kind,
            target=target,
            tool=tool,
            sha256=sha256,
            source=source,
            created=now(),
            data=data,
        )
        run.setdefault('evidence', []).append(record)
        return record['evidence_id']

    def interrupted(self):
        recovered = []
        for run in self.list_runs():
            if run['status'] == 'running':
                owner = run.get('owner_process', {})
                try:
                    if (
                        owner.get('pid')
                        and abs(
                            psutil.Process(owner['pid']).create_time() - owner.get('create_time', 0)
                        )
                        < 0.01
                    ):
                        # Another GUI/CLI instance may still own this live run.
                        continue
                except (psutil.Error, ValueError, TypeError):
                    pass
                run['status'] = 'partial'
                for name, stage in run.get('stages', {}).items():
                    if stage.get('status') == 'running':
                        stage.update(
                            status='failed',
                            reason='이전 프로세스가 중단되었습니다. 재시도와 소유 자원 점검이 필요합니다',
                        )
                self.event(
                    run, 'interrupted_recovery', reason='startup audit; no result reused as success'
                )
                self.save(run)
                recovered.append(run['run_id'])
        return recovered

    def diagnostic_zip(self, run_id, settings):
        run = self.load_run(run_id)
        # Target stdout, stdin, decompiled code and auxiliary data may contain arbitrary secrets;
        # share metadata/status only by default, not opaque content.
        # Scrub decoded values. Regex replacement on serialized JSON can corrupt
        # delimiters/escape sequences in real model metadata and nested logs.
        masked = dict(run=run, settings=settings)
        sensitive = {
            'stdout',
            'stderr',
            'stdout_b64',
            'stderr_b64',
            'stdin',
            'env',
            'decompile',
            'decompiled',
            'decompiled_code',
            'args',
            'content',
            'prompt',
            'messages',
            'raw_response',
            'claim',
            'rationale',
            'summary',
            'description',
            'question',
            'answer',
            'error',
            'message',
            'reason',
            'validated_text',
            'normalized_text',
        }

        def scrub(value):
            if isinstance(value, dict):
                return {
                    k: (
                        '<MASKED>'
                        if re.search(
                            r'(?i)(api[_-]?key|password|secret|authorization|access[_-]?token|refresh[_-]?token)',
                            k,
                        )
                        or k.lower() == 'token'
                        else '<CONTENT OMITTED>' if k in sensitive else scrub(v)
                    )
                    for k, v in value.items()
                    if k
                    not in (
                        'functions',
                        'data',
                        'events',
                        'history',
                        'logs',
                        'process',
                        'response',
                        'responses',
                        'context',
                        'interpretation',
                        'function_candidates',
                        'findings',
                        'suggested_tests',
                        'model_metadata',
                        'metadata',
                        'license',
                        'template',
                        'parameters',
                        'citation_completions',
                    )
                }
            if isinstance(value, list):
                return [scrub(x) for x in value]
            if isinstance(value, str):
                value = re.sub(r'(?i)([A-Z]:[\\/]+Users[\\/]+)[^\\/"\s]+', r'\1<USER>', value)
                return re.sub(
                    r'(?i)\b(api[_-]?key|password|token|secret)(\s*[:=]\s*)[^\s,;]+',
                    r'\1\2<MASKED>',
                    value,
                )
            return value

        path = self.run_dir(run_id) / 'diagnostics-masked.zip'
        with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                'diagnostics.json', json.dumps(scrub(masked), ensure_ascii=False, indent=2)
            )
            event_fields = {'event_id', 'time', 'kind', 'stage', 'status', 'target', 'code'}
            archive.writestr(
                'events-summary.jsonl',
                '\n'.join(
                    json.dumps(
                        scrub({k: v for k, v in event.items() if k in event_fields}),
                        ensure_ascii=False,
                    )
                    for event in run.get('events', [])
                ),
            )
            archive.writestr(
                'README.txt',
                '공유용: 입력/출력/디컴파일/프롬프트/원문 로그를 제외한 상태와 설정입니다. 공유 전 검토하세요.',
            )
        return path
