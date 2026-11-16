"""Qt fixture checks; do not execute any analysed binary or external tool."""

import asyncio
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from shiboken6 import delete
from recheck.gui import MainWindow, DEFAULT_TESTS
from recheck.models import Settings


def test_file_card_explains_unverified_runtime_without_promising_comparison(app):
    from recheck.gui import FileCard

    widget = FileCard('original')
    try:
        widget.path.setText('fixture.exe')
        widget.inspection_done({'path': 'fixture.exe', 'value': {
            'supported': True, 'format': 'PE', 'architecture': 'x86-64',
            'execution_validation': 'unverified',
            'reason': 'Windows 격리 실행 환경 검증이 필요합니다.',
        }})
        assert 'Windows 격리' in widget.info.text()
        assert '비교할 수 있는 파일입니다' not in widget.info.text()
    finally:
        widget.close()
        delete(widget)


class FakeStore:
    def __init__(self, root):
        self.root = root

    def list_runs(self):
        return []


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    monkeypatch.setenv("RECHECK_DATA_DIR", str(tmp_path))

    class Engine:
        def __init__(self, settings, on_event=None):
            self.store = FakeStore(tmp_path)

        async def environment(self, cancel=None):
            return {"ollama": {"connected": False, "error": "fixture unavailable"}}

    widget = MainWindow(Settings(), engine_factory=Engine, auto_check=False)
    yield widget
    if widget.worker and widget.worker.isRunning():
        widget.worker.cancel_event.set()
        widget.worker.wait(5000)
    widget.close()
    app.processEvents()
    delete(widget)
    app.processEvents()


def settle(app, predicate, timeout=3):
    until = time.monotonic() + timeout
    while not predicate() and time.monotonic() < until:
        app.processEvents()
        time.sleep(0.005)
    app.processEvents()
    assert predicate()


def test_first_screen_defaults_and_test_schema(window):
    assert window.pages.count() == 6
    assert window.navigation.currentRow() == 0
    assert window.mode.currentData() == "automatic"
    assert window.settings.capabilities == {}
    assert not window.mode.model().item(1).isEnabled()
    assert window.tests_value()[0]["id"] == "baseline"
    assert window.tests_value()[0]["stdin"] == ""


def test_comparison_reads_pending_easy_input_edits_and_keeps_limits(window):
    import json

    window.tests_editor.setPlainText(json.dumps([dict(
        id='literal', args=[], stdin='old\r\n', timeout_seconds=7,
        env={'MODE': 'qa'}, aux_files=['C:/한글/입력.txt'],
    )]))
    window.input_panel.args_input.setText('"두 단어" 100')
    window.input_panel.stdin_input.setPlainText('새 입력\n')
    case = window.tests_value()[0]
    assert case['args'] == ['두 단어', '100']
    assert case['stdin'] == '새 입력\n'
    assert case['timeout_seconds'] == 7
    assert case['env'] == {'MODE': 'qa'}
    assert case['aux_files'] == ['C:/한글/입력.txt']


def test_invalid_pending_input_blocks_comparison_instead_of_using_old_value(window):
    window.input_panel.args_input.setText('"닫히지 않은 값')
    assert window.validate_inputs() is None
    assert window.tests_editor.toPlainText().find('baseline') >= 0


def test_pending_easy_input_survives_opening_advanced_tab_before_run(window):
    window.input_panel.args_input.setText('100')
    window.input_panel.tabs.setCurrentIndex(1)
    assert window.tests_value()[0]['args'] == ['100']


def test_static_file_card_distinguishes_comparison_from_execution(app):
    from recheck.gui import FileCard

    widget = FileCard('원본')
    try:
        widget.path.setText('fixture.dll')
        widget.inspection_done({'path': 'fixture.dll', 'value': {
            'supported': False, 'static_supported': True, 'execution_supported': False,
            'format': 'PE', 'architecture': 'x86',
            'static_reason': 'REA 정적 분석으로 비교합니다.',
            'execution_reason': 'DLL은 직접 실행할 수 없습니다.',
            'reason': 'DLL은 직접 실행할 수 없습니다.',
        }})
        assert '정적 비교 가능' in widget.info.text()
        assert '직접 실행할 수 없습니다' in widget.info.text()
    finally:
        widget.close()
        delete(widget)


