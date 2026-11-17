"""Provider browser boundaries are tested without network or a Chromium process."""

import importlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
from urllib.parse import urlencode

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QApplication, QLabel, QWidget
from shiboken6 import delete


CALLBACK = 'http://127.0.0.1:49123/auth/callback'
AUTH = 'https://auth.openai.com/api/accounts/authorize?' + urlencode({
    'redirect_uri': CALLBACK, 'state': 'private-state', 'login_hint': 'private@example.test',
})
CONSOLE = 'https://aistudio.google.com/api-keys'


def module():
    assert importlib.util.find_spec('recheck.provider_browser') is not None, (
        'The product needs an isolated provider browser component'
    )
    return importlib.import_module('recheck.provider_browser')


class FakeSession:
    def __init__(self, parent, **callbacks):
        self.widget = QWidget(parent)
        self.callbacks = callbacks
        self.loaded = []
        self.disposed = False

    def navigate(self, url):
        self.loaded.append(url)

    def dispose(self):
        self.disposed = True
        self.widget.deleteLater()


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def browser(app):
    sessions, opened = [], []

    def factory(parent, **callbacks):
        session = FakeSession(parent, **callbacks)
        sessions.append(session)
        return session

    dialog = module().ProviderBrowserDialog(
        browser_factory=factory,
        external_opener=lambda url: opened.append(url.toString()) or True,
    )
    yield dialog, sessions, opened
    dialog.shutdown()
    delete(dialog)
    app.processEvents()


@pytest.mark.parametrize('url,expected', [
    ('https://auth.openai.com/login?state=private', True),
    ('https://accounts.google.com.evil.test/login', True),
    ('https://auth.openai.com:443/login', True),
    ('https://user:password@auth.openai.com/login', False),
    ('https://auth.openai.com:bad/login', False),
    ('https://auth.openai.com:65536/login', False),
    ('https:///missing-host', False),
    ('https://auth.openai.com\\@evil.test/', False),
    ('https://auth.openai.com/\nlogin', False),
    ('http://auth.openai.com/login', False),
    ('file:///C:/private.txt', False),
    ('data:text/html,private', False),
    ('javascript:alert(1)', False),
    ('custom-auth:private', False),
    ('about:blank', True),
    ('about:blank?private=1', False),
    ('about:settings', False),
    (CALLBACK + '?code=private&state=private', True),
    (CALLBACK + '#private', False),
    (CALLBACK.replace('49123', '49124'), False),
    (CALLBACK.replace('127.0.0.1', 'localhost'), False),
    (CALLBACK.replace('/auth/callback', '/other'), False),
    (CALLBACK.replace('/auth/callback', '/auth/%63allback'), False),
    (CALLBACK.replace('127.0.0.1', 'user@127.0.0.1'), False),
    ('https://accounts.google.com/o/oauth2/v2/auth', False),
    ('https://accounts.google.com/ServiceLogin', False),
])
def test_navigation_enforces_https_exact_callback_and_google_policy(url, expected):
    assert module().allowed_navigation(url, CALLBACK) is expected


@pytest.mark.parametrize('url', [
    AUTH + '&redirect_uri=' + CALLBACK,
    AUTH.replace('redirect_uri=', 'redirect_uri=&unused='),
    'https://auth.openai.com/api/accounts/authorize?redirect_uri=https://evil.test/callback',
    'https://auth.openai.com/api/accounts/authorize?' + urlencode({
        'redirect_uri': CALLBACK + '?query=not-allowed',
    }),
    'https://evil.test/api/accounts/authorize?' + urlencode({'redirect_uri': CALLBACK}),
    AUTH.replace('/api/accounts/authorize', '/other'),
])
def test_invalid_authorization_is_rejected_without_loading_or_external_open(browser, url):
    dialog, sessions, opened = browser
    notices = []
    dialog.notice.connect(notices.append)
    assert dialog.open_authorization(url, provider='chatgpt', attempt_id='a') is False
    assert sessions == [] and opened == []
    assert notices and all('private' not in notice and 'evil.test' not in notice for notice in notices)


