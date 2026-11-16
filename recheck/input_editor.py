"""Plain Korean test entry backed by the shared registered-input schema."""

from __future__ import annotations

from copy import deepcopy
import json
import shlex

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .models import validate_tests


class TestInputPanel(QWidget):
    __test__ = False
    error = Signal(str)
    notice = Signal(str)

    def __init__(self, tests=None, parent=None):
        super().__init__(parent)
        self._records = []
        self._valid = False
        self._loaded_id = None
        self._loaded_stdin = ''
        self.tabs = QTabWidget()
        self.tabs.setObjectName('inputTabs')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.tabs)

        simple = QWidget()
        body = QVBoxLayout(simple)
        body.setContentsMargins(0, 10, 0, 0)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(['이름', '실행할 때 전달할 값', '실행 후 입력할 값'])
        self.tree.setAccessibleName('등록한 입력 목록')
        self.tree.setRootIsDecorated(False)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setMinimumHeight(120)
        self.tree.header().setSectionResizeMode(0, QHeaderView.Interactive)
        self.tree.setColumnWidth(0, 220)
        self.tree.header().setSectionResizeMode(1, QHeaderView.Stretch)
        self.tree.header().setSectionResizeMode(2, QHeaderView.Stretch)
        body.addWidget(self.tree)
        form = QFormLayout()
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText('예: 경계값 확인 (생략 가능)')
        self.args_input = QLineEdit()
        self.args_input.setPlaceholderText('예: 100 또는 "두 단어" · 필요 없으면 비워 두세요')
        self.args_input.setToolTip('옵션은 공백으로 나눕니다. 공백이 든 값은 따옴표로 묶으세요.')
        self.stdin_input = QPlainTextEdit()
        self.stdin_input.setPlaceholderText(
            '프로그램을 실행한 뒤 키보드로 입력하던 내용이 있다면 적으세요.'
        )
        self.stdin_input.setMaximumHeight(90)
        for widget, name in (
            (self.name_input, '입력 이름'),
            (self.args_input, '실행 옵션'),
            (self.stdin_input, '입력 내용'),
        ):
            widget.setAccessibleName(name)
        form.addRow('이름 (생략 가능)', self.name_input)
        form.addRow('실행할 때 전달할 값', self.args_input)
        form.addRow('실행 후 입력할 값', self.stdin_input)
        body.addLayout(form)
        actions = QHBoxLayout()
        self.add_button = QPushButton('입력 추가')
        self.update_button = QPushButton('선택한 입력 수정')
        self.delete_button = QPushButton('선택 삭제')
        for button in (self.add_button, self.update_button, self.delete_button):
            actions.addWidget(button)
        actions.addStretch()
        body.addLayout(actions)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        body.addWidget(self.status_label)
        self.tabs.addTab(simple, '쉽게 입력')

        advanced = QWidget()
        advanced_layout = QVBoxLayout(advanced)
        advanced_help = QLabel(
            '입력 파일 내용을 직접 편집합니다. 형식이 잘못되면 아래 내용을 고친 뒤 쉽게 입력을 이용하세요.'
        )
        advanced_help.setWordWrap(True)
        advanced_layout.addWidget(advanced_help)
        self.json_editor = QPlainTextEdit()
        self.json_editor.setAccessibleName('입력 목록 직접 편집')
        advanced_layout.addWidget(self.json_editor)
        self.tabs.addTab(advanced, '직접 편집 (고급)')

        self.json_editor.textChanged.connect(self._sync_from_json)
        self.tree.currentItemChanged.connect(self._selection_changed)
        self.add_button.clicked.connect(self.add_input)
        self.update_button.clicked.connect(self.update_selected)
        self.delete_button.clicked.connect(self.delete_selected)
        if tests is None:
            tests = [dict(id='baseline', description='입력 없는 기본 실행', args=[], stdin='')]
        self.json_editor.setPlainText(
            tests if isinstance(tests, str) else json.dumps(tests, ensure_ascii=False, indent=2)
        )

    def _read_records(self):
        value = json.loads(self.json_editor.toPlainText())
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            raise ValueError('입력 목록 형식을 확인하세요.')
        validate_tests(value)
        return value

    def _sync_from_json(self):
        selected = self.tree.currentItem()
        selected_id = selected.data(0, Qt.UserRole) if selected else None
        try:
            records = self._read_records()
        except (ValueError, TypeError):
            self._valid = False
            self.tree.clear()
            self.status_label.setText(
                '직접 편집 내용을 먼저 확인하세요. 입력 개수와 각 항목의 형식·중복을 확인하세요.'
            )
            return
        self._valid = True
        self._records = deepcopy(records)
        self.tree.clear()
        for test in records:
            args = shlex.join(test.get('args', []))
            stdin = test.get('stdin', '')
            preview = stdin.replace('\r\n', '\n').replace('\n', ' / ')
            item = QTreeWidgetItem(
                [
                    test.get('description') or test['id'],
                    args,
                    preview[:120] + ('…' if len(preview) > 120 else ''),
                ]
            )
            item.setData(0, Qt.UserRole, test['id'])
            item.setToolTip(0, test.get('description') or test['id'])
            item.setToolTip(1, args)
            item.setToolTip(2, stdin)
            self.tree.addTopLevelItem(item)
        if not self._select_id(selected_id):
            self._select_id(records[0]['id'])
        self.status_label.setText(f'입력 {len(records)}개가 등록되어 있습니다.')

    def _select_id(self, test_id):
        for index in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(index)
            if item.data(0, Qt.UserRole) == test_id:
                self.tree.setCurrentItem(item)
                return True
        return False

    def _selection_changed(self, current, previous=None):
        if (current is not None and self._loaded_id is not None
                and current.data(0, Qt.UserRole) != self._loaded_id and self._has_pending()):
            next_id, old_id = current.data(0, Qt.UserRole), self._loaded_id
            if self.commit_pending():
                # Saving rebuilds the tree; only retain IDs across that operation.
                self._select_id(next_id)
            else:
                blocked = self.tree.blockSignals(True)
                self._select_id(old_id)
                self.tree.blockSignals(blocked)
            return
        if current is None:
            self._loaded_id = None
            return
        test_id = current.data(0, Qt.UserRole)
        test = next((item for item in self._records if item['id'] == test_id), None)
        if test is None:
            return
        self._loaded_id = test_id
        self.name_input.setText(test.get('description', ''))
        self.args_input.setText(shlex.join(test.get('args', [])))
        self.stdin_input.setPlainText(test.get('stdin', ''))
        self._loaded_stdin = self.stdin_input.toPlainText()

    def _reject(self, message):
        self.status_label.setText(message)
        self.error.emit(message)
        return False

    def _editable_records(self):
        try:
            return self._read_records()
        except (ValueError, TypeError):
            self._reject('직접 편집 내용을 먼저 확인하세요. 내용을 고친 뒤 다시 시도하세요.')
            return None

    def _form_values(self):
        try:
            args = shlex.split(self.args_input.text(), posix=True)
        except ValueError:
            self._reject('실행 옵션의 따옴표를 확인하세요. 공백이 든 값은 "두 단어"처럼 묶으세요.')
            return None
        return dict(
            description=self.name_input.text().strip(),
            args=args,
            stdin=self.stdin_input.toPlainText(),
        )

    def _write_records(self, records, selected_id, message):
        try:
            validate_tests(records)
        except (ValueError, TypeError):
            return self._reject(
                '입력 내용을 확인하세요. 입력 개수·내용 길이·항목 중복·실행 제한이 허용 범위를 벗어났습니다.'
            )
        self.json_editor.setPlainText(json.dumps(records, ensure_ascii=False, indent=2))
        self._select_id(selected_id)
        self.status_label.setText(message)
        self.notice.emit(message)
        return True

    def add_input(self):
        records = self._editable_records()
        if records is None:
            return False
        if len(records) >= 100:
            return self._reject('입력은 최대 100개까지 등록할 수 있습니다.')
        values = self._form_values()
        if values is None:
            return False
        ids = {test['id'] for test in records}
        index = 1
        while f'case_{index}' in ids:
            index += 1
        test_id = f'case_{index}'
        records.append(dict(id=test_id, source='user', **values))
        return self._write_records(records, test_id, '새 입력을 추가했습니다.')

    def update_selected(self):
        records = self._editable_records()
        if records is None:
            return False
        item = self.tree.currentItem()
        if item is None:
            return self._reject('수정할 입력을 목록에서 선택하세요.')
        test_id = self._loaded_id
        values = self._form_values()
        if values is None:
            return False
        for test in records:
            if test['id'] == test_id:
                # A displayed newline is normalized by Qt; an untouched field retains its raw CRLF.
                if self._loaded_id == test_id and values['stdin'] == self._loaded_stdin:
                    values['stdin'] = test.get('stdin', '')
                test.update(values)
                return self._write_records(records, test_id, '선택한 입력을 수정했습니다.')
        return self._reject('선택한 입력을 다시 확인하세요.')

    def _has_pending(self):
        if not self._valid or self._loaded_id is None:
            return False
        test = next((item for item in self._records if item['id'] == self._loaded_id), None)
        if test is None:
            return False
        unchanged = (
            self.name_input.text() == test.get('description', '')
            and self.args_input.text() == shlex.join(test.get('args', []))
            and self.stdin_input.toPlainText() == self._loaded_stdin
        )
        return not unchanged

    def commit_pending(self):
        """Apply the visible form before reading registered inputs for a run/export."""
        return self.update_selected() if self._has_pending() else True

    def delete_selected(self):
        records = self._editable_records()
        if records is None:
            return False
        ids = {item.data(0, Qt.UserRole) for item in self.tree.selectedItems()}
        if not ids:
            return self._reject('삭제할 입력을 목록에서 선택하세요.')
        remaining = [test for test in records if test['id'] not in ids]
        if not remaining:
            return self._reject('마지막 입력은 삭제할 수 없습니다. 입력을 하나 이상 남겨 주세요.')
        return self._write_records(remaining, remaining[0]['id'], '선택한 입력을 삭제했습니다.')
