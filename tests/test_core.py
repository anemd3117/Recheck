import base64
import copy
import json
import struct
from pathlib import Path
from unittest.mock import AsyncMock
import pytest
from recheck.models import Settings, TestCase, inspect_binary, validate_tests, atomic_json
from recheck.storage import Store, new_id
from recheck.comparison import compare_behavior, match_functions
from recheck.engine import Engine
from recheck.report import render_report


def elf_header():
    data = bytearray(64)
    data[:6] = b'\x7fELF\x02\x01'
    struct.pack_into('<HH', data, 16, 2, 62)
    return data


def pe_header(subsystem=3, flags=2, clr=False):
    data = bytearray(512)
    data[:2] = b'MZ'
    struct.pack_into('<I', data, 60, 128)
    data[128:132] = b'PE\0\0'
    struct.pack_into('<H', data, 132, 0x8664)
    struct.pack_into('<H', data, 134, 1)
    struct.pack_into('<H', data, 148, 240)
    struct.pack_into('<H', data, 150, flags)
    struct.pack_into('<H', data, 152, 0x20B)
    struct.pack_into('<H', data, 220, subsystem)
    struct.pack_into('<I', data, 260, 16)
    if clr:
        struct.pack_into('<I', data, 128 + 24 + 112 + 112, 1)
    return data


@pytest.mark.parametrize(
    'data,supported,format',
    [
        (elf_header(), True, 'ELF'),
        (pe_header(), True, 'PE'),
        (pe_header(2), False, 'PE'),
        (pe_header(flags=0x2000), False, 'PE'),
        (pe_header(clr=True), False, 'PE'),
        (b'abc', False, 'unknown'),
    ],
)
def test_binary_headers(tmp_path, data, supported, format):
    path = tmp_path / '한글 공백 데이터.bin'
    path.write_bytes(data)
    result = inspect_binary(path)
    assert (
        result['supported'] == supported
        and result['format'] == format
        and len(result['sha256']) == 64
    )


@pytest.mark.parametrize(
    'values',
    [
        [],
        [{'id': 'a'}, {'id': 'a'}],
        [{'id': '../a'}],
        [{'id': 'a', 'args': ['\x00']}],
        [{'id': 'a', 'timeout_seconds': 999}],
        [{'id': 'a', 'aux_files': ['A/file', 'B/file']}],
        [{'id': 'a', 'command': 'arbitrary'}],
    ],
)
def test_input_validation(values):
    with pytest.raises(ValueError):
        validate_tests(values)


def execution(value=b'ok', exit_code=0, **overrides):
    return dict(
        status='success',
        exit_code=exit_code,
        stdout_b64=base64.b64encode(value).decode(),
        stderr_b64='',
        **overrides,
    )


def test_behavior_denominator_and_expected_error():
    rows = [
        dict(test_id='good', original=execution(), reimplementation=execution()),
        dict(
            test_id='bad_input',
            original=execution(exit_code=3),
            reimplementation=execution(exit_code=3),
        ),
        dict(test_id='oom', original=execution(oom_killed=True), reimplementation=execution()),
        dict(test_id='different', original=execution(b'a'), reimplementation=execution(b'b')),
    ]
    result = compare_behavior(rows, Settings())
    assert result['matched'] == 2 and result['denominator'] == 3 and result['match_rate'] == 2 / 3
    assert result['tests'][2]['match'] is None


def test_behavior_byte_preservation_and_normalization():
    rows = [
        dict(test_id='a', original=execution(b'\xff\r\n'), reimplementation=execution(b'\xff\n'))
    ]
    assert compare_behavior(rows, Settings())['match_rate'] == 0
    assert compare_behavior(rows, Settings(normalize_newlines=True))['match_rate'] == 1


def fn(id, name='FUN_a'):
    return dict(
        id=id, name=name, blocks=[{'id': 'b'}], edges=[], calls=[], mnemonics=['MOV', 'RET']
    )