@pytest.mark.parametrize('provider,url', [
    ('openai', 'https://platform.openai.com/api-keys'),
    ('gemini', CONSOLE),
    ('gemini', 'https://aistudio.google.com/apikey'),
    ('grok', 'https://console.x.ai/team/default/api-keys'),
    ('deepseek', 'https://platform.deepseek.com/api_keys'),
    ('claude', 'https://platform.claude.com/settings/keys'),
])
def test_console_accepts_only_known_provider_entrypoints(browser, provider, url):
    dialog, sessions, opened = browser
    assert dialog.open_console(provider, url, '공식 API 키') is True
    assert sessions[-1].loaded == [url] and opened == []


@pytest.mark.parametrize('provider,url', [
    ('gemini', 'https://evil.test/api-keys'),
    ('gemini', 'https://aistudio.google.com/api-keys?private=token'),
    ('gemini', 'https://user@aistudio.google.com/api-keys'),
    ('gemini', 'http://aistudio.google.com/api-keys'),
    ('unknown', CONSOLE),
    ('openai', CONSOLE),
])
def test_console_rejects_untrusted_entrypoints(browser, provider, url):
    dialog, sessions, opened = browser
    assert dialog.open_console(provider, url, 'private-title') is False
    assert sessions == [] and opened == []


def test_render_failure_waits_for_explicit_external_action_using_original_url(browser):
    dialog, sessions, opened = browser
    events, notices = [], []
    dialog.external_opened.connect(events.append)
    dialog.notice.connect(notices.append)
    assert dialog.open_authorization(AUTH, provider='chatgpt', attempt_id='a')
    session = sessions[-1]
    session.callbacks['location_changed']('https://auth.openai.com/private?code=secret-code')
    session.callbacks['failed']()
    assert opened == [] and events == []
    assert '기본 브라우저' in dialog.status.text()
    dialog.external_button.click()
    assert opened == [AUTH] and events == ['a']
    assert session.disposed
    assert not any(marker in ' '.join(notices) for marker in ('private', 'secret-code', '@'))


def test_landing_and_load_finished_never_claim_authentication_success(browser):
    dialog, sessions, opened = browser
    assert dialog.open_authorization(AUTH, provider='chatgpt', attempt_id='a')
    session = sessions[-1]
    session.callbacks['location_changed'](CALLBACK + '?code=secret&state=private-state')
    session.callbacks['load_finished'](True)
    assert '검증' in dialog.status.text()
    assert '완료' not in dialog.status.text() and opened == []
    assert 'secret' not in dialog.location.text() and 'private' not in dialog.location.text()


def test_google_navigation_is_blocked_with_an_explicit_external_option(browser):
    dialog, sessions, opened = browser
    assert dialog.open_console('gemini', CONSOLE, 'Gemini')
    allowed = sessions[-1].callbacks['allow_navigation'](
        'https://accounts.google.com/ServiceLogin?continue=private-token'
    )
    assert allowed is False and opened == []
    assert 'Google' in dialog.status.text() and '기본 브라우저' in dialog.status.text()
    assert 'private' not in dialog.status.text()
    sessions[-1].callbacks['load_finished'](False)
    assert 'Google' in dialog.status.text(), 'An expected blocked load must retain the policy reason'
    dialog.external_button.click()
    assert opened == [CONSOLE]


def test_google_authorization_never_creates_an_embedded_browser(browser):
    dialog, sessions, opened = browser
    url = 'https://accounts.google.com/o/oauth2/v2/auth?' + urlencode({'redirect_uri': CALLBACK})
    assert dialog.open_authorization(url, provider='google', attempt_id='google-a') is True
    assert sessions == [] and opened == []
    assert 'Google' in dialog.status.text() and dialog.external_button.isEnabled()
    dialog.external_button.click()
    assert opened == [url]


