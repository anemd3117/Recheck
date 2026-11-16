"""Presentation must preserve measured denominators and treat program text as data."""

from recheck.ui_presenters import interpretation_html


def test_ai_explanation_escapes_program_text_and_links_real_selected_references():
    run = {
        'llm': {
            'status': 'success',
            'interpretation': {
                'summary': '<script>sample</script>',
                'findings': [
                    {
                        'claim': '<b>data</b>',
                        'rationale': '원인 후보입니다.',
                        'test_ids': ['case-1'],
                        'evidence_ids': ['ev_real'],
                        'recommended_actions': ['<img src=x>'],
                    }
                ],
            },
        }
    }
    page = interpretation_html(run)
    assert '<script>' not in page and '&lt;script&gt;' in page
    assert '<img src=x>' not in page and '&lt;img src=x&gt;' in page
    assert 'href="evidence:ev_real"' in page
    assert 'case-1' in page and '참고 의견' in page


def test_rejected_model_output_does_not_become_an_ai_explanation():
    page = interpretation_html(
        {'llm': {'status': 'failed', 'responses': [{'content': 'UNVALIDATED'}]}}
    )
    assert 'UNVALIDATED' not in page
    assert '비교한 결과는 그대로' in page
