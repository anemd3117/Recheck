"""Package inventory results and local exports, separate from behavior reports."""

from __future__ import annotations

import copy
import html
import json
import os
from pathlib import Path
import tempfile

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QFileDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from .paths import filesystem_path

SCOPE_NOTE = (
    '패키지 구성 파일의 경로·종류·크기·SHA-256을 비교합니다. '
    '코드 의미·서명 유효성·실행 동작의 동등성은 판정하지 않습니다.'
)
CATEGORIES = (('added', '추가'), ('removed', '삭제'), ('changed', '변경'), ('unchanged', '유지'))
HEADERS = ('종류', '경로', '원본 크기 (bytes)', '변경본 크기 (bytes)', '원본 SHA-256', '변경본 SHA-256')
STATUS_NAMES = {'success': '완료', 'partial': '일부 읽음', 'failed': '실패',
                'cancelled': '취소', 'blocked': '미측정'}


def _complete(result):
    return (result.get('status') == 'success'
            and result.get('scope') == 'package_inventory'
            and all(result.get(side, {}).get('complete') is True
                    for side in ('original', 'reimplementation'))
            and isinstance(result.get('comparison'), dict)
            and all(isinstance(result['comparison'].get(key), list) for key, _ in CATEGORIES))


def _summary(result):
    if _complete(result):
        return '패키지 구성 비교 완료 · ' + ' · '.join(
            f'{title} {len(result["comparison"][key])}' for key, title in CATEGORIES
        )
    status = STATUS_NAMES.get(result.get('status'), '미완료')
    return f'전체 구성을 비교하지 못했습니다 · {status}'


def _diagnostics(result):
    lines = [f'비교 상태: {STATUS_NAMES.get(result.get("status"), "미완료")} '
             f'· {result.get("code") or "오류 없음"}']
    for side, title in (('original', '원본'), ('reimplementation', '변경본')):
        record = result.get(side, {})
        state = STATUS_NAMES.get(record.get('status'), '미측정')
        complete = '전체 수집' if record.get('complete') is True else '수집 미완료'
        lines.append(f'{title}: {state} · {complete} · 확인한 항목 {len(record.get("members", []))}'
                     f' · {record.get("code") or "오류 없음"}')
    if not _complete(result):
        lines.append('수집하지 못한 항목은 추가·삭제·일치로 계산하지 않습니다.')
    return '\n'.join(lines)


def _row(path, before, after):
    kinds = {'directory': '폴더', 'file': '파일'}
    old_kind, new_kind = before.get('kind'), after.get('kind')
    if old_kind and new_kind and old_kind != new_kind:
        kind = kinds.get(old_kind, old_kind) + ' → ' + kinds.get(new_kind, new_kind)
    else:
        kind = kinds.get(old_kind or new_kind, old_kind or new_kind or '항목')
    return [kind, str(path), str(before.get('size', '—')), str(after.get('size', '—')),
            str(before.get('sha256', '—')), str(after.get('sha256', '—'))]


def _groups(result):
    if _complete(result):
        left = {row['path']: row for row in result['original'].get('members', [])}
        right = {row['path']: row for row in result['reimplementation'].get('members', [])}
        return [(f'{title} ({len(result["comparison"][key])})',
                 [_row(path, left.get(path, {}), right.get(path, {}))
                  for path in result['comparison'][key]]) for key, title in CATEGORIES]
    groups = []
    for side, title in (('original', '원본에서 확인한 항목'),
                        ('reimplementation', '변경본에서 확인한 항목')):
        members = result.get(side, {}).get('members', [])
        rows = [_row(row['path'], row if side == 'original' else {},
                     row if side == 'reimplementation' else {}) for row in members]
        groups.append((f'{title} ({len(rows)}) · 비교 미완료', rows))
    return groups


def package_report_html(result):
    """Render a standalone, escaped inventory-only report with no active content."""
    escape = lambda value: html.escape(str(value), quote=True)
    sections = []
    for title, rows in _groups(result):
        table_rows = ''.join('<tr>' + ''.join(f'<td>{escape(cell)}</td>' for cell in row)
                             + '</tr>' for row in rows)
        if not rows:
            table_rows = '<tr><td colspan="6">표시할 항목 없음</td></tr>'
        sections.append(f'<h2>{escape(title)}</h2><table><thead><tr>'
                        + ''.join(f'<th>{escape(title)}</th>' for title in HEADERS)
                        + '</tr></thead><tbody>' + table_rows + '</tbody></table>')
    return ('<!doctype html><html lang="ko"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>Recheck 패키지 구성 비교</title><style>'
            'body{font:15px system-ui,sans-serif;color:#20314b;margin:28px;background:#fff}'
            'h1{font-size:24px}h2{font-size:18px;margin-top:26px}'
            'p,pre{white-space:pre-wrap;overflow-wrap:anywhere}'
            'table{border-collapse:collapse;width:100%;font-size:13px;table-layout:fixed}'
            'th,td{border:1px solid #d7deea;padding:8px;text-align:left;overflow-wrap:anywhere}'
            'th{background:#eef3fb}pre{background:#f4f6fa;padding:12px}'
            '</style></head><body><h1>Recheck 패키지 구성 비교</h1>'
            f'<p>{escape(_summary(result))}</p><p>{escape(SCOPE_NOTE)}</p>'
            f'<pre>{escape(_diagnostics(result))}</pre>'
            + ''.join(sections) + '</body></html>')


