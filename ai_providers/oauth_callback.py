"""Static, private HTML for the local OAuth listener's response only.

Receiving an authorization response does not confirm token exchange, account
identity, credential storage, or an inference grant. Keep those results in the
app, and never interpolate callback parameters into this document.
"""

import base64
import hashlib


_STYLE = """
:root {
    color-scheme: light;
    font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", "Malgun Gothic", sans-serif;
    color: #22334d;
    background: #f3f7fd;
    font-synthesis: none;
    -webkit-font-smoothing: antialiased;
}
* { box-sizing: border-box; }
body {
    margin: 0;
    min-height: 100vh;
    min-height: 100svh;
    display: grid;
    place-items: center;
    padding: 40px 24px;
    background: radial-gradient(ellipse at 18% 12%, #e7f0ff 0, transparent 52%),
                radial-gradient(ellipse at 92% 92%, #eaf2ff 0, transparent 46%);
}
.page { width: 100%; max-width: 520px; }
.card {
    padding: 36px 44px 38px;
    border: 1px solid #e0e9f5;
    border-radius: 24px;
    background: #ffffff;
    box-shadow: 0 18px 64px #31598a0d, 0 3px 12px #31598a05;
}
.brand { display: flex; align-items: center; gap: 10px; font-size: 20px; font-weight: 750; letter-spacing: -0.6px; }
.brand-mark {
    display: grid;
    place-items: center;
    width: 29px;
    height: 29px;
    border: 1px solid #c5d9f5;
    border-radius: 9px;
    background: #edf4ff;
}
.brand-mark::before {
    content: "";
    width: 12px;
    height: 8px;
    border-left: 2px solid #3973c6;
    border-bottom: 2px solid #3973c6;
    transform: translateY(-2px) rotate(-45deg);
}
.result { padding-top: 42px; text-align: center; }
.result-mark {
    display: grid;
    place-items: center;
    width: 76px;
    height: 76px;
    margin: 0 auto 24px;
    border: 1px solid #d7e6fc;
    border-radius: 50%;
    background: #edf4ff;
}
.result-mark::before {
    content: "";
    width: 24px;
    height: 14px;
    border-left: 3px solid #3973c6;
    border-bottom: 3px solid #3973c6;
    transform: translateY(-4px) rotate(-45deg);
}
.invalid .result-mark::before {
    content: "!";
    width: auto;
    height: auto;
    border: 0;
    transform: none;
    color: #3973c6;
    font-size: 34px;
    font-weight: 600;
    line-height: 1;
}
.eyebrow { margin: 0 0 10px; color: #3973c6; font-size: 13px; font-weight: 650; letter-spacing: 0.2px; }
h1 { margin: 0; font-size: 27px; line-height: 1.45; letter-spacing: -1.1px; font-weight: 700; }
.description { margin: 15px auto 0; max-width: 330px; color: #5d6e86; font-size: 15px; line-height: 1.85; letter-spacing: -0.25px; }
h1, p { word-break: keep-all; overflow-wrap: break-word; }
.next-step {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 9px;
    margin: 30px 0 0;
    padding: 14px 12px;
    border: 1px solid #e4edf9;
    border-radius: 12px;
    background: #f6f9fe;
    color: #526782;
    font-size: 12px;
    line-height: 1.65;
}
.next-step::before { content: ""; flex: 0 0 5px; height: 5px; border-radius: 50%; background: #8cafdF; }
.eligibility { margin: 24px 0 0; padding-top: 22px; border-top: 1px solid #e8eef7; text-align: left; }
.eligibility h2 { margin: 0; color: #3973c6; font-size: 12px; line-height: 1.65; font-weight: 650; }
.eligibility p { margin: 8px 0 0; color: #64758d; font-size: 12px; line-height: 1.85; letter-spacing: -0.1px; }
.footer { margin: 20px 0 0; color: #8b9ab0; font-size: 11px; line-height: 1.6; text-align: center; letter-spacing: 0.3px; }
@media (max-width: 420px) {
    body { padding: 24px 18px; }
    .card { padding: 28px 24px 28px; border-radius: 20px; }
    .result { padding-top: 34px; }
    h1 { font-size: 24px; letter-spacing: -0.9px; }
    .description { font-size: 14px; }
    .next-step { margin-top: 26px; font-size: 11px; }
}
"""

_STYLE_HASH = base64.b64encode(hashlib.sha256(_STYLE.encode('utf-8')).digest()).decode('ascii')
CALLBACK_CSP = (
    "default-src 'none'; style-src 'sha256-" + _STYLE_HASH
    + "'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)

_ELIGIBILITY_NOTE = (
    '<section class="eligibility" aria-labelledby="eligibility-title">\n'
    '<h2 id="eligibility-title">ChatGPT 연결 이용 조건</h2>\n'
    '<p>이 연결에는 ChatGPT 플랜을 외부 앱에서 사용할 수 있는 권한이 필요합니다. '
    '무료 계정에서 권한 제한 안내가 표시되면, 지원되는 플랜의 계정으로 진행하거나 '
    '다른 AI를 선택해 주세요.</p>\n</section>\n'
)


def callback_document(received: bool) -> bytes:
    """Return one of two fixed documents, with no account or request data."""
    title = '로그인 결과를 받았습니다' if received else '로그인 요청을 확인할 수 없습니다'
    eyebrow = '로그인 응답 확인' if received else '요청 확인 필요'
    note = '연결 결과는 앱에서 안내됩니다.' if received else '앱에서 로그인 상태를 확인한 뒤 다시 시도해 주세요.'
    state_class = 'received' if received else 'invalid'
    return (
        '<!doctype html>\n<html lang="ko">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<title>Recheck · 계정 연결</title>\n'
        '<style>' + _STYLE + '</style>\n</head>\n<body>\n'
        '<div class="page">\n<main class="card ' + state_class + '" aria-labelledby="result-title">\n'
        '<header class="brand"><span class="brand-mark" aria-hidden="true"></span>Recheck</header>\n'
        '<section class="result">\n<div class="result-mark" aria-hidden="true"></div>\n'
        '<p class="eyebrow">' + eyebrow + '</p>\n'
        '<h1 id="result-title">' + title + '</h1>\n'
        '<p class="description">Recheck 앱으로 돌아가 연결 상태를 확인해 주세요.</p>\n'
        '<p class="next-step">' + note + '</p>\n</section>\n'
        + (_ELIGIBILITY_NOTE if received else '')
        + '</main>\n'
        '<p class="footer">Recheck · 계정 연결</p>\n</div>\n</body>\n</html>\n'
    ).encode('utf-8')
