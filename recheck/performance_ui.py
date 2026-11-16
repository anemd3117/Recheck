"""Progressive-disclosure UI for local hardware measurements and selected settings."""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .models import atomic_json, data_root
from .ui_theme import SummaryCard

LABELS = {
    'num_ctx': '로컬 AI가 한 번에 읽는 분량',
    'num_predict': '로컬 AI 설명 길이',
    'llm_timeout': '로컬 AI 응답 대기 시간',
    'memory_mb': '실행 메모리 상한',
    'cpu': '실행 CPU 상한',
    'output_limit': '결과 수집 상한',
    'static_timeout': '구조 분석 대기 시간',
}
STAGES = {
    'cpu': 'CPU 연산',
    'memory': '메모리',
    'disk': '저장장치',
    'ollama': '로컬 AI',
    'ghidra': '파일 구조 분석',
    'docker': '격리 실행',
}
STATES = {
    'success': '측정 완료',
    'partial': '일부 완료',
    'failed': '측정 실패',
    'blocked': '준비 필요',
    'cancelled': '중단됨',
    'running': '측정 중',
    'pending': '대기',
    'skipped': '이번 측정에서 생략',
    'not_tested': '미측정',
}


def size(value):
    if not isinstance(value, (int, float)) or value < 0:
        return '확인 불가'
    for scale, unit in [(1024**3, 'GiB'), (1024**2, 'MiB')]:
        if value >= scale:
            return f'{value / scale:,.1f} {unit}'
    return f'{value:,.0f} bytes'


def setting_value(key, value):
    if key in ('llm_timeout', 'static_timeout'):
        return f'{value:,}초'
    if key == 'memory_mb':
        return f'{value:,} MiB'
    if key == 'output_limit':
        return size(value)
    if key in ('num_ctx', 'num_predict'):
        return f'{value:,} 토큰'
    return f'{value:g} 코어' if key == 'cpu' else str(value)


def observed(value, unit=''):
    return '확인 불가' if value is None or value == '' else str(value) + unit