def test_close_cancels_the_attempt_and_disposes_its_browser(browser, app):
    dialog, sessions, _ = browser
    cancelled = []
    dialog.cancelled.connect(cancelled.append)
    dialog.open_authorization(AUTH, provider='chatgpt', attempt_id='a')
    dialog.close()
    app.processEvents()
    assert cancelled == ['a'] and sessions[-1].disposed
    assert not dialog.external_button.isEnabled()
    assert dialog.location.text().endswith('없음')


def test_old_finish_and_old_callbacks_do_not_close_or_change_a_new_attempt(browser):
    dialog, sessions, opened = browser
    dialog.open_authorization(AUTH, provider='chatgpt', attempt_id='a')
    old = sessions[-1]
    dialog.cancel_attempt('a')
    dialog.open_authorization(AUTH, provider='chatgpt', attempt_id='b')
    current = sessions[-1]
    before = dialog.status.text(), dialog.location.text()
    old.callbacks['failed']()
    old.callbacks['location_changed']('https://evil.test/private')
    dialog.finish_attempt('a')
    assert not current.disposed and before == (dialog.status.text(), dialog.location.text())
    assert opened == [] and len(sessions) == 2
    dialog.finish_attempt('b')
    assert current.disposed and not dialog.isVisible()


def test_fresh_authorization_for_same_operation_replaces_browser_without_cancelling(browser):
    dialog, sessions, opened = browser
    cancelled = []
    dialog.cancelled.connect(cancelled.append)
    assert dialog.open_authorization(AUTH, provider='chatgpt', attempt_id='a')
    old = sessions[-1]
    fresh = AUTH.replace('private-state', 'fresh-state').replace('49123', '49124')
    assert dialog.open_authorization(fresh, provider='chatgpt', attempt_id='a')
    current = sessions[-1]
    assert cancelled == [], 'A fresh code round must keep its login worker alive'
    assert old.disposed and current is not old and not current.disposed
    assert current.loaded == [fresh] and dialog._original_url == fresh
    before = dialog.status.text(), dialog.location.text()
    old.callbacks['failed']()
    old.callbacks['location_changed']('https://old.invalid')
    assert before == (dialog.status.text(), dialog.location.text())
    assert old.callbacks['allow_navigation'](CALLBACK) is False
    assert current.callbacks['allow_navigation'](CALLBACK) is False
    assert current.callbacks['allow_navigation'](CALLBACK.replace('49123', '49124')) is True
    dialog.external_button.click()
    assert opened == [fresh] and cancelled == []
    dialog.cancel_attempt('a')
    assert cancelled == ['a'] and not dialog._active


def test_different_authorization_operation_still_cancels_the_previous_worker(browser):
    dialog, sessions, _ = browser
    cancelled = []
    dialog.cancelled.connect(cancelled.append)
    assert dialog.open_authorization(AUTH, provider='chatgpt', attempt_id='a')
    old = sessions[-1]
    assert dialog.open_authorization(AUTH, provider='chatgpt', attempt_id='b')
    assert cancelled == ['a'] and old.disposed
    assert dialog._active and dialog._attempt_id == 'b' and not sessions[-1].disposed


def test_shutdown_clears_private_urls_and_is_idempotent(browser):
    dialog, sessions, opened = browser
    cancelled = []
    dialog.cancelled.connect(cancelled.append)
    dialog.open_authorization(AUTH, provider='chatgpt', attempt_id='a')
    dialog.shutdown()
    dialog.shutdown()
    dialog.external_button.click()
    assert sessions[-1].disposed and cancelled == ['a'] and opened == []
    assert all('private' not in label.text() for label in dialog.findChildren(QLabel))
    assert AUTH not in vars(dialog).values()


