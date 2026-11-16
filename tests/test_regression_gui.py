"""Save a real retained finding and prepare/rerun it through the normal GUI."""
import copy
from test_gui import app, window, settle
from recheck.storage import Store
from review_fixtures import issue_run


def retained_run(window, tmp_path):
    store = Store(tmp_path / 'records')
    run = issue_run()
    store.save(run)
    window.engine.store = store
    window.show_result(run)
    return store, run


def test_save_and_prepare_failed_inputs_preserves_run_and_global_settings(window, app, tmp_path):
    from recheck.regression import RegressionLibrary
    store, run = retained_run(window, tmp_path)
    before = (store.run_dir(run['run_id']) / 'result.json').read_bytes()
    original_settings = window.settings.model_dump()
    window.save_regression()
    settle(app, lambda: not window._task_join_pending)
    suites = RegressionLibrary(store.root).list_suites()
    assert len(suites) == 1, 'The result action must persist its failed/unmeasured inputs'
    suite = RegressionLibrary(store.root).load_suite(suites[0]['suite_id'])
    assert {test['id'] for test in suite['tests']} == {'changed', 'timeout'}
    window.prepare_regression(suite['suite_id'])
    settle(app, lambda: not window._task_join_pending)
    settle(app, lambda: not any(card.inspector and card.inspector.isRunning()
                               for card in (window.original, window.reimplementation)))
    assert window._regression_suite_id == suite['suite_id']
    assert window.analysis_mode.currentData() == 'dynamic'
    assert not window.ai_explanation.isChecked()
    assert not window.inputs_group.isEnabled()
    assert '출력 비교 정책' in window.regression_request_label.text()
    assert window.settings.model_dump() == original_settings
    assert (store.run_dir(run['run_id']) / 'result.json').read_bytes() == before
    window.clear_regression_selection()
    assert window._regression_suite_id is None
    assert window.inputs_group.isEnabled()


def test_prepared_regression_uses_linked_backend_and_saved_policy_without_global_change(window, app, monkeypatch):
    window._regression_suite_id = 'regression_' + 'a' * 32
    window._regression_policy = dict(normalize_newlines=True, trim_trailing_whitespace=False)
    window.analysis_mode.setCurrentIndex(window.analysis_mode.findData('dynamic'))
    window.ai_explanation.setChecked(False)
    window.original.path.setText('new-reference')
    window.reimplementation.path.setText('fixed-program')
    captured = []
    settings_before = copy.deepcopy(window.settings.model_dump())

    class Engine:
        async def recheck_regression(self, suite_id, original, changed, cancel=None):
            captured.append((suite_id, original, changed))
            return {'run_id': 'fixture', 'status': 'success'}
        async def analyze(self, *args, **kwargs):
            raise AssertionError('A saved recheck must preserve its source/suite link')

    def engine(settings, emit):
        assert settings.normalize_newlines is True
        return Engine()
    monkeypatch.setattr(window, 'engine_for', engine)
    monkeypatch.setattr(window, 'persist_settings', lambda: True)
    async def ready(settings, cancel, emit, analysis_mode, paths):
        return {'can_analyze': True, 'environment': {}, 'files': []}
    monkeypatch.setattr(window, 'analysis_preflight', ready)
    monkeypatch.setattr(window, 'show_result', lambda run: None)
    window.start_analysis()
    settle(app, lambda: not window._task_join_pending)
    assert captured == [('regression_' + 'a' * 32, 'new-reference', 'fixed-program')]
    assert window.settings.model_dump() == settings_before