def test_managed_comparison_exposes_changed_cil_without_native_similarity_claim(window):
    window.show_result({'run_id': 'managed-fixture', 'status': 'partial', 'structure': {
        'analysis_kind': 'managed',
        'matches': [{'original': '0x06000001', 'reimplementation': '0x06000001',
                     'score': 1, 'cil_observation': 'different'}],
        'managed_differences': [{'original': '0x06000001', 'reimplementation': '0x06000001',
                                 'original_name': 'Sample.Add', 'reimplementation_name': 'Sample.Add',
                                 'original_cil': {'il_sha256': 'a' * 64},
                                 'reimplementation_cil': {'il_sha256': 'b' * 64}}],
    }})
    rows = [window.measurements.topLevelItem(i) for i in range(window.measurements.topLevelItemCount())]
    managed = next(row for row in rows if '.NET' in row.text(0))
    assert managed.childCount() == 2
    assert 'CIL' in managed.child(1).text(0)
    assert 'Sample.Add' in managed.child(1).text(1)
    assert '유사도' not in managed.child(0).text(3)


def test_file_and_json_validation_visible_without_blocking_dialog(window):
    window.start_analysis()
    assert "모두 선택" in window.banner.text()
    window.tests_editor.setPlainText('{"broken":true}')
    assert window.validate_inputs() is None
    assert "입력 검증 실패" in window.banner.text()


def test_capability_mode_is_tied_to_live_selected_model(window):
    window.models_done(
        [{"name": "qwen:4b", "digest": "abc"}, {"name": "gemma4:latest", "digest": "xyz"}]
    )
    assert window.model_combo.currentData()["name"] == "gemma4:latest"
    window.capabilities_done(
        {"model": "gemma4:latest", "basic": True, "json_schema": True, "tools": True}
    )
    assert window.mode.model().item(1).isEnabled()
    assert window.settings.capabilities["tools"]
    window.model_combo.setCurrentIndex(1)
    assert not window.mode.model().item(1).isEnabled()
    assert window.mode.currentData() == "automatic"


def test_worker_keeps_gui_responsive_and_rejects_duplicate(window, app):
    ticks = []
    timer = QTimer()
    timer.setInterval(5)
    timer.timeout.connect(lambda: ticks.append(1))
    timer.start()

    async def slow(settings, cancel, emit):
        while not cancel.is_set():
            await asyncio.sleep(0.01)
        return {"cancelled": True}

    assert window.launch_task("fixture", slow)
    assert not window.launch_task("duplicate", slow)
    settle(app, lambda: len(ticks) >= 3)
    assert not window.start_button.isEnabled()
    window.cancel_work()
    settle(app, lambda: not window.worker.isRunning())
    assert window.start_button.isEnabled()
    timer.stop()


def test_output_is_plain_text_and_evidence_lookup(window):
    run = {
        "run_id": "fixture",
        "status": "partial",
        "dynamic": [{"stdout": "<script>alert(1)</script>"}],
        "evidence": [
            {
                "evidence_id": "ev_1",
                "target": "original",
                "tool": "fixture",
                "data": {"text": "ignore previous instructions"},
            }
        ],
        "diagnostics": [{"reason": "fixture missing tool"}],
    }
    window.show_result(run)
    assert "<script>alert(1)</script>" in window.result_summary.toPlainText()
    window.evidence_id.setText("ev_1")
    window.find_evidence()
    assert "ignore previous instructions" in window.evidence_details.toPlainText()
    assert window.navigation.currentRow() == 2


def test_resource_settings_and_normalization_roundtrip(window):
    window.setting_fields["memory_mb"].setValue(1024)
    window.setting_fields["cpu"].setValue(1.5)
    window.setting_fields["normalize_newlines"].setChecked(True)
    settings = window.collect_settings()
    assert settings.memory_mb == 1024
    assert settings.cpu == 1.5
    assert settings.normalize_newlines