def test_function_ambiguity_and_address_independence():
    result = match_functions([fn('0x100')], [fn('0x200'), fn('0x300')])
    assert result['matches'][0]['ambiguous']
    assert len(result['unmatched_reimplementation']) == 1
    assert result['matches'][0]['score'] < 1
    assert match_functions([fn('0x100', 'main')], [fn('0x200', 'main')])['score'] == 1


def test_equal_cfg_does_not_hide_decompiler_constant_difference():
    a = {
        **fn('a', 'discount'),
        'decompile': 'if (99 < amount) return amount*90/100;',
        'evidence_id': 'ev_left',
    }
    b = {
        **fn('b', 'discount'),
        'decompile': 'if (100 < amount) return amount*90/100;',
        'evidence_id': 'ev_right',
    }
    result = match_functions([a], [b])
    assert result['score'] == 1
    assert result['decompile_differences'][0]['evidence_ids'] == ['ev_left', 'ev_right']
    assert '-if (99 < amount)' in result['decompile_differences'][0]['diff']


def run_record():
    return dict(
        run_id=new_id('run'),
        attempt_id=new_id('attempt'),
        status='running',
        stages={'static': {'status': 'running'}},
        evidence=[],
        events=[],
    )


def test_storage_restart_and_transaction(tmp_path):
    store = Store(tmp_path / '한글 경로')
    run = run_record()
    store.save(run)
    assert store.interrupted() == [run['run_id']]
    assert store.load_run(run['run_id'])['status'] == 'partial'
    assert store.list_runs()[0]['status'] == 'partial'
    with pytest.raises(ValueError):
        store.run_dir('../outside')


def test_startup_does_not_interrupt_another_live_owner(tmp_path):
    import os, psutil

    store = Store(tmp_path)
    run = run_record()
    run['owner_process'] = {'pid': os.getpid(), 'create_time': psutil.Process().create_time()}
    store.save(run)
    assert store.interrupted() == []
    assert store.load_run(run['run_id'])['status'] == 'running'


def test_evidence_event_link_and_html_escape(tmp_path):
    store = Store(tmp_path)
    run = run_record()
    event = store.event(run, 'test', text='<script>alert(1)</script>')
    eid = store.evidence(run, 'log', 'both', event, source='events.jsonl#' + event['event_id'])
    run.update(
        files={'original': {'name': '<img src=x onerror=alert(1)>'}}, llm={'evidence_ids': [eid]}
    )
    path = render_report(run, tmp_path / 'report.html')
    text = path.read_text(encoding='utf-8')
    assert '<script>alert' not in text and '&lt;script&gt;' in text and f'href="#{eid}"' in text
    assert (
        len(
            (store.run_dir(run['run_id']) / 'events.jsonl').read_text(encoding='utf-8').splitlines()
        )
        == 1
    )


def test_zip_omits_target_content(tmp_path):
    import zipfile

    store = Store(tmp_path)
    run = run_record()
    run['dynamic'] = [{'stdout': 'PRIVATE', 'stdin': 'SECRET', 'stdout_b64': 'U0VDUkVU'}]
    store.save(run)
    path = store.diagnostic_zip(run['run_id'], {})
    with zipfile.ZipFile(path) as z:
        text = z.read('diagnostics.json').decode()
    assert 'PRIVATE' not in text and 'SECRET' not in text and 'U0VDUkVU' not in text


def test_zip_scrubs_decoded_json_without_breaking_nested_metadata(tmp_path):
    import zipfile

    store = Store(tmp_path)
    run = run_record()
    run['environment'] = {
        'path': r'C:\Users\NamedUser\folder',
        'token': 'hidden',
        'nested': {'api_key': 'hidden2', 'extra': r'token=abc "quoted" \\ value'},
    }
    run['metadata'] = {'tokenizer.ggml.tokens': ['"', '\\', 'secret=opaque']}
    store.save(run)
    path = store.diagnostic_zip(run['run_id'], {'token': 'hidden3'})
    with zipfile.ZipFile(path) as z:
        text = z.read('diagnostics.json').decode()
        value = json.loads(text)
        assert 'events-summary.jsonl' in z.namelist()
    assert 'hidden' not in text and 'NamedUser' not in text and 'tokenizer' not in text
    assert value['settings']['token'] == '<MASKED>'


