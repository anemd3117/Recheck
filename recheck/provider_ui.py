"""프로필 선택과 명시적인 인증·연결·추론 검사를 제공하는 한국어 Qt 설정."""

from __future__ import annotations

import asyncio
import copy
import re
import threading

from PySide6.QtCore import Qt, QThread, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)
from .setup_flow import RECOMMENDED_MODEL

PROVIDERS = [
    ('ollama', 'Ollama · 내 PC'),
    ('openai', 'OpenAI / ChatGPT'),
    ('anthropic', 'Claude API'),
    ('gemini', 'Gemini API'),
    ('compatible', 'OpenAI 호환 서버'),
]
AUTH = {
    'ollama': [('none', '로컬 연결')],
    'openai': [('api_key', 'API 키'), ('chatgpt', 'ChatGPT 브라우저 로그인')],
    'anthropic': [('api_key', 'Claude Console API 키')],
    'gemini': [('api_key', 'API 키'), ('google_oauth', '등록한 Desktop 앱으로 로그인')],
    'compatible': [('api_key', 'API 키'), ('none', '인증 없는 로컬 서버')],
}
ENDPOINTS = {
    'ollama': 'http://127.0.0.1:11434',
    'openai': 'https://api.openai.com/v1',
    'anthropic': 'https://api.anthropic.com/v1',
    'gemini': 'https://generativelanguage.googleapis.com/v1beta',
    'compatible': '',
}
EVIDENCE = [
    ('runtime', '등록 입력과 선택한 실행 관찰'),
    ('static', '선택한 함수·정적 분석 근거'),
    ('code', '함수의 디컴파일 코드 · 선택한 함수만'),
]


def value_dict(value):
    return value.model_dump() if hasattr(value, 'model_dump') else dict(value)


def label(text, name=''):
    result = QLabel(text)
    result.setWordWrap(True)
    result.setTextFormat(Qt.TextFormat.PlainText)
    if name:
        result.setObjectName(name)
    return result


def supported(value):
    if isinstance(value, dict):
        return any(value.get(key) is True for key in ('supported', 'ok', 'success'))
    return value is True


class ProviderWorker(QThread):
    """공용 작업 슬롯이 없는 독립 위젯에서도 UI 스레드를 막지 않는다."""

    progress = Signal(dict)

    def __init__(self, work, parent=None):
        super().__init__(parent)
        self.work = work
        self.cancel_event = threading.Event()
        self.result = None

    def run(self):
        try:
            self.result = asyncio.run(self.work(self.cancel_event, self.progress.emit))
        except BaseException:
            self.result = {
                'ok': False,
                'code': 'cancelled' if self.cancel_event.is_set() else 'operation_failed',
            }