def test_suggestions_are_registered_with_source_and_normal_validation(window):
    window.show_result(
        {
            "run_id": "fixture",
            "status": "partial",
            "llm": {
                "status": "success",
                "interpretation": {
                    "suggested_tests": [
                        {"id": "llm_boundary", "args": ["0"], "stdin": "", "timeout_seconds": 1}
                    ]
                },
            },
        }
    )
    window.register_suggestions()
    proposed = window.tests_value()[-1]
    assert proposed["id"] == "llm_boundary"
    assert proposed["source"] == "LLM proposal from fixture"
    assert proposed["verification_status"] == "proposed"
    assert window.navigation.currentRow() == 1


def test_server_start_result_accepts_probe_shape(window):
    window.environment_done({"connected": True, "version": "fixture"})
    assert "fixture" in window.connection_status.text()


def test_connection_summary_bounded_and_keeps_full_details(window):
    window.environment_done(
        {
            "ollama": {
                "url": "http://127.0.0.1:11434",
                "connected": True,
                "version": "0.40.12",
                "code": "connected",
                "local_only_setting": "기존 서버 확인 필요 " * 500,
                "candidates": ["a" * 1000],
            },
            "docker": {"ready": True, "version": {"Server": {"Os": "linux", "Arch": "amd64"}}},
            "ghidra": {
                "ready": False,
                "reason": "설치 경로 없음",
                "java_major": 25,
                "java": {"stderr": 'openjdk version "25.0.4.1"\r\nOpenJDK Runtime Environment'},
            },
        }
    )
    text = window.connection_status.text()
    assert "0.40.12" in text
    assert text.count("\n") == 3
    assert len(text) < 400
    assert "a" * 1000 in window.connection_status.toolTip()
    assert "linux / amd64" in window.env_summary.text()
    assert "25.0.4.1" in window.env_summary.text()
    assert "설치 경로 없음" in window.env_summary.text()


def test_developer_scenario_loads_relative_input_json_and_rejects_unknown_actions(tmp_path):
    import json
    from recheck.gui import load_verification_scenario

    (tmp_path / "file-a").write_bytes(b"schema fixture")
    (tmp_path / "file-b").write_bytes(b"schema fixture")
    (tmp_path / "tests.json").write_text(json.dumps(DEFAULT_TESTS), encoding="utf-8")
    config = {
        "original": "file-a",
        "reimplementation": "file-b",
        "tests": "tests.json",
        "output_dir": "results",
    }
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    scenario = load_verification_scenario(path)
    assert scenario["original"] == str(tmp_path / "file-a")
    assert scenario["tests"][0]["id"] == "baseline"
    assert scenario["output_dir"] == str(tmp_path / "results")
    config["shell"] = "must never be accepted"
    path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ValueError):
        load_verification_scenario(path)

    del config["shell"]
    path.write_text(json.dumps(config), encoding="utf-8")
    (tmp_path / "results").mkdir()
    (tmp_path / "results" / "scenario.json").write_text('{"historical":true}', encoding="utf-8")
    with pytest.raises(ValueError, match="이전 근거"):
        load_verification_scenario(path)


def test_capability_response_cannot_enable_another_model_or_endpoint(window):
    window.models_done([{"name": "model:a", "digest": "one"}, {"name": "model:b", "digest": "two"}])
    context = window.capability_context()
    window.model_combo.setCurrentIndex(1)
    window.capabilities_done({"model": "model:a", "basic": True, "tools": True}, context)
    assert not window.mode.model().item(1).isEnabled()
    assert window.settings.capabilities == {}
    assert "변경" in window.banner.text()
    context = window.capability_context()
    window.server_url.setText("http://127.0.0.1:11435")
    window.capabilities_done({"model": "model:b", "basic": True, "tools": True}, context)
    assert not window.mode.model().item(1).isEnabled()
    assert window.settings.capabilities == {}


