from recheck.report import render_report
from recheck.engine import Engine


def test_remote_report_names_actual_provider_role_auth_and_usage(tmp_path):
    record = {
        'run_id': 'run_fixture',
        'status': 'success',
        'settings': {'model': 'old-local'},
        'llm': {
            'provider': 'openai',
            'auth_mode': 'api_key',
            'role': 'analysis',
            'profile_id': 'chosen',
            'model': 'actual-cloud',
            'server': 'https://api.openai.com/v1',
            'execution': {'kind': 'remote_api'},
            'usage': {'input_tokens': 21, 'output_tokens': 9},
            'interpretation': {'summary': '등록 입력 범위의 가설', 'findings': []},
        },
    }
    text = render_report(record, tmp_path / 'report.html').read_text(encoding='utf-8')
    for value in (
        'AI 제공자',
        'openai',
        '인증 방식',
        'api_key',
        'AI 역할',
        'analysis',
        'actual-cloud',
        '원격 API',
        'input_tokens',
        'output_tokens',
        '비용 확인 불가',
    ):
        assert value in text
    assert '로컬 LLM 해석' not in text


def test_old_report_keeps_legacy_interpretation_heading(tmp_path):
    record = {
        'run_id': 'run_old',
        'status': 'success',
        'llm': {'interpretation': {'summary': '이전 기록', 'findings': []}},
    }
    text = render_report(record, tmp_path / 'report.html').read_text(encoding='utf-8')
    assert '로컬 LLM 해석' in text


def test_request_usage_totals_include_failed_json_attempts_without_replacing_native_usage():
    result = {}
    Engine._provider_metadata(
        result,
        {
            'provider': 'ollama',
            'usage': {'input_tokens': 11, 'output_tokens': 7, 'native': {'eval_count': 7}},
        },
    )
    Engine._provider_metadata(
        result,
        {
            'provider': 'ollama',
            'usage': {'input_tokens': 13, 'output_tokens': 5, 'native': {'eval_count': 5}},
        },
    )
    assert result['usage_totals'] == {'input_tokens': 24, 'output_tokens': 12}
    assert len(result['request_usage']) == 2
    assert result['usage']['native'] == {'eval_count': 5}
