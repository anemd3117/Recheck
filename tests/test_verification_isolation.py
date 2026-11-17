"""QA command-line options must not change a user's normal settings or history."""

import json
from pathlib import Path
import runpy
import signal
import sys

import pytest

from recheck import engine as engine_module
from recheck.models import Settings
from recheck.storage import Store, new_id


@pytest.mark.parametrize('explicit_data_dir', [True, False])
def test_real_comparison_cli_keeps_qa_settings_and_history_separate(
    tmp_path, monkeypatch, explicit_data_dir
):
    personal = tmp_path / 'personal'
    personal.mkdir()
    personal_settings = personal / 'settings.json'
    personal_settings.write_text(
        Settings(model='personal-model', ghidra_path='personal-ghidra').model_dump_json(),
        encoding='utf-8',
    )
    before = personal_settings.read_bytes()
    monkeypatch.setenv('RECHECK_DATA_DIR', str(personal))
    qa_root = tmp_path / 'qa'
    qa_root.mkdir()
    qa_settings = qa_root / 'settings.json'
    qa_settings.write_text(Settings(ghidra_path='qa-ghidra').model_dump_json(), encoding='utf-8')
    qa_before = qa_settings.read_bytes()
    instances = []

    class HarmlessEngine:
        def __init__(self, settings, data_dir=None, on_event=None):
            self.settings = settings
            self.store = Store(data_dir)
            instances.append(self)

        async def analyze(self, *args, **kwargs):
            run = dict(
                run_id=new_id('run'), attempt_id=new_id('attempt'), status='success',
                stages={}, files={}, static={}, structure={'score': None, 'matches': []},
                behavior={}, llm={}, agent={}, diagnostics=[], evidence=[],
            )
            self.store.save(run)
            return run

        def report(self, run_id):
            return self.store.run_dir(run_id) / 'report.html'

    # Replace binary analysis at its external-tool boundary, keeping real settings,
    # SQLite history, command-line parsing, and summary writes under test.
    monkeypatch.setattr(engine_module, 'Engine', HarmlessEngine)
    monkeypatch.setattr(signal, 'signal', lambda *args: None)
    summary = tmp_path / 'results' / 'summary.json'
    tests = tmp_path / 'tests.json'
    tests.write_text('[{"id":"known","stdin":"42"}]', encoding='utf-8')
    script = Path(__file__).resolve().parents[1] / 'scripts/verify_real_comparison.py'
    argv = [str(script), '--original', 'original', '--reimplementation', 'changed',
            '--tests', str(tests), '--summary', str(summary), '--model', 'qa-model']
    if explicit_data_dir:
        argv += ['--data-dir', str(qa_root)]
    monkeypatch.setattr(sys, 'argv', argv)

    with pytest.raises(SystemExit) as result:
        runpy.run_path(str(script), run_name='__main__')

    assert result.value.code == 0
    assert personal_settings.read_bytes() == before
    assert set(personal.iterdir()) == {personal_settings}
    assert qa_settings.read_bytes() == qa_before
    assert json.loads(summary.read_text(encoding='utf-8'))['model'] == 'qa-model'
    engine = instances[0]
    assert engine.settings.active_ai_profile == 'legacy-local'
    if explicit_data_dir:
        assert engine.store.root == qa_root
        assert engine.settings.ghidra_path == 'qa-ghidra'
    else:
        assert engine.store.root.parent == summary.parent
        assert engine.settings.ghidra_path == ''