def test_server_edit_invalidates_previously_verified_agent_mode(window):
    window.models_done([{"name": "model:a", "digest": "one"}])
    window.capabilities_done({"model": "model:a", "basic": True, "tools": True})
    assert window.mode.model().item(1).isEnabled()
    window.mode.setCurrentIndex(1)
    window.server_url.setText("http://localhost:11435")
    assert window.mode.currentData() == "automatic"
    assert not window.mode.model().item(1).isEnabled()
    assert window.settings.capabilities == {}


def test_launch_entrypoint_preserves_main_failure_exit_code():
    """The PyInstaller entrypoint must expose failed scenarios to CI callers."""
    from pathlib import Path
    import subprocess
    import sys

    script = (
        "import runpy,sys,types; module=types.ModuleType('recheck.gui'); "
        "module.main=lambda:7; sys.modules['recheck.gui']=module; "
        "runpy.run_path(sys.argv[1],run_name='__main__')"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(Path(__file__).resolve().parents[1] / "launch.py")],
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 7


@pytest.fixture
def persisted_gui_result(window, tmp_path):
    import json
    import base64
    from recheck.report import render_report

    run = {
        "run_id": "run_fixture",
        "attempt_id": "attempt_fixture",
        "status": "success",
        "files": {},
        "behavior": {
            "matched": 0,
            "denominator": 1,
            "match_rate": 0,
            "tests": [
                {
                    "test_id": "boundary",
                    "execution_status": "measured",
                    "match": False,
                    "original_exit_code": 0,
                    "reimplementation_exit_code": 0,
                }
            ],
        },
        "dynamic": [
            {
                "test_id": "boundary",
                "input": {"args": ["4"]},
                "original": {"stdout": "output=<a>\n", "stderr": "", "exit_code": 0},
                "reimplementation": {"stdout": "output=<b>\n", "stderr": "", "exit_code": 0},
            }
        ],
        "evidence": [],
    }
    run['tests'] = [{'id': 'boundary', 'args': ['4']}]
    run['settings'] = dict(normalize_newlines=False, trim_trailing_whitespace=False)
    for row in run['dynamic']:
        for side in ('original', 'reimplementation'):
            observation = row[side]
            observation.update(status='success', stdout_b64=base64.b64encode(observation['stdout'].encode()).decode(),
                               stderr_b64='')
    folder = tmp_path / "runs" / run["run_id"]
    folder.mkdir(parents=True)
    (folder / "result.json").write_text(json.dumps(run), encoding="utf-8")
    render_report(run, folder / "report.html")
    window.show_result(run)
    return folder


def test_scenario_reads_back_result_report_and_gui(window, persisted_gui_result):
    from recheck.gui import verification_artifacts

    checks = verification_artifacts(window)
    assert checks["stored_result_matches_gui"]
    assert checks["gui_match_count"] == "0 / 1"
    assert checks["broken_report_links"] == []
    assert len(checks["result_sha256"]) == len(checks["report_sha256"]) == 64


@pytest.mark.parametrize("corruption", ["stored", "links", "gui", "report_rate", "report_output"])
def test_scenario_rejects_conflicting_or_broken_artifacts(window, persisted_gui_result, corruption):
    from recheck.gui import verification_artifacts

    if corruption == "stored":
        import json

        stored = dict(window.run_result, status="failed")
        (persisted_gui_result / "result.json").write_text(json.dumps(stored), encoding="utf-8")
    elif corruption == "gui":
        for index in range(window.measurements.topLevelItemCount()):
            item = window.measurements.topLevelItem(index)
            if item.text(0) == "입력별 동작 비교":
                item.child(0).setText(3, "일치")
    else:
        path = persisted_gui_result / "report.html"
        content = path.read_text(encoding="utf-8")
        if corruption == "links":
            content += '<a href="#ev_missing">missing</a>'
        elif corruption == "report_rate":
            content = content.replace("0.00%", "100.00%")
        else:
            content = content.replace("output=&lt;a&gt;\n", "incorrect")
        path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError):
        verification_artifacts(window)


