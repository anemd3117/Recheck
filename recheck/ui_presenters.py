"""Plain-language presentation of stored observations, without changing them."""

import html


def interpretation_html(result):
    def esc(value):
        return html.escape(str(value))

    llm = result.get('llm') or {}
    interpretation = llm.get('interpretation') or result.get('interpretation') or {}
    style = '<style>body{font-family:Malgun Gothic;color:#2f4566;font-size:13px}h2{font-size:18px}h3{font-size:14px;color:#3d6eae}p{line-height:1.7}a{color:#3973c6}small{color:#7b8ba5}</style>'
    if llm.get('status') == 'success' or interpretation:
        parts = [
            style,
            '<h2>AI가 살펴본 내용</h2><p>'
            + esc(interpretation.get('summary', '설명 내용을 확인하세요.'))
            + '</p>',
        ]
        for index, finding in enumerate(interpretation.get('findings', []), 1):
            parts.extend(
                [
                    '<h3>'
                    + str(index)
                    + '. '
                    + esc(finding.get('claim', '확인이 필요한 내용'))
                    + '</h3>',
                    '<p>' + esc(finding.get('rationale', '')) + '</p>',
                ]
            )
            if finding.get('test_ids'):
                parts.append(
                    '<p><small>관련 입력: ' + esc(', '.join(finding['test_ids'])) + '</small></p>'
                )
            links = [
                '<a href="evidence:' + esc(eid) + '">근거 ' + str(i) + '</a>'
                for i, eid in enumerate(finding.get('evidence_ids', []), 1)
            ]
            if links:
                parts.append('<p>' + ' · '.join(links) + '</p>')
            checks = finding.get('recommended_actions', []) or finding.get('additional_checks', [])
            if checks:
                parts.append(
                    '<p><b>다음으로 확인해 보세요</b><br>'
                    + '<br>'.join(esc(check) for check in checks)
                    + '</p>'
                )
        parts.append(
            '<p><small>AI 설명은 원인을 찾기 위한 참고 의견입니다. 실제 실행 결과와 구분해 확인하세요.</small></p>'
        )
        return ''.join(parts)
    heading = '아직 AI 설명이 없습니다.'
    if llm.get('status') in ('failed', 'cancelled'):
        heading = 'AI 설명을 마치지 못했어요.'
    parts = [
        style,
        '<h2>'
        + heading
        + '</h2><p>프로그램을 실행해서 비교한 결과는 그대로 확인할 수 있습니다. 시작하기에서 AI 연결을 확인한 뒤 “AI 설명 다시 받기”를 눌러 주세요.</p>',
    ]
    for diagnostic in result.get('diagnostics', []):
        parts.append(
            '<p><b>'
            + esc(diagnostic.get('summary', '확인이 필요한 항목'))
            + '</b><br>'
            + esc(diagnostic.get('reason', diagnostic.get('message', '')))
            + '</p>'
        )
    return ''.join(parts)
