"""Persisted, explicitly blocked checkpoint for the forthcoming generation service.

The public GUI/CLI contract is available. Evidence collection, model-directed
generation and compiler/comparison orchestration are deliberately NOT claimed
as implemented by this checkpoint.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import shutil
import threading
from pathlib import Path

from .models import inspect_binary, validate_tests
from .ollama import cancelled
from .storage import Store, new_id, now


class ReconstructionService:
    def __init__(self, settings, data_dir=None, on_event=None):
        self.settings = settings.model_copy(deep=True)
        self.store = Store(data_dir)
        self.on_event = on_event
        self._lock = threading.Lock()

    async def run(
        self,
        original,
        tests,
        requirements,
        mode='reconstruction',
        source_path=None,
        language='c',
        approve_build=False,
        max_repairs=1,
        cancel=None,
    ):
        if not self._lock.acquire(blocking=False):
            raise RuntimeError('이미 재구현·유지보수 작업이 진행 중입니다.')
        workflow_id = new_id('workflow')
        workspace = self.store.workflow_dir(workflow_id)
        record = dict(
            workflow_id=workflow_id,
            status='running',
            created=now(),
            mode=mode,
            workspace=str(workspace),
            requirements=requirements,
            language=language,
            approve_build=approve_build,
            max_repairs=max_repairs,
            repairs=0,
            specification=None,
            source_path=None,
            builds=[],
            comparison_run_id=None,
            comparison_report=None,
            report=str(workspace / 'report.html'),
            diagnostics=[],
            tool_calls=[],
            events=[],
            versions=dict(schema='1.0', reconstruction='checkpoint-scaffold'),
            settings=self.settings.model_dump(),
            revisions=[],
            implementation_status='NOT IMPLEMENTED',
        )
        try:
            if mode not in ('reconstruction', 'modernization') or language not in ('c', 'cpp'):
                raise ValueError('Linux 단일 C/C++ 재구현 또는 유지보수 모드만 지원합니다.')
            if (
                not isinstance(approve_build, bool)
                or isinstance(max_repairs, bool)
                or not isinstance(max_repairs, int)
                or not 0 <= max_repairs <= 2
            ):
                raise ValueError('빌드 허가와 최대 수리 횟수(0~2)를 확인하세요.')
            if not isinstance(requirements, str) or len(requirements) > 16000:
                raise ValueError('자연어 요구사항은 최대 16,000자의 텍스트여야 합니다.')
            record['tests'] = [test.model_dump() for test in validate_tests(tests)]
            canonical = json.dumps(
                record['tests'], ensure_ascii=False, sort_keys=True, separators=(',', ':')
            ).encode('utf-8')
            record['tests_sha256'] = hashlib.sha256(canonical).hexdigest()
            if cancelled(cancel):
                record.update(status='cancelled', completion_reason='cancelled')
                return record
            metadata = await asyncio.to_thread(inspect_binary, original)
            if not metadata['supported'] or metadata['format'] != 'ELF':
                raise ValueError('재구현 흐름은 Linux ELF x86-64 콘솔 실행 파일만 대상으로 합니다.')
            inputs = workspace / 'inputs'
            inputs.mkdir()
            snapshot = inputs / 'original'
            await asyncio.to_thread(shutil.copy2, original, snapshot)
            frozen = await asyncio.to_thread(inspect_binary, snapshot)
            if frozen['sha256'] != metadata['sha256']:
                raise ValueError('원본 파일이 등록 중 변경되었습니다.')
            record['original'] = dict(metadata, snapshot=str(snapshot))
            if mode == 'modernization':
                if not source_path:
                    raise ValueError(
                        '유지보수 모드에는 사용자가 선택한 단일 소스 파일이 필요합니다.'
                    )
                selected = Path(source_path).resolve(strict=True)
                content = await asyncio.to_thread(selected.read_bytes)
                if len(content) > 65536 or b'\0' in content:
                    raise ValueError('선택 소스는 NUL 없는 UTF-8 텍스트로 최대 64KB여야 합니다.')
                content.decode('utf-8')
                copied = inputs / ('user_source.c' if language == 'c' else 'user_source.cpp')
                copied.write_bytes(content)
                record['source_path'] = str(copied)
                record['selected_source'] = dict(
                    path=str(selected),
                    snapshot=str(copied),
                    sha256=hashlib.sha256(content).hexdigest(),
                    status='user_copy_not_generated',
                )
            record.update(status='blocked', completion_reason='generation_pipeline_not_implemented')
            record['diagnostics'].append(
                dict(
                    stage='reconstruction',
                    evidence_level='NOT IMPLEMENTED',
                    summary='재구현·유지보수 실행 흐름이 아직 완성되지 않았습니다.',
                    reason='컴파일 어댑터 초안과 공용 호출·이력·보고서 계약만 준비되었습니다. 원본 근거 수집, 행동 명세, 모델 도구 제어, 소스 제안, 빌드 및 차등 검증 연결이 필요합니다.',
                    next_action='후속 개발자가 ARCHITECTURE_DECISION.md의 수용 기준에 따라 실행 흐름을 구현해야 합니다.',
                )
            )
            return record
        except Exception as exc:
            record.update(
                status='cancelled' if cancelled(cancel) else 'failed',
                completion_reason='input_or_storage_failure',
            )
            record['diagnostics'].append(
                dict(
                    stage='registration',
                    evidence_level='FAILED',
                    summary='작업 등록에 실패했습니다.',
                    reason=str(exc),
                )
            )
            return record
        finally:
            record['finished'] = now()
            event = dict(
                event_id=new_id('event'),
                time=now(),
                workflow_id=workflow_id,
                kind='workflow_checkpoint',
                stage='reconstruction',
                status=record['status'],
                reason=record.get('completion_reason'),
            )
            record['events'].append(event)
            try:
                self._write_report(record, workspace / 'report.html')
                with (workspace / 'events.jsonl').open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps(event, ensure_ascii=False) + '\n')
                self.store.save_workflow(record)
                if self.on_event:
                    self.on_event(event)
            finally:
                self._lock.release()

    @staticmethod
    def _write_report(record, destination):
        escape = lambda value: html.escape(str(value))
        statuses = {
            'blocked': '미구현 단계로 차단됨',
            'failed': '등록 실패',
            'cancelled': '취소됨',
            'running': '진행 중',
        }
        destination.write_text(
            '<!doctype html><html lang="ko"><meta charset="utf-8"><title>Recheck 재구현 작업 기록</title>'
            '<style>body{font:16px sans-serif;max-width:1000px;margin:36px auto;padding:20px}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style>'
            '<h1>재구현·유지보수 작업 기록</h1><p>상태: <b>'
            + escape(statuses.get(record['status'], record['status']))
            + '</b></p>'
            '<p>이 기록은 원본·선택 소스의 등록과 보존만 확인합니다. 행동 명세, 자동 소스 생성, 컴파일 및 차등 검증은 실행하지 않았습니다.</p>'
            '<h2>사용자 요구사항</h2><pre>'
            + escape(record.get('requirements', ''))
            + '</pre><h2>작업·진단 상세</h2><pre>'
            + escape(json.dumps(record, ensure_ascii=False, indent=2, default=str))
            + '</pre></html>',
            encoding='utf-8',
        )
