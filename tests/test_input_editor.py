"""Offscreen widget fixtures; no model, binary, or external service runs."""

import json
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest

pytest.importorskip('PySide6')
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from shiboken6 import delete

from recheck.input_editor import TestInputPanel


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def panel(app):
    widget = TestInputPanel([dict(id='baseline', description='기본 입력', args=[], stdin='')])
    yield widget
    widget.close()
    delete(widget)
    app.processEvents()


def values(panel):
    return json.loads(panel.json_editor.toPlainText())


def test_add_input_supports_korean_name_and_literal_quoted_options(panel):
    notices = []
    panel.notice.connect(notices.append)
    panel.name_input.setText('두 단어 입력')
    panel.args_input.setText('--name "두 단어" "" \'$(literal)\'')
    panel.stdin_input.setPlainText('첫째 줄\n둘째 줄\n')
    assert panel.add_input()
    added = values(panel)[-1]
    assert added['id'] == 'case_1' and added['description'] == '두 단어 입력'
    assert added['args'] == ['--name', '두 단어', '', '$(literal)']
    assert added['stdin'] == '첫째 줄\n둘째 줄\n' and notices


def test_invalid_raw_json_survives_all_simple_edits(panel):
    errors = []
    panel.error.connect(errors.append)
    broken = '[ { "unfinished": '
    panel.json_editor.setPlainText(broken)
    assert panel.tree.topLevelItemCount() == 0
    assert not panel.add_input() and not panel.update_selected() and not panel.delete_selected()
    assert panel.json_editor.toPlainText() == broken
    assert all('직접 편집 내용을 먼저 확인하세요' in error for error in errors)


def test_external_set_plain_text_syncs_list_and_form_without_rewriting_json(panel):
    raw = '[ {"id":"imported", "description":"가져온 입력", "args":["a b"], "stdin":"값\\n"} ]'
    panel.json_editor.setPlainText(raw)
    assert panel.json_editor.toPlainText() == raw
    assert panel.tree.topLevelItem(0).text(0) == '가져온 입력'
    assert panel.args_input.text() == "'a b'" and panel.stdin_input.toPlainText() == '값\n'


def test_selected_update_preserves_dependencies_environment_and_limits(panel):
    case = dict(
        id='rich',
        description='이전',
        args=['old'],
        stdin='a\r\nb\r\n',
        aux_files=['C:/한글/공백 파일.dat'],
        env={'MODE': 'strict'},
        limits={'memory_mb': 256, 'cpus': 0.5, 'output_bytes': 2048},
        timeout_seconds=7,
        source='imported',
        verification_status='registered',
    )
    panel.json_editor.setPlainText(json.dumps([case]))
    panel.name_input.setText('수정한 입력')
    panel.args_input.setText('"새 값"')
    assert panel.update_selected()
    updated = values(panel)[0]
    assert updated == dict(case, description='수정한 입력', args=['새 값'])


def test_duplicate_ids_are_rejected_and_generated_ids_avoid_collisions(panel):
    duplicate = '[{"id":"same"},{"id":"same"}]'
    panel.json_editor.setPlainText(duplicate)
    assert not panel.add_input() and panel.json_editor.toPlainText() == duplicate
    panel.json_editor.setPlainText('[{"id":"case_1"},{"id":"case_2"}]')
    assert panel.add_input() and values(panel)[-1]['id'] == 'case_3'


def test_maximum_and_last_input_deletion_are_enforced(panel):
    original = panel.json_editor.toPlainText()
    assert not panel.delete_selected() and panel.json_editor.toPlainText() == original
    panel.json_editor.setPlainText(json.dumps([dict(id=f'test_{index}') for index in range(100)]))
    before = panel.json_editor.toPlainText()
    assert not panel.add_input() and panel.json_editor.toPlainText() == before
    assert '100개' in panel.status_label.text()


def test_bad_quotes_leave_registered_inputs_unchanged(panel):
    before = panel.json_editor.toPlainText()
    panel.args_input.setText('--name "닫히지 않음')
    assert not panel.add_input() and panel.json_editor.toPlainText() == before
    assert '따옴표' in panel.status_label.text()


def test_switching_inputs_preserves_pending_edits_and_original_extra_fields(panel):
    panel.json_editor.setPlainText(json.dumps([
        dict(id='first', args=[], stdin='old\r\n', timeout_seconds=7, env={'MODE': 'qa'}),
        dict(id='second', args=['keep'], stdin='second'),
    ]))
    panel.args_input.setText('"두 단어"')
    panel.stdin_input.setPlainText('수정한 내용\n')
    panel.tree.setCurrentItem(panel.tree.topLevelItem(1))
    records = values(panel)
    assert records[0]['args'] == ['두 단어']
    assert records[0]['stdin'] == '수정한 내용\n'
    assert records[0]['env'] == {'MODE': 'qa'} and records[0]['timeout_seconds'] == 7
    assert records[1] == dict(id='second', args=['keep'], stdin='second')
    assert panel.args_input.text() == 'keep'


def test_invalid_pending_edit_blocks_row_switch_without_losing_visible_text(panel):
    panel.json_editor.setPlainText('[{"id":"first"},{"id":"second"}]')
    panel.args_input.setText('"unfinished')
    panel.tree.setCurrentItem(panel.tree.topLevelItem(1))
    assert panel.tree.currentItem().data(0, Qt.UserRole) == 'first'
    assert panel.args_input.text() == '"unfinished'
    assert values(panel) == [{'id': 'first'}, {'id': 'second'}]


def test_delete_selected_retains_other_cases_and_their_extra_fields(panel):
    kept = dict(id='kept', env={'KEEP': 'yes'}, timeout_seconds=9, args=['--safe'])
    panel.json_editor.setPlainText(json.dumps([dict(id='remove'), kept]))
    assert panel.tree.currentItem().data(0, Qt.UserRole) == 'remove'
    assert panel.delete_selected()
    assert values(panel) == [kept]
