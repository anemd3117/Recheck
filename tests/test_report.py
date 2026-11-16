"""Offline report content and provenance checks using controlled records."""

import copy

from recheck.report import render_report


def test_managed_cil_difference_is_readable_and_escaped_in_report(tmp_path):
    target = tmp_path / 'managed.html'
    render_report({'run_id': 'managed-fixture', 'status': 'partial', 'structure': {
        'analysis_kind': 'managed', 'managed_differences': [{
            'original_name': '<Sample>.Add', 'reimplementation_name': '<Sample>.Add',
            'original': '0x06000001', 'reimplementation': '0x06000001',
            'original_cil': {'il_sha256': 'a' * 64},
            'reimplementation_cil': {'il_sha256': 'b' * 64},
        }],
    }}, target)
    document = target.read_text(encoding='utf-8')
    assert '<h2>.NET 메서드 본문 차이</h2>' in document
    assert '&lt;Sample&gt;.Add' in document
    assert '<Sample>' not in document
    assert 'a' * 64 in document and 'b' * 64 in document


def test_report_readable_reproduction_preserves_observed_records_and_escapes_data(tmp_path):
    record = {
        "run_id": "run_fixture",
        "attempt_id": "attempt_fixture",
        "status": "success",
        "created": "2026-11-13T03:00:00+00:00",
        "schema_version": "1.0",
        "versions": {"recheck": "fixture-version", "prompt": "fixture-prompt"},
        "files": {"original": {"name": "입력<원본>", "sha256": "fixture-hash"}},
        "stages": {
            "dynamic": {"status": "success", "duration_seconds": 1.25},
            "agent": {"status": "blocked", "reason": "자동 분석 모드"},
        },
        "behavior": {
            "matched": 0,
            "denominator": 1,
            "match_rate": 0,
            "tests": [
                {
                    "test_id": "case_1",
                    "execution_status": "measured",
                    "match": False,
                    "original_exit_code": 0,
                    "reimplementation_exit_code": 0,
                }
            ],
        },
        "dynamic": [
            {
                "test_id": "case_1",
                "input": {"args": ["<script>input</script>"]},
                "original": {
                    "status": "success",
                    "stdout": "<script>output</script>",
                    "exit_code": 0,
                },
                "reimplementation": {"status": "success", "stdout": "different", "exit_code": 0},
            }
        ],
        "tests": [
            {
                "id": "case_1",
                "description": "경계 입력",
                "args": ["한국어 경로"],
                "stdin": "줄1\n줄2",
                "env": {"LANG": "C.UTF-8"},
                "aux_files": [],
                "timeout_seconds": 5,
            }
        ],
        "settings": {"model": "fixture:model", "memory_mb": 512, "normalize_newlines": False},
        "llm": {
            "status": "success",
            "model": "fixture:model",
            "server_info": {"version": "fixture-ollama"},
            "execution": {"kind": "local", "digest": "fixture-digest"},
        },
        "environment": {
            "ghidra": {"version": "fixture-ghidra", "java": {"stderr": "fixture-java"}},
            "docker": {"version": {"Server": {"Version": "fixture-docker"}}},
        },
        "image": {"image_id": "sha256:fixture-image"},
        "attempts": [
            {"attempt_id": "attempt_fixture", "finished": "fixture-finished", "status": "success"}
        ],
        "reproduction": {"argv": ["python", "-m", "recheck", "--tests", "한국어 tests.json"]},
        "evidence": [
            {
                "evidence_id": "ev_log",
                "kind": "log_segment",
                "target": "both",
                "source": "ghidra.log:2-5",
                "data": {"text": "<script>log</script>"},
            }
        ],
    }
    before = copy.deepcopy(record)
    document = render_report(record, tmp_path / "보고서.html").read_text(encoding="utf-8")
    assert record == before
    for expected in (
        "<b>성공</b>",
        "<td>결과 확인됨</td>",
        "원본 · 종료 0",
        "같은 입력으로 실행해 비교",
        "재현 입력과 실행 설정",
        "도구와 실행 환경 버전",
        "복구 시도와 로그 근거",
        "확인된 사실과 결론의 제한",
        "fixture-digest",
        "fixture-docker",
        "fixture-java",
        "한국어 tests.json",
        "줄1\n줄2",
        "1.25",
        'href="#ev_log"',
        'id="ev_log"',
    ):
        assert expected in document
    assert "<script>" not in document
    assert "&lt;script&gt;output&lt;/script&gt;" in document
    assert "추정 의사 코드" in document and "모든 입력에서의 동등성" in document


def test_report_missing_versions_are_explicitly_unrecorded(tmp_path):
    document = render_report(
        {"run_id": "run_fixture", "status": "partial"}, tmp_path / "report.html"
    ).read_text(encoding="utf-8")
    assert "<b>부분 완료</b>" in document
    assert "미기록 항목은 해당 실행에서 확인하지 못한 값" in document
    assert "저장된 완료 시도 기록이 없습니다" in document
    assert "인용된 로그 근거가 없습니다" in document


def test_reconstruction_metadata_is_readable_escaped_and_preserved(tmp_path):
    record = {
        "run_id": "run_fixture",
        "status": "partial",
        "reconstruction": {
            "workflow_id": "workflow_fixture",
            "mode": "modernization",
            "workspace": "C:/새 작업공간",
            "source_path": "C:/새 작업공간/revision-2/main.cpp",
            "requirements": "<script>요구사항</script>",
            "input_source_sha256": "fixture-original-source-hash",
            "max_repairs": 2,
            "specification": {"scope": "관찰된 입력만", "evidence_ids": ["ev_original_run"]},
            "builds": [{"revision": 1, "status": "failed"}, {"revision": 2, "status": "success"}],
            "repairs": [{"revision": 2, "reason": "미관찰 원인은 가설"}],
            "tool_calls": [{"name": "propose_source", "ok": True}],
            "completion_reason": "build_approval_required",
        },
    }
    before = copy.deepcopy(record)
    document = render_report(record, tmp_path / "report.html").read_text(encoding="utf-8")
    assert record == before
    for expected in (
        "재구현·현대화 작업과 변경 요구",
        "유지보수·현대화",
        "새 작업공간",
        "main.cpp",
        "fixture-original-source-hash",
        "실행 파일 만들기 허가 필요",
        "근거 기반 행동 명세",
        "빌드·수리 시도",
        "propose_source",
        "모든 입력의 동등성",
    ):
        assert expected in document
    assert "<script>" not in document
    assert "&lt;script&gt;요구사항&lt;/script&gt;" in document
    assert (
        'href="#ev_original_run"' not in document
    )  # Another run's evidence is not a local anchor.