def test_factory_or_navigation_failure_is_safe_and_still_allows_manual_fallback(app):
    for navigate_failure in (False, True):
        sessions, notices, opened = [], [], []

        def factory(parent, **callbacks):
            if not navigate_failure:
                raise RuntimeError('private-factory-token')
            session = FakeSession(parent, **callbacks)
            sessions.append(session)

            def fail(url):
                raise RuntimeError('private-navigation-token')

            session.navigate = fail
            return session

        dialog = module().ProviderBrowserDialog(
            browser_factory=factory, external_opener=lambda url: opened.append(url.toString()) or True
        )
        dialog.notice.connect(notices.append)
        assert dialog.open_console('gemini', CONSOLE, 'Gemini') is True
        assert opened == []
        assert all('private' not in message for message in notices)
        assert all(session.disposed for session in sessions)
        dialog.external_button.click()
        assert opened == [CONSOLE]
        dialog.shutdown()
        delete(dialog)


def test_renderer_unavailable_accepts_auth_handoff_and_keeps_manual_fallback_alive(app):
    opened, cancelled = [], []

    def unavailable(parent, **callbacks):
        raise RuntimeError('private-renderer-unavailable')

    dialog = module().ProviderBrowserDialog(
        browser_factory=unavailable,
        external_opener=lambda url: opened.append(url.toString()) or True,
    )
    dialog.cancelled.connect(cancelled.append)
    assert dialog.open_authorization(AUTH, provider='chatgpt', attempt_id='waiting') is True
    assert dialog.isVisible() and dialog.external_button.isEnabled()
    assert dialog._active and dialog._attempt_id == 'waiting'
    assert opened == [] and cancelled == []
    assert '기본 브라우저' in dialog.status.text() and 'private' not in dialog.status.text()
    dialog.external_button.click()
    assert opened == [AUTH] and cancelled == []
    assert dialog._active, 'External handoff must keep the real OAuth listener attempt alive'
    dialog.finish_attempt('waiting')
    assert not dialog._active and not dialog.isVisible()
    delete(dialog)


def test_external_opener_failure_is_redacted_and_does_not_cancel_waiting_auth(browser):
    dialog, sessions, _ = browser
    notices, cancelled, external = [], [], []
    dialog.notice.connect(notices.append)
    dialog.cancelled.connect(cancelled.append)
    dialog.external_opened.connect(external.append)

    def fail(url):
        raise RuntimeError('private-external-token')

    dialog._external_opener = fail
    dialog.open_authorization(AUTH, provider='chatgpt', attempt_id='a')
    dialog.external_button.click()
    assert not sessions[-1].disposed and cancelled == [] and external == []
    assert notices and all('private' not in message for message in notices)


def test_stage_accepts_only_fixed_non_sensitive_copy(browser):
    dialog, _, _ = browser
    dialog.open_authorization(AUTH, provider='chatgpt', attempt_id='a')
    dialog.set_stage('응답 검사')
    assert dialog.status.text() == '응답 검사'
    dialog.set_stage('private-token https://auth.openai.com/?code=private')
    assert dialog.status.text() == '응답 검사'