class ProviderPanel(QWidget):
    profile_changed = Signal(object)
    capabilities_checked = Signal(object)
    settings_refreshed = Signal()
    notice = Signal(str)
    busy_changed = Signal(bool)
    setup_ready = Signal(object)
    oauth_url_received = Signal(str)

    def __init__(self, manager, launch_task=None, is_busy=None, parent=None, prepare_change=None):
        super().__init__(parent)
        self.manager, self._launcher = manager, launch_task
        self._is_busy = is_busy or (lambda: False)
        self._prepare_change = prepare_change
        self.oauth_url_received.connect(self._show_web_oauth_dialog)
        self._worker = None
        self._workers = set()
        self._worker_completions = {}
        self._operation_active = False
        self._active_cancel = None
        self._cancel_pending = False
        self._external_busy = False
        self._generation = 0
        self._loading = False
        self._base = {}
        self._draft = None
        self._current_id = ''
        self._build()
        self.reload_profiles()

    def _build(self):
        frame = QVBoxLayout(self)
        frame.addWidget(label('AI 연결', 'pageTitle'))
        self.setup_scroll = QScrollArea()
        self.setup_scroll.setWidgetResizable(True)
        self.setup_scroll.setAccessibleName('Gemini 기본 연결과 GPT 추천 전체 안내')
        setup_content = QWidget()
        outer = QVBoxLayout(setup_content)
        frame.addWidget(self.setup_scroll)
        self._build_gemini_card(outer)
        quick = QGroupBox('추천 · ChatGPT로 시작하기')
        quick_layout = QVBoxLayout(quick)
        self.quick_summary = label(
            '추천 모델: GPT-6.1 Sol · 코드 분석에 맞는 설정을 자동으로 준비합니다.'
        )
        quick_layout.addWidget(self.quick_summary)
        quick_layout.addWidget(
            label(
                '로그인 → 모델 자동 선택 → 응답 확인 → 기본 AI 저장까지 한 번에 진행합니다.',
                'muted',
            )
        )
        quick_layout.addWidget(
            label(
                '비교에 필요한 입력·실행 결과·선택한 함수 정보를 GPT에 전달하는 설정을 적용합니다. '
                '연결 확인에는 짧은 AI 요청이 최대 4회 발생합니다. ChatGPT 이용 한도가 적용됩니다.',
                'muted',
            )
        )
        quick_row = QHBoxLayout()
        self.quick_button = QPushButton('ChatGPT로 연결하고 자동 설정')
        self.quick_button.setObjectName('primary')
        self.quick_button.setAccessibleName('ChatGPT 원클릭 연결')
        self.quick_cancel = QPushButton('연결 중단')
        self.quick_cancel.setEnabled(False)
        quick_row.addWidget(self.quick_button, 1)
        quick_row.addWidget(self.quick_cancel)
        quick_layout.addLayout(quick_row)
        self.quick_progress = label(
            '이 버튼을 누른 뒤 브라우저에서 계정과 사용 권한을 승인해 주세요.', 'muted'
        )
        quick_layout.addWidget(self.quick_progress)
        outer.addWidget(quick)
        self.details_toggle = QPushButton('다른 AI 사용 · 세부 설정')
        self.details_toggle.setCheckable(True)
        outer.addWidget(self.details_toggle)
        scroll = QScrollArea()
        self.details_scroll = scroll
        scroll.setWidgetResizable(True)
        content = QWidget()
        layout = QVBoxLayout(content)
        profiles = QGroupBox('저장한 연결')
        row = QHBoxLayout(profiles)
        self.profile_combo = QComboBox()
        self.profile_combo.setAccessibleName('저장한 AI 프로필')
        row.addWidget(self.profile_combo, 1)
        self.new_button = QPushButton('새 연결')
        self.delete_button = QPushButton('연결 삭제')
        row.addWidget(self.new_button)
        row.addWidget(self.delete_button)
        layout.addWidget(profiles)

        connection = QGroupBox('1. 제공자와 연결 방법')
        form = QFormLayout(connection)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.name = QLineEdit()
        self.name.setAccessibleName('연결 이름')
        self.provider_combo = QComboBox()
        for provider, title in PROVIDERS:
            self.provider_combo.addItem(title, provider)
        self.provider_combo.setAccessibleName('AI 제공자')
        self.auth_combo = QComboBox()
        self.auth_combo.setAccessibleName('AI 인증 방식')
        form.addRow('연결 이름', self.name)
        form.addRow('제공자', self.provider_combo)
        form.addRow('연결 방법', self.auth_combo)
        self.support_note = label('', 'muted')
        form.addRow(self.support_note)
        layout.addWidget(connection)

        self.web_login_box = QGroupBox('🌐 AI 웹 브라우저 (Gemini · Claude · ChatGPT · Grok)')
        web_layout = QVBoxLayout(self.web_login_box)
        web_layout.addWidget(
            label(
                'API 키 발급 페이지 대신 Gemini, Claude, ChatGPT, Grok 공식 웹 서비스를 내장 브라우저로 직접 띄웁니다.',
                'muted',
            )
        )
        web_btn_row = QHBoxLayout()
        self.web_login_btn = QPushButton('🌐 AI 웹 브라우저 열기 (QWebEngine)')
        self.web_login_btn.setStyleSheet(
            'background-color: #0d6efd; color: white; font-weight: bold; padding: 7px 16px; border-radius: 4px; font-size: 13px;'
        )
        self.web_login_btn.clicked.connect(self.open_web_login)
        web_btn_row.addWidget(self.web_login_btn)
        web_btn_row.addStretch()
        web_layout.addLayout(web_btn_row)
        layout.addWidget(self.web_login_box)

        self.key_box = QGroupBox('API 키 보안 저장')
        keys = QVBoxLayout(self.key_box)
        key_row = QHBoxLayout()
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setAccessibleName('API 키 보안 입력')
        self.api_key.setPlaceholderText('이 앱에서 사용할 키를 입력하세요')
        self.key_save = QPushButton('키 보안 저장')
        self.key_delete = QPushButton('저장한 키 삭제')
        key_row.addWidget(self.api_key, 1)
        key_row.addWidget(self.key_save)
        key_row.addWidget(self.key_delete)
        keys.addLayout(key_row)
        self.key_status = label('저장된 키 없음', 'muted')
        keys.addWidget(self.key_status)
        keys.addWidget(
            label(
                '키는 사용자 보안 저장소에 보관합니다. 입력한 값은 저장 시도 후 화면에서 지웁니다.',
                'muted',
            )
        )
        layout.addWidget(self.key_box)

        self.oauth_box = QGroupBox('공식 브라우저 로그인')
        oauth = QVBoxLayout(self.oauth_box)
        self.oauth_help = label('', 'muted')
        oauth.addWidget(self.oauth_help)
        self.google_fields = QWidget()
        google = QFormLayout(self.google_fields)
        google.setContentsMargins(0, 0, 0, 0)
        self.oauth_client_id = QLineEdit()
        self.oauth_client_id.setAccessibleName('Desktop OAuth client ID')
        self.oauth_client_secret = QLineEdit()
        self.oauth_client_secret.setEchoMode(QLineEdit.EchoMode.Password)
        self.oauth_client_secret.setAccessibleName('Desktop OAuth client secret 보안 입력')
        self.google_project_id = QLineEdit()
        self.google_project_id.setAccessibleName('Google Cloud 프로젝트 ID')
        self.oauth_scopes = QLineEdit()
        self.oauth_scopes.setPlaceholderText('등록 앱에서 허용한 scope를 공백으로 구분')
        google.addRow('Desktop client ID', self.oauth_client_id)
        google.addRow('Client secret · 필요한 경우', self.oauth_client_secret)
        google.addRow('Cloud 프로젝트 ID', self.google_project_id)
        google.addRow('요청 scope', self.oauth_scopes)
        oauth.addWidget(self.google_fields)
        oauth_row = QHBoxLayout()
        self.login_button = QPushButton('브라우저로 로그인')
        self.logout_button = QPushButton('로그아웃')
        oauth_row.addWidget(self.login_button)
        oauth_row.addWidget(self.logout_button)
        self.account_button = QPushButton('계정 상태 확인')
        oauth_row.addWidget(self.account_button)
        oauth_row.addStretch()
        oauth.addLayout(oauth_row)
        self.account_label = label('연결된 계정 없음', 'muted')
        oauth.addWidget(self.account_label)
        layout.addWidget(self.oauth_box)

        models = QGroupBox('2. 모델과 기능 확인')
        model_layout = QVBoxLayout(models)
        model_row = QHBoxLayout()
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.model_combo.setAccessibleName('AI 모델 목록 또는 직접 모델 ID')
        self.model_combo.lineEdit().setPlaceholderText('모델 ID를 직접 입력하거나 목록에서 선택')
        self.models_button = QPushButton('모델 목록 조회')
        model_row.addWidget(self.model_combo, 1)
        model_row.addWidget(self.models_button)
        model_layout.addLayout(model_row)
        probe_row = QHBoxLayout()
        self.connection_button = QPushButton('연결 확인 · 추론 없음')
        self.test_button = QPushButton('추론 기능 검사 · 비용 발생 가능')
        probe_row.addWidget(self.connection_button)
        probe_row.addWidget(self.test_button)
        model_layout.addLayout(probe_row)
        model_layout.addWidget(
            label(
                '수동 기능 검사는 작업당 요청 상한과 별도인 최대 4회입니다. 지원 API는 응답당 최대 256토큰을 요청합니다. '
                'ChatGPT 플랜은 서버 출력 토큰 상한을 강제할 수 없어 앱의 시간·수신량·4회 상한으로 제한합니다.',
                'muted',
            )
        )
        self.capability_summary = label(
            '기본 응답 · 구조화 출력 · 도구 호출: 아직 확인 전', 'muted'
        )
        model_layout.addWidget(self.capability_summary)
        layout.addWidget(models)

        self.remote_box = QGroupBox('3. 외부 전송과 사용 범위')
        remote = QVBoxLayout(self.remote_box)
        self.remote_note = label(
            '분석에 필요한 자료를 선택한 AI에 전달합니다. 연결과 실제 응답을 확인한 뒤 사용하세요.',
            'banner',
        )
        remote.addWidget(self.remote_note)
        self.enabled_check = QCheckBox('이 연결 사용 허용')
        self.external_consent = QCheckBox('선택 AI 서버에 근거 전달 허용')
        remote.addWidget(self.enabled_check)
        remote.addWidget(self.external_consent)
        self.evidence_checks = {}
        for kind, title in EVIDENCE:
            checkbox = QCheckBox(title)
            self.evidence_checks[kind] = checkbox
            remote.addWidget(checkbox)
        remote.addWidget(
            label(
                '함수·정적 분석 근거에 디컴파일 코드가 포함되면 두 종류를 함께 허용해야 합니다. 전체 원본 소스 파일 전송은 이 화면에서 제공하지 않습니다.',
                'muted',
            )
        )
        remote.addWidget(
            label(
                '비용은 제공자·모델·계정에 따라 달라 확인되지 않을 수 있습니다. 앱의 호출 제한과 계정 결제 한도는 별개입니다. 바이너리·DB·전체 로그를 자동 전송하지 않습니다.',
                'muted',
            )
        )
        layout.addWidget(self.remote_box)

        self.advanced_toggle = QPushButton('고급 주소·예산 보기')
        self.advanced_toggle.setCheckable(True)
        layout.addWidget(self.advanced_toggle)
        self.advanced = QGroupBox('고급 연결 설정과 호출 예산')
        advanced_form = QFormLayout(self.advanced)
        advanced_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.endpoint = QLineEdit()
        self.endpoint.setAccessibleName('AI API endpoint')
        advanced_form.addRow('API 주소', self.endpoint)
        self.workspace_id = QLineEdit()
        self.api_version = QLineEdit()
        advanced_form.addRow('Workspace ID · 필요한 경우', self.workspace_id)
        advanced_form.addRow('API 버전 · 지정한 경우', self.api_version)
        self.schema_mode = QComboBox()
        self.schema_mode.addItem('제공자의 JSON Schema 지원 사용', 'native_schema')
        self.schema_mode.addItem('JSON 요청 + 앱에서 스키마 검사', 'json_prompt')
        advanced_form.addRow('구조화 응답 방식', self.schema_mode)
        advanced_form.addRow(
            label(
                '새 OpenAI·호환 연결은 JSON 요청 방식을 기본으로 사용합니다. 엄격한 서버 JSON Schema는 지원하는 스키마 범위가 제한되어 있습니다. 저장한 명시 선택은 유지하며 자동 전환하지 않습니다.',
                'muted',
            )
        )
        self.option_fields = {}
        for key, title, low, high, default in [
            ('context_budget', '앱 문맥 예산 (토큰)', 1024, 131072, 8192),
            ('max_output_tokens', '출력 예산 (토큰)', 16, 16384, 2048),
            ('timeout_seconds', '응답 대기 (초)', 1, 900, 300),
            ('max_requests', '작업당 최대 추론 요청', 1, 5, 1),
            ('max_response_bytes', '최대 응답 크기 (bytes)', 1024, 16777216, 1048576),
        ]:
            spin = QDoubleSpinBox() if key == 'timeout_seconds' else QSpinBox()
            spin.setRange(low, high)
            spin.setValue(default)
            self.option_fields[key] = spin
            advanced_form.addRow(title, spin)
        temperature = QDoubleSpinBox()
        temperature.setRange(0, 2)
        temperature.setSingleStep(0.1)
        temperature.setValue(0.1)
        self.option_fields['temperature'] = temperature
        advanced_form.addRow('Temperature · 지원 경로만 적용', temperature)
        advanced_form.addRow(
            label(
                'ChatGPT plan usage는 서버 출력 토큰 상한을 받지 않습니다. 이 경로에서는 앱의 시간·수신량·호출 횟수 제한이 적용됩니다.',
                'muted',
            )
        )
        self.advanced.hide()
        layout.addWidget(self.advanced)
        save_row = QHBoxLayout()
        self.role_combo = QComboBox()
        for role, title in [('analysis', '분석')]:
            self.role_combo.addItem(title, role)
        self.role_combo.setAccessibleName('AI 연결을 사용할 역할')
        save_row.addWidget(self.role_combo)
        self.save_button = QPushButton('연결 설정 저장')
        self.use_button = QPushButton('분석 AI로 사용')
        self.cancel_button = QPushButton('현재 연결 작업 중단')
        self.cancel_button.setEnabled(False)
        save_row.addWidget(self.save_button)
        save_row.addWidget(self.use_button)
        save_row.addWidget(self.cancel_button)
        layout.addLayout(save_row)
        self.status = label('사용할 AI를 연결하면 모델과 기능을 확인할 수 있습니다.', 'muted')
        layout.addWidget(self.status)
        layout.addStretch()
        scroll.setWidget(content)
        outer.addWidget(scroll)
        scroll.hide()
        outer.addStretch()
        self.setup_scroll.setWidget(setup_content)

        self.quick_button.clicked.connect(self.quick_connect)
        self.quick_cancel.clicked.connect(self.cancel_operation)
        self.details_toggle.toggled.connect(scroll.setVisible)

        self.profile_combo.currentIndexChanged.connect(self._profile_selected)
        self.provider_combo.currentIndexChanged.connect(self._provider_selected)
        self.auth_combo.currentIndexChanged.connect(self._auth_selected)
        self.new_button.clicked.connect(self.new_profile)
        self.delete_button.clicked.connect(self.delete_profile)
        self.key_save.clicked.connect(self.save_key)
        self.key_delete.clicked.connect(self.delete_key)
        self.save_button.clicked.connect(self.save_current)
        self.use_button.clicked.connect(self.use_current)
        self.models_button.clicked.connect(self.refresh_models)
        self.connection_button.clicked.connect(self.check_connection)
        self.test_button.clicked.connect(self.test_capabilities)
        self.login_button.clicked.connect(self.login)
        self.logout_button.clicked.connect(self.logout)
        self.account_button.clicked.connect(self.refresh_account)
        self.cancel_button.clicked.connect(self.cancel_operation)
        self.advanced_toggle.toggled.connect(self.advanced.setVisible)
        self.model_combo.currentTextChanged.connect(self._invalidate)
        self.endpoint.textChanged.connect(self._endpoint_edited)
        self.oauth_client_id.textEdited.connect(self._invalidate)
        self.schema_mode.currentIndexChanged.connect(self._invalidate)
        for field in (
            self.name,
            self.oauth_client_id,
            self.oauth_scopes,
            self.google_project_id,
            self.workspace_id,
            self.api_version,
        ):
            field.textChanged.connect(self._invalidate)
        for field in self.option_fields.values():
            field.valueChanged.connect(self._invalidate)
        self.enabled_check.toggled.connect(self._update_actions)
        self.enabled_check.toggled.connect(self._invalidate)
        self.external_consent.toggled.connect(self._update_actions)
        self.external_consent.toggled.connect(self._invalidate)
        for checkbox in self.evidence_checks.values():
            checkbox.toggled.connect(self._update_actions)
            checkbox.toggled.connect(self._invalidate)

    def _build_gemini_card(self, outer):
        self.gemini_card = QGroupBox('기본 · Gemini 무료로 시작하기')
        layout = QVBoxLayout(self.gemini_card)
        self.gemini_summary = label(
            'Google AI Studio에서 무료 프로젝트의 API 키를 만들고 연결하세요.'
        )
        layout.addWidget(self.gemini_summary)
        help_row = QHBoxLayout()
        self.gemini_key_help = QPushButton('AI Studio에서 API 키 만들기')
        self.gemini_key_help.setAccessibleName('Gemini 공식 API 키 만들기 페이지 열기')
        self.gemini_help_toggle = QPushButton('키 만드는 순서 보기')
        self.gemini_help_toggle.setCheckable(True)
        help_row.addWidget(self.gemini_key_help)
        help_row.addWidget(self.gemini_help_toggle)
        help_row.addStretch()
        layout.addLayout(help_row)
        self.gemini_help = label(
            '1. AI Studio에 Google 계정으로 로그인한 뒤 Create API key를 누르세요.\n'
            '2. Create a new project로 새 프로젝트를 만들거나 사용할 기존 프로젝트를 선택하세요. '
            '프로젝트 인증을 요청하면 안내에 따라 완료하세요.\n'
            '3. 프로젝트의 요금 등급이 Free tier이고 결제가 연결되지 않았는지 확인하세요. '
            '유료 업그레이드는 필요하지 않습니다.\n'
            '4. 만든 API 키를 아래 보안 입력란에 붙여 넣고 두 확인란을 선택한 뒤 연결하세요. '
            '키를 채팅·소스 코드·배포 파일에 넣지 마세요.',
            'muted',
        )
        self.gemini_help.hide()
        layout.addWidget(self.gemini_help)
        self.gemini_api_key = QLineEdit()
        self.gemini_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.gemini_api_key.setAccessibleName('Gemini 개인 API 키 보안 입력')
        self.gemini_api_key.setPlaceholderText('내 무료 프로젝트의 API 키 · 이 PC의 DPAPI 보안 저장소에 저장')
        layout.addWidget(self.gemini_api_key)
        self.gemini_free_confirm = QCheckBox(
            '이 키의 프로젝트가 Free tier이고 결제가 연결되지 않았음을 확인했습니다.'
        )
        self.gemini_evidence_consent = QCheckBox(
            '비교 입력·실행 결과·선택한 함수와 디컴파일 근거를 Gemini로 보내는 데 동의합니다.'
        )
        layout.addWidget(self.gemini_free_confirm)
        layout.addWidget(self.gemini_evidence_consent)
        layout.addWidget(label(
            '무료 한도는 모델·계정·프로젝트에 따라 달라집니다. 앱이 요금 등급을 자동 검증하지는 않습니다. '
            '연결 기능 확인은 짧은 요청 최대 4회이며 비교 작업에는 별도 요청 예산이 적용됩니다. '
            '무료 등급의 전송 내용은 Google 제품 개선에 사용될 수 있습니다.', 'muted'
        ))
        actions = QHBoxLayout()
        self.gemini_connect_button = QPushButton('Gemini 키 보안 저장하고 응답 확인')
        self.gemini_connect_button.setObjectName('primary')
        self.gemini_cancel_button = QPushButton('Gemini 연결 중단')
        self.gemini_cancel_button.setEnabled(False)
        actions.addWidget(self.gemini_connect_button, 1)
        actions.addWidget(self.gemini_cancel_button)
        layout.addLayout(actions)
        self.gemini_progress = label(
            '키는 현재 Windows 사용자의 DPAPI로 보호합니다. 확인된 응답 이후에만 기본 AI로 선택합니다.',
            'muted',
        )
        layout.addWidget(self.gemini_progress)
        self.gemini_key_help.clicked.connect(self.open_gemini_key_page)
        self.gemini_api_key.textEdited.connect(
            lambda _: self.gemini_free_confirm.setChecked(False)
        )
        self.gemini_help_toggle.toggled.connect(self.gemini_help.setVisible)
        self.gemini_connect_button.clicked.connect(self.connect_gemini)
        self.gemini_cancel_button.clicked.connect(self.cancel_operation)
        outer.addWidget(self.gemini_card)

    def _gemini_message(self, text):
        self.gemini_progress.setText(text)
        self.status.setText(text)
        self.notice.emit(text)

    def open_gemini_key_page(self, *_):
        if not QDesktopServices.openUrl(QUrl('https://gemini.google.com/app')):
            self._gemini_message(
                'Gemini 페이지를 열지 못했습니다. 기본 브라우저에서 https://gemini.google.com/app 을 여세요.'
            )

    def open_gemini_setup(self, *_):
        """키 입력 안내만 열며 프로필·인증·원격 요청은 변경하지 않는다."""
        if self._operation_active or self._external_busy or self._is_busy():
            self._gemini_message('현재 작업을 완료하거나 중단한 뒤 Gemini 키를 연결하세요.')
            return False
        self.gemini_card.show()
        self.gemini_api_key.setFocus(Qt.FocusReason.OtherFocusReason)
        self.setup_scroll.ensureWidgetVisible(self.gemini_api_key)
        return True

    def connect_gemini(self, *_):
        secret = self.gemini_api_key.text().strip()
        self.gemini_api_key.clear()
        try:
            if self._blocked():
                return False
            if not self.gemini_free_confirm.isChecked():
                self._gemini_message(
                    '먼저 AI Studio에서 이 키의 프로젝트가 Free tier이고 결제가 연결되지 않았는지 확인해 주세요.'
                )
                return False
            if not self.gemini_evidence_consent.isChecked():
                self._gemini_message('Gemini로 보낼 비교 근거의 범위를 확인하고 전송에 동의해 주세요.')
                return False
            draft_selected = bool(
                self._draft and self._draft['profile_id'] == self._current_id
            )
            if draft_selected:
                from ai_providers.contracts import ProviderProfile

                selected = ProviderProfile.model_validate(self.profile_values())
            else:
                selected = self.manager.get_profile(self._current_id) if self._current_id else None
            existing = (
                selected if selected is not None and selected.provider == 'gemini'
                and selected.auth_mode == 'api_key' else None
            )
            if not secret and not (existing and existing.credential_ref):
                self._gemini_message('Gemini 개인 API 키를 보안 입력란에 입력해 주세요. 다른 연결의 키는 사용하지 않습니다.')
                return False
            from .gemini_setup import GeminiSetup

            setup = GeminiSetup(self.manager)
            if existing and draft_selected:
                existing = self._save()
            profile = setup.prepare_profile(
                existing.profile_id if existing else None, consent=True
            )
            if secret:
                profile = self.manager.save_secret(profile.profile_id, secret)
            self.reload_profiles(profile.profile_id)
            self.settings_refreshed.emit()
        except Exception as exc:
            self.settings_refreshed.emit()
            self._safe_failure(exc, provider='gemini')
            return False
        finally:
            secret = ''

        def done(result):
            if result.get('ready') is not True:
                self._show_failure('setup_inference', provider='gemini')
                return
            self.reload_profiles(result['profile']['profile_id'])
            self.profile_changed.emit(self.manager.get_profile(result['profile']['profile_id']))
            self.capabilities_checked.emit(result['capabilities'])
            self.gemini_summary.setText('연결 완료 · Gemini · ' + result['model'])
            self._gemini_message(
                'Gemini의 실제 응답을 확인하고 기본 분석 AI로 저장했습니다. '
                '무료 프로젝트의 한도·요금 등급은 AI Studio에서 계속 확인해 주세요.'
            )
            self.setup_ready.emit(result)

        return self._start(
            'Gemini 무료 API 연결',
            lambda pid, cancel, emit: setup.connect(pid, cancel=cancel, on_event=emit),
            done,
            failure_provider='gemini',
        )

    def _message(self, text):
        self.status.setText(text)
        self.quick_progress.setText(text)
        self.notice.emit(text)

    def _show_failure(self, code, *, provider=None):
        if provider == 'gemini' or (provider is None and self.provider_combo.currentData() == 'gemini'):
            from .gemini_ui import gemini_error_message

            text = gemini_error_message(code)
            self.gemini_progress.setText(text)
            self.status.setText(text + ' (진단 코드: ' + code + ')')
            self.notice.emit(text)
            return
        from .setup_flow import ai_error_message

        text = ai_error_message(code)
        self.quick_progress.setText(text)
        self.status.setText(text + ' (진단 코드: ' + code + ')')
        self.notice.emit(text)

    def _safe_failure(self, exc, *, provider=None):
        # 원시 예외 문자열은 키·토큰·URL을 포함할 수 있어 UI로 전달하지 않는다.
        code = getattr(exc, 'code', '')
        if not isinstance(code, str) or not re.fullmatch(r'[A-Za-z0-9_]{1,80}', code):
            code = 'operation_failed'
        self._show_failure(code, provider=provider)

    def show_setup_progress(self, event):
        if event.get('kind') == 'gemini_setup' and self._operation_active:
            messages = {
                'models': '1/3 · Gemini 무료 후보 모델을 이 프로젝트에서 사용할 수 있는지 확인하고 있습니다.',
                'response': '2/3 · Gemini의 실제 응답과 구조화 출력 기능을 확인하고 있습니다.',
                'activate': '3/3 · 검증된 Gemini를 기본 분석 AI로 저장하고 있습니다.',
            }
            self._gemini_message(messages.get(event.get('step'), 'Gemini 연결을 확인하고 있습니다.'))
            return
        if event.get('kind') != 'quick_setup' or not self._operation_active:
            return
        from .setup_flow import activity_message

        self._message(activity_message(event) or '연결을 준비하고 있습니다.')

    def quick_connect(self, *_):
        if self._blocked():
            return False
        from .setup_flow import ChatGPTSetup

        setup = ChatGPTSetup(self.manager)
        try:
            if self._draft and self._draft['profile_id'] == self._current_id:
                self._save()
            profile = setup.prepare_profile(self._current_id or None)
            self.profile_changed.emit(profile)
            self.reload_profiles(profile.profile_id)
        except Exception as exc:
            self._safe_failure(exc)
            return False

        def done(result):
            self.reload_profiles(result['profile']['profile_id'])
            self.profile_changed.emit(self.manager.get_profile(result['profile']['profile_id']))
            self._show_account(result['account'])
            self.capabilities_checked.emit(result['capabilities'])
            model_name = 'GPT-6.1 Sol' if result['model'] == RECOMMENDED_MODEL else result['model']
            self.quick_summary.setText('연결 완료 · ChatGPT · ' + model_name)
            self._message(
                'GPT가 기본 AI로 저장됐습니다. 모델과 권한을 다시 선택하지 않아도 됩니다.'
            )
            self.setup_ready.emit(result)

        return self._start(
            'ChatGPT 자동 연결',
            lambda pid, cancel, emit: setup.connect(pid, cancel=cancel, on_event=emit),
            done,
        )

    def reload_profiles(self, profile_id=None):
        try:
            profiles = self.manager.list_profiles()
            target = profile_id or self.manager.selected_profile_id()
            self._loading = True
            self.profile_combo.clear()
            for profile in profiles:
                data = value_dict(profile)
                self.profile_combo.addItem(data['name'], data['profile_id'])
            index = self.profile_combo.findData(target)
            self.profile_combo.setCurrentIndex(index if index >= 0 else 0)
        except Exception as exc:
            self._safe_failure(exc)
        finally:
            self._loading = False
        self._profile_selected()

    def _profile_selected(self, *_):
        if self._loading:
            return
        profile_id = self.profile_combo.currentData()
        if not profile_id:
            return
        try:
            if self._draft and self._draft['profile_id'] == profile_id:
                data = copy.deepcopy(self._draft)
            else:
                data = value_dict(self.manager.get_profile(profile_id))
            self._show_profile(data)
        except Exception as exc:
            self._safe_failure(exc)

    def _show_profile(self, data):
        self._loading = True
        self._generation += 1
        if self._current_id != data['profile_id']:
            self.gemini_free_confirm.setChecked(False)
            self.gemini_api_key.clear()
        self._base = copy.deepcopy(data)
        self._current_id = data['profile_id']
        self.gemini_summary.setText(
            '연결 대상: ' + data.get('name', 'Gemini') + ' · ' + data.get('model', '')
            if data.get('provider') == 'gemini' else
            'Google AI Studio에서 무료 프로젝트의 개인 키를 만들어 새 Gemini 연결을 시작하세요.'
        )
        self.api_key.clear()
        self.oauth_client_secret.clear()
        self.name.setText(data.get('name', ''))
        self.provider_combo.setCurrentIndex(
            self.provider_combo.findData(data.get('provider', 'ollama'))
        )
        self._set_auth_options(data.get('auth_mode'))
        self.endpoint.setText(data.get('endpoint', ''))
        self.model_combo.clear()
        if data.get('model'):
            self.model_combo.addItem(data['model'], data['model'])
        self.model_combo.setEditText(data.get('model', ''))
        self.enabled_check.setChecked(bool(data.get('enabled')))
        self.external_consent.setChecked(bool(data.get('external_consent')))
        for kind, checkbox in self.evidence_checks.items():
            checkbox.setChecked(kind in data.get('allowed_evidence', ()))
        for key in ('oauth_client_id', 'google_project_id', 'workspace_id', 'api_version'):
            getattr(self, key).setText(data.get(key, ''))
        self.oauth_scopes.setText(' '.join(data.get('oauth_scopes', ())))
        self.schema_mode.setCurrentIndex(
            max(0, self.schema_mode.findData(data.get('structured_mode', 'native_schema')))
        )
        options = data.get('options') or {}
        if hasattr(options, 'model_dump'):
            options = options.model_dump()
        for key, field in self.option_fields.items():
            if options.get(key) is not None:
                field.setValue(options[key])
        self.key_status.setText(
            '보안 저장소에 키가 있습니다' if data.get('credential_ref') else '저장된 키 없음'
        )
        self.capability_summary.setText('기본 응답 · 구조화 출력 · 도구 호출: 아직 확인 전')
        self._loading = False
        self._refresh_auth_visibility()
        self._show_account()
        self._update_actions()
        try:
            cached = getattr(self.manager, 'cached_capabilities', lambda pid: None)(
                self._current_id
            )
            from ai_providers.contracts import ProviderProfile

            if (
                cached
                and cached.get('profile_fingerprint')
                == ProviderProfile.model_validate(data).fingerprint()
            ):
                self._show_capabilities(cached)
        except Exception:
            # Failure to read a protected result leaves the connection unverified.
            pass
        if data.get('auth_mode') == 'chatgpt' and data.get('credential_ref'):
            self.quick_summary.setText(
                'ChatGPT 계정 연결됨 · ' + (data.get('model') or '추천 모델을 자동 선택합니다.')
            )
        if data.get('provider') == 'gemini' and data.get('credential_ref'):
            self.gemini_summary.setText(
                'Gemini 키 보안 저장됨 · ' + (data.get('model') or '무료 후보 모델 확인 필요')
            )

    def _set_auth_options(self, auth_mode=None):
        self.auth_combo.blockSignals(True)
        self.auth_combo.clear()
        for key, title in AUTH.get(self.provider_combo.currentData(), []):
            self.auth_combo.addItem(title, key)
        index = self.auth_combo.findData(auth_mode)
        self.auth_combo.setCurrentIndex(index if index >= 0 else 0)
        self.auth_combo.blockSignals(False)

    def _provider_selected(self, *_):
        if self._loading:
            return
        self._set_auth_options()
        self.endpoint.setText(ENDPOINTS.get(self.provider_combo.currentData(), ''))
        if self._draft is not None and self.provider_combo.currentData() in (
            'openai',
            'compatible',
        ):
            self.schema_mode.setCurrentIndex(self.schema_mode.findData('json_prompt'))
        self.api_key.clear()
        self.oauth_client_secret.clear()
        self.external_consent.setChecked(False)
        for checkbox in self.evidence_checks.values():
            checkbox.setChecked(False)
        local = self.provider_combo.currentData() == 'ollama'
        self.enabled_check.setChecked(local)
        self.model_combo.setEditText('')
        self._invalidate()
        self._refresh_auth_visibility()

    def _auth_selected(self, *_):
        if self._loading:
            return
        self.api_key.clear()
        self.oauth_client_secret.clear()
        self._invalidate()
        self._refresh_auth_visibility()

    def _refresh_auth_visibility(self):
        provider, auth = self.provider_combo.currentData(), self.auth_combo.currentData()
        self.key_box.setVisible(auth == 'api_key')
        self.oauth_box.setVisible(auth in ('chatgpt', 'google_oauth'))
        self.google_fields.setVisible(auth == 'google_oauth')
        self.remote_box.setVisible(provider != 'ollama')
        self.test_button.setText(
            '추론 기능 검사 · 로컬 모델'
            if provider == 'ollama'
            else '추론 기능 검사 · 비용 발생 가능'
        )
        if provider == 'anthropic':
            note = 'Claude Console API 키로 연결합니다. Claude.ai 구독 계정 로그인은 이 앱에서 지원하지 않습니다.'
        elif provider == 'ollama':
            note = '내 PC의 기존 Ollama와 설치 모델을 사용합니다. 서버가 없어도 다른 제공자 설정을 열 수 있습니다.'
        else:
            note = '시험 기능 · 실제 인증·추론은 아직 확인 전입니다. 모델 이름만으로 기능을 보장하지 않습니다.'
        self.support_note.setText(note)
        self.oauth_help.setText(
            'ChatGPT 공식 동적 앱 등록과 시스템 브라우저 로그인을 사용합니다. 계정 로그인과 plan usage 추론 권한은 별도로 확인합니다.'
            if auth == 'chatgpt'
            else '자체 Google Cloud 프로젝트·API 활성화·Desktop OAuth 앱 등록·허용 scope가 필요합니다. Google AI Pro/Ultra 구독과 API 할당량은 별개입니다.'
        )
        self._update_actions()

    def _invalidate(self, *_):
        if self._loading:
            return
        self._generation += 1
        self.capability_summary.setText('선택이 바뀌었습니다. 현재 모델의 기능을 다시 확인하세요.')
        self._refresh_auth_visibility()

    def _show_capabilities(self, result):
        labels = []
        for key, alias, title in (
            ('basic', 'basic', '기본 응답'),
            ('json_schema', 'structured_output', '구조화 출력'),
            ('tools', 'tool_calling', '도구 호출'),
        ):
            labels.append(
                title + (' 가능' if supported(result.get(key, result.get(alias))) else ' 확인 필요')
            )
        self.capability_summary.setText(' · '.join(labels))
        if (
            result.get('actual_inference_verified') is True
            and result.get('proof_persisted') is True
        ):
            self.support_note.setText(
                '현재 연결의 실제 응답 확인됨 · 저장된 기능 검사 결과를 사용합니다.'
            )

    def _endpoint_edited(self, *_):
        if self._loading:
            return
        self.external_consent.setChecked(False)
        self._invalidate()

    def _show_account(self, result=None):
        if result is None:
            self.account_label.setText('계정 상태 확인 전 · 계정 상태 확인을 눌러 조회하세요')
            return
        try:
            if not isinstance(result, dict):
                result = value_dict(result)
            authenticated = result.get('authenticated') is True or result.get('logged_in') is True
            account = result.get('account') or result.get('email') or result.get('account_id') or ''
            if isinstance(account, dict):
                account = (
                    account.get('email') or account.get('name') or account.get('account_id') or ''
                )
            text = (
                '활성 계정: ' + (str(account) if account else '로그인됨')
                if authenticated
                else '연결된 계정 없음'
            )
            text += ' · 추론 권한 ' + (
                '확인됨' if result.get('inference_authorized') is True else '미확인'
            )
            self.account_label.setText(text)
        except Exception:
            self.account_label.setText('계정 상태 확인 전 · 등록 정보와 로그인 상태를 확인하세요')

    def _update_actions(self, *_):
        busy = self._operation_active or self._external_busy
        provider = self.provider_combo.currentData()
        remote_ready = (
            self.enabled_check.isChecked()
            and self.external_consent.isChecked()
            and any(check.isChecked() for check in self.evidence_checks.values())
        )
        for button in (
            self.connection_button,
            self.models_button,
            self.login_button,
            self.logout_button,
            self.account_button,
            self.save_button,
            self.use_button,
            self.new_button,
            self.key_save,
            self.key_delete,
        ):
            button.setEnabled(not busy)
        self.delete_button.setEnabled(not busy and self._current_id != 'legacy-local')
        self.test_button.setEnabled(not busy and (provider == 'ollama' or remote_ready))
        self.cancel_button.setEnabled(self._operation_active)
        self.quick_button.setEnabled(not busy)
        self.quick_cancel.setEnabled(self._operation_active)
        for control in (
            self.gemini_connect_button, self.gemini_api_key, self.gemini_free_confirm,
            self.gemini_evidence_consent, self.gemini_key_help, self.gemini_help_toggle,
        ):
            control.setEnabled(not busy)
        self.gemini_cancel_button.setEnabled(self._operation_active)
        self.details_toggle.setEnabled(not busy)

    def set_busy(self, busy):
        """root의 공용 분석/벤치마크 작업 슬롯과 연결한다."""
        self._external_busy = bool(busy)
        self._update_actions()

    def _blocked(self):
        if self._operation_active or self._external_busy or self._is_busy():
            self._message('현재 작업을 완료하거나 중단한 뒤 연결 작업을 실행하세요.')
            return True
        if self._prepare_change:
            try:
                self._prepare_change()
            except Exception as exc:
                self._safe_failure(exc)
                return True
        return False

    def profile_values(self):
        data = copy.deepcopy(self._base)
        data.update(
            profile_id=self._current_id,
            name=self.name.text().strip(),
            provider=self.provider_combo.currentData(),
            auth_mode=self.auth_combo.currentData(),
            endpoint=self.endpoint.text().strip(),
            model=self.model_combo.currentText().strip(),
            enabled=self.enabled_check.isChecked(),
            experimental=(
                self._base.get('experimental', True)
                if self.provider_combo.currentData() == self._base.get('provider')
                else self.provider_combo.currentData() != 'ollama'
            ),
            external_consent=self.external_consent.isChecked(),
            allowed_evidence=tuple(
                kind for kind, checkbox in self.evidence_checks.items() if checkbox.isChecked()
            ),
            options={key: field.value() for key, field in self.option_fields.items()},
            oauth_client_id=self.oauth_client_id.text().strip(),
            oauth_scopes=tuple(self.oauth_scopes.text().split()),
            google_project_id=self.google_project_id.text().strip(),
            workspace_id=self.workspace_id.text().strip(),
            api_version=self.api_version.text().strip(),
        )
        data['structured_mode'] = self.schema_mode.currentData()
        return data

    def _save(self):
        profile = self.manager.save_profile(self.profile_values())
        self._base = value_dict(profile)
        self._draft = None
        self.profile_changed.emit(profile)
        index = self.profile_combo.findData(self._current_id)
        if index >= 0:
            self.profile_combo.setItemText(index, self._base['name'])
        return profile

    def save_current(self, *_):
        if self._blocked():
            return None
        try:
            profile = self._save()
            self._message(
                '연결 설정과 전송 범위를 저장했습니다. 실제 추론 검사는 별도로 실행하세요.'
            )
            return profile
        except Exception as exc:
            self._safe_failure(exc)
            return None

    def use_current(self, *_):
        if self._blocked():
            return
        try:
            self._save()
            profile = self.manager.select_profile(
                self._current_id, role=self.role_combo.currentData()
            )
            self.profile_changed.emit(profile)
            self._message(
                '현재 프로필을 ' + self.role_combo.currentText() + ' 역할에 선택했습니다.'
            )
        except Exception as exc:
            self._safe_failure(exc)

    def new_profile(self, *_):
        if self._blocked():
            return
        from .gemini_setup import new_free_profile

        self._draft = value_dict(new_free_profile())
        self.profile_combo.addItem('Gemini · 무료 기본 · 저장 전', self._draft['profile_id'])
        self.profile_combo.setCurrentIndex(self.profile_combo.count() - 1)

    def delete_profile(self, *_):
        if self._blocked() or self._current_id == 'legacy-local':
            return
        try:
            if self._draft and self._draft['profile_id'] == self._current_id:
                self._draft = None
            else:
                self.manager.delete_profile(self._current_id)
            self.reload_profiles()
            self.profile_changed.emit(None)
            self._message('연결 프로필을 삭제했습니다.')
        except Exception as exc:
            self._safe_failure(exc)

    def save_key(self, *_):
        if self._blocked():
            return
        secret = self.api_key.text()
        try:
            if self.auth_combo.currentData() != 'api_key' or not secret.strip():
                self._message('현재 연결 방법에서 사용할 API 키를 보안 입력란에 입력하세요.')
                return
            self._save()
            profile = self.manager.save_secret(self._current_id, secret)
            self._base = value_dict(profile)
            self._invalidate()
            self.key_status.setText('보안 저장소에 키를 저장했습니다')
            self.profile_changed.emit(profile)
            self._message('키를 보안 저장했습니다. 연결과 실제 추론 기능은 별도로 확인하세요.')
        except Exception as exc:
            self._safe_failure(exc)
        finally:
            self.api_key.clear()
            secret = ''

    def delete_key(self, *_):
        if self._blocked():
            return
        try:
            profile = self.manager.delete_secret(self._current_id)
            self._base = value_dict(profile)
            self._invalidate()
            self.api_key.clear()
            self.key_status.setText('저장된 키 없음')
            self.profile_changed.emit(profile)
            self._message('이 프로필의 저장한 키를 삭제했습니다.')
        except Exception as exc:
            self._safe_failure(exc)

    def _start(self, title, operation, complete, *, failure_provider=None):
        if self._blocked():
            return False
        try:
            self._save()
        except Exception as exc:
            self._safe_failure(exc)
            return False
        generation, profile_id = self._generation, self._current_id
        self._operation_active = True
        self._cancel_pending = False
        self._update_actions()
        self.busy_changed.emit(True)

        async def work(cancel, emit):
            self._active_cancel = cancel
            if self._cancel_pending:
                cancel.set()
            try:
                return {'ok': True, 'result': await operation(profile_id, cancel, emit)}
            except Exception as exc:
                code = getattr(exc, 'code', '')
                if not isinstance(code, str) or not re.fullmatch(r'[A-Za-z0-9_]{1,80}', code):
                    code = 'operation_failed'
                return {'ok': False, 'code': code}
            finally:
                self._active_cancel = None

        def done(envelope):
            self._operation_active = False
            self._update_actions()
            self.busy_changed.emit(False)
            stale = generation != self._generation or profile_id != self._current_id
            # 취소·실패 전 앱 등록 ID 등 일부 안전한 설정이 이미 commit될 수 있다.
            self.settings_refreshed.emit()
            if stale:
                self._message('선택이 바뀌어 이전 연결의 응답을 적용하지 않았습니다.')
                return
            if not envelope.get('ok'):
                self._show_failure(envelope.get('code', 'operation_failed'), provider=failure_provider)
                return
            try:
                complete(envelope['result'])
            except Exception as exc:
                self._safe_failure(exc)

        if self._launcher:
            if self._launcher(title, work, done) is False:
                self._operation_active = False
                self._update_actions()
                self.busy_changed.emit(False)
                return False
        else:
            self._worker = ProviderWorker(work, self)
            self._worker.progress.connect(
                self.show_setup_progress, Qt.ConnectionType.QueuedConnection
            )
            self._workers.add(self._worker)
            self._worker_completions[self._worker] = done
            self._worker.finished.connect(
                self._standalone_finished, Qt.ConnectionType.QueuedConnection
            )
            self._worker.start()
        return True

    @Slot()
    def _standalone_finished(self):
        worker = self.sender()
        if worker not in self._worker_completions:
            return
        # finished 신호 뒤에도 native TLS 정리가 남을 수 있다. wait가 끝난
        # 뒤에만 GUI 작업 슬롯과 스레드 참조를 해제한다.
        worker.wait()
        envelope = worker.result
        # work closes over this QWidget. Keeping it on a finished child QThread
        # forms a Python cycle that a later worker's GC may destroy off the GUI
        # thread. Release all completed callbacks/results here, never in run().
        worker.work = None
        worker.result = None
        worker.finished.disconnect(self._standalone_finished)
        done = self._worker_completions.pop(worker)
        self._workers.discard(worker)
        done(envelope)

    def check_connection(self, *_):
        def done(result):
            connected = result.get('connected') is True or result.get('ready') is True
            self._message(
                '연결 확인 완료 · 추론은 실행하지 않았습니다.'
                if connected
                else '연결 확인 필요 · 주소와 인증 상태를 확인하세요.'
            )

        return self._start(
            'AI 연결 확인',
            lambda pid, cancel, emit: self.manager.probe_connection(
                pid, cancel=cancel, on_event=emit
            ),
            done,
        )

    def refresh_models(self, *_):
        def done(models):
            current = self.model_combo.currentText()
            self._loading = True
            self.model_combo.clear()
            for model in models:
                data = value_dict(model)
                model_id = data.get('id', '')
                if model_id:
                    self.model_combo.addItem(model_id, model_id)
            if current and self.model_combo.findData(current) < 0:
                self.model_combo.addItem(current, current)
            self.model_combo.setEditText(current)
            self._loading = False
            self._message(
                '모델 목록을 조회했습니다. 기존 선택을 보존했으며 ID를 직접 입력할 수도 있습니다.'
            )

        return self._start(
            'AI 모델 목록 조회',
            lambda pid, cancel, emit: self.manager.list_models(pid, cancel=cancel, on_event=emit),
            done,
        )

    def test_capabilities(self, *_):
        if self.provider_combo.currentData() != 'ollama' and not (
            self.enabled_check.isChecked()
            and self.external_consent.isChecked()
            and any(check.isChecked() for check in self.evidence_checks.values())
        ):
            self._message(
                '원격 추론 검사는 연결 사용 허용과 전송할 근거 범위 동의 후 직접 실행하세요. 비용이 발생할 수 있습니다.'
            )
            return False

        def done(result):
            self._show_capabilities(result)
            self.capabilities_checked.emit(result)
            self._message(
                '응답 검사는 끝났지만 보호된 검사 결과를 저장하지 못했습니다. 기능 사용 전에 저장소 상태를 확인하고 다시 검사하세요.'
                if result.get('proof_status') == 'partial'
                else '추론 기능 검사를 마쳤습니다. 근거 인용 검증과 실제 분석 성공은 별도로 확인합니다.'
            )

        return self._start(
            'AI 추론 기능 검사',
            lambda pid, cancel, emit: self.manager.probe_capabilities(
                pid, cancel=cancel, on_event=emit
            ),
            done,
        )

    def login(self, *_):
        auth = self.auth_combo.currentData()
        if auth == 'chatgpt':
            return self.quick_connect()
        if auth not in ('chatgpt', 'google_oauth'):
            return False
        options = {
            'client_id': self.oauth_client_id.text().strip(),
            'client_secret': self.oauth_client_secret.text(),
            'scopes': self.oauth_scopes.text().split(),
            'google_project_id': self.google_project_id.text().strip(),
        }
        self.oauth_client_secret.clear()
        if auth == 'google_oauth' and not options['client_id']:
            options['client_secret'] = ''
            self._message('Gemini 로그인을 위해 등록한 Desktop OAuth client ID를 입력하세요.')
            return False

        def done(result):
            self._show_account(result)
            self._invalidate()
            try:
                profile = self.manager.get_profile(self._current_id)
                self._base = value_dict(profile)
                self.profile_changed.emit(profile)
            except Exception:
                pass
            self._message(
                '로그인 결과를 확인했습니다. 계정 로그인·추론 권한·실제 추론 성공을 구분해 확인하세요.'
            )

        async def operation(pid, cancel, emit):
            try:
                def on_url_cb(url):
                    self.oauth_url_received.emit(url)
                return await self.manager.login(
                    pid, options, cancel=cancel, on_event=emit, on_url=on_url_cb
                )
            finally:
                options.clear()

        started = self._start('공식 브라우저 로그인', operation, done)
        if not started:
            options.clear()
        return started

    @Slot(str)
    def _show_web_oauth_dialog(self, url):
        from .web_login import WebEngineLoginDialog
        dialog = WebEngineLoginDialog(initial_url=url, parent=self.window())
        dialog.exec()

    def open_web_login(self, *_):
        from .web_login import WebEngineLoginDialog
        prov = self.provider_combo.currentData() or 'gemini'

        def on_completed(result):
            active_id = self.manager.settings.active_ai_profile
            self.reload_profiles(active_id)
            profile = self.manager.get_profile(active_id)
            self._base = value_dict(profile)
            self._invalidate()
            self.profile_changed.emit(profile)
            self._message(f"{result.get('provider', '').upper()} 웹 연결이 완료되었습니다!")

        dialog = WebEngineLoginDialog(
            provider=prov,
            parent=self.window(),
            manager=self.manager,
            on_success=on_completed,
        )
        dialog.exec()

    def refresh_account(self, *_):
        async def operation(pid, cancel, emit):
            return await asyncio.to_thread(self.manager.account_status, pid)

        return self._start('AI 계정 상태 확인', operation, self._show_account)

    def logout(self, *_):
        def done(result):
            if result.get('local_deleted') is not True:
                self._message(
                    '로컬 인증 삭제가 확인되지 않아 로그아웃을 완료하지 못했습니다. 다시 시도하세요.'
                )
                return
            self.api_key.clear()
            self.oauth_client_secret.clear()
            self._invalidate()
            self._show_account()
            revoked = result.get('remote_revoked') is True or result.get('revoked') is True
            self._message(
                '로컬 로그아웃 완료 · 원격 세션 폐기 확인됨'
                if revoked
                else '로컬 로그아웃 완료 · 원격 세션 폐기는 확인되지 않았습니다.'
            )
            try:
                profile = self.manager.get_profile(self._current_id)
                self._base = value_dict(profile)
                self.key_status.setText(
                    '보안 저장소에 키가 있습니다'
                    if self._base.get('credential_ref')
                    else '저장된 키 없음'
                )
                self.profile_changed.emit(profile)
            except Exception:
                pass

        return self._start(
            'AI 계정 로그아웃',
            lambda pid, cancel, emit: self.manager.logout(pid, cancel=cancel, on_event=emit),
            done,
        )

    def cancel_operation(self, *_):
        if self._operation_active:
            self._cancel_pending = True
        if self._active_cancel is not None:
            self._active_cancel.set()
        if self._worker and self._worker.isRunning():
            self._worker.cancel_event.set()
        for worker in self._workers:
            worker.cancel_event.set()
        if self._operation_active:
            self._message('연결 작업 중단을 요청했습니다. 종료 결과를 기다리고 있습니다.')
            self.gemini_progress.setText('연결 작업 중단을 요청했습니다. 종료 결과를 기다리고 있습니다.')

    def closeEvent(self, event):
        self.cancel_operation()
        if self._operation_active or self._workers:
            event.ignore()
            return
        super().closeEvent(event)
