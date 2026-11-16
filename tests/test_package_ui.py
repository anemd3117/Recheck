"""Real package results and real export files; only the file picker is replaced."""

import copy
import html
import importlib
import importlib.util
import json
import os
from pathlib import Path
import zipfile

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication, QDialog

from recheck.package_comparison import compare_packages

_APP = None


def package_ui():
    assert importlib.util.find_spec('recheck.package_ui') is not None, 'Package result UI is missing'
    return importlib.import_module('recheck.package_ui')


@pytest.fixture
def app():
    global _APP
    _APP = QApplication.instance() or QApplication([])
    yield _APP
    # Qt top-level dialogs outlive their local Python variable through signal
    # connections. Dispose test-owned windows while QApplication still exists.
    for widget in _APP.topLevelWidgets():
        if isinstance(widget, package_ui().PackageResultDialog):
            widget.close()
            widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    _APP.processEvents()


@pytest.fixture
def result(tmp_path):
    for name, entries in {
        'old.zip': [('same', b'abc'), ('changed', b'before'), ('removed', b'removed')],
        'new.zip': [('same', b'abc'), ('changed', b'after'), ('added', b'added')],
    }.items():
        with zipfile.ZipFile(tmp_path / name, 'w') as archive:
            for member, content in entries:
                archive.writestr(member, content)
    return compare_packages(tmp_path / 'old.zip', tmp_path / 'new.zip')


def find_row(tree, path):
    for index in range(tree.topLevelItemCount()):
        group = tree.topLevelItem(index)
        for child in range(group.childCount()):
            item = group.child(child)
            if item.text(1) == path:
                return item
    pytest.fail(f'No inventory row for {path}')


def test_complete_result_shows_categories_paths_and_real_old_new_evidence(app, result):
    dialog = package_ui().PackageResultDialog(result)
    dialog.show()
    app.processEvents()
    assert not dialog.isModal() and dialog.isVisible()
    assert dialog.width() <= 900 and dialog.height() <= 600
    assert '완료' in dialog.summary.text() and '추가 1' in dialog.summary.text()
    assert '실행 동작' in dialog.scope_label.text() and '판정하지' in dialog.scope_label.text()
    assert dialog.summary.textFormat() == Qt.TextFormat.PlainText
    assert dialog.tree.topLevelItemCount() == 4
    row = find_row(dialog.tree, 'same')
    assert row.text(2) == row.text(3) == '3'
    assert row.text(4) == row.text(5) == 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad'
    assert find_row(dialog.tree, 'added').text(2) == '—'
    assert find_row(dialog.tree, 'removed').text(3) == '—'
    assert find_row(dialog.tree, 'changed').text(2) == '6'
    assert find_row(dialog.tree, 'changed').text(3) == '5'
    dialog.close_button.click()
    app.processEvents()
    assert not dialog.isVisible() and dialog.result() == QDialog.DialogCode.Rejected


@pytest.mark.parametrize('status', ['failed', 'partial', 'cancelled'])
def test_incomplete_results_never_report_an_empty_match_and_retain_diagnostics(app, tmp_path, status):
    path = tmp_path / 'broken.zip'
    path.write_bytes(b'not zip')
    result = compare_packages(path, path)
    result['status'] = status
    result['code'] = 'cancelled' if status == 'cancelled' else 'package_invalid'
    dialog = package_ui().PackageResultDialog(result)
    assert '전체 구성을 비교하지 못했습니다' in dialog.summary.text()
    assert '같습니다' not in dialog.summary.text()
    assert result['code'] in dialog.diagnostics.toPlainText()
    assert 'package_not_inspected' in dialog.diagnostics.toPlainText()
    document = package_ui().package_report_html(result)
    assert '전체 구성을 비교하지 못했습니다' in document
    assert 'inventory_equal' not in document and '추가 0' not in document
    dialog.close()


def test_inconsistent_success_without_complete_sides_is_not_presented_as_compared(app, result):
    result['original']['complete'] = False
    dialog = package_ui().PackageResultDialog(result)
    assert '전체 구성을 비교하지 못했습니다' in dialog.summary.text()
    assert '추가 1' not in dialog.summary.text()
    dialog.close()