@pytest.mark.parametrize('name,value', [
    ('QTWEBENGINE_CHROMIUM_FLAGS', '--ignore-certificate-errors'),
    ('QTWEBENGINE_CHROMIUM_FLAGS', '--no-sandbox'),
    ('QTWEBENGINE_CHROMIUM_FLAGS', '--disable-web-security'),
    ('QTWEBENGINE_CHROMIUM_FLAGS', '--user-agent=private-spoof'),
    ('QTWEBENGINE_DISABLE_SANDBOX', '1'),
    ('QTWEBENGINE_REMOTE_DEBUGGING', '9222'),
])
def test_unsafe_inherited_webengine_settings_fail_closed_without_fallback(app, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    opened = []
    dialog = module().ProviderBrowserDialog(
        external_opener=lambda url: opened.append(url.toString()) or True
    )
    assert dialog.open_console('gemini', CONSOLE, 'Gemini') is True
    assert opened == [] and dialog._session is None
    assert 'private' not in dialog.status.text()
    dialog.shutdown()
    delete(dialog)


def test_origin_redacts_path_query_fragment_credentials_and_malformed_urls():
    assert module().sanitized_origin('https://auth.openai.com/login?private#secret') == (
        'https://auth.openai.com'
    )
    assert module().sanitized_origin(QUrl('https://auth.openai.com:8443/private')) == (
        'https://auth.openai.com:8443'
    )
    for url in ('https://private@auth.openai.com/', 'https://auth.openai.com:bad/', 'file:///C:/private'):
        assert module().sanitized_origin(url) == ''


def test_google_login_filter_is_requested_only_for_the_embedded_oauth_session(app):
    sessions = []

    class LoginSession(FakeSession):
        def __init__(self, parent, **callbacks):
            super().__init__(parent, **callbacks)
            self.google_filter_requests = 0

        def hide_unsupported_google_login(self):
            self.google_filter_requests += 1

    def factory(parent, **callbacks):
        session = LoginSession(parent, **callbacks)
        sessions.append(session)
        return session

    dialog = module().ProviderBrowserDialog(browser_factory=factory)
    assert dialog.open_console('openai', 'https://platform.openai.com/api-keys', 'OpenAI')
    assert sessions[-1].google_filter_requests == 0
    assert dialog.open_authorization(AUTH, provider='chatgpt', attempt_id='a')
    assert sessions[-1].google_filter_requests == 1
    dialog.shutdown()
    delete(dialog)


_GOOGLE_FILTER_FIXTURE_PROBE = r'''
import json
import time
from PySide6.QtCore import QEventLoop, Qt, QTimer, QUrl
from PySide6.QtWidgets import QApplication, QDialog
QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
from PySide6.QtWebEngineCore import QWebEngineScript, QWebEngineUrlRequestInterceptor
from recheck.provider_browser import _WebEngineSession

app = QApplication([])
owner = QDialog()
session = _WebEngineSession(owner, allow_navigation=lambda url: True,
    location_changed=lambda url: None, load_finished=lambda ok: None, failed=lambda: None)

class NoNetwork(QWebEngineUrlRequestInterceptor):
    def interceptRequest(self, info):
        if info.requestUrl().scheme() not in ('about', 'data'):
            info.block(True)

interceptor = NoNetwork()
session.profile.setUrlRequestInterceptor(interceptor)
before = session.profile.scripts().count()
value_spy = QWebEngineScript()
value_spy.setName('synthetic-fixture-value-read-counter')
value_spy.setWorldId(QWebEngineScript.ScriptWorldId.ApplicationWorld)
value_spy.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
value_spy.setRunsOnSubFrames(False)
value_spy.setSourceCode("""
globalThis.fixtureValueReads = 0;
for (const constructor of [HTMLInputElement, HTMLTextAreaElement, HTMLSelectElement]) {
    const descriptor = Object.getOwnPropertyDescriptor(constructor.prototype, 'value');
    Object.defineProperty(constructor.prototype, 'value', {...descriptor,
        get() { globalThis.fixtureValueReads++; return descriptor.get.call(this); }
    });
}
""")
session.profile.scripts().insert(value_spy)
configure = getattr(session, 'hide_unsupported_google_login', None)
if configure:
    configure()
    configure()
scripts = session.profile.scripts().find('recheck-hide-unsupported-google-login')

def pause(milliseconds):
    loop = QEventLoop()
    QTimer.singleShot(milliseconds, loop.quit)
    loop.exec()

def javascript(source, world=QWebEngineScript.ScriptWorldId.MainWorld):
    loop, result = QEventLoop(), []
    session.widget.page().runJavaScript(source, world, lambda value: (result.append(value), loop.quit()))
    QTimer.singleShot(3000, loop.quit)
    loop.exec()
    assert result, 'Synthetic fixture JavaScript did not complete'
    return result[0]

html = """<!doctype html><html><body>
<form id="login"><input id="email" type="email"><input id="password" type="password">
<button id="google_en" type="button">Continue with Google</button>
<button id="google_ko" type="button">Google 계정으로 계속하기</button>
<button id="google_structural" type="button" data-provider="google">G</button>
<button id="microsoft" type="button">Continue with Microsoft Account</button>
<button id="apple" type="button">Continue with Apple</button>
<button id="email_continue" type="submit">Continue</button>
<button id="google_settings" type="button">Google account settings</button>
</form><a id="docs" href="https://example.invalid/help">Continue with Google</a>
</body></html>"""

def load(url, content=html):
    loop = QEventLoop()
    def loaded(ok):
        loop.quit()
    session.widget.loadFinished.connect(loaded)
    session.widget.setHtml(content, QUrl(url))
    QTimer.singleShot(3000, loop.quit)
    loop.exec()
    session.widget.loadFinished.disconnect(loaded)
    pause(150)

def hidden(element_id):
    return javascript("getComputedStyle(document.getElementById(" + json.dumps(element_id) + ")).display === 'none'")

def wait_for_display(element_id, expected):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if hidden(element_id) is expected:
            return True
        pause(30)
    return False

load('https://auth.openai.com/log-in')
trusted = all(hidden(key) for key in ('google_en', 'google_ko', 'google_structural'))
preserved = all(not hidden(key) for key in ('microsoft', 'apple', 'email_continue', 'google_settings', 'docs'))
input_reads = javascript('globalThis.fixtureValueReads', QWebEngineScript.ScriptWorldId.ApplicationWorld)
javascript("document.getElementById('google_en').remove(); const added = document.createElement('button'); added.id = 'google_added'; added.textContent = 'Continue with Google'; document.getElementById('login').appendChild(added)")
rerender = wait_for_display('google_added', True)
javascript("history.replaceState(null, '', '/select-account'); document.body.setAttribute('data-testid', 'account-selector')")
restored_on_account_route = wait_for_display('google_added', False)

boundaries = []
for url in ('https://auth.openai.com/select-account', 'https://auth.openai.com/consent',
            'https://auth.openai.com.evil.test/log-in', 'https://example.test/log-in',
            'https://auth.openai.com:444/log-in'):
    load(url)
    boundaries.append(not hidden('google_en'))
load('https://auth.openai.com/log-in', html.replace('<form id="login">', '<form id="login" data-testid="account-selector">'))
chooser_preserved = not hidden('google_en')
script_configuration = (before == 0 and len(scripts) == 1 and
    scripts[0].worldId() == QWebEngineScript.ScriptWorldId.ApplicationWorld and
    not scripts[0].runsOnSubFrames())
session.dispose()
print(json.dumps(dict(trusted=trusted, preserved=preserved, input_reads=input_reads,
    rerender=rerender, restored_on_account_route=restored_on_account_route,
    boundaries=all(boundaries), chooser_preserved=chooser_preserved,
    script_configuration=script_configuration)), flush=True)
'''


@pytest.mark.skipif(not os.environ.get('RECHECK_WEBENGINE_FIXTURE_PYTHON'),
                    reason='Explicit isolated WebEngine fixture interpreter is required')
def test_real_webengine_hides_only_unsupported_google_login_on_synthetic_pages():
    probe_env = {**os.environ, 'QT_QPA_PLATFORM': 'offscreen'}
    result = subprocess.run(
        [os.environ['RECHECK_WEBENGINE_FIXTURE_PYTHON'], '-c', _GOOGLE_FILTER_FIXTURE_PROBE],
        cwd=Path(__file__).resolve().parents[1], env=probe_env,
        text=True, capture_output=True, timeout=45,
    )
    assert result.returncode == 0, result.stderr
    evidence = json.loads(result.stdout.strip().splitlines()[-1])
    assert evidence == dict(trusted=True, preserved=True, input_reads=0, rerender=True,
                           restored_on_account_route=True, boundaries=True,
                           chooser_preserved=True, script_configuration=True)