@pytest.mark.asyncio
async def test_pipeline_fixtures_partial_without_llm(tmp_path, monkeypatch):
    # Header fixtures exercise parsing; they are never executed and are not demo programs.
    source = tmp_path / '원본 공백.bin'
    rebuilt = tmp_path / '재구현.bin'
    source.write_bytes(elf_header())
    rebuilt.write_bytes(elf_header())
    engine = Engine(Settings(), data_dir=tmp_path / '사용자 데이터')
    monkeypatch.setattr(engine, 'environment', AsyncMock(return_value={'fixture': True}))

    class Ghidra:
        async def analyze(self, *a, **kw):
            return dict(
                status='success', schema_version='1.0', functions=[fn('fn1', 'main')], complete=True
            )

    class Docker:
        async def probe(self):
            return dict(ready=True)

        async def prepare_image(self, *a, **kw):
            return dict(status='success', image='fixture', image_id='sha256:fixture')

        async def execute(self, *a, **kw):
            return execution()

    monkeypatch.setattr(engine, '_ghidra', lambda: Ghidra())
    monkeypatch.setattr(engine, '_docker', lambda: Docker())

    class Client:
        async def probe(self):
            return dict(connected=True)

        async def close(self):
            pass

    monkeypatch.setattr(engine, '_client', lambda: Client())
    run = await engine.analyze(str(source), str(rebuilt), [{'id': 'baseline'}])
    assert run['status'] == 'success' and run['behavior']['match_rate'] == 1
    assert run['stages']['llm']['status'] == 'blocked' and len(run['evidence']) >= 8
    assert engine.report(run['run_id']).is_file()
    assert engine.store.load_run(run['run_id'])['attempt_id'] == run['attempt_id']
    rerun = await engine.recover(run['run_id'])
    assert rerun['status'] == 'success' and len(rerun['attempts']) == 2
    assert rerun['attempt_id'] != run['attempt_id']


@pytest.mark.asyncio
async def test_pipeline_bad_file_records_failure_and_report(tmp_path):
    path = tmp_path / 'unsupported.bin'
    path.write_bytes(b'not executable')
    engine = Engine(Settings(), data_dir=tmp_path / 'data')
    run = await engine.analyze(str(path), str(path), [{'id': 'baseline'}])
    assert run['status'] == 'partial' and run['diagnostics']
    assert run['stages']['dynamic']['status'] == 'blocked'
    assert engine.report(run['run_id']).exists()


@pytest.mark.asyncio
async def test_missing_ghidra_is_unmeasured_not_zero_similarity(tmp_path, monkeypatch):
    source = tmp_path / 'header-fixture.bin'
    source.write_bytes(elf_header())
    engine = Engine(Settings(), data_dir=tmp_path / 'data')
    monkeypatch.setattr(engine, 'environment', AsyncMock(return_value={}))

    class Missing:
        async def analyze(self, *a, **kw):
            return dict(status='blocked', functions=[], error='Ghidra not installed')

    monkeypatch.setattr(engine, '_ghidra', lambda: Missing())
    run = await engine.analyze(str(source), str(source), [{'id': 'baseline'}], mode='static')
    assert run['stages']['structure']['status'] == 'blocked'
    assert run['structure'] == {} and run['status'] == 'partial'


@pytest.mark.asyncio
async def test_cancel_and_duplicate_guard(tmp_path):
    import threading

    engine = Engine(Settings(), data_dir=tmp_path)
    engine._lock.acquire()
    with pytest.raises(RuntimeError):
        await engine.analyze('a', 'b', [{'id': 'x'}])
    engine._lock.release()
    cancel = threading.Event()
    cancel.set()
    run = await engine.analyze('a', 'b', [{'id': 'x'}], cancel=cancel)
    assert run['status'] == 'cancelled'


def test_atomic_save_failure_never_changes_previous_file(tmp_path, monkeypatch):
    path = tmp_path / 'settings.json'
    atomic_json(path, {'version': 1})
    monkeypatch.setattr('os.replace', lambda *args: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError):
        atomic_json(path, {'version': 2})
    assert json.loads(path.read_text())['version'] == 1