def test_html_report_escapes_actual_untrusted_member_names_without_external_resources(tmp_path):
    filename = 'evil<img src=x onerror=alert(1)>.txt'
    for name in ('a.zip', 'b.zip'):
        with zipfile.ZipFile(tmp_path / name, 'w') as archive:
            archive.writestr(filename, b'abc')
    result = compare_packages(tmp_path / 'a.zip', tmp_path / 'b.zip')
    assert result['status'] == 'success'
    document = package_ui().package_report_html(result)
    assert '<!doctype html>' in document.lower() and 'charset="utf-8"' in document.lower()
    assert html.escape(filename) in document and filename not in document
    assert '<script' not in document.lower() and '<img' not in document.lower()
    assert '<link' not in document.lower() and 'https://' not in document.lower()
    assert 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad' in document
    assert '실행 동작' in document and '판정하지' in document


def test_member_tooltips_cannot_interpret_untrusted_names_as_active_markup(app, tmp_path):
    filename = 'evil<img src=x onerror=alert(1)>.txt'
    path = tmp_path / 'tooltip.zip'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr(filename, b'abc')
    dialog = package_ui().PackageResultDialog(compare_packages(path, path))
    row = find_row(dialog.tree, filename)
    assert row.text(1) == filename
    assert '<img' not in row.toolTip(1) and '&lt;img' in row.toolTip(1)
    dialog.close()


def test_actual_partial_member_evidence_remains_visible_without_a_comparison_category(app, tmp_path):
    path = tmp_path / 'partial.zip'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('verified', b'abc')
        archive.writestr('broken', b'CORRUPT')
    path.write_bytes(path.read_bytes().replace(b'CORRUPT', b'CHANGED', 1))
    result = compare_packages(path, path)
    assert result['status'] == 'partial' and result['comparison'] is None
    dialog = package_ui().PackageResultDialog(result)
    assert '전체 구성을 비교하지 못했습니다' in dialog.summary.text()
    row = find_row(dialog.tree, 'verified')
    assert row.text(2) == '3' and row.text(3) == '—'
    assert row.text(4) == 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad'
    assert '비교 미완료' in row.parent().text(0)
    dialog.close()


@pytest.mark.parametrize('kind', ['json', 'html'])
def test_export_buttons_write_actual_new_files_without_mutating_input(
    app, result, tmp_path, monkeypatch, kind
):
    module = package_ui()
    original = copy.deepcopy(result)
    dialog = module.PackageResultDialog(result)
    path = tmp_path / ('export.' + kind)
    monkeypatch.setattr(module.QFileDialog, 'getSaveFileName', lambda *args, **kwargs: (str(path), ''))
    getattr(dialog, 'export_' + kind + '_button').click()
    if kind == 'json':
        assert json.loads(path.read_text(encoding='utf-8')) == original
    else:
        document = path.read_text(encoding='utf-8')
        assert 'changed' in document and 'same' in document and '패키지 구성' in document
    assert result == original
    assert '저장했습니다' in dialog.export_status.text()
    dialog.close()


def test_cancelled_file_picker_has_no_file_side_effects(app, result, tmp_path, monkeypatch):
    module = package_ui()
    before = {path.name for path in tmp_path.iterdir()}
    dialog = module.PackageResultDialog(result)
    monkeypatch.setattr(module.QFileDialog, 'getSaveFileName', lambda *args, **kwargs: ('', ''))
    dialog.export_json_button.click()
    assert {path.name for path in tmp_path.iterdir()} == before
    assert '저장했습니다' not in dialog.export_status.text()
    dialog.close()


def test_existing_report_is_not_silently_overwritten(app, result, tmp_path, monkeypatch):
    module = package_ui()
    path = tmp_path / 'existing.json'
    path.write_bytes(b'keep original report')
    dialog = module.PackageResultDialog(result)
    monkeypatch.setattr(module.QFileDialog, 'getSaveFileName', lambda *args, **kwargs: (str(path), ''))
    dialog.export_json_button.click()
    assert path.read_bytes() == b'keep original report'
    assert '덮어쓰지' in dialog.export_status.text()
    assert not list(tmp_path.glob('.recheck-package-*'))
    dialog.close()


def test_export_snapshot_stays_bound_to_the_result_shown_in_dialog(app, result, tmp_path, monkeypatch):
    module = package_ui()
    dialog = module.PackageResultDialog(result)
    result['comparison']['changed'].append('later mutation')
    path = tmp_path / 'snapshot.json'
    monkeypatch.setattr(module.QFileDialog, 'getSaveFileName', lambda *args, **kwargs: (str(path), ''))
    dialog.export_json_button.click()
    assert json.loads(path.read_text(encoding='utf-8'))['comparison']['changed'] == ['changed']
    dialog.close()
