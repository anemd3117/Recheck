"""Scope, failure-only export preview and saved recheck selection."""
from __future__ import annotations
import copy
from pathlib import Path
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                               QTextBrowser, QCheckBox, QFileDialog, QTreeWidget, QTreeWidgetItem)
from .review_views import coverage_section, regression_section
from .failure_report import build_failure_report, failure_report_html, export_failure_report


class CoveragePanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        self.browser = QTextBrowser()
        self.browser.setOpenLinks(False)
        layout.addWidget(self.browser)

    def set_run(self, run):
        self.browser.setHtml(coverage_section(run) + regression_section(run))


class FailureReportPanel(QWidget):
    notice = Signal(str)
    error = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.run = {}
        self.report = {}
        layout = QVBoxLayout(self)
        self.include_observations = QCheckBox('재현용 입력·출력 원문 포함 (공유 전 확인)')
        self.include_observations.setToolTip('기본 보고서는 입력·출력·설정·인증·전체 경로를 제외합니다.')
        self.include_observations.toggled.connect(self.refresh)
        layout.addWidget(self.include_observations)
        actions = QHBoxLayout()
        self.export_buttons = []
        for format, label in [('html', '실패 항목 HTML 저장'), ('json', '분석용 JSON 저장')]:
            button = QPushButton(label)
            self.export_buttons.append(button)
            button.clicked.connect(lambda checked=False, format=format: self.choose_export(format))
            actions.addWidget(button)
        actions.addStretch()
        layout.addLayout(actions)
        self.browser = QTextBrowser()
        self.browser.setOpenLinks(False)
        layout.addWidget(self.browser)
        self.refresh()

    def set_run(self, run):
        self.run = copy.deepcopy(run)
        self.include_observations.setChecked(False)
        self.refresh()

    def refresh(self):
        selected = bool(self.run.get('run_id'))
        self.include_observations.setEnabled(selected)
        for button in self.export_buttons:
            button.setEnabled(selected)
        if not selected:
            self.report = {}
            self.browser.setPlainText('비교 결과를 선택하면 실패·차이 항목을 표시합니다.')
            return
        self.report = build_failure_report(self.run, include_observations=self.include_observations.isChecked())
        self.browser.setHtml(failure_report_html(self.report))

    def export_to(self, path, format):
        if not self.run.get('run_id'):
            raise ValueError('보고서를 만들 비교 결과를 먼저 선택하세요.')
        return export_failure_report(self.run, path, format=format,
                                     include_observations=self.include_observations.isChecked())

    def choose_export(self, format):
        path, _ = QFileDialog.getSaveFileName(self, '실패 항목 보고서 저장',
                                             'Recheck-failures.' + format,
                                             f'{format.upper()} (*.{format})')
        if path:
            try:
                destination = self.export_to(Path(path), format)
                self.notice.emit('실패 항목 보고서를 저장했습니다: ' + str(destination))
            except (OSError, ValueError) as exc:
                self.error.emit(str(exc))


class RegressionPanel(QWidget):
    refresh_requested = Signal()
    load_requested = Signal(str)
    error = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        note = QLabel('저장한 문제 입력으로 두 프로그램을 다시 비교합니다. 이전 결과는 유지하고 새 분석을 만듭니다. '
                      '이번 두 파일의 일치는 과거 원본 동작과의 완전한 호환성 증명이 아닙니다.')
        note.setWordWrap(True)
        layout.addWidget(note)
        actions = QHBoxLayout()
        refresh = QPushButton('보관함 새로고침')
        refresh.clicked.connect(self.refresh_requested)
        load = QPushButton('선택한 입력으로 재검사 준비')
        load.clicked.connect(self.load_selected)
        actions.addWidget(refresh)
        actions.addWidget(load)
        actions.addStretch()
        layout.addLayout(actions)
        self.suites = QTreeWidget()
        self.suites.setHeaderLabels(['저장한 입력 묶음', '입력 수', '최초 분석', '저장 시간'])
        self.suites.setRootIsDecorated(False)
        self.suites.setColumnWidth(0, 260)
        self.suites.itemDoubleClicked.connect(lambda *_: self.load_selected())
        layout.addWidget(self.suites)

    def set_suites(self, suites):
        self.suites.clear()
        for suite in suites:
            count = suite.get('case_count', len(suite.get('cases', suite.get('tests', []))))
            row = QTreeWidgetItem([str(suite.get('name', '')), str(count),
                                  str(suite.get('source_run_id', '')), str(suite.get('created', ''))])
            if suite.get('status') == 'unreadable':
                row.setText(0, '읽을 수 없는 입력 묶음 · 원본 보존')
                row.setToolTip(0, str(suite.get('reason', '저장 자료를 확인하세요.')))
            row.setData(0, Qt.ItemDataRole.UserRole, suite['suite_id'])
            self.suites.addTopLevelItem(row)

    def load_selected(self):
        rows = self.suites.selectedItems()
        if len(rows) != 1:
            self.error.emit('재검사할 입력 묶음을 하나 선택하세요.')
            return
        self.load_requested.emit(rows[0].data(0, Qt.ItemDataRole.UserRole))
