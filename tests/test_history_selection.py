"""A saved finding must be reopened from the selected row, including UIA selection."""

from test_gui import app, window
from PySide6.QtCore import QItemSelectionModel, QModelIndex
from PySide6.QtWidgets import QAbstractItemView
import pytest
from recheck.storage import Store


@pytest.mark.parametrize('stale_current', [False, True])
def test_history_opens_selected_result_instead_of_absent_or_stale_current(window, tmp_path, stale_current):
    store = Store(tmp_path / 'history')
    ids = ['run_' + 'a' * 32, 'run_' + 'b' * 32]
    for rid in ids:
        store.save(dict(run_id=rid, status='success', created=rid, files={}))
    window.engine.store = store
    window.refresh_history()
    first, selected = window.history.topLevelItem(0), window.history.topLevelItem(1)
    target_id = selected.data(0, 256)
    if stale_current:
        window.history.setCurrentItem(first)
    else:
        window.history.setCurrentIndex(QModelIndex())
    window.history.clearSelection()
    window.history.selectionModel().select(
        window.history.indexFromItem(selected),
        QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
    window.open_history()
    assert window.run_result['run_id'] == target_id


def test_history_with_no_selection_does_not_reopen_stale_current(window, tmp_path):
    store = Store(tmp_path / 'history')
    store.save(dict(run_id='run_' + 'a' * 32, status='success', files={}))
    window.engine.store = store
    window.refresh_history()
    window.history.setCurrentItem(window.history.topLevelItem(0))
    window.history.clearSelection()
    window.open_history()
    assert window.run_result == {}
    assert '선택' in window.banner.text()


def test_history_ambiguous_selection_is_rejected(window, tmp_path):
    store = Store(tmp_path / 'history')
    for char in 'ab':
        store.save(dict(run_id='run_' + char * 32, status='success', files={}))
    window.engine.store = store
    window.refresh_history()
    window.history.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
    for index in range(2):
        window.history.topLevelItem(index).setSelected(True)
    window.open_history()
    assert window.run_result == {}
    assert '하나' in window.banner.text()