def test_scenario_invokes_normal_gui_analysis_handler(
    window, app, tmp_path, monkeypatch, persisted_gui_result
):
    import json
    from recheck.gui import start_verification_scenario

    run = window.run_result
    window.run_result = {}
    called, exit_codes = [], []

    class ScenarioEngine:
        def __init__(self, settings, on_event=None):
            self.store = FakeStore(tmp_path)

        async def analyze(self, original, replacement, tests, mode, cancel, *, analysis_mode, ai_mode):
            called.append((original, replacement, tests, mode))
            return run

    window.engine_factory = ScenarioEngine
    async def ready(settings, cancel, emit, analysis_mode, paths):
        return {'can_analyze': True, 'environment': {}, 'files': []}
    monkeypatch.setattr(window, 'analysis_preflight', ready)
    original = tmp_path / "원본 fixture"
    replacement = tmp_path / "재구현 fixture"
    original.write_bytes(b"header fixture only")
    replacement.write_bytes(b"header fixture only")
    output = tmp_path / "새 검증"
    monkeypatch.setattr(app, "exit", lambda code: exit_codes.append(code))
    normal_start = window.start_analysis
    starts = []

    def record_start():
        starts.append(True)
        normal_start()

    monkeypatch.setattr(window, "start_analysis", record_start)
    start_verification_scenario(
        app,
        window,
        {
            "original": str(original),
            "reimplementation": str(replacement),
            "tests": DEFAULT_TESTS,
            "output_dir": str(output),
        },
    )
    settle(app, lambda: bool(exit_codes))
    assert starts == [True]
    assert len(called) == 1 and called[0][3] == "automatic"
    assert called[0][:2] == (str(original), str(replacement))
    assert exit_codes == [0]
    summary = json.loads((output / "scenario.json").read_text(encoding="utf-8"))
    assert summary["artifact_checks"]["stored_result_matches_gui"]
    assert summary["runtime_kind"] == "source"
    assert (output / "scenario.png").is_file()


def test_history_displays_engine_created_timestamp_and_legacy_field(window, monkeypatch):
    records = [
        {"run_id": "run_current", "created": "2026-11-13T03:00:00+00:00", "status": "success"},
        {"run_id": "run_legacy", "created_at": "2026-11-12T03:00:00+00:00", "status": "partial"},
    ]
    monkeypatch.setattr(window.engine.store, "list_runs", lambda: records)
    window.refresh_history()
    assert window.history.topLevelItem(0).text(1) == records[0]["created"]
    assert window.history.topLevelItem(1).text(1) == records[1]["created_at"]
    assert window.history.topLevelItem(0).text(2) == "성공"


def test_stage_labels_are_korean_while_event_identifiers_are_preserved(window):
    window.accept_event({"stage": "static_original", "status": "cached"})
    item = window.stage_items["static_original"]
    assert item.text(0) == "원본 프로그램 구조 살펴보기"
    assert item.text(1) == "이전 분석 결과 다시 사용"
    assert "static_original" in window.logs.toPlainText()
    window.show_result(
        {"run_id": "run_fixture", "status": "partial", "stages": {"dynamic": {"status": "blocked"}}}
    )
    stage_root = window.measurements.topLevelItem(2)
    assert stage_root.child(0).text(0) == "같은 입력으로 실행해 비교"
    assert stage_root.child(0).text(1) == "진행할 수 없음"


def test_model_default_prefers_exact_e4b_then_local_models(window):
    from recheck.models import load_settings

    window.models_done(
        [
            {"name": "gemma4:12b", "digest": "large"},
            {"name": "aaa:cloud", "remote_host": "remote"},
            {"name": "gemma4:e4b", "digest": "preferred"},
            {"name": "aaa:local", "digest": "small"},
        ]
    )
    assert window.model_combo.currentData()["name"] == "gemma4:e4b"
    assert load_settings().model == "gemma4:e4b"
    names = [window.model_combo.itemData(i)["name"] for i in range(window.model_combo.count())]
    assert names == ["gemma4:e4b", "aaa:local", "gemma4:12b", "aaa:cloud"]


