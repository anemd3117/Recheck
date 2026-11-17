"""Ephemeral official-site display; OAuth validation remains in OAuthService.

The injected browser factory is also the offline test boundary. It receives a
parent and four callbacks, and returns an object with widget, navigate(url), and
dispose(). WebEngine is imported only by the default factory, when first opened.
Full authorization URLs stay in this component's memory until the attempt ends.
"""

from __future__ import annotations

import os
import re
from urllib.parse import parse_qs, urlsplit

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout


_CONSOLES = {
    'openai': ('platform.openai.com', ('/api-keys',)),
    'gemini': ('aistudio.google.com', ('/api-keys', '/apikey')),
    'grok': ('console.x.ai', ('/team/default/api-keys',)),
    'deepseek': ('platform.deepseek.com', ('/api_keys',)),
    'claude': ('platform.claude.com', ('/settings/keys',)),
}
_NAMES = {
    'openai': 'OpenAI API', 'gemini': 'Gemini', 'grok': 'Grok',
    'deepseek': 'DeepSeek', 'claude': 'Claude', 'chatgpt': 'ChatGPT', 'google': 'Google',
}
_STAGES = frozenset(('로그인 대기', '승인 확인', '응답 검사', '연결 완료'))
_GOOGLE_NOTICE = (
    'Google 인증은 제공자 정책에 따라 앱 내부에서 진행하지 않습니다. '
    '기본 브라우저에서 계속을 선택해 주세요.'
)
_DISPLAY_FAILURE = (
    '공식 웹 화면을 표시하지 못했습니다. 다시 연결하거나 기본 브라우저에서 계속을 선택해 주세요.'
)

# User-requested display adaptation, not an authentication or account hook. Only
# action labels/known provider attributes and login-form structure are inspected;
# input values, account rows, cookies and page data are never read or returned.
_GOOGLE_LOGIN_FILTER = r'''
(() => {
    'use strict';
    if (window.top !== window || location.origin !== 'https://auth.openai.com') return;
    const loginPaths = new Set(['/log-in', '/login']);
    const onLoginPath = () => loginPaths.has(location.pathname.replace(/\/$/, ''));
    if (!onLoginPath()) return;
    const accountSelectors = '[data-testid="account-selector"], [data-testid="account-chooser"], '
        + '[data-testid="account-selection"], [data-account-id], [data-user-id], [data-email], '
        + '[role="listbox"]';
    const providerSelectors = 'button[data-provider="google" i], '
        + 'button[data-provider-name="google" i], button[name="provider"][value="google" i], '
        + 'button[name="intent"][value="google" i], button[data-testid="google-login-button"], '
        + 'button[data-testid="google-signin-button"], a[role="button"][data-provider="google" i]';
    const googleLabels = new Set(['continue with google', 'google 계정으로 계속하기',
        'google로 계속하기', 'google로 계속', 'google로 로그인']);
    const normalize = label => (label || '').replace(/\s+/g, ' ').trim().toLowerCase();
    const hidden = new Map();
    let pending = 0;
    let scans = 0;
    let stopped = false;
    let expiry = 0;
    const loginContext = () => onLoginPath() && !document.querySelector(accountSelectors)
        && document.querySelector('form input[type="email"], form input[name="email"], '
            + 'form input[autocomplete="username"]');
    const restore = (action, display) => {
        if (display[0]) action.style.setProperty('display', display[0], display[1]);
        else action.style.removeProperty('display');
    };
    const stop = () => {
        stopped = true;
        observer.disconnect();
        clearTimeout(pending);
        clearTimeout(expiry);
        window.removeEventListener('popstate', schedule);
        for (const [action, display] of hidden) restore(action, display);
        hidden.clear();
    };
    const apply = () => {
        pending = 0;
        if (stopped) return;
        if (++scans > 600) { stop(); return; }
        const matches = new Set();
        if (loginContext()) {
            for (const action of document.querySelectorAll('button, a[role="button"]')) {
                if (action.closest(accountSelectors)
                    || action.querySelector('input, textarea, select, [contenteditable]')) continue;
                if (action.matches(providerSelectors)
                    || googleLabels.has(normalize(action.getAttribute('aria-label')))
                    || googleLabels.has(normalize(action.textContent))) matches.add(action);
            }
        }
        for (const [action, display] of hidden) {
            if (!matches.has(action)) { restore(action, display); hidden.delete(action); }
        }
        for (const action of matches) {
            if (!hidden.has(action)) hidden.set(action, [action.style.getPropertyValue('display'),
                action.style.getPropertyPriority('display')]);
            if (action.style.getPropertyValue('display') !== 'none'
                || action.style.getPropertyPriority('display') !== 'important')
                action.style.setProperty('display', 'none', 'important');
        }
    };
    function schedule() {
        if (!stopped && !pending) pending = setTimeout(apply, 40);
    }
    const observer = new MutationObserver(schedule);
    observer.observe(document.documentElement, {childList: true, subtree: true,
        characterData: true, attributes: true,
        attributeFilter: ['data-provider', 'data-provider-name', 'data-testid', 'aria-label',
            'data-account-id', 'data-user-id', 'data-email', 'role', 'style']});
    window.addEventListener('popstate', schedule);
    expiry = setTimeout(stop, 15 * 60 * 1000);
    apply();
})();
'''


