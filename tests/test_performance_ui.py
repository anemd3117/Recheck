"""Qt fixture validation. No hardware probes or model inference are invoked."""

import asyncio
import os
import time
from datetime import datetime, timezone

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox
from recheck.gui import MainWindow
from recheck.models import Settings, atomic_json, save_settings


@pytest.fixture
def window(tmp_path, monkeypatch):
    monkeypatch.setenv('RECHECK_DATA_DIR', str(tmp_path))
    app = QApplication.instance() or QApplication([])

    class Engine:
        def __init__(self, settings, on_event=None):
            self.store = type('Store', (), {'root': tmp_path, 'list_runs': lambda self: []})()

    widget = MainWindow(Settings(model='gemma4:e4b'), engine_factory=Engine, auto_check=False)
    yield widget, app
    if widget.worker and widget.worker.isRunning():
        widget.cancel_work()
        widget.worker.wait(5000)
    widget.close()
    app.processEvents()


def hardware():
    return dict(
        schema_version='1.0',
        collected_at=datetime.now(timezone.utc).isoformat(),
        cpu=dict(name='Fixture CPU', usable_cores=8, usage_percent=10),
        memory=dict(
            total_bytes=16 * 1024**3, available_bytes=10 * 1024**3, safe_budget_bytes=6 * 1024**3
        ),
        gpus=[],
        disks=[],
        environment={'docker': {'ready': False}, 'ollama': {'connected': False}},
        warnings=[],
    )


def test_settings_stay_accessible_without_external_tools(window):
    widget, _ = window
    panel = widget.performance_panel
    assert widget.settings_tabs.tabText(0) == '도구 · 고급 설정'
    assert widget.settings_tabs.tabText(1) == '내 PC 벤치마크'
    panel.show_hardware(hardware())
    assert '확인 불가' in panel.cards['gpu'].value.text()
    assert panel.recommendations.topLevelItemCount() == 7
    assert widget.setting_fields['memory_mb'].isEnabled()
    assert not panel.detail.isVisible()
    assert not panel.reason.isVisible()


def test_no_automatic_setting_application_on_profile_or_measurement(window, monkeypatch, tmp_path):
    widget, _ = window
    before = widget.collect_settings().model_dump()
    panel = widget.performance_panel
    panel.show_hardware(hardware())
    for index in range(3):
        panel.profile.setCurrentIndex(index)
    assert widget.collect_settings().model_dump() == before
    assert not (tmp_path / 'settings.json').exists()
    monkeypatch.setattr(panel, 'confirm', lambda *a: False)
    # Explicit refusal must leave both disk and in-memory Settings unchanged.
    for i in range(panel.recommendations.topLevelItemCount()):
        row = panel.recommendations.topLevelItem(i)
        row.setCheckState(0, Qt.CheckState.Checked)
    panel.apply_selected()
    assert widget.collect_settings().model_dump() == before
    assert not (tmp_path / 'settings.json').exists()


def test_measurement_text_does_not_execute_html_and_saved_history_reloads(window, tmp_path):
    widget, _ = window
    benchmark = dict(
        benchmark_id='bench_' + 'a' * 32,
        created_at='2026-11-13T04:00:00+00:00',
        mode='quick',
        status='partial',
        hardware=hardware(),
        settings=widget.settings.model_dump(),
        stages={
            'ollama': {
                'status': 'success',
                'data': {
                    'model': '<b>fixture model</b>',
                    'samples': [
                        {
                            'eval_count': 8,
                            'tokens_per_second': 20.0,
                            'total_seconds': 0.8,
                            'load_duration_ns': 1000000,
                        }
                    ],
                },
                'duration_seconds': 0.8,
            },
            'docker': {
                'status': 'blocked',
                'error': {'message': '<script>untrusted</script>'},
                'data': None,
            },
        },
    )
    atomic_json(tmp_path / 'benchmarks' / benchmark['benchmark_id'] / 'result.json', benchmark)
    panel = widget.performance_panel
    panel.refresh_history()
    panel.load_history()
    assert panel.benchmark['benchmark_id'] == benchmark['benchmark_id']
    assert '20.00 토큰/초' in panel.measurement_text.toPlainText()
    assert '<b>fixture model</b>' in panel.measurement_text.toPlainText()
    assert panel.history.count() == 1