def hardware_details(hardware):
    """Expose collected fields without guessing absent device capabilities."""
    cpu, ram = hardware.get('cpu', {}), hardware.get('memory', {})
    env = hardware.get('environment', {})
    docker, ollama = env.get('docker', {}), env.get('ollama', {})
    ghidra, system = env.get('ghidra', {}), env.get('os', {})
    lines = [
        f"조회 시각: {observed(hardware.get('collected_at'))}",
        'CPU: ' + observed(cpu.get('name')) + ' · 제조사 ' + observed(cpu.get('vendor')),
        f"물리 코어 {observed(cpu.get('physical_cores'))}개 · 논리 프로세서 {observed(cpu.get('logical_cores'))}개"
        + f" · 사용 가능 {observed(cpu.get('usable_cores'))}개 · 구조 {observed(cpu.get('architecture'))}",
        '기본 클럭 '
        + observed(cpu.get('base_mhz'), ' MHz')
        + ' · 최대 클럭 '
        + observed(cpu.get('max_mhz'), ' MHz'),
        'RAM: 전체 '
        + size(ram.get('total_bytes'))
        + ' · 사용 '
        + size(ram.get('used_bytes'))
        + ' · 여유 '
        + size(ram.get('available_bytes'))
        + ' · 사용률 '
        + observed(ram.get('percent'), '%'),
        '분석용 안전 예산: ' + size(ram.get('safe_budget_bytes')),
    ]
    if ram.get('safe_budget_policy'):
        lines.append(str(ram['safe_budget_policy']))
    for module in ram.get('modules', []):
        lines.append(
            '메모리 모듈: '
            + observed(module.get('locator'))
            + ' · '
            + size(module.get('capacity_bytes'))
            + ' · '
            + observed(module.get('manufacturer'))
            + ' · '
            + observed(module.get('part_number'))
            + ' · 설정 속도 '
            + observed(module.get('configured_clock_mhz'), ' MHz')
        )
    gpus = hardware.get('gpus') or []
    lines.append(f'조회된 그래픽카드: {len(gpus)}개' if gpus else '그래픽카드: 확인 불가')
    for gpu in gpus:
        lines.extend(
            [
                'GPU: ' + observed(gpu.get('name')) + ' · 제조사 ' + observed(gpu.get('vendor')),
                '전용 VRAM: 전체 '
                + size(gpu.get('vram_total_bytes'))
                + ' · 사용 '
                + size(gpu.get('vram_used_bytes'))
                + ' · 여유 '
                + size(gpu.get('vram_free_bytes')),
                'GPU 사용률 '
                + observed(gpu.get('usage_percent'), '%')
                + ' · 드라이버 '
                + observed(gpu.get('driver'))
                + ' · 조회 백엔드 '
                + observed(gpu.get('backend')),
            ]
        )
    lines.append(
        'GPU 가속은 실제 AI 실행 기록으로 확인합니다. 드라이버나 VRAM만으로 실행 가능성을 확정하지 않습니다.'
    )
    roles = {
        'system': '시스템',
        'project': '프로젝트',
        'user_data': '분석 기록',
        'temporary': '임시 파일',
    }
    for disk in hardware.get('disks') or []:
        lines.append(
            roles.get(disk.get('role'), '저장장치')
            + ': '
            + observed(disk.get('path'))
            + ' · '
            + observed(disk.get('type'))
            + ' · '
            + observed(disk.get('model'))
            + ' · 전체 '
            + size(disk.get('total_bytes'))
            + ' · 여유 '
            + size(disk.get('free_bytes'))
        )
    lines.extend(
        [
            '운영체제: '
            + ' '.join(
                observed(system.get(k)) for k in ('system', 'release', 'version', 'architecture')
            ),
            'Python: ' + observed(env.get('python', {}).get('version')),
            'JAVA_HOME: ' + observed(env.get('java_home')),
            'Ghidra: '
            + ('실행 준비됨' if ghidra.get('ready') else '준비 상태 확인 필요')
            + ' · 버전 '
            + observed(ghidra.get('version'))
            + ' · JDK '
            + observed(ghidra.get('java_major'))
            + ' · '
            + observed(ghidra.get('home')),
            'Docker: '
            + ('연결됨' if docker.get('ready') else '연결되지 않음')
            + ' · 컨텍스트 '
            + observed(docker.get('context'))
            + ' · 엔진 운영체제 '
            + observed(docker.get('os')),
            f"Docker 엔진 할당: CPU {observed(docker.get('cpus'))}개 · 메모리 {size(docker.get('memory_bytes'))}",
            '엔진 전체 할당량과 개별 테스트 컨테이너의 자원 제한은 별개입니다.',
        ]
    )
    for image in docker.get('images') or []:
        lines.append(
            '분석 이미지: '
            + observed(image.get('reference'))
            + (' · 설치됨' if image.get('present') else ' · 사용 불가 또는 확인 불가')
            + ' · '
            + observed(image.get('os'))
            + '/'
            + observed(image.get('architecture'))
        )
    lines.append(
        'Ollama: '
        + ('연결됨' if ollama.get('connected') else '연결되지 않음')
        + ' · 버전 '
        + observed(ollama.get('version'))
    )
    for model in ollama.get('models') or []:
        details = model.get('details') or {}
        lines.append(
            '설치 모델: '
            + observed(model.get('name'))
            + ' · '
            + size(model.get('size'))
            + ' · 파라미터 '
            + observed(details.get('parameter_size'))
            + ' · 양자화 '
            + observed(details.get('quantization_level'))
            + ' · 컨텍스트 상한 '
            + observed(details.get('context_length'), ' 토큰')
        )
    lines.extend(str(w) for w in hardware.get('warnings', []))
    return '\n'.join(lines)