def _write_new_file(path, content):
    """Atomically publish a new report, preserving any existing destination."""
    target = filesystem_path(path)
    descriptor, temporary = tempfile.mkstemp(prefix='.recheck-package-', dir=target.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name == 'nt':
            os.rename(temporary, target)
        else:
            os.link(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


class PackageResultDialog(QDialog):
    def __init__(self, result, parent=None):
        super().__init__(parent)
        self.result_data = copy.deepcopy(result)
        self.setWindowTitle('Recheck · 패키지 구성 비교')
        self.setModal(False)
        self.resize(880, 560)
        layout = QVBoxLayout(self)
        self.summary = QLabel(_summary(self.result_data))
        self.summary.setObjectName('sectionTitle')
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.scope_label = QLabel(SCOPE_NOTE)
        self.scope_label.setObjectName('muted')
        self.scope_label.setTextFormat(Qt.TextFormat.PlainText)
        self.scope_label.setWordWrap(True)
        layout.addWidget(self.scope_label)
        self.tree = QTreeWidget()
        self.tree.setAccessibleName('패키지 구성 파일 비교 목록')
        self.tree.setHeaderLabels(HEADERS)
        self.tree.setAlternatingRowColors(True)
        self.tree.setColumnWidth(0, 90)
        self.tree.setColumnWidth(1, 240)
        self.tree.setColumnWidth(2, 125)
        self.tree.setColumnWidth(3, 125)
        self.tree.setColumnWidth(4, 280)
        self.tree.setColumnWidth(5, 280)
        for title, rows in _groups(self.result_data):
            group = QTreeWidgetItem([title])
            self.tree.addTopLevelItem(group)
            for row in rows:
                item = QTreeWidgetItem(row)
                group.addChild(item)
                for index, cell in enumerate(row):
                    # Qt tooltips auto-detect rich text. A member name must not
                    # become an image/link when the pointer rests over it.
                    item.setToolTip(index, '<pre>' + html.escape(cell, quote=True) + '</pre>')
            group.setExpanded(True)
        layout.addWidget(self.tree, 1)
        self.diagnostics = QPlainTextEdit(_diagnostics(self.result_data))
        self.diagnostics.setAccessibleName('패키지 비교 수집 상태')
        self.diagnostics.setReadOnly(True)
        self.diagnostics.setMaximumHeight(100)
        layout.addWidget(self.diagnostics)
        self.export_status = QLabel('JSON 또는 HTML로 이 결과를 저장할 수 있습니다.')
        self.export_status.setTextFormat(Qt.TextFormat.PlainText)
        self.export_status.setWordWrap(True)
        layout.addWidget(self.export_status)
        buttons = QHBoxLayout()
        self.export_json_button = QPushButton('JSON 내보내기')
        self.export_json_button.clicked.connect(lambda: self._export('json'))
        self.export_html_button = QPushButton('HTML 보고서 내보내기')
        self.export_html_button.clicked.connect(lambda: self._export('html'))
        self.close_button = QPushButton('닫기')
        self.close_button.clicked.connect(self.reject)
        buttons.addWidget(self.export_json_button)
        buttons.addWidget(self.export_html_button)
        buttons.addStretch()
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)

    def _export(self, kind):
        filename, _ = QFileDialog.getSaveFileName(
            self, '패키지 구성 비교 저장', f'Recheck-package-comparison.{kind}',
            f'{kind.upper()} (*.{kind})',
        )
        if not filename:
            return
        content = (json.dumps(self.result_data, ensure_ascii=False, indent=2) + '\n'
                   if kind == 'json' else package_report_html(self.result_data))
        try:
            _write_new_file(filename, content)
        except FileExistsError:
            self.export_status.setText('기존 보고서를 덮어쓰지 않습니다. 다른 파일 이름을 선택하세요.')
        except OSError:
            self.export_status.setText('보고서를 저장하지 못했습니다. 저장 위치와 권한을 확인하세요.')
        else:
            self.export_status.setText(f'{kind.upper()} 보고서를 저장했습니다.')