def test_cancel_and_analysis_share_one_worker_slot(window):
    widget, app = window
    ticks = []

    async def measuring(settings, cancel, emit):
        while not cancel.is_set():
            await asyncio.sleep(0.01)
        ticks.append('cancelled')
        return {'status': 'cancelled'}

    panel = widget.performance_panel
    panel._benchmark_active = True
    assert widget.launch_task('성능 측정 중', measuring)
    assert not panel.quick_button.isEnabled()
    assert not widget.start_button.isEnabled()
    assert not widget.launch_task('중복 분석', measuring)
    widget.cancel_work()
    end = time.monotonic() + 3
    while widget.worker.isRunning() and time.monotonic() < end:
        app.processEvents()
        time.sleep(0.005)
    app.processEvents()
    assert ticks == ['cancelled']
    assert panel.quick_button.isEnabled()
    assert widget.start_button.isEnabled()


def test_manual_edits_after_preview_require_new_confirmation(window, monkeypatch):
    widget, _ = window
    panel = widget.performance_panel
    panel.show_hardware(hardware())
    for i in range(panel.recommendations.topLevelItemCount()):
        row = panel.recommendations.topLevelItem(i)
        row.setCheckState(
            0,
            (
                Qt.CheckState.Checked
                if row.data(0, Qt.ItemDataRole.UserRole) == 'cpu'
                else Qt.CheckState.Unchecked
            ),
        )
    widget.setting_fields['cpu'].setValue(0.5)
    monkeypatch.setattr(panel, 'confirm', lambda *a: True)
    panel.apply_selected()
    assert widget.setting_fields['cpu'].value() == 0.5
    assert '바뀌어' in widget.banner.text()


def test_restored_model_and_url_match_saved_settings(window):
    widget, _ = window
    widget.models_done(
        [{'name': 'fixture:new', 'digest': 'new'}, {'name': 'fixture:old', 'digest': 'old'}]
    )
    restored = Settings(model='fixture:old', ollama_url='http://127.0.0.1:11435')
    save_settings(restored, root=widget.performance_panel.root)
    widget.performance_panel.sync_settings(restored)
    assert widget.collect_settings().model == 'fixture:old'
    assert widget.collect_settings().ollama_url == 'http://127.0.0.1:11435'
    assert widget.collect_settings().capabilities == {}
    assert not widget.mode.model().item(1).isEnabled()


def test_old_history_does_not_replace_latest_hardware(window, tmp_path):
    widget, _ = window
    panel = widget.performance_panel
    current = hardware()
    panel.show_hardware(current)
    old = hardware()
    old['cpu']['name'] = 'Past PC'
    old['collected_at'] = '2026-01-01T00:00:00+00:00'
    panel.show_benchmark(
        {'benchmark_id': 'bench_' + 'b' * 32, 'hardware': old, 'stages': {}, 'mode': 'quick'},
        historical=True,
    )
    assert panel.hardware['cpu']['name'] == 'Fixture CPU'
    import json

    assert (
        json.loads((tmp_path / 'benchmarks' / 'latest-hardware.json').read_text(encoding='utf-8'))
        == current
    )


def test_backend_measurement_fields_are_visible_with_units(window):
    widget, _ = window
    panel = widget.performance_panel
    record = dict(
        benchmark_id='bench_' + 'c' * 32,
        hardware=hardware(),
        stages={
            'cpu': {
                'status': 'success',
                'data': {
                    'single_operations_per_second': 12.5,
                    'parallel_operations_per_second': 31.0,
                },
            },
            'memory': {'status': 'success', 'data': {'copy_bytes_per_second': 50 * 1024**2}},
            'disk': {'status': 'skipped', 'data': None},
            'ghidra': {'status': 'skipped', 'data': None},
            'ollama': {'status': 'skipped', 'data': None},
            'docker': {'status': 'skipped', 'data': None},
        },
        status='success',
        mode='quick',
    )
    panel.show_benchmark(record)
    panel.measurements.setCurrentItem(panel.measurements.topLevelItem(0))
    assert '12.5' in panel.measurement_text.toPlainText()
    panel.measurements.setCurrentItem(panel.measurements.topLevelItem(1))
    assert '50.00 MiB/초' in panel.measurement_text.toPlainText()
    assert panel.progress.value() == panel.progress.maximum()


