"""Scope selection and observed status stay distinct in exported reports."""

from html.parser import HTMLParser

import pytest

from recheck.report import render_report


class Rows(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.row = None
        self.cell = None

    def handle_starttag(self, tag, attrs):
        if tag == 'tr':
            self.row = []
        elif tag in ('td', 'th') and self.row is not None:
            self.cell = ''

    def handle_data(self, data):
        if self.cell is not None:
            self.cell += data

    def handle_endtag(self, tag):
        if tag in ('td', 'th') and self.cell is not None:
            self.row.append(self.cell)
            self.cell = None
        elif tag == 'tr' and self.row is not None:
            self.rows.append(self.row)
            self.row = None


def report_rows(record, tmp_path):
    document = render_report(record, tmp_path / 'report.html').read_text(encoding='utf-8')
    parser = Rows()
    parser.feed(document)
    return document, parser.rows


def test_static_only_report_never_presents_unselected_execution_as_success(tmp_path):
    document, rows = report_rows({
        'run_id': 'static-fixture', 'status': 'success',
        'mode': 'automatic', 'analysis_mode': 'static', 'ai_mode': 'off',
        'analysis_status': {'static': 'success', 'dynamic': 'not_requested', 'ai': 'not_requested'},
    }, tmp_path)
    assert ['선택한 분석', '정적분석'] in rows
    assert ['AI 사용', '사용 안 함'] in rows
    assert ['정적분석', '성공'] in rows
    assert ['동적분석', '선택하지 않음'] in rows
    assert ['AI 설명·추가 확인', '선택하지 않음'] in rows
    assert '동적분석을 선택하지 않아 실행 동작은 검증하지 않았습니다.' in document
    assert '동작 일치율: <b>선택하지 않음</b>' in document
    assert '<p>파일에서 수집한 정적 근거만 확인한 결과 · 실행 동작 미검증</p>' in document


def test_both_report_keeps_failed_execution_distinct_from_successful_static_and_ai(tmp_path):
    document, rows = report_rows({
        'run_id': 'partial-fixture', 'status': 'partial',
        'mode': 'automatic', 'analysis_mode': 'both', 'ai_mode': 'explain',
        'analysis_status': {'static': 'success', 'dynamic': 'blocked', 'ai': 'success'},
    }, tmp_path)
    assert ['선택한 분석', '둘 다 분석'] in rows
    assert ['AI 사용', '결과 설명'] in rows
    assert ['정적분석', '성공'] in rows
    assert ['동적분석', '진행할 수 없음'] in rows
    assert ['AI 설명·추가 확인', '성공'] in rows
    assert '정적분석의 성공만으로 실행 동작이 같다고 판단할 수 없습니다.' in document


def test_legacy_dynamic_report_derives_scope_but_requires_recorded_stage_success(tmp_path):
    _, rows = report_rows({
        'run_id': 'legacy-fixture', 'status': 'success', 'mode': 'dynamic',
        'stages': {'metadata': {'status': 'success'}, 'image': {'status': 'success'},
                   'dynamic': {'status': 'success'}},
    }, tmp_path)
    assert ['선택한 분석', '동적분석'] in rows
    assert ['정적분석', '선택하지 않음'] in rows
    assert ['동적분석', '성공'] in rows


@pytest.mark.parametrize('record', [
    {'run_id': 'legacy-incomplete', 'status': 'success', 'mode': 'automatic'},
    {'run_id': 'unknown-scope', 'status': 'success'},
])
def test_missing_scope_observations_are_not_inferred_from_overall_success(record, tmp_path):
    _, rows = report_rows(record, tmp_path)
    assert ['동적분석', '미기록'] in rows
    assert ['정적분석', '성공'] not in rows
    assert ['동적분석', '성공'] not in rows
