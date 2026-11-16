"""Developer-only verification of the normal review and saved recheck GUI.

The caller supplies an isolated RECHECK_DATA_DIR and an existing real dynamic
comparison. There is no alternative analysis engine, direct target launcher, AI
client, installer, or success override in this verification path.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

from PySide6.QtCore import QTimer, Qt

from .comparison import compare_behavior
from .coverage import coverage_summary
from .models import Settings, atomic_json, inspect_binary
from .regression import RegressionLibrary


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _recorded_policy(run):
    settings = run.get('settings') or {}
    policy = {name: settings.get(name) for name in ('normalize_newlines', 'trim_trailing_whitespace')}
    _require(all(type(value) is bool for value in policy.values()), '검증 원본의 출력 비교 정책이 기록되지 않았습니다.')
    return policy


def load_review_scenario(config_path):
    path = Path(config_path).resolve(strict=True)
    config = json.loads(path.read_text(encoding='utf-8-sig'))
    _require(isinstance(config, dict) and set(config) == {'run_id', 'output_dir', 'reimplementation'},
             '검증 설정에는 run_id, output_dir, reimplementation만 필요합니다.')
    _require(isinstance(config['run_id'], str) and re.fullmatch(r'run_[a-f0-9]{32}', config['run_id']),
             '검증할 분석 ID를 확인하세요.')
    result = dict(run_id=config['run_id'])
    for name in ('output_dir', 'reimplementation'):
        value = config[name]
        _require(isinstance(value, str) and value and '\0' not in value, '검증 경로를 확인하세요.')
        value = Path(value)
        result[name] = str((value if value.is_absolute() else path.parent / value).resolve())
    _require(not Path(result['output_dir']).exists(), '기존 근거를 보존하도록 새 검증 폴더를 지정하세요.')
    info = inspect_binary(result['reimplementation'])
    _require(info.get('os') == 'linux' and info.get('execution_supported') is True,
             '검증 재실행 대상은 지원되는 Linux x64 ELF여야 합니다.')
    return result


def verify_recheck_result(source, suite, result):
    """Validate raw observations and identity links, never cached match claims."""
    _require(result.get('run_id') != source.get('run_id') and result.get('status') == 'success',
             '재검사는 성공한 별도 분석 기록이어야 합니다.')
    _require(result.get('analysis_mode') == 'dynamic' and result.get('ai_mode') == 'off',
             '재검사는 동적분석·AI 사용 안 함이어야 합니다.')
    _require(not result.get('static') and not result.get('structure')
             and not result.get('llm') and not result.get('agent'), '요청하지 않은 정적분석 또는 AI 결과가 있습니다.')
    _require(_recorded_policy(result) == suite['comparison_policy'], '저장한 출력 비교 정책과 다릅니다.')
    regression = result.get('regression') or {}
    _require(regression.get('suite_id') == suite['suite_id']
             and regression.get('source_run_id') == source['run_id'], '저장 묶음·원본 분석 연결이 다릅니다.')
    ids = [test['id'] for test in suite['tests']]
    _require([test['id'] for test in result.get('tests', [])] == ids, '저장한 입력과 실제 재검사 입력이 다릅니다.')
    rows = result.get('dynamic') or []
    _require([row.get('test_id') for row in rows] == ids, '원시 관찰이 누락·중복되었거나 입력과 다릅니다.')
    observed = compare_behavior(rows, Settings(**suite['comparison_policy']))
    _require(observed['denominator'] == observed['matched'] == len(ids) and bool(ids),
             '저장 입력의 원시 관찰이 모두 유효한 일치 쌍이 아닙니다.')
    cases = regression.get('case_results') or []
    _require([case.get('test_id') for case in cases] == ids
             and all(case.get('status') == 'resolved' and case.get('saved_input_reproduced') is True for case in cases),
             '저장 입력이 재현되어 현재 두 파일이 일치했다고 확인되지 않았습니다.')
    _require(regression.get('policy') == 'pair_equivalence' and regression.get('reference_changed') is False,
             '기준 파일 또는 재검사 판정 범위를 확인하세요.')
    original_hash = (source.get('files', {}).get('original') or {}).get('sha256')
    _require(original_hash and (result.get('files', {}).get('original') or {}).get('sha256') == original_hash,
             '기준 프로그램의 바이트가 변경되었습니다.')
    return dict(new_record=True, linked_source_and_suite=True, saved_inputs_preserved=True,
                saved_policy_preserved=True, raw_observations_valid_and_equal=True,
                regression_all_current_pairs_match=True, original_reference_preserved=True,
                dynamic_only=True, ai_off_no_inference=True)


class ReviewVerification:
    def __init__(self, app, window, scenario):
        self.app, self.window, self.scenario = app, window, scenario
        self.finished = False
        self.phase = 'source'
        self.started = time.monotonic()
        self.failure = None
        self.output = Path(scenario['output_dir'])
        self.output.mkdir(parents=True, exist_ok=False)
        self.exports, self.screenshots = {}, {}
        self.timer = QTimer(window)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.poll)
        self.timer.start()

    def busy(self):
        return (self.window._task_join_pending or (self.window.worker and self.window.worker.isRunning())
                or any(card.inspector and card.inspector.isRunning()
                       for card in (self.window.original, self.window.reimplementation)))

    def capture(self, name):
        path = self.output / name
        _require(self.window.isVisible() and self.window.grab().save(str(path)), '검증 화면을 저장하지 못했습니다.')
        self.screenshots[name] = dict(sha256=_sha(path), size=path.stat().st_size)

    def source_and_exports(self):
        data = os.environ.get('RECHECK_DATA_DIR')
        _require(bool(data) and Path(data).resolve() == Path(self.window.engine.store.root).resolve(),
                 '검증에는 별도 RECHECK_DATA_DIR이 필요합니다.')
        self.library = RegressionLibrary(self.window.engine.store.root)
        self.source = self.window.engine.store.load_run(self.scenario['run_id'])
        self.source_path = self.window.engine.store.run_dir(self.source['run_id']) / 'result.json'
        self.source_bytes = self.source_path.read_bytes()
        self.settings_before = self.window.settings.model_dump()
        original = self.source.get('files', {}).get('original') or {}
        fixed = inspect_binary(self.scenario['reimplementation'])
        _require(fixed.get('os') == 'linux' and fixed.get('execution_supported') is True
                 and fixed['sha256'] == original.get('sha256'),
                 '이 검증은 원본과 동일한 Linux ELF를 수정본으로 선택해 일치 재검사를 확인합니다.')
        source_observed = compare_behavior(self.source.get('dynamic') or [], Settings(**_recorded_policy(self.source)))
        _require(any(case['match'] is False for case in source_observed['tests']),
                 '원본 분석에는 원시 관찰로 확인한 동작 차이가 있어야 합니다.')
        self.window.show_result(self.source)
        self.window.open_coverage()
        self.capture('source-coverage.png')
        self.window.open_failure_report()
        panel = self.window.failure_panel
        for details, label in ((False, 'safe'), (True, 'details')):
            panel.include_observations.setChecked(details)
            _require(panel.report.get('source', {}).get('run_id') == self.source['run_id'], '실패 보고서의 원본이 다릅니다.')
            _require(panel.report.get('counts', {}).get('behavior_mismatch', 0) >= 1, '실패 보고서에 원시 불일치가 없습니다.')
            for format in ('html', 'json'):
                path = self.output / f'failures-{label}.{format}'
                panel.export_to(path, format)
                self.exports[path.name] = dict(sha256=_sha(path), size=path.stat().st_size)
            exported = json.loads((self.output / f'failures-{label}.json').read_text(encoding='utf-8'))
            _require(exported['privacy']['observations_included'] is details, '보고서 원문 포함 설정이 다릅니다.')
            self.capture(f'failures-{label}.png')
        panel.include_observations.setChecked(False)
        self.suites_before = {suite['suite_id'] for suite in self.library.list_suites()}
        self.window.save_regression()
        self.phase = 'saved'

    def select_saved(self):
        new = [suite for suite in self.library.list_suites() if suite['suite_id'] not in self.suites_before]
        _require(len(new) == 1, '정상 GUI 저장 동작에서 새 입력 묶음 하나를 만들지 못했습니다.')
        self.suite = self.library.load_suite(new[0]['suite_id'])
        _require(self.suite['source_run_id'] == self.source['run_id'], '저장한 입력 묶음의 원본 분석이 다릅니다.')
        self.suite_path = self.library.directory / self.suite['suite_id'] / 'suite.json'
        self.suite_bytes = self.suite_path.read_bytes()
        self.window.regression_panel.set_suites(new)
        tree = self.window.regression_panel.suites
        _require(tree.topLevelItemCount() == 1, '저장한 입력 묶음을 GUI에서 확인하지 못했습니다.')
        tree.setCurrentItem(tree.topLevelItem(0))
        _require(tree.selectedItems()[0].data(0, Qt.ItemDataRole.UserRole) == self.suite['suite_id'],
                 '다른 입력 묶음이 선택되었습니다.')
        self.window.regression_panel.load_selected()
        self.phase = 'prepared'

    def start_recheck(self):
        _require(self.window._regression_suite_id == self.suite['suite_id'], 'GUI 재검사 준비가 완료되지 않았습니다.')
        _require(self.window.analysis_mode.currentData() == 'dynamic' and not self.window.ai_explanation.isChecked(),
                 'GUI 재검사 범위가 다릅니다.')
        _require(not self.window.inputs_group.isEnabled(), '저장 입력이 읽기 전용으로 고정되지 않았습니다.')
        self.window.reimplementation.set_path(self.scenario['reimplementation'])
        self.phase = 'ready'

    def begin(self):
        self.capture('recheck-prefill.png')
        self.window.start_analysis()
        self.phase = 'rechecked'

    def verify(self):
        result = self.window.run_result
        checks = verify_recheck_result(self.source, self.suite, result)
        checks.update(source_run_bytes_unchanged=self.source_path.read_bytes() == self.source_bytes,
                      suite_bytes_unchanged=self.suite_path.read_bytes() == self.suite_bytes,
                      global_settings_unchanged=self.window.settings.model_dump() == self.settings_before,
                      source_failure_exports=len(self.exports) == 4)
        _require(all(checks.values()), '이전 기록·저장 입력·설정 또는 내보내기 검증에 실패했습니다.')
        self.window.open_coverage()
        self.capture('rechecked.png')
        self.finish(dict(status='verified', runtime_kind='packaged' if getattr(sys, 'frozen', False) else 'source',
                         runtime_executable=sys.executable, source_run_id=self.source['run_id'],
                         new_run_id=result['run_id'], suite_id=self.suite['suite_id'], checks=checks,
                         source_result_sha256=hashlib.sha256(self.source_bytes).hexdigest(),
                         suite_sha256=hashlib.sha256(self.suite_bytes).hexdigest(),
                         source_coverage=coverage_summary(self.source), rechecked_coverage=coverage_summary(result),
                         regression=result['regression'], exports=self.exports, screenshots=self.screenshots,
                         scope='Normal GUI review, failure exports and saved-input dynamic recheck; no AI or host target execution'), 0)

    def finish(self, proof, code):
        self.finished = True
        self.timer.stop()
        proof.update(exit_code=code, seconds=time.monotonic() - self.started)
        atomic_json(self.output / 'review-proof.json', proof)
        self.window.close()
        self.app.exit(code)

    def poll(self):
        if self.finished:
            return
        elapsed = time.monotonic() - self.started
        if elapsed > 540 and self.failure is None:
            self.failure = '검증 시간 제한에 도달했습니다.'
            self.window.cancel_work()
        if self.failure:
            if not self.busy() or elapsed > 600:
                self.finish(dict(status='failed', error=self.failure, phase=self.phase,
                                 cleanup_completed=not bool(self.busy()), exports=self.exports,
                                 screenshots=self.screenshots), 1)
            return
        if self.busy():
            return
        try:
            {'source': self.source_and_exports, 'saved': self.select_saved,
             'prepared': self.start_recheck, 'ready': self.begin, 'rechecked': self.verify}[self.phase]()
        except Exception as exc:
            self.failure = f'{type(exc).__name__}: {exc}'
            self.window.cancel_work()


def start_review_verification(app, window, scenario):
    controller = ReviewVerification(app, window, scenario)
    window.review_verification = controller
    return controller