def test_saved_installed_model_is_preserved_and_missing_selection_falls_back(window):
    from recheck.models import load_settings

    window.settings.model = "gemma4:12b"
    assert window.collect_settings().model == "gemma4:12b"  # List is not loaded at startup yet.
    installed = [{"name": "gemma4:12b"}, {"name": "gemma4:e4b"}]
    window.models_done(installed)
    assert window.model_combo.currentData()["name"] == "gemma4:12b"
    assert load_settings().model == "gemma4:12b"
    window.models_done([{"name": "gemma4:e4b"}])
    assert load_settings().model == "gemma4:e4b"


def test_user_model_selection_is_saved_with_capabilities_invalidated(window):
    from recheck.models import load_settings

    window.models_done([{"name": "gemma4:12b"}, {"name": "gemma4:e4b"}])
    window.capabilities_done({"model": "gemma4:e4b", "basic": True, "tools": True})
    assert load_settings().capabilities["tools"]
    window.model_combo.setCurrentIndex(1)
    saved = load_settings()
    assert saved.model == "gemma4:12b"
    assert saved.capabilities == {}
    assert not window.mode.model().item(1).isEnabled()


def test_previous_endpoint_model_refresh_is_discarded(window):
    window.models_done([{"name": "gemma4:e4b"}])
    endpoint = window.server_url.text()
    window.server_url.setText("http://127.0.0.1:11435")
    window.models_done([{"name": "wrong:endpoint"}], expected_endpoint=endpoint)
    assert window.model_combo.currentData()["name"] == "gemma4:e4b"
    assert "이전 모델 목록을 적용하지 않았습니다" in window.banner.text()
    assert not window.mode.model().item(1).isEnabled()


def test_empty_installed_model_list_clears_missing_saved_model(window):
    from recheck.models import load_settings

    window.settings.model = "missing:model"
    window.models_done([])
    assert window.model_combo.currentData() is None
    assert load_settings().model == ""


def test_product_modes_preserve_comparison_as_default(window):
    assert window.product_mode.currentData() == "comparison"
    assert window.product_mode.findData('package') >= 0
    assert window.reconstruction_panel.isHidden()
    assert not window.reimplementation.isHidden()
    assert not window.approve_build.isChecked()
    assert window.max_repairs.minimum() == 0 and window.max_repairs.maximum() == 2
    window.product_mode.setCurrentIndex(1)
    assert not window.reconstruction_panel.isHidden()
    assert window.reimplementation.isHidden()
    assert not window.source_path.isEnabled()
    window.product_mode.setCurrentIndex(2)
    assert window.source_path.isEnabled()
    assert "저장" in window.start_button.text()
    assert "준비 중" in window.analysis_note.text()


def test_modernization_requires_explicit_existing_source(window):
    window.product_mode.setCurrentIndex(2)
    window.original.path.setText("original-fixture")
    window.requirements_editor.setPlainText("기존 동작을 보존하고 이름을 명확하게 바꿔줘")
    window.start_analysis()
    assert "기존 C/C++ 소스 파일" in window.banner.text()
    assert window.worker is None


@pytest.mark.parametrize(
    "product,approved,language,repairs", [(1, False, "c", 0), (2, True, "cpp", 2)]
)
def test_generation_modes_use_shared_service_with_explicit_permissions(
    window, app, tmp_path, product, approved, language, repairs
):
    calls = []
    source = tmp_path / "원본 소스.cpp"
    original_bytes = b"// fixture original source\nint main(){return 0;}\n"
    source.write_bytes(original_bytes)

    class Service:
        def __init__(self, settings, data_dir=None, on_event=None):
            assert data_dir == tmp_path

        async def run(self, original, tests, requirements, **options):
            calls.append((original, tests, requirements, options))
            return {
                "workflow_id": "workflow_fixture",
                "status": "blocked",
                "completion_reason": "build_approval_required",
                "workspace": str(tmp_path),
                "source_path": str(tmp_path / "revisions/main.c"),
                "specification": {"observed": []},
            }

    window.reconstruction_factory = Service
    window.product_mode.setCurrentIndex(product)
    window.original.path.setText("원본 ELF fixture")
    window.requirements_editor.setPlainText("등록된 입력의 기존 동작을 보존해줘")
    window.source_path.setText(str(source))
    window.source_language.setCurrentIndex(0 if language == "c" else 1)
    window.approve_build.setChecked(approved)
    window.max_repairs.setValue(repairs)
    window.start_analysis()
    settle(app, lambda: bool(window.worker) and not window.worker.isRunning())
    assert len(calls) == 1
    assert calls[0][0] == "원본 ELF fixture"
    assert calls[0][1][0]["id"] == "baseline"
    options = calls[0][3]
    assert options["mode"] == ("reconstruction" if product == 1 else "modernization")
    assert options["source_path"] == (str(source) if product == 2 else None)
    assert options["approve_build"] is approved
    assert options["language"] == language and options["max_repairs"] == repairs
    assert source.read_bytes() == original_bytes
    assert "workflow_fixture" in window.workflow_view.toPlainText()
    assert not window.run_result


