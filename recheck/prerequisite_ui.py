"""Explicit tool installation and first-use runtime preparation."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)


class ToolSetupPanel(QWidget):
    def __init__(self, *, launch, open_url, settings=None, on_prepared=None,
                 on_cancel=None, parent=None):
        super().__init__(parent)
        self.launch = launch
        self.open_url = open_url
        self.settings = settings
        self.on_prepared = on_prepared
        self.on_cancel = on_cancel
        self._busy = False
        self._inspection = {}
        layout = QVBoxLayout(self)
        description = QLabel(
            '배포 ZIP의 Recheck.exe에는 실행용 Python이 포함되어 있습니다. '
            'Git과 별도 Python은 소스를 수정할 때만 필요합니다.\n'
            '프로그램 실행 비교에는 Docker Desktop이 필요합니다. '
            'Ghidra·Java와 Ollama는 사용 방식에 따라 선택하세요.'
        )
        description.setWordWrap(True)
        layout.addWidget(description)
        self.summary = QLabel('도구 확인을 눌러 설치 상태를 살펴보세요.')
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.tools = QTreeWidget()
        self.tools.setHeaderLabels(['도구', '상태', '용도', '설치 위치·안내'])
        self.tools.setRootIsDecorated(False)
        self.tools.setColumnWidth(0, 125)
        self.tools.setColumnWidth(1, 90)
        self.tools.setColumnWidth(2, 220)
        self.tools.currentItemChanged.connect(self.selection_changed)
        layout.addWidget(self.tools, 1)
        actions = QHBoxLayout()
        self.refresh_button = QPushButton('도구 확인')
        self.refresh_button.clicked.connect(self.refresh)
        self.install_button = QPushButton('선택한 도구 설치')
        self.install_button.clicked.connect(self.install_selected)
        self.download_button = QPushButton('공식 다운로드 안내')
        self.download_button.clicked.connect(self.download_selected)
        self.winget_button = QPushButton('설치 관리자 받기')
        self.winget_button.clicked.connect(self.download_winget)
        self.winget_button.hide()
        for button in (self.refresh_button, self.install_button, self.download_button,
                       self.winget_button):
            actions.addWidget(button)
        layout.addLayout(actions)
        note = QLabel(
            'Docker를 설치했다면 먼저 실행해 Linux 컨테이너가 준비될 때까지 기다리세요. '
            '아래 준비는 인터넷에서 분석 도구를 받아 이 PC의 Docker 환경을 만듭니다. '
            '처음에는 수 GB의 공간과 시간이 필요합니다.'
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.include_ollama = QCheckBox('로컬 AI 실행 환경도 준비 (ChatGPT만 쓰면 선택하지 않아도 됩니다)')
        layout.addWidget(self.include_ollama)
        self.build_button = QPushButton('분석 도구 내려받아 준비')
        self.build_button.clicked.connect(self.build_environment)
        layout.addWidget(self.build_button)
        self.cancel_button = QPushButton('중단하기')
        self.cancel_button.clicked.connect(self.request_cancel)
        self.cancel_button.setEnabled(False)
        layout.addWidget(self.cancel_button)
        self.selection_changed()

    def confirm(self, title, message):
        return QMessageBox.question(
            self, title, message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) == QMessageBox.StandardButton.Yes

    def selected(self):
        item = self.tools.currentItem()
        return item.data(0, Qt.ItemDataRole.UserRole) if item else None

    def selection_changed(self, *_):
        tool = self.selected() or {}
        self.install_button.setEnabled(
            not self._busy and bool(tool.get('can_install'))
            and tool.get('status') not in ('ready', 'bundled')
        )
        self.download_button.setEnabled(not self._busy and bool(tool.get('download_url')))

    def set_busy(self, busy):
        self._busy = busy
        self.cancel_button.setEnabled(busy)
        for control in (self.refresh_button, self.build_button, self.include_ollama,
                        self.winget_button, self.tools):
            control.setEnabled(not busy)
        self.selection_changed()

    def request_cancel(self, *_):
        if self._busy and self.on_cancel:
            self.on_cancel()
            self.cancel_button.setEnabled(False)
            self.summary.setText('중단을 요청했습니다. 정리될 때까지 기다려 주세요. 이미 적용된 설치는 남을 수 있습니다.')

    def _launch(self, title, work, done):
        if self._busy:
            return
        self.set_busy(True)
        if not self.launch(title, work, done):
            self.set_busy(False)

    def refresh(self, *_):
        from .prerequisites import inspect_prerequisites

        self._launch(
            '기본 도구 확인 중',
            lambda cancel, emit: inspect_prerequisites(cancel=cancel, on_event=emit),
            self.inspection_done,
        )

    def inspection_done(self, result):
        self._inspection = result
        self.tools.clear()
        labels = {'ready': '사용 가능', 'bundled': '포함됨', 'missing': '설치 필요',
                  'unavailable': '확인 필요', 'cancelled': '취소됨'}
        for tool in result.get('tools', []):
            detail = ' · '.join(str(tool.get(key) or '') for key in ('path', 'reason')).strip(' ·')
            row = QTreeWidgetItem([
                tool['name'], labels.get(tool.get('status'), '확인 필요'),
                tool.get('purpose', ''), detail,
            ])
            row.setData(0, Qt.ItemDataRole.UserRole, tool)
            row.setToolTip(3, detail)
            self.tools.addTopLevelItem(row)
        if self.tools.topLevelItemCount():
            self.tools.setCurrentItem(self.tools.topLevelItem(0))
        winget = result.get('winget', {})
        self.winget_button.setVisible(not winget.get('ready', False))
        self.summary.setText(
            '확인을 중단했습니다. 다시 확인할 수 있습니다.'
            if result.get('status') == 'cancelled'
            else '필요한 도구를 선택해 설치하세요. 설치 후 상태를 다시 확인합니다. '
                 '이미 설치된 도구는 PATH에 없어도 알려진 설치 위치에서 찾습니다.'
        )
        self.set_busy(False)

    def install_selected(self, *_):
        from .prerequisites import install_prerequisite

        tool = self.selected()
        if self._busy or not tool or not self.install_button.isEnabled():
            return
        if not self.confirm(
            tool['name'] + ' 설치',
            '공식 WinGet 패키지로 ' + tool['name'] + '을(를) 설치합니다. '
            '설치 관리자가 권한 승인이나 사용 조건 확인을 요청할 수 있으며 '
            'PC 재시작이 필요할 수 있습니다. 계속할까요?',
        ):
            return
        self._launch(
            tool['name'] + ' 설치 중',
            lambda cancel, emit: install_prerequisite(tool['id'], cancel=cancel, on_event=emit),
            self.installation_done,
        )

    def installation_done(self, result):
        updated = result.get('tool')
        if updated:
            tools = [updated if t['id'] == updated['id'] else t
                     for t in self._inspection.get('tools', [])]
            self.inspection_done({**self._inspection, 'tools': tools})
        self.summary.setText(result.get('message') or '설치 결과를 다시 확인해 주세요.')
        self.set_busy(False)

    def download_selected(self, *_):
        tool = self.selected() or {}
        if not self._busy and tool.get('download_url'):
            self.open_url(tool['download_url'])

    def download_winget(self, *_):
        if not self._busy:
            self.open_url(self._inspection.get('winget', {}).get('download_url')
                          or 'https://apps.microsoft.com/detail/9nblggh4nns1')

    def build_environment(self, *_):
        if self._busy or not self.confirm(
            '분석 환경 처음 준비',
            '인터넷에서 도구와 기반 파일을 받아 이 PC의 Docker에 분석 환경을 만듭니다. '
            '수 GB의 여유 공간이 필요하고 처음에는 시간이 걸립니다. '
            '비교할 프로그램은 실행하지 않습니다. 계속할까요?',
        ):
            return
        include_ollama = self.include_ollama.isChecked()
        try:
            if self.settings:
                settings = self.settings()
            else:
                from .models import Settings
                settings = Settings()
        except (ValueError, TypeError):
            self.summary.setText('설정 값을 확인하고 저장한 뒤 분석 환경 준비를 다시 눌러 주세요.')
            return

        async def prepare(cancel, emit):
            from .runtime_source import build_analysis_environment
            return await build_analysis_environment(
                settings, cancel=cancel, on_event=emit, include_ollama=include_ollama)

        self._launch(
            '분석 도구 내려받아 준비 중',
            prepare,
            self.environment_done,
        )

    def environment_done(self, result):
        self.summary.setText(result.get('message') or '분석 환경 준비 결과를 확인해 주세요.')
        self.set_busy(False)
        if result.get('ready') and self.on_prepared:
            self.on_prepared()
