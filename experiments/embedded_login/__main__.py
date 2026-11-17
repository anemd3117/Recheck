"""Throwaway embedded OAuth probe. Run from the repository root with -m.

No production settings or credentials are read. The normal OAuth validator is
reused with an ephemeral DPAPI store and an in-app browser URL handler.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
import tempfile
import threading
from urllib.parse import parse_qs, urlsplit

from PySide6.QtCore import QThread, QTimer, Qt, QUrl, Signal
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PySide6.QtWebEngineWidgets import QWebEngineView

from ai_providers.credentials import AuthError, CredentialStore
from ai_providers.oauth import OAuthConfig, OAuthService


class LoginWorker(QThread):
    authorization_url = Signal(str)
    outcome = Signal(bool, str)

    def __init__(self, provider, client_id, parent):
        super().__init__(parent)
        self.provider, self.client_id = provider, client_id
        self.cancel = threading.Event()

    def run(self):
        try:
            # Outside the repository/OneDrive, removed after this single attempt.
            with tempfile.TemporaryDirectory(prefix='recheck-embedded-oauth-') as directory:
                store = CredentialStore(Path(directory))
                service = OAuthService(store)
                config = OAuthConfig(client_id=self.client_id, timeout=300)
                asyncio.run(
                    service.login(
                        self.provider,
                        'embedded-spike',
                        config,
                        cancel=self.cancel,
                        on_url=self.authorization_url.emit,
                    )
                )
            self.outcome.emit(True, '인증 완료: 토큰·서명 검증 통과 / AI 응답은 미검사')
        except AuthError as error:
            # Never emit exception details, tokens, callback parameters or account data.
            self.outcome.emit(False, '인증 미완료: ' + error.code)
        except Exception:
            self.outcome.emit(False, '인증 미완료: experiment_error')


class LoginPage(QWebEnginePage):
    def __init__(self, owner, view):
        super().__init__(owner.profile, view)
        self.owner = owner
        self.certificateError.connect(lambda error: error.rejectCertificate())

    def javaScriptConsoleMessage(self, level, message, line_number, source_id):
        # Provider console messages may contain identifying or authorization data.
        pass

    def acceptNavigationRequest(self, url, navigation_type, is_main_frame):
        if url.scheme() in ('https', 'about'):
            return True
        parsed = urlsplit(url.toString())
        target = urlsplit(self.owner.redirect_uri)
        return bool(
            target.hostname == '127.0.0.1'
            and parsed.scheme == target.scheme == 'http'
            and parsed.netloc == target.netloc
            and parsed.path == target.path == '/auth/callback'
        )

    def createWindow(self, window_type):
        return self.owner.popup_page()


class ProbeWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('Recheck · 내장 로그인 실험 (운영 미적용)')
        self.resize(1080, 840)
        self.worker = None
        self.redirect_uri = ''
        self.closing = False
        self.popups = []
        # Anonymous profile: no persistent browser cookies/cache or system-browser import.
        self.profile = QWebEngineProfile(self)
        self.profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.MemoryHttpCache)
        self.profile.setPersistentCookiesPolicy(
            QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies
        )
        self.profile.downloadRequested.connect(lambda request: request.cancel())

        panel = QWidget(self)
        layout = QVBoxLayout(panel)
        heading = QLabel('QWebEngineView에서 공식 OAuth 로그인 과정을 확인하는 실험입니다.')
        heading.setWordWrap(True)
        layout.addWidget(heading)
        notice = QLabel(
            'Google은 내장 OAuth를 정책상 허용하지 않습니다. '
            'ChatGPT도 공식 안내는 시스템 브라우저입니다. '
            '차단을 우회하지 않으며, 로그인 화면 표시와 인증 성공을 구분합니다.'
        )
        notice.setWordWrap(True)
        layout.addWidget(notice)
        row = QHBoxLayout()
        self.chatgpt = QPushButton('ChatGPT 내장 로그인 시도')
        self.google = QPushButton('Google 내장 로그인 시도')
        self.stop = QPushButton('시도 중단 · 화면 지우기')
        self.stop.setEnabled(False)
        row.addWidget(self.chatgpt)
        row.addWidget(self.google)
        row.addWidget(self.stop)
        layout.addLayout(row)
        self.client_id = QLineEdit(os.environ.get('RECHECK_EXPERIMENT_GOOGLE_CLIENT_ID', ''))
        self.client_id.setPlaceholderText(
            'Google 실험 시 본인의 Desktop OAuth client ID (공개 식별자)'
        )
        self.client_id.setAccessibleName('Google 실험용 Desktop OAuth client ID')
        layout.addWidget(self.client_id)
        self.status = QLabel('대기: 제공자를 선택하세요. 실계정 로그인은 사용자가 직접 진행합니다.')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.location = QLabel('표시 중인 사이트: 없음 (주소의 쿼리·토큰은 표시하지 않음)')
        layout.addWidget(self.location)
        self.browser = QWebEngineView(panel)
        self.page = LoginPage(self, self.browser)
        self.browser.setPage(self.page)
        self.browser.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.browser.urlChanged.connect(self.location_changed)
        layout.addWidget(self.browser, 1)
        self.setCentralWidget(panel)
        self.chatgpt.clicked.connect(lambda: self.start('chatgpt'))
        self.google.clicked.connect(lambda: self.start('google'))
        self.stop.clicked.connect(self.abort)

    def popup_page(self):
        dialog = QDialog(self)
        dialog.setWindowTitle('Recheck · 로그인 추가 창 (같은 임시 세션)')
        dialog.resize(900, 720)
        layout = QVBoxLayout(dialog)
        view = QWebEngineView(dialog)
        page = LoginPage(self, view)
        view.setPage(page)
        view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        view.urlChanged.connect(self.location_changed)
        layout.addWidget(view)
        self.popups.append(dialog)
        dialog.show()
        return page

    def start(self, provider):
        if self.worker is not None:
            return
        client_id = self.client_id.text().strip() if provider == 'google' else ''
        if provider == 'google' and not client_id:
            self.status.setText('Google OAuth에는 본인이 등록한 Desktop client ID가 필요합니다.')
            return
        self.chatgpt.setEnabled(False)
        self.google.setEnabled(False)
        self.client_id.setEnabled(False)
        self.stop.setEnabled(True)
        self.status.setText('공식 인증 메타데이터 확인 중…')
        self.worker = LoginWorker(provider, client_id, self)
        self.worker.authorization_url.connect(self.open_authorization)
        self.worker.outcome.connect(self.show_outcome)
        self.worker.finished.connect(self.finished)
        self.worker.start()

    def open_authorization(self, url):
        if self.worker is None or self.worker.cancel.is_set():
            return
        self.redirect_uri = parse_qs(urlsplit(url).query)['redirect_uri'][0]
        self.status.setText('로그인·승인 대기: 아래 공식 사이트에서 직접 진행하세요.')
        self.browser.setUrl(QUrl(url))

    def location_changed(self, url):
        # Deliberately omit path, query and fragment, including all OAuth parameters.
        if url.scheme() in ('https', 'http'):
            origin = url.scheme() + '://' + url.host()
            if url.port() != -1:
                origin += ':' + str(url.port())
            self.location.setText('표시 중인 사이트: ' + origin)
        else:
            self.location.setText('표시 중인 사이트: 없음')
        if self.redirect_uri and url.toString().split('?', 1)[0] == self.redirect_uri:
            self.status.setText('콜백 화면 도착: 인증 결과 검증 중 (성공 확정 전)')

    def show_outcome(self, success, message):
        self.status.setText(message)
        self.clear_browser()

    def clear_browser(self):
        self.browser.stop()
        self.browser.setUrl(QUrl('about:blank'))
        for dialog in self.popups:
            dialog.close()
            dialog.deleteLater()
        self.popups.clear()
        self.profile.cookieStore().deleteAllCookies()
        self.profile.clearHttpCache()
        self.redirect_uri = ''

    def abort(self):
        if self.worker is not None:
            self.worker.cancel.set()
        self.status.setText('중단 요청: 인증 작업 정리 중…')
        self.clear_browser()

    def finished(self):
        self.worker.deleteLater()
        self.worker = None
        self.chatgpt.setEnabled(True)
        self.google.setEnabled(True)
        self.client_id.setEnabled(True)
        self.stop.setEnabled(False)
        if self.closing:
            QTimer.singleShot(0, self.close)

    def closeEvent(self, event):
        if self.worker is not None:
            self.closing = True
            self.abort()
            event.ignore()
            return
        self.clear_browser()
        event.accept()


def main():
    if sys.platform != 'win32':
        raise SystemExit('이 실험은 현재 Windows 사용자 DPAPI 저장소를 사용합니다.')
    flags = os.environ.get('QTWEBENGINE_CHROMIUM_FLAGS', '')
    if (
        any(
            flag in flags
            for flag in (
                '--disable-web-security',
                '--ignore-certificate-errors',
                '--no-sandbox',
                '--user-agent',
                '--remote-debugging',
            )
        )
        or os.environ.get('QTWEBENGINE_DISABLE_SANDBOX')
        or os.environ.get('QTWEBENGINE_REMOTE_DEBUGGING')
    ):
        raise SystemExit('우회·디버깅용 WebEngine 환경 설정을 제거한 뒤 실험하세요.')
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    app = QApplication(sys.argv)
    window = ProbeWindow()
    window.show()
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