def test_generation_worker_cancels_and_rejects_duplicate_without_mutating_approval(window, app):
    calls = []

    class Service:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, original, tests, requirements, **options):
            calls.append(options)
            while not options["cancel"].is_set():
                await asyncio.sleep(0.01)
            return {"workflow_id": "workflow_cancelled", "status": "cancelled"}

    window.reconstruction_factory = Service
    window.product_mode.setCurrentIndex(1)
    window.original.path.setText("fixture")
    window.requirements_editor.setPlainText("현재 동작을 보존해줘")
    window.start_analysis()
    settle(app, lambda: bool(calls))
    assert not window.approve_build.isEnabled() and not window.start_button.isEnabled()
    window.start_analysis()
    assert len(calls) == 1
    window.cancel_work()
    settle(app, lambda: not window.worker.isRunning())
    assert calls[0]["approve_build"] is False
    assert window.workflow_result["status"] == "cancelled"
    assert window.approve_build.isEnabled()


def test_workflow_opens_authoritative_comparison_and_workflow_history(window, monkeypatch):
    run = {
        "run_id": "run_fixture",
        "status": "success",
        "behavior": {"matched": 1, "denominator": 1, "match_rate": 1},
    }
    workflow = {
        "workflow_id": "workflow_fixture",
        "status": "success",
        "comparison_run_id": "run_fixture",
        "created": "2026-11-13",
        "specification": {"scope": "fixture"},
    }
    monkeypatch.setattr(window.engine.store, "load_run", lambda rid: run, raising=False)
    monkeypatch.setattr(window.engine.store, "list_workflows", lambda: [workflow], raising=False)
    monkeypatch.setattr(window.engine.store, "load_workflow", lambda wid: workflow, raising=False)
    window.refresh_history()
    window.history.setCurrentItem(window.history.topLevelItem(0))
    window.open_history()
    assert window.run_result == run
    assert window.workflow_result == workflow
    assert window.selected_run_id() == "run_fixture"
    assert "workflow_fixture" in window.workflow_view.toPlainText()


def test_natural_language_command_uses_engine_and_refreshes_persisted_run(window, app, tmp_path):
    calls = []
    record = {
        "run_id": "run_fixture",
        "status": "partial",
        "stages": {"dynamic": {"status": "partial"}},
    }

    class CommandEngine:
        def __init__(self, *args, **kwargs):
            self.store = FakeStore(tmp_path)
            self.store.load_run = lambda rid: record

        async def command(self, rid, instruction, cancel=None):
            calls.append((rid, instruction, cancel))
            return {
                "run_id": rid,
                "status": "partial",
                "instruction": instruction,
                "agent": {"status": "blocked"},
            }

    window.engine_factory = CommandEngine
    window.show_result(record)
    window.question_text.setText("불일치 테스트의 근거를 조회해줘")
    window.run_analysis_command()
    settle(app, lambda: bool(window.worker) and not window.worker.isRunning())
    assert calls[0][:2] == ("run_fixture", "불일치 테스트의 근거를 조회해줘")
    assert "불일치 테스트의 근거를 조회해줘" in window.answer_view.toPlainText()
    assert window.run_result == record
    assert window.result_tabs.currentWidget() == window.question_panel