def test_model_switch_invalidates_old_recommendation_before_application(window, monkeypatch):
    widget, _ = window
    panel = widget.performance_panel
    panel.show_hardware(hardware())
    panel.recommendations.topLevelItem(0).setCheckState(0, Qt.CheckState.Checked)
    widget.settings.model = 'different:model'
    monkeypatch.setattr(panel, 'confirm', lambda *a: True)
    panel.apply_selected()
    assert '바뀌어' in widget.banner.text()
    assert panel.preview_settings['model'] == 'different:model'


def test_hardware_details_show_collected_devices_without_guessing_missing_values(window):
    widget, _ = window
    snapshot = hardware()
    snapshot['cpu'].update(
        vendor='Fixture vendor',
        physical_cores=4,
        logical_cores=8,
        architecture='AMD64',
        max_mhz=4200,
    )
    snapshot['gpus'] = [
        dict(
            name='<b>Fixture GPU</b>',
            vendor='Fixture',
            driver='123',
            vram_total_bytes=8 * 1024**3,
            vram_used_bytes=2 * 1024**3,
            vram_free_bytes=6 * 1024**3,
        )
    ]
    snapshot['memory']['modules'] = [
        dict(locator='slot 1', capacity_bytes=16 * 1024**3, manufacturer='Fixture RAM')
    ]
    snapshot['environment']['docker'].update(
        context='desktop-linux',
        cpus=8,
        memory_bytes=8 * 1024**3,
        images=[dict(reference='fixture:linux', present=True, os='linux', architecture='amd64')],
    )
    snapshot['environment']['ollama']['models'] = [
        dict(
            name='fixture:model',
            size=1024**3,
            details=dict(parameter_size='2B', quantization_level='Q4', context_length=8192),
        )
    ]
    widget.performance_panel.show_hardware(snapshot)
    text = widget.performance_panel.hardware_text.toPlainText()
    assert 'Fixture vendor' in text and '물리 코어 4개' in text
    assert '기본 클럭 확인 불가' in text
    assert '<b>Fixture GPU</b>' in text and '여유 6.0 GiB' in text
    assert 'slot 1' in text and 'desktop-linux' in text and 'fixture:linux' in text
    assert 'fixture:model' in text and '8192 토큰' in text


def test_ollama_prompt_and_resource_metrics_and_ghidra_timing_are_visible(window):
    widget, _ = window
    panel = widget.performance_panel
    panel.show_benchmark(
        dict(
            benchmark_id='bench_' + 'd' * 32,
            hardware=hardware(),
            mode='detailed',
            status='success',
            stages={
                'ollama': dict(
                    status='success',
                    data=dict(
                        model='fixture:model',
                        samples=[
                            dict(
                                eval_count=16,
                                tokens_per_second=8.0,
                                total_seconds=3.5,
                                prompt_eval_count=12,
                                prompt_eval_duration_ns=500000000,
                                eval_duration_ns=2000000000,
                                load_duration_ns=1000000000,
                                ollama_size_vram_bytes=1024**3,
                                resource_before=dict(
                                    memory_used_bytes=4 * 1024**3, cpu_usage_percent=12
                                ),
                                resource_after=dict(
                                    memory_used_bytes=5 * 1024**3, cpu_usage_percent=25
                                ),
                            )
                        ],
                    ),
                ),
                'ghidra': dict(
                    status='success',
                    data=dict(
                        process_seconds=4.5,
                        function_count=3,
                        timing_scope='세부 시간과 최대 메모리는 별도 계측하지 않았습니다.',
                    ),
                ),
            },
        )
    )
    text = panel.measurement_text.toPlainText()
    assert '입력 12 토큰' in text and '프롬프트 처리: 0.500초' in text
    assert '토큰 생성: 2.000초' in text
    assert '추론 전 RAM 사용 4.0 GiB' in text and '추론 후 RAM 사용 5.0 GiB' in text
    assert 'CPU 사용률 25%' in text and 'Ollama 보고 VRAM 할당: 1.0 GiB' in text
    panel.measurements.setCurrentItem(panel.measurements.topLevelItem(1))
    text = panel.measurement_text.toPlainText()
    assert '도구 전체 실행 시간(초): 4.5' in text and '별도 계측하지 않았습니다' in text