def _url_text(url):
    return url.toString(QUrl.ComponentFormattingOption.FullyEncoded) if isinstance(url, QUrl) else url


def _parsed(url):
    """Reject ambiguous forms before urllib or Chromium can normalize them."""
    value = _url_text(url)
    if not isinstance(value, str) or not value or len(value) > 32768:
        return None
    if '\\' in value or any(ord(character) <= 32 or ord(character) == 127 for character in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ('https', 'http'):
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        host, port = parsed.hostname, parsed.port
        if not host or not re.fullmatch(r'[A-Za-z0-9.-]+', host):
            return None
        if '..' in host or host.startswith('.') or parsed.netloc.endswith(':'):
            return None
        if port is not None and not 1 <= port <= 65535:
            return None
        return parsed
    except (ValueError, TypeError):
        return None


def _google_host(parsed):
    host = parsed.hostname.lower().rstrip('.')
    return host == 'accounts.google.com' or host.endswith('.accounts.google.com')


def _callback(url):
    parsed = _parsed(url)
    if not parsed or (
        parsed.scheme != 'http' or parsed.hostname != '127.0.0.1'
        or parsed.port is None or parsed.netloc != '127.0.0.1:' + str(parsed.port)
        or parsed.path != '/auth/callback' or parsed.query or parsed.fragment
    ):
        return None
    return parsed


def allowed_navigation(url, redirect_uri=''):
    """Only HTTPS, blank teardown, or the current exact loopback callback."""
    if _url_text(url) == 'about:blank':
        return True
    parsed = _parsed(url)
    if not parsed:
        return False
    if parsed.scheme == 'https':
        return not _google_host(parsed)
    target = _callback(redirect_uri)
    return bool(
        target and parsed.netloc == target.netloc and parsed.path == target.path
        and not parsed.fragment
    )


def sanitized_origin(url):
    parsed = _parsed(url)
    if not parsed:
        return ''
    origin = parsed.scheme + '://' + parsed.hostname.lower()
    if parsed.port is not None:
        origin += ':' + str(parsed.port)
    return origin


def _authorization_redirect(url, provider):
    parsed = _parsed(url)
    endpoints = {
        'chatgpt': ('auth.openai.com', '/api/accounts/authorize'),
        'google': ('accounts.google.com', '/o/oauth2/v2/auth'),
    }
    if not parsed or provider not in endpoints or (
        parsed.scheme != 'https' or parsed.port not in (None, 443) or parsed.fragment
        or (parsed.hostname.lower(), parsed.path) != endpoints[provider]
    ):
        return ''
    parameters = parse_qs(parsed.query, keep_blank_values=True)
    redirects = parameters.get('redirect_uri', [])
    if len(redirects) != 1 or not _callback(redirects[0]):
        return ''
    return redirects[0]


def _console_url(provider, url):
    parsed = _parsed(url)
    expected = _CONSOLES.get(provider)
    return bool(
        parsed and expected and parsed.scheme == 'https' and parsed.port in (None, 443)
        and parsed.hostname.lower() == expected[0] and parsed.path in expected[1]
        and not parsed.query and not parsed.fragment
    )


class ProviderBrowserDialog(QDialog):
    cancelled = Signal(object)
    notice = Signal(str)
    external_opened = Signal(object)

    def __init__(self, parent=None, *, external_opener=None, browser_factory=None):
        super().__init__(parent)
        self._external_opener = external_opener or QDesktopServices.openUrl
        self._browser_factory = browser_factory or _WebEngineSession
        self._session = None
        self._active = False
        self._attempt_id = None
        self._serial = 0
        self._original_url = ''
        self._redirect_uri = ''
        self._policy_blocked = False
        self.resize(1050, 800)
        self.setWindowTitle('Recheck · 공식 연결')
        layout = QVBoxLayout(self)
        self.location = QLabel('표시 중인 사이트: 없음')
        self.location.setTextFormat(Qt.TextFormat.PlainText)
        self.location.setWordWrap(True)
        layout.addWidget(self.location)
        self._browser_layout = QVBoxLayout()
        layout.addLayout(self._browser_layout, 1)
        self.status = QLabel('공식 사이트를 열면 연결 준비를 계속할 수 있습니다.')
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        actions = QHBoxLayout()
        self.external_button = QPushButton('기본 브라우저에서 계속')
        self.external_button.setEnabled(False)
        self.cancel_button = QPushButton('연결 중단 · 닫기')
        actions.addWidget(self.external_button)
        actions.addStretch()
        actions.addWidget(self.cancel_button)
        layout.addLayout(actions)
        self.external_button.clicked.connect(self._open_external)
        self.cancel_button.clicked.connect(self.close)

    def _say(self, text):
        self.status.setText(text)
        self.notice.emit(text)

    def _begin(self, url, provider, attempt_id, redirect_uri=''):
        if attempt_id is not None and self._active and self._attempt_id == attempt_id:
            # A rejected code can require a fresh authorization in this same
            # login operation. Retire its browser without cancelling its worker.
            self.finish_attempt(attempt_id)
        else:
            self.cancel_attempt()
        self._active = True
        self._attempt_id = attempt_id
        self._original_url = _url_text(url)
        self._redirect_uri = redirect_uri
        self._policy_blocked = False
        self.setWindowTitle('Recheck · ' + _NAMES[provider] + ' 공식 연결')
        self.external_button.setEnabled(True)
        self._location_changed(self._original_url)
        self.show()
        self.raise_()

    def open_console(self, provider, url, title):
        """Accept a valid native handoff, including a renderer-failure fallback frame."""
        # Use the fixed provider name, never a web title or caller-supplied HTML.
        if not _console_url(provider, url):
            self._say('공식 키 발급 페이지 주소를 확인하지 못했습니다. 연결 화면에서 다시 선택해 주세요.')
            return False
        self._begin(url, provider, None)
        self._say('공식 사이트에서 직접 키를 만든 뒤 Recheck의 보안 입력란에 붙여 넣으세요.')
        self._create_browser()
        return True

    def open_authorization(self, url, *, provider, attempt_id):
        """Return handoff acceptance, never rendering or authentication success.

        A valid native frame retains its explicit external action even when the
        renderer is unavailable or policy requires an external browser. The
        caller must keep its OAuth listener alive until completion or cancel.
        """
        redirect_uri = _authorization_redirect(url, provider)
        if not redirect_uri or attempt_id is None:
            self._say('공식 로그인 요청을 확인하지 못했습니다. 연결을 중단한 뒤 다시 시도해 주세요.')
            return False
        self._begin(url, provider, attempt_id, redirect_uri)
        if provider == 'google':
            self._policy_blocked = True
            self._say(_GOOGLE_NOTICE)
            return True
        self._say(
            '로그인 대기 · ChatGPT 공식 안내는 시스템 브라우저입니다. '
            '내장 표시는 호환 경로이며 로그인·승인은 직접 진행해 주세요.'
        )
        self._create_browser()
        return True

    def _create_browser(self):
        serial = self._serial

        def current(callback, default=None):
            def guarded(*args):
                if self._active and serial == self._serial:
                    return callback(*args)
                return default
            return guarded

        try:
            self._session = self._browser_factory(
                self,
                allow_navigation=current(self._allow_navigation, False),
                location_changed=current(self._location_changed),
                load_finished=current(self._load_finished),
                failed=current(self._display_failed),
            )
            self._browser_layout.addWidget(self._session.widget, 1)
            if self._redirect_uri:
                configure_login = getattr(self._session, 'hide_unsupported_google_login', None)
                if configure_login is not None:
                    configure_login()
            self._session.navigate(self._original_url)
            return True
        except Exception:
            # Exception text can include an entire authorization URL or account.
            self._dispose_browser()
            self._display_failed()
            return False

    def _allow_navigation(self, url):
        parsed = _parsed(url)
        if parsed and _google_host(parsed):
            self._policy_blocked = True
            self._say(_GOOGLE_NOTICE)
            return False
        allowed = allowed_navigation(url, self._redirect_uri)
        if not allowed:
            self._say('이 주소는 앱 내부에서 열 수 없습니다. 필요하면 기본 브라우저에서 계속을 선택해 주세요.')
        return allowed

    def _location_changed(self, url):
        origin = sanitized_origin(url)
        self.location.setText('표시 중인 사이트: ' + (origin or '없음'))
        parsed, target = _parsed(url), _callback(self._redirect_uri)
        if target and parsed and parsed.scheme == 'http' and (
            parsed.netloc == target.netloc and parsed.path == target.path
        ):
            self.status.setText('콜백 화면 도착 · 인증 결과를 검증하고 있습니다.')

    def _load_finished(self, success):
        if not success and not self._policy_blocked:
            self._display_failed()

    def _display_failed(self):
        self._say(_DISPLAY_FAILURE)

    def set_stage(self, text):
        """Only application-owned fixed copy, never provider error/event text."""
        if self._active and text in _STAGES:
            self.status.setText(text)

    def _dispose_browser(self):
        session, self._session = self._session, None
        if session is not None:
            self._browser_layout.removeWidget(session.widget)
            session.dispose()

    def _clear(self):
        self._serial += 1
        self._dispose_browser()
        self._active = False
        self._attempt_id = None
        self._original_url = ''
        self._redirect_uri = ''
        self._policy_blocked = False
        self.external_button.setEnabled(False)
        self.location.setText('표시 중인 사이트: 없음')
        self.status.setText('연결 창을 정리했습니다.')
        self.hide()

    def finish_attempt(self, attempt_id):
        if self._active and self._attempt_id == attempt_id:
            self._clear()

    def cancel_attempt(self, attempt_id=None):
        if self._active and (attempt_id is None or self._attempt_id == attempt_id):
            previous = self._attempt_id
            self._clear()
            if previous is not None:
                self.cancelled.emit(previous)

    def shutdown(self):
        self.cancel_attempt()

    def _open_external(self):
        if not self._active or not self._original_url or not self.external_button.isEnabled():
            return
        try:
            opened = self._external_opener(QUrl(self._original_url))
        except Exception:
            opened = False
        if not opened:
            self._say('기본 브라우저를 열지 못했습니다. 기본 브라우저 설정을 확인한 뒤 다시 선택해 주세요.')
            return
        self._serial += 1
        self._dispose_browser()
        self.external_button.setEnabled(False)
        self.location.setText('표시 중인 사이트: 없음')
        self._say('기본 브라우저에서 연결을 계속하세요. 연결 결과는 Recheck에서 별도로 확인합니다.')
        self.external_opened.emit(self._attempt_id)

    def reject(self):
        # QDialog's Escape key closes through reject(), not closeEvent().
        self.cancel_attempt()
        super().reject()

    def closeEvent(self, event):
        self.cancel_attempt()
        event.accept()


class _WebEngineSession:
    """One unnamed off-record profile; its pages are destroyed before the profile."""

    def __init__(self, parent, *, allow_navigation, location_changed, load_finished, failed):
        flags = os.environ.get('QTWEBENGINE_CHROMIUM_FLAGS', '')
        if any(flag in flags for flag in (
            '--disable-web-security', '--ignore-certificate-errors', '--no-sandbox',
            '--user-agent', '--remote-debugging',
        )) or any(os.environ.get(name) for name in (
            'QTWEBENGINE_DISABLE_SANDBOX', 'QTWEBENGINE_REMOTE_DEBUGGING',
        )):
            raise RuntimeError('Unsafe WebEngine environment')

        from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings
        from PySide6.QtWebEngineWidgets import QWebEngineView

        self._disposed = False
        self._pages = []
        self._popups = []
        self._views = []
        self._parent = parent
        self.profile = QWebEngineProfile(parent)
        self.profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.MemoryHttpCache)
        self.profile.setPersistentCookiesPolicy(
            QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies
        )
        self.profile.downloadRequested.connect(lambda request: request.cancel())
        session = self

        class Page(QWebEnginePage):
            def __init__(self, view):
                super().__init__(session.profile, view)
                self.certificateError.connect(self._reject_certificate)
                self.permissionRequested.connect(lambda permission: permission.deny())
                self.fileSystemAccessRequested.connect(lambda request: request.reject())
                self.registerProtocolHandlerRequested.connect(lambda request: request.reject())
                self.fullScreenRequested.connect(lambda request: request.reject())
                self.windowCloseRequested.connect(view.close)
                self.settings().setAttribute(
                    QWebEngineSettings.WebAttribute.JavascriptCanAccessClipboard, False
                )
                self.settings().setAttribute(
                    QWebEngineSettings.WebAttribute.JavascriptCanPaste, False
                )

            def _reject_certificate(self, error):
                error.rejectCertificate()
                failed()

            def javaScriptConsoleMessage(self, level, message, line_number, source_id):
                pass

            def chooseFiles(self, mode, old_files, accepted_mime_types):
                return []

            def acceptNavigationRequest(self, url, navigation_type, is_main_frame):
                return not session._disposed and allow_navigation(url)

            def createWindow(self, window_type):
                if session._disposed or len(session._popups) >= 4:
                    return None
                popup = QDialog(parent)
                popup.setWindowTitle('Recheck · 공식 사이트 추가 창')
                popup.resize(900, 720)
                layout = QVBoxLayout(popup)
                view = make_view(popup)
                layout.addWidget(view)
                session._popups.append(popup)
                popup.show()
                return view.page()

        def make_view(owner):
            view = QWebEngineView(owner)
            page = Page(view)
            view.setPage(page)
            view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
            view.urlChanged.connect(location_changed)
            view.loadFinished.connect(load_finished)
            page.renderProcessTerminated.connect(lambda *_: failed())
            session._pages.append(page)
            session._views.append(view)
            return view

        self.widget = make_view(parent)

    def hide_unsupported_google_login(self):
        """Install the isolated, login-scoped UI filter before OAuth navigation."""
        from PySide6.QtWebEngineCore import QWebEngineScript

        name = 'recheck-hide-unsupported-google-login'
        if self.profile.scripts().find(name):
            return
        script = QWebEngineScript()
        script.setName(name)
        script.setWorldId(QWebEngineScript.ScriptWorldId.ApplicationWorld)
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentReady)
        script.setRunsOnSubFrames(False)
        script.setSourceCode(_GOOGLE_LOGIN_FILTER)
        self.profile.scripts().insert(script)

    def navigate(self, url):
        if not self._disposed:
            self.widget.setUrl(QUrl(url))

    def dispose(self):
        if self._disposed:
            return
        self._disposed = True
        from shiboken6 import delete

        for view in self._views:
            view.stop()
            view.hide()
        for popup in self._popups:
            popup.close()
        for page in self._pages:
            delete(page)
        for view in self._views:
            delete(view)
        for popup in self._popups:
            delete(popup)
        self._pages.clear()
        self._views.clear()
        self._popups.clear()
        # Off-record cookies and cache are memory-only and die with the profile.
        delete(self.profile)
        self.profile = None
        self._parent = None
