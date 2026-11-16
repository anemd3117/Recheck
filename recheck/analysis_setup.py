"""Read-only analysis readiness and contextual links to the existing installer.

``files`` contains already inspected binary metadata, never paths to inspect on
the GUI thread. No installation, process launch, settings write or target
execution occurs while evaluating readiness or changing the displayed scope.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget


def _requirement(identifier, name, probe, guidance, *, can_configure=False):
    ready = None if probe.get('status') == 'not_requested' else probe.get('ready')
    status = 'ready' if ready is True else 'blocked' if ready is False else 'unknown'
    return dict(id=identifier, name=name, kind='tool', status=status,
                guidance='사용할 준비가 되었습니다.' if status == 'ready' else guidance,
                can_install=status == 'blocked',
                can_configure=can_configure and status == 'blocked')


def analysis_readiness(analysis_mode, environment, *, static_backend='auto', files=()):
    """Return selected-scope requirements, without silently dropping a scope.

    ``status`` is ready/blocked/unknown. Unknown means no applicable probe has
    confirmed availability; it is neither an installation failure nor approval
    to start. ``blockers`` include unsupported inputs separately from tools.
    A caller may start only when ``can_analyze`` is true.
    """
    if analysis_mode not in ('static', 'dynamic', 'both'):
        raise ValueError('지원하지 않는 분석 모드입니다.')
    environment = environment or {}
    requirements = []
    ghidra = environment.get('ghidra') or {}
    backend = ghidra.get('backend') or static_backend
    if analysis_mode in ('static', 'both'):
        if backend == 'rea':
            guidance = ('Docker Desktop을 설치·실행한 뒤 도구 설치·관리에서 '
                        '분석 도구 내려받아 준비를 누르세요. REA 정적 분석 도구가 포함됩니다.')
        elif static_backend == 'direct':
            guidance = ('Ghidra와 설치한 Ghidra 버전에 맞는 JDK를 설치하세요. '
                        '분석 설정에서 Ghidra·JDK 설치 폴더를 선택한 뒤 다시 확인하세요.')
        else:
            guidance = ('도구 설치·관리에서 Docker Desktop 설치·실행 후 분석 도구 내려받아 준비를 '
                        '누르세요. 직접 설치를 쓰려면 Ghidra와 호환 JDK 설치 폴더를 분석 설정에서 선택하세요.')
        requirements.append(_requirement('static', '정적분석 도구', ghidra, guidance,
                                         can_configure=True))
    if analysis_mode in ('dynamic', 'both'):
        docker = environment.get('docker') or {}
        if docker.get('engine_ready') is True and docker.get('ready') is False:
            guidance = ('Docker 엔진은 연결되었습니다. 도구 설치·관리에서 분석 도구 내려받아 준비를 '
                        '눌러 격리 실행 환경을 준비한 뒤 다시 확인하세요.')
        elif docker.get('status') == 'not_installed':
            guidance = ('Docker Desktop을 설치하고 실행하세요. Linux 컨테이너가 준비되면 '
                        '도구 설치·관리에서 분석 도구 내려받아 준비를 누르세요.')
        else:
            guidance = ('Docker Desktop 실행·로컬 연결·Linux 컨테이너 상태를 확인하세요. '
                        '미설치라면 도구 설치·관리에서 설치하고 분석 도구 내려받아 준비를 누르세요.')
        requirement = _requirement('dynamic', '동적분석 격리 실행 환경', docker, guidance,
                                   can_configure=True)
        if docker.get('ready') is True and (
            docker.get('local_endpoint') is False
            or ('os' in docker and docker['os'] != 'linux')
            or ('arch' in docker and docker['arch'] not in ('amd64', 'x86_64'))
        ):
            requirement.update(status='blocked', can_install=False, can_configure=True,
                               guidance='로컬 Linux x64 Docker 환경이 필요합니다. '
                                        'Docker Desktop의 Linux 컨테이너 전환과 분석 설정의 '
                                        '로컬 Docker 컨텍스트를 확인하세요.')
        requirements.append(requirement)

    for index, info in enumerate(files):
        if not isinstance(info, dict):
            continue
        name = str(info.get('name') or f'파일 {index + 1}')
        if analysis_mode in ('static', 'both'):
            if info.get('static_supported') is False:
                requirements.append(dict(
                    id=f'file-{index}-static', name=name, kind='input', status='blocked',
                    guidance=info.get('static_reason') or '이 파일은 정적분석을 지원하지 않습니다.',
                    can_install=False, can_configure=False))
            elif info.get('analysis_kind') == 'managed' and backend == 'direct':
                requirements.append(dict(
                    id=f'file-{index}-rea', name=name, kind='backend', status='blocked',
                    guidance='.NET 정적분석에는 REA가 필요합니다. 분석 설정에서 REA를 선택하고 '
                             '도구 설치·관리에서 분석 도구 내려받아 준비를 누르세요.',
                    can_install=True, can_configure=True))
        if analysis_mode in ('dynamic', 'both'):
            unsupported = info.get('execution_supported') is False
            unverified_windows = (info.get('os') == 'windows'
                                  and info.get('execution_validation') != 'verified')
            if unsupported or unverified_windows:
                reason = (info.get('execution_reason') or '이 파일은 동적분석을 지원하지 않습니다.')
                if unverified_windows and not unsupported:
                    reason = ('Windows 실행 비교는 현재 동봉된 격리 환경에서 지원하지 않습니다. '
                              '설치만으로 지원되는 형식이 되지는 않습니다. 정적분석을 선택할 수 있습니다.')
                requirements.append(dict(
                    id=f'file-{index}-dynamic', name=name, kind='input', status='blocked',
                    guidance=reason, can_install=False, can_configure=False))

    blockers = [row for row in requirements if row['status'] == 'blocked']
    unknown = [row for row in requirements if row['status'] == 'unknown']
    status = 'blocked' if blockers else 'unknown' if unknown else 'ready'
    return dict(analysis_mode=analysis_mode, status=status,
                ready=True if status == 'ready' else False if blockers else None,
                can_analyze=status == 'ready', requirements=requirements,
                blockers=blockers, unknown=unknown)


class AnalysisSetupPanel(QWidget):
    """Mode-specific guidance; callbacks own all asynchronous work and dialogs."""

    def __init__(self, *, check_environment, install_tools, open_settings=None, parent=None):
        super().__init__(parent)
        self._busy = False
        self.state = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.summary = QLabel()
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        self.summary.setWordWrap(True)
        self.summary.setAccessibleName('선택한 분석 모드의 도구 준비 상태')
        layout.addWidget(self.summary)
        self.details = QLabel()
        self.details.setTextFormat(Qt.TextFormat.PlainText)
        self.details.setWordWrap(True)
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.details)
        actions = QHBoxLayout()
        self.check_button = QPushButton('필요 도구 확인')
        self.check_button.clicked.connect(lambda: check_environment())
        self.install_button = QPushButton('도구 설치·준비 안내')
        self.install_button.clicked.connect(lambda: install_tools())
        self.settings_button = QPushButton('분석 도구 설정')
        if open_settings:
            self.settings_button.clicked.connect(lambda: open_settings())
        self.settings_button.setVisible(open_settings is not None)
        self._has_settings = open_settings is not None
        for button in (self.check_button, self.install_button, self.settings_button):
            actions.addWidget(button)
        actions.addStretch()
        layout.addLayout(actions)
        self.update_state('both', {})

    def update_state(self, analysis_mode, environment, *, static_backend='auto', files=()):
        self.state = analysis_readiness(analysis_mode, environment,
                                        static_backend=static_backend, files=files)
        names = {'static': '정적분석', 'dynamic': '동적분석', 'both': '둘 다 분석'}
        statuses = {'ready': '도구 준비 완료', 'blocked': '준비 또는 파일 확인 필요',
                    'unknown': '필요한 도구를 확인해 주세요'}
        self.summary.setText(f"{names[analysis_mode]} · {statuses[self.state['status']]}")
        labels = {'ready': '준비됨', 'blocked': '확인 필요', 'unknown': '미확인'}
        self.details.setText('\n'.join(
            f"{row['name']} [{labels[row['status']]}] · "
            + ('필요 도구 확인을 눌러 설치와 연결 상태를 확인하세요.'
               if row['status'] == 'unknown' else str(row['guidance']))
            for row in self.state['requirements']))
        self.set_busy(self._busy)
        return self.state

    def set_busy(self, busy):
        self._busy = bool(busy)
        blockers = self.state.get('blockers', [])
        self.check_button.setEnabled(not busy)
        self.install_button.setEnabled(not busy and (
            bool(self.state.get('unknown')) or any(row['can_install'] for row in blockers)))
        self.settings_button.setEnabled(not busy and self._has_settings
                                        and any(row['can_configure'] for row in blockers))