class PerformancePanel(QWidget):
    def __init__(self, owner, root=None):
        super().__init__()
        self.owner = owner
        self.root = Path(root or data_root())
        self.hardware = {}
        self.benchmark = {}
        self.recommendation = {}
        self.preview_settings = None
        self._benchmark_active = False
        self._busy = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 8, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        box = QVBoxLayout(inner)
        box.setContentsMargins(10, 10, 10, 16)
        box.setSpacing(16)
        intro = QLabel(
            '필요할 때 내 PC의 실행 환경과 로컬 AI 설정을 조정하세요. GPT 같은 외부 AI의 모델과 응답 설정은 분석 AI 연결에서 관리합니다.'
        )
        intro.setObjectName('muted')
        intro.setWordWrap(True)
        box.addWidget(intro)
        self.cards = {}
        self.resource_bars = {}
        cards = QGridLayout()
        for index, (key, title) in enumerate(
            [('cpu', 'CPU'), ('memory', '메모리'), ('gpu', '그래픽카드'), ('disk', '저장 공간')]
        ):
            card = SummaryCard(title, '아직 조회 전', '')
            card.value.setStyleSheet('font-size: 15px; font-weight: 600;')
            cards.addWidget(card, index // 2, index % 2)
            self.cards[key] = card
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setFixedHeight(4)
            bar.setTextVisible(False)
            bar.hide()
            card.layout().addWidget(bar)
            self.resource_bars[key] = bar
        box.addLayout(cards)
        actions = QHBoxLayout()
        self.refresh_button = QPushButton('하드웨어 다시 조회')
        self.refresh_button.clicked.connect(self.refresh_hardware)
        self.quick_button = QPushButton('빠른 벤치마크')
        self.quick_button.setObjectName('primary')
        self.quick_button.clicked.connect(lambda: self.start_benchmark('quick'))
        self.detail_button = QPushButton('상세 벤치마크')
        self.detail_button.clicked.connect(lambda: self.start_benchmark('detailed'))
        self.cancel_button = QPushButton('벤치마크 취소')
        self.cancel_button.clicked.connect(self.owner.cancel_work)
        self.cancel_button.hide()
        actions.addWidget(self.quick_button)
        actions.addWidget(self.detail_button)
        actions.addWidget(self.refresh_button)
        actions.addStretch()
        actions.addWidget(self.cancel_button)
        box.addLayout(actions)
        self.state_label = QLabel(
            '빠른 측정은 짧은 CPU·AI 작업을 실행합니다. 상세 측정은 메모리·디스크·분석 도구도 확인합니다.'
        )
        self.state_label.setObjectName('muted')
        self.state_label.setWordWrap(True)
        self.state_label.setTextFormat(Qt.TextFormat.PlainText)
        box.addWidget(self.state_label)
        self.progress = QProgressBar()
        self.progress.setRange(0, 6)
        self.progress.setValue(0)
        self.progress.setFixedHeight(5)
        self.progress.setTextVisible(False)
        box.addWidget(self.progress)
        profile_row = QHBoxLayout()
        profile_row.addWidget(QLabel('추천 설정'))
        self.profile = QComboBox()
        self.profile.setAccessibleName('추천 설정 프로필')
        for label, key in [
            ('안정성 우선', 'stability'),
            ('균형 · 기본 추천', 'balanced'),
            ('성능 우선', 'performance'),
        ]:
            self.profile.addItem(label, key)
        self.profile.setCurrentIndex(1)
        self.profile.currentIndexChanged.connect(self.preview)
        profile_row.addWidget(self.profile)
        profile_row.addStretch()
        self.preview_button = QPushButton('추천 설정 미리보기')
        self.preview_button.clicked.connect(self.preview)
        profile_row.addWidget(self.preview_button)
        box.addLayout(profile_row)
        self.recommendations = QTreeWidget()
        self.recommendations.setHeaderLabels(['적용할 항목', '현재', '추천', '판단 근거'])
        self.recommendations.setRootIsDecorated(False)
        self.recommendations.setMinimumHeight(215)
        self.recommendations.setMaximumHeight(285)
        self.recommendations.setColumnWidth(0, 205)
        self.recommendations.setColumnWidth(1, 120)
        self.recommendations.setColumnWidth(2, 120)
        self.recommendations.header().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.recommendations.currentItemChanged.connect(self.show_reason)
        box.addWidget(self.recommendations)
        self.reason = QTextBrowser()
        self.reason.setMaximumHeight(160)
        self.reason.setMinimumHeight(95)
        self.reason.setPlaceholderText('추천 항목을 누르면 계산 이유와 주의 사항을 볼 수 있습니다.')
        box.addWidget(owner.detail_section('추천 이유와 측정 근거', self.reason))
        apply_row = QHBoxLayout()
        self.apply_button = QPushButton('선택한 추천 설정 적용')
        self.apply_button.clicked.connect(self.apply_selected)
        self.restore_button = QPushButton('기존 설정 복원')
        self.restore_button.clicked.connect(self.restore_settings)
        apply_row.addWidget(self.apply_button)
        apply_row.addWidget(self.restore_button)
        apply_row.addStretch()
        box.addLayout(apply_row)
        self.detail = QWidget()
        dl = QVBoxLayout(self.detail)
        dl.setContentsMargins(0, 0, 0, 0)
        self.measurements = QTreeWidget()
        self.measurements.setHeaderLabels(['측정 항목', '상태', '소요 시간'])
        self.measurements.setRootIsDecorated(False)
        self.measurements.setMaximumHeight(240)
        self.measurements.currentItemChanged.connect(self.show_measurement)
        dl.addWidget(self.measurements)
        self.measurement_text = QTextBrowser()
        self.measurement_text.setMaximumHeight(190)
        dl.addWidget(self.measurement_text)
        history_row = QHBoxLayout()
        self.history = QComboBox()
        self.history.setAccessibleName('지난 PC 성능 측정')
        history_row.addWidget(self.history, 1)
        self.load_button = QPushButton('지난 측정 보기')
        self.load_button.clicked.connect(self.load_history)
        history_row.addWidget(self.load_button)
        self.retry_button = QPushButton('실패한 측정만 재시도')
        self.retry_button.clicked.connect(
            lambda: self.start_benchmark(self.benchmark.get('mode', 'quick'), True)
        )
        history_row.addWidget(self.retry_button)
        dl.addLayout(history_row)
        self.hardware_text = QTextBrowser()
        self.hardware_text.setMaximumHeight(220)
        dl.addWidget(self.hardware_text)
        box.addWidget(owner.detail_section('성능 결과와 지난 측정', self.detail))
        box.addStretch()
        scroll.setWidget(inner)
        outer.addWidget(scroll)
        self.refresh_history()
        # A startup reads saved local metadata only; it never starts a measurement.
        snapshot = self.root / 'benchmarks' / 'latest-hardware.json'
        if snapshot.is_file():
            try:
                self.show_hardware(json.loads(snapshot.read_text(encoding='utf-8')), save=False)
            except (ValueError, OSError, TypeError):
                pass
        if self.history.count():
            self.load_history()
        self.set_busy(False)

    def set_busy(self, busy):
        self._busy = busy
        for button in (
            self.refresh_button,
            self.quick_button,
            self.detail_button,
            self.apply_button,
            self.restore_button,
            self.preview_button,
            self.load_button,
            self.retry_button,
        ):
            button.setEnabled(not busy)
        self.profile.setEnabled(not busy)
        self.cancel_button.setVisible(busy and self._benchmark_active)
        self.retry_button.setEnabled(
            not busy
            and bool(self.benchmark)
            and any(
                x.get('status') in ('failed', 'blocked', 'cancelled')
                for x in self.benchmark.get('stages', {}).values()
            )
        )
        self.apply_button.setEnabled(not busy and bool(self.recommendation.get('items')))

    def refresh_hardware(self):
        from .hardware import detect_hardware

        self._benchmark_active = True
        self.owner.launch_task(
            'PC 정보 확인 중', lambda s, c, e: detect_hardware(s, cancel=c), self.show_hardware
        )

    def start_benchmark(self, mode='quick', retry=False):
        from .performance import BenchmarkRunner

        self._benchmark_active = True
        self.owner.launch_task(
            'PC 성능 측정 중',
            lambda s, c, e: BenchmarkRunner(s, root=self.root / 'benchmarks', on_event=e).run(
                mode=mode, cancel=c, previous=self.benchmark if retry else None, retry_failed=retry
            ),
            self.show_benchmark,
        )

    def accept_event(self, event):
        if not self._benchmark_active or not self._busy:
            return
        stage = event.get('stage', '')
        if stage in STAGES:
            self.state_label.setText(
                STAGES[stage] + ' · ' + STATES.get(event.get('status'), '측정 중')
            )
        if isinstance(event.get('progress'), (int, float)):
            self.progress.setValue(int(event['progress']))
        elif event.get('status') in ('success', 'blocked', 'failed', 'cancelled', 'skipped'):
            self.progress.setValue(min(6, self.progress.value() + 1))

    def show_hardware(self, hardware, save=True):
        self.hardware = hardware
        cpu, ram = hardware.get('cpu', {}), hardware.get('memory', {})
        usage = cpu.get('usage_percent')
        self.cards['cpu'].update_value(
            cpu.get('name') or '확인 불가',
            f"사용 가능한 CPU {cpu.get('usable_cores') or '확인 불가'}개 · "
            + (f'사용률 {usage:.1f}%' if isinstance(usage, (int, float)) else '사용률 확인 불가'),
        )
        self.cards['memory'].update_value(
            size(ram.get('total_bytes')),
            '여유 '
            + size(ram.get('available_bytes'))
            + ' · 분석용 안전 예산 '
            + size(ram.get('safe_budget_bytes')),
        )
        gpus = hardware.get('gpus') or []
        self.cards['gpu'].update_value(
            ' / '.join(str(g.get('name', '확인 불가')) for g in gpus) or '확인 불가',
            '전용 그래픽 메모리 '
            + (' / '.join(size(g.get('vram_total_bytes')) for g in gpus) or '확인 불가'),
        )
        disks = hardware.get('disks') or []
        disk = next((d for d in disks if d.get('role') == 'project'), disks[0] if disks else {})
        self.cards['disk'].update_value(
            '여유 ' + size(disk.get('free_bytes')),
            str(disk.get('path') or '확인 불가') + ' · 전체 ' + size(disk.get('total_bytes')),
        )
        gpu = next(
            (g for g in gpus if g.get('vram_total_bytes') and g.get('vram_used_bytes') is not None),
            {},
        )
        ratios = {
            'cpu': cpu.get('usage_percent'),
            'memory': ram.get('percent'),
            'gpu': gpu['vram_used_bytes'] / gpu['vram_total_bytes'] * 100 if gpu else None,
            'disk': (
                (1 - disk['free_bytes'] / disk['total_bytes']) * 100
                if disk.get('total_bytes') and disk.get('free_bytes') is not None
                else None
            ),
        }
        for key, ratio in ratios.items():
            known = isinstance(ratio, (int, float)) and 0 <= ratio <= 100
            self.resource_bars[key].setVisible(known)
            if known:
                self.resource_bars[key].setValue(round(ratio))
                self.resource_bars[key].setToolTip(f'조회 당시 사용률 {ratio:.1f}%')
        self.hardware_text.setPlainText(hardware_details(hardware))
        if save:
            try:
                atomic_json(self.root / 'benchmarks' / 'latest-hardware.json', hardware)
            except OSError as exc:
                self.owner.show_error('PC 정보 저장 실패: ' + str(exc))
        self.preview()

    def show_benchmark(self, result, historical=False):
        self.benchmark = result
        if not historical or not self.hardware:
            self.show_hardware(result.get('hardware') or self.hardware, save=not historical)
        self.measurements.clear()
        for key, stage in result.get('stages', {}).items():
            duration = stage.get('duration_seconds')
            row = QTreeWidgetItem(
                [
                    STAGES.get(key, key),
                    STATES.get(stage.get('status'), '미측정'),
                    f'{duration:.2f}초' if isinstance(duration, (float, int)) else '미측정',
                ]
            )
            row.setData(0, Qt.ItemDataRole.UserRole, key)
            self.measurements.addTopLevelItem(row)
        self.progress.setValue(
            sum(
                s.get('status') not in ('pending', 'running')
                for s in result.get('stages', {}).values()
            )
        )
        self.state_label.setText(
            STATES.get(result.get('status'), '측정 기록 저장됨')
            + ' · 측정된 속도는 이 짧은 작업의 결과이며 AI 답변 품질이나 향상률을 뜻하지 않습니다.'
        )
        if self.measurements.topLevelItemCount():
            self.measurements.setCurrentItem(self.measurements.topLevelItem(0))
        self.refresh_history()
        self.preview()
        self.set_busy(self._busy)

    def preview(self, *_):
        from .recommendations import recommend_settings

        if not self.hardware:
            return
        try:
            current = self.owner.collect_settings()
            self.recommendation = recommend_settings(
                current, self.hardware, self.benchmark or None, self.profile.currentData()
            )
            self.preview_settings = current.model_dump()
            self.recommendations.clear()
            for item in self.recommendation['items']:
                key = item['key']
                changed = item['current'] != item['recommended']
                evidence = item.get('evidence') or {}
                observed = item['basis'] == 'measured' or (
                    isinstance(evidence, dict)
                    and (
                        evidence.get('tokens_per_second') is not None
                        or evidence.get('sample_seconds') is not None
                        or bool(evidence.get('post_inference_samples'))
                    )
                )
                row = QTreeWidgetItem(
                    [
                        LABELS.get(key, key),
                        setting_value(key, item['current']),
                        setting_value(key, item['recommended']),
                        '실측 참고 · 추정' if observed else '보수적 추정',
                    ]
                )
                row.setData(0, Qt.ItemDataRole.UserRole, key)
                row.setFlags(row.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                row.setCheckState(
                    0,
                    (
                        Qt.CheckState.Checked
                        if changed and not item.get('apply_blocked')
                        else Qt.CheckState.Unchecked
                    ),
                )
                if item.get('apply_blocked'):
                    row.setFlags(row.flags() & ~Qt.ItemFlag.ItemIsEnabled)
                self.recommendations.addTopLevelItem(row)
            if self.recommendations.topLevelItemCount():
                self.recommendations.setCurrentItem(self.recommendations.topLevelItem(0))
            self.apply_button.setEnabled(not self._busy)
        except Exception as exc:
            self.owner.show_error('추천 설정을 계산하지 못했습니다: ' + str(exc))

    def show_reason(self, row, *_):
        if not row:
            return
        key = row.data(0, Qt.ItemDataRole.UserRole)
        item = next((i for i in self.recommendation.get('items', []) if i['key'] == key), None)
        if not item:
            return
        confidence = {'low': '낮음', 'medium': '보통', 'high': '높음'}.get(
            item.get('confidence'), '확인 불가'
        )
        # Plain text also prevents values from local tools becoming active HTML.
        self.reason.setPlainText(
            '\n'.join(
                [
                    str(item['reason']),
                    f"추천 신뢰도: {confidence}",
                    f"근거 조회 시각: {item.get('measured_at') or self.hardware.get('collected_at') or '확인 불가'}",
                    '예상 효과: '
                    + (
                        '현재 값을 유지합니다.'
                        if item['current'] == item['recommended']
                        else '선택한 자원 제한 또는 대기 시간을 조정합니다. 속도 향상률은 측정하지 않았습니다.'
                    ),
                    '계산 근거: ' + self.evidence_text(item.get('evidence', {})),
                ]
                + [str(x) for x in item.get('warnings', [])]
            )
        )

    @staticmethod
    def evidence_text(evidence):
        if not isinstance(evidence, dict):
            return str(evidence)
        titles = {
            'safe_budget_bytes': '분석용 안전 메모리',
            'host_safe_budget_bytes': '호스트 안전 메모리',
            'docker_memory_bytes': 'Docker 엔진 메모리',
            'effective_budget_bytes': '유효 메모리 예산',
            'model_context_limit': '모델 입력 상한',
            'loaded_context_length': '현재 AI 입력 설정',
            'profile_cap': '프로필 상한',
            'profile_share': 'CPU 사용 비율',
            'recommended_context': '추천 입력 분량',
            'usable_cores': '사용 가능한 CPU',
            'docker_cpus': 'Docker 엔진 CPU',
            'usage_percent': '현재 CPU 사용률(%)',
            'profile_cap_bytes': '프로필 결과 크기 상한',
            'effective_cap_bytes': '유효 결과 크기 상한',
            'tokens_per_second': '생성 토큰/초',
            'projected_seconds': '추정 대기 시간(초)',
            'sample_seconds': '예제 분석 시간(초)',
            'estimated_sample_budget_seconds': '예제 기준 여유 시간(초)',
            'benchmark_id': '측정 기록',
        }
        return ' · '.join(
            titles.get(k, k)
            + ': '
            + (
                '확인 불가'
                if v is None
                else (
                    size(v)
                    if k.endswith('_bytes')
                    else f'{v:.2f}' if isinstance(v, float) else str(v)
                )
            )
            for k, v in evidence.items()
        )

    def show_measurement(self, row, *_):
        if not row:
            return
        key = row.data(0, Qt.ItemDataRole.UserRole)
        stage = self.benchmark.get('stages', {}).get(key, {})
        lines = [STAGES.get(key, key) + ' · ' + STATES.get(stage.get('status'), '미측정')]
        if stage.get('error'):
            error = stage['error']
            lines.append(
                '확인할 사항: '
                + str(error.get('message', '') if isinstance(error, dict) else error)
            )
        data = stage.get('data') or {}
        if key == 'ollama':
            lines.append('선택 모델: ' + str(data.get('model', '확인 불가')))
            for index, sample in enumerate(data.get('samples', []), 1):
                speed = sample.get('tokens_per_second')
                lines.append(
                    f"{index}번째 요청: 생성 {sample.get('eval_count', '미측정')}토큰 · "
                    + (f'{speed:.2f} 토큰/초' if isinstance(speed, (int, float)) else '속도 미측정')
                    + f" · 전체 {sample.get('total_seconds', '미측정')}초"
                )
                load = sample.get('load_duration_ns')
                if isinstance(load, (int, float)):
                    lines.append(f'모델 준비 시간 {load / 1e9:.3f}초')
                lines.append('입력 ' + observed(sample.get('prompt_eval_count'), ' 토큰'))
                for field, title in [
                    ('prompt_eval_duration_ns', '프롬프트 처리'),
                    ('eval_duration_ns', '토큰 생성'),
                ]:
                    duration = sample.get(field)
                    lines.append(
                        title
                        + ': '
                        + (
                            f'{duration / 1e9:.3f}초'
                            if isinstance(duration, (int, float))
                            else '미측정'
                        )
                    )
                for field, title in [('resource_before', '추론 전'), ('resource_after', '추론 후')]:
                    resource = sample.get(field) or {}
                    lines.append(
                        title
                        + ' RAM 사용 '
                        + size(resource.get('memory_used_bytes'))
                        + ' · 여유 '
                        + size(resource.get('memory_available_bytes'))
                        + ' · 호스트 CPU 사용률 '
                        + observed(resource.get('cpu_usage_percent'), '%')
                    )
                    for gpu in (resource.get('gpu_sample') or {}).get('gpus', []):
                        lines.append(
                            title
                            + ' '
                            + observed(gpu.get('name'))
                            + ' VRAM 사용 '
                            + size(gpu.get('vram_used_bytes'))
                            + ' · 여유 '
                            + size(gpu.get('vram_free_bytes'))
                        )
                lines.append('Ollama 보고 VRAM 할당: ' + size(sample.get('ollama_size_vram_bytes')))
                lines.append(
                    str(
                        sample.get('gpu_execution_note')
                        or 'VRAM 할당만으로 모든 연산의 GPU 실행을 증명하지 않습니다.'
                    )
                )
            lines.append(
                '첫 요청도 이미 메모리에 올라온 모델을 사용할 수 있습니다. 준비 시간과 생성 속도는 별개입니다.'
            )
        else:
            names = {
                'single_operations_per_second': '단일 작업 처리량(회/초)',
                'parallel_operations_per_second': '동시 작업 처리량(회/초)',
                'duration_seconds': '실제 작업 시간(초)',
                'process_seconds': '도구 전체 실행 시간(초)',
                'function_count': '식별한 함수 수',
                'requested_memory_mb': '테스트 컨테이너 메모리 제한(MiB)',
                'requested_cpus': '테스트 컨테이너 CPU 제한',
            }
            for name, title in names.items():
                if name in data:
                    lines.append(title + ': ' + str(data[name]))
            for field, title in [
                ('read_bytes_per_second', '읽기'),
                ('write_bytes_per_second', '쓰기'),
                ('copy_bytes_per_second', '메모리 복사'),
            ]:
                rate = data.get(field)
                if isinstance(rate, (int, float)):
                    lines.append(f'{title}: {rate / 1024**2:,.2f} MiB/초')
            limits = data.get('expected_limits', {})
            if limits:
                lines.append(
                    f"테스트 컨테이너 제한: CPU {limits.get('cpus', '미확인')}개 · 메모리 {size(limits.get('memory_bytes'))}"
                )
                lines.append(
                    '위 값은 테스트 컨테이너의 제한이며 Docker 엔진 전체 할당량과 다릅니다.'
                )
            if data.get('startup_seconds') is not None:
                lines.append(f"컨테이너 시작 및 짧은 작업: {data['startup_seconds']:.3f}초")
            if data.get('kind') == 'connection_probe_only':
                lines.append(
                    '연결 상태만 확인했습니다. 컨테이너 실행과 자원 제한은 상세 벤치마크에서 측정합니다.'
                )
            method = data.get('method') or data.get('implementation')
            if method:
                lines.append('측정 방법: ' + str(method))
            if data.get('timing_scope'):
                lines.append(str(data['timing_scope']))
        if data.get('limitation'):
            lines.append(str(data['limitation']))
        for warning in data.get('warnings', []):
            lines.append(str(warning))
        lines.append(
            '기록 위치: ' + str(self.root / 'benchmarks' / self.benchmark.get('benchmark_id', ''))
        )
        self.measurement_text.setPlainText('\n'.join(lines))

    def selected_keys(self):
        return [
            self.recommendations.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole)
            for i in range(self.recommendations.topLevelItemCount())
            if self.recommendations.topLevelItem(i).checkState(0) == Qt.CheckState.Checked
        ]

    def confirm(self, title, text, action):
        dialog = QMessageBox(self)
        dialog.setWindowTitle(title)
        dialog.setTextFormat(Qt.TextFormat.PlainText)
        dialog.setText(text)
        accept = dialog.addButton(action, QMessageBox.ButtonRole.AcceptRole)
        cancel = dialog.addButton('취소', QMessageBox.ButtonRole.RejectRole)
        dialog.setDefaultButton(cancel)
        dialog.setEscapeButton(cancel)
        dialog.exec()
        return dialog.clickedButton() == accept

    def apply_selected(self):
        from .recommendations import SettingsHistory

        if self._busy:
            return
        selected = self.selected_keys()
        if not selected:
            self.owner.notice('적용할 추천 항목을 선택해 주세요.')
            return
        changes = [i for i in self.recommendation.get('items', []) if i['key'] in selected]
        summary = '\n'.join(
            f"{LABELS[i['key']]}: {setting_value(i['key'], i['current'])} → {setting_value(i['key'], i['recommended'])}"
            for i in changes
        )
        if not self.confirm(
            '선택한 설정을 적용할까요?',
            summary + '\n\n현재 저장된 설정을 백업한 뒤 선택 항목만 저장합니다.',
            '선택한 설정 적용',
        ):
            return
        try:
            current = self.owner.collect_settings()
            # Recalculate after confirmation; a manual edit must never be silently overwritten.
            if current.model_dump() != self.preview_settings or any(
                getattr(current, i['key']) != i['current'] for i in changes
            ):
                self.preview()
                self.owner.notice(
                    '설정값이 바뀌어 추천을 갱신했습니다. 새 값을 확인한 뒤 적용해 주세요.'
                )
                return
            updated = SettingsHistory(self.root).apply(current, self.recommendation, selected)
            self.sync_settings(updated)
            self.owner.notice(
                '선택한 설정을 저장했습니다. 이전 설정은 ‘기존 설정 복원’에서 되돌릴 수 있습니다.'
            )
        except Exception as exc:
            self.owner.show_error('추천 설정 적용 실패: ' + str(exc))

    def restore_settings(self):
        from .recommendations import SettingsHistory

        if self._busy:
            return
        if not self.confirm(
            '이전 설정을 복원할까요?',
            '가장 최근 추천 또는 자동 연결 설정을 적용하기 직전으로 되돌립니다.',
            '기존 설정 복원',
        ):
            return
        try:
            restored = SettingsHistory(self.root).restore(self.owner.collect_settings())
            self.sync_settings(restored)
            self.owner.notice('추천 또는 자동 연결 설정을 적용하기 전으로 복원했습니다.')
        except Exception as exc:
            self.owner.show_error('기존 설정 복원 실패: ' + str(exc))

    def sync_settings(self, settings):
        from PySide6.QtWidgets import QCheckBox, QSpinBox, QDoubleSpinBox
        import copy

        protected_capabilities = copy.deepcopy(settings.capabilities)
        self.owner.settings = settings
        self.owner.server_url.blockSignals(True)
        self.owner.server_url.setText(settings.ollama_url)
        self.owner.server_url.blockSignals(False)
        self.owner.model_combo.blockSignals(True)
        index = next(
            (
                i
                for i in range(self.owner.model_combo.count())
                if (self.owner.model_combo.itemData(i) or {}).get('name') == settings.model
            ),
            -1,
        )
        if index < 0 and settings.model:
            self.owner.model_combo.addItem(
                settings.model + ' · 설치 상태 다시 확인', {'name': settings.model}
            )
            index = self.owner.model_combo.count() - 1
        self.owner.model_combo.setCurrentIndex(index)
        self.owner.model_combo.blockSignals(False)
        self.owner.invalidate_capabilities()
        self.owner.settings.capabilities = protected_capabilities
        for key, widget in self.owner.setting_fields.items():
            value = getattr(settings, key)
            if isinstance(widget, QCheckBox):
                widget.setChecked(value)
            elif isinstance(widget, QComboBox):
                widget.setCurrentIndex(widget.findData(value))
            elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
                widget.setValue(value)
            else:
                widget.setText(value)
        self.preview()
        if hasattr(self.owner, 'sync_provider_settings'):
            self.owner.sync_provider_settings()

    def refresh_history(self):
        previous = self.history.currentData()
        self.history.clear()
        folder = self.root / 'benchmarks'
        if folder.is_dir():
            for path in sorted(
                folder.glob('bench_*/result.json'), key=lambda p: p.stat().st_mtime, reverse=True
            )[:30]:
                try:
                    record = json.loads(path.read_text(encoding='utf-8'))
                    self.history.addItem(
                        str(record.get('created_at', ''))
                        + ' · '
                        + ('빠른 측정' if record.get('mode') == 'quick' else '상세 측정')
                        + ' · '
                        + STATES.get(record.get('status'), '미확인'),
                        str(path),
                    )
                except (OSError, ValueError):
                    pass
        if previous:
            index = self.history.findData(previous)
            if index >= 0:
                self.history.setCurrentIndex(index)

    def load_history(self):
        path = self.history.currentData()
        if not path:
            return
        try:
            record = json.loads(Path(path).read_text(encoding='utf-8'))
            self.show_benchmark(record, historical=True)
            self.owner.notice(
                '저장된 측정을 불러왔습니다. 현재 PC 상태가 달라졌다면 다시 측정해 주세요.'
            )
        except Exception as exc:
            self.owner.show_error('지난 측정 읽기 실패: ' + str(exc))
