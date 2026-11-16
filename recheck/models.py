from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import sys
from pathlib import Path
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from ai_providers.contracts import ProviderProfile
from .paths import filesystem_path

SCHEMA_VERSION = '1.0'
STATES = ('pending', 'running', 'success', 'partial', 'failed', 'cancelled', 'blocked')


def data_root() -> Path:
    return Path(
        os.environ.get('RECHECK_DATA_DIR')
        or Path(os.environ.get('LOCALAPPDATA', Path.home() / '.local/share')) / 'Recheck'
    )


def resources_root() -> Path:
    return Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent.parent)) / 'resources'


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Settings(StrictModel):
    settings_version: Literal[2] = 2
    ollama_url: str = 'http://127.0.0.1:11434'
    model: str = ''
    ghidra_path: str = ''
    java_home: str = ''
    static_backend: Literal['auto', 'direct', 'rea'] = 'auto'
    docker_context: str = ''
    linux_image: str = 'recheck-analysis-linux:1'
    windows_image: str = 'recheck-analysis-windows:1'
    num_ctx: int = Field(default=8192, ge=1024, le=131072)
    num_predict: int = Field(default=2048, ge=64, le=16384)
    llm_timeout: int = Field(default=120, ge=5, le=900)
    memory_mb: int = Field(default=512, ge=64, le=32768)
    cpu: float = Field(default=1, ge=0.1, le=16)
    output_limit: int = Field(default=1048576, ge=1024, le=16777216)
    static_timeout: int = Field(default=300, ge=10, le=3600)
    normalize_newlines: bool = False
    trim_trailing_whitespace: bool = False
    capabilities: dict[str, Any] = Field(default_factory=dict)
    ai_profiles: list[ProviderProfile] = Field(default_factory=list)
    active_ai_profile: str = 'legacy-local'
    ai_role_profiles: dict[Literal['generation', 'review'], str] = Field(
        default_factory=lambda: {'generation': 'legacy-local', 'review': 'legacy-local'}
    )
    ai_host_id: str = Field(default='', max_length=300)

    @model_validator(mode='before')
    @classmethod
    def previous_version(cls, value):
        if isinstance(value, dict) and value.get('settings_version') == 1:
            value = dict(value)
            value['settings_version'] = 2
        return value

    @model_validator(mode='after')
    def profile_references(self):
        ids = [profile.profile_id for profile in self.ai_profiles]
        if len(set(ids)) != len(ids) or 'legacy-local' in ids:
            raise ValueError('AI 연결 ID가 중복되었거나 예약된 로컬 ID입니다')
        known = set(ids) | {'legacy-local'}
        if self.active_ai_profile not in known or not set(self.ai_role_profiles.values()) <= known:
            raise ValueError('선택한 AI 연결 또는 역할의 프로필이 없습니다')
        if any(ord(char) < 32 for char in self.ai_host_id):
            raise ValueError('앱 등록 ID에 제어 문자를 넣지 마세요')
        return self


def load_settings(root=None) -> Settings:
    root = Path(root) if root is not None else data_root()
    path = root / 'settings.json'
    if not path.exists():
        return Settings()
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        settings = Settings.model_validate(value)
    except (ValueError, TypeError):
        raise ValueError(
            '저장된 설정 형식을 읽을 수 없습니다. 값은 진단에 표시하지 않습니다.'
        ) from None
    if value.get('settings_version') != 2:
        from .recommendations import SettingsHistory

        settings = SettingsHistory(root).replace(settings, settings, action='ai_settings_migration')
    return settings


def atomic_json(path: Path, value: Any):
    path = filesystem_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    with temp.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, default=str)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def save_settings(settings: Settings, root=None):
    root = Path(root) if root is not None else data_root()
    from .recommendations import SettingsHistory

    history = SettingsHistory(root)
    try:
        current = (
            Settings.model_validate_json(history.path.read_text(encoding='utf-8'))
            if history.path.exists()
            else Settings()
        )
    except ValueError:
        raise ValueError('저장된 설정 형식이 달라 새 설정으로 덮어쓰지 않았습니다.') from None
    return history.replace(current, settings, action='settings_save')


class TestLimits(StrictModel):
    memory_mb: int | None = Field(default=None, ge=64, le=32768)
    cpus: float | None = Field(default=None, ge=0.1, le=16)
    output_bytes: int | None = Field(default=None, ge=1024, le=16777216)


class TestCase(StrictModel):
    __test__ = False
    id: str = Field(min_length=1, max_length=80, pattern=r'^[A-Za-z0-9_.-]+$')
    description: str = Field(default='', max_length=2000)
    args: list[str] = Field(default_factory=list, max_length=128)
    stdin: str = Field(default='', max_length=1048576)
    aux_files: list[str] = Field(default_factory=list, max_length=64)
    env: dict[str, str] = Field(default_factory=dict, max_length=64)
    limits: TestLimits = Field(default_factory=TestLimits)
    timeout_seconds: float = Field(default=5, ge=0.1, le=300)
    source: str = Field(default='user', max_length=200)
    verification_status: Literal['registered', 'proposed', 'executed'] = 'registered'

    @field_validator('args')
    @classmethod
    def safe_args(cls, args):
        if any('\x00' in arg or len(arg) > 32768 for arg in args):
            raise ValueError('인자에 NUL 또는 과도한 길이가 있습니다')
        return args

    @field_validator('aux_files')
    @classmethod
    def safe_aux(cls, files):
        if len({Path(x).name.casefold() for x in files}) != len(files):
            raise ValueError('보조 파일 이름이 중복됩니다')
        return files

    @field_validator('env')
    @classmethod
    def safe_env(cls, values):
        # Docker receives each assignment as one argv value, never a shell fragment.
        for key, value in values.items():
            if (
                not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,127}', key)
                or '\x00' in value
                or len(value) > 32768
            ):
                raise ValueError('환경 변수 이름 또는 값이 잘못되었습니다')
        return values


def validate_tests(values: list[dict]) -> list[TestCase]:
    if not values or len(values) > 100:
        raise ValueError('테스트는 1~100개 필요합니다')
    tests = [TestCase.model_validate(x) for x in values]
    if len({x.id for x in tests}) != len(tests):
        raise ValueError('테스트 ID가 중복됩니다')
    return tests


class Finding(StrictModel):
    finding_id: str
    claim: str
    targets: list[str]
    function_ids: list[str]
    test_ids: list[str]
    evidence_ids: list[str]
    rationale: str
    additional_checks: list[str] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)
    verification_status: Literal['hypothesis', 'supported', 'needs_verification'] = 'hypothesis'


class FunctionCandidate(StrictModel):
    original: str
    reimplementation: str
    evidence_ids: list[str]
    rationale: str
    source: str = 'LLM opinion'
    verification_status: Literal['needs_verification'] = 'needs_verification'


class Interpretation(StrictModel):
    summary: str
    findings: list[Finding] = Field(default_factory=list)
    suggested_tests: list[TestCase] = Field(default_factory=list)
    function_candidates: list[FunctionCandidate] = Field(default_factory=list)


def inspect_binary(path: str | Path) -> dict:
    path = Path(path).resolve(strict=True)
    if not path.is_file():
        raise ValueError('일반 파일을 선택하세요')
    with path.open('rb') as stream:
        header = stream.read(4096)
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    result = dict(
        path=str(path),
        name=path.name,
        sha256=digest.hexdigest(),
        size=path.stat().st_size,
        format='unknown',
        os='unknown',
        architecture='unknown',
        supported=False,
        analysis_kind='native',
        static_supported=False,
        static_reason='이 파일 형식에 연결된 정적 비교 도구가 없습니다.',
        reason='ELF x86-64 또는 PE x64 네이티브 콘솔 실행 파일이 필요합니다',
    )
    if header.startswith(b'\x7fELF') and len(header) >= 52:
        if header[4] not in (1, 2) or header[5] not in (1, 2) or (header[4] == 2 and len(header) < 64):
            raise ValueError('ELF 헤더의 클래스·바이트 순서·크기를 확인하세요.')
        endian = '<' if header[5] == 1 else '>'
        kind, machine = struct.unpack_from(endian + 'HH', header, 16)
        result.update(
            format='ELF',
            os='linux',
            architecture={3: 'x86', 40: 'arm', 62: 'x86-64', 183: 'arm64'}.get(machine, f'machine-{machine}'),
            static_supported=(((header[4] == 1 and machine in (3, 40)) or
                               (header[4] == 2 and machine in (62, 183))) and kind in (2, 3)),
        )
        if result['static_supported']:
            result['static_reason'] = '네이티브 ELF의 함수·구조를 정적으로 비교합니다. 실제 추출 범위는 분석 결과에서 확인하세요.'
        result['supported'] = header[4] == 2 and header[5] == 1 and machine == 62 and kind in (2, 3)
        result['reason'] = (
            '콘솔 여부와 동적 로더는 격리 실행에서 확인'
            if result['supported']
            else '지원하지 않는 ELF 형식/아키텍처'
        )
        if result['supported'] and kind == 3 and struct.unpack_from('<Q', header, 24)[0] == 0:
            result.update(
                supported=False,
                reason='진입점이 없는 ELF 공유 라이브러리입니다. 독립 콘솔 실행 파일을 선택하세요.',
            )
    elif header[:4] in (b'\xce\xfa\xed\xfe', b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xce', b'\xfe\xed\xfa\xcf'):
        endian = '<' if header[0] in (0xCE, 0xCF) else '>'
        is64 = header[:4] in (b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf')
        header_size = 32 if is64 else 28
        if len(header) < header_size:
            raise ValueError('잘린 Mach-O 헤더입니다.')
        cpu, subtype, kind, commands, command_bytes = struct.unpack_from(endian + 'IIIII', header, 4)
        if commands > 65536 or command_bytes > result['size'] - header_size or commands * 8 > command_bytes:
            raise ValueError('Mach-O 로드 명령 범위가 잘못되었습니다.')
        with path.open('rb') as stream:
            at, end = header_size, header_size + command_bytes
            for _ in range(commands):
                stream.seek(at)
                command = stream.read(8)
                if at > end - 8 or len(command) != 8:
                    raise ValueError('잘린 Mach-O 로드 명령입니다.')
                command_size = struct.unpack_from(endian + 'I', command, 4)[0]
                if command_size < 8 or command_size % 4 or command_size > end - at:
                    raise ValueError('Mach-O 로드 명령 크기가 잘못되었습니다.')
                at += command_size
            if at != end:
                raise ValueError('Mach-O 로드 명령 목록과 크기가 일치하지 않습니다.')
        result.update(
            format='Mach-O', os='macos', kind={2: 'console', 6: 'dylib', 8: 'bundle'}.get(kind, 'other'),
            architecture={7: 'x86', 0x1000007: 'x86-64', 12: 'arm', 0x100000C: 'arm64'}.get(cpu, f'cpu-{cpu}'),
            static_supported=cpu in (7, 0x1000007, 12, 0x100000C) and kind in (2, 6, 8),
            static_reason='REA/Ghidra의 Linux 관리 환경에서 단일 아키텍처 Mach-O의 함수·구조를 정적으로 비교합니다.',
            reason='macOS 프로그램의 실행 비교 환경은 제공하지 않습니다. 정적 비교를 사용하세요.',
        )
    elif header[:4] in (b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca', b'\xca\xfe\xba\xbf', b'\xbf\xba\xfe\xca'):
        result.update(format='Mach-O universal', os='macos',
                      static_reason='여러 아키텍처를 포함한 Mach-O입니다. 비교할 아키텍처의 단일 파일을 선택하세요.',
                      reason='단일 아키텍처 Mach-O 파일이 필요합니다.')
    elif header.startswith(b'MZ'):
        if len(header) < 64:
            raise ValueError('손상된 PE DOS 헤더입니다. 파일을 다시 확인하세요.')
        offset = struct.unpack_from('<I', header, 60)[0]
        if offset < 64 or offset > result['size'] - 24:
            raise ValueError('손상된 PE 헤더')
        with path.open('rb') as stream:
            stream.seek(offset)
            pe = stream.read(24)
        if pe[:4] != b'PE\0\0':
            raise ValueError('PE 서명이 없습니다')
        machine, sections = struct.unpack_from('<HH', pe, 4)
        optional_size, flags = struct.unpack_from('<HH', pe, 20)
        if not 1 <= sections <= 96 or optional_size < 2:
            raise ValueError('손상된 PE 섹션 또는 선택 헤더입니다. 실행 파일을 다시 확인하세요.')
        table_size = optional_size + sections * 40
        if offset + 24 + table_size > result['size']:
            raise ValueError('잘린 PE 선택 헤더 또는 섹션 테이블입니다.')
        with path.open('rb') as stream:
            stream.seek(offset + 24)
            optional = stream.read(table_size)
        magic = struct.unpack_from('<H', optional)[0]
        directories_offset = {0x10B: 96, 0x20B: 112}.get(magic)
        if directories_offset is None or optional_size < directories_offset:
            raise ValueError('지원하지 않거나 손상된 PE 선택 헤더입니다.')
        directory_count = struct.unpack_from('<I', optional, directories_offset - 4)[0]
        if directory_count * 8 > optional_size - directories_offset:
            raise ValueError('PE 데이터 디렉터리가 선택 헤더 범위를 벗어났습니다.')
        for index in range(sections):
            raw_size, raw_pointer = struct.unpack_from('<II', optional, optional_size + index * 40 + 16)
            if raw_size and (raw_pointer == 0 or raw_pointer + raw_size > result['size']):
                raise ValueError('PE 섹션 데이터가 파일 범위를 벗어났습니다.')
        subsystem = struct.unpack_from('<H', optional, 68)[0]
        clr = directory_count > 14 and any(
            struct.unpack_from('<II', optional, directories_offset + 14 * 8)
        )
        kind = (
            'managed' if clr else 'dll' if flags & 0x2000 else
            'console' if subsystem == 3 else 'gui' if subsystem == 2 else 'other'
        )
        result.update(
            format='PE',
            os='windows',
            architecture={0x8664: 'x86-64', 0x14C: 'x86', 0x1C4: 'armnt', 0xAA64: 'arm64'}.get(machine, f'machine-{machine}'),
            subsystem=subsystem,
            managed=clr,
            kind=kind,
            execution_validation='unverified',
            analysis_kind='managed' if clr else 'native',
            static_supported=(bool(flags & 2) and (
                (machine in (0x8664, 0xAA64) and magic == 0x20B)
                or (machine in (0x14C, 0x1C4) and magic == 0x10B)
            )),
        )
        if clr and result['static_supported']:
            result['static_reason'] = '.NET의 메서드·시그니처·CIL 메타데이터를 REA로 읽고 비교합니다. 실행하거나 C# 소스를 복원하지 않습니다.'
        elif result['static_supported']:
            result['static_reason'] = 'REA/Ghidra로 네이티브 PE의 함수·구조를 비교합니다. DLL·GUI도 실행 없이 분석합니다.'
        elif clr:
            result['static_reason'] = '.NET은 REA의 관리 코드 전용 분석이 필요합니다. 현재 네이티브 함수 비교 경로에는 연결되지 않았습니다.'
        result['supported'] = (
            machine == 0x8664
            and magic == 0x20B
            and subsystem == 3
            and bool(flags & 2)
            and not (flags & 0x2000)
            and not clr
        )
        if clr:
            result['reason'] = '.NET 관리 실행 파일입니다. 현재 네이티브 프로그램 비교에서는 지원하지 않습니다.'
        elif flags & 0x2000:
            result['reason'] = 'DLL은 독립 콘솔 프로그램이 아닙니다. 현재 stdin/stdout 실행 비교에서는 지원하지 않습니다.'
        elif not flags & 2:
            result['reason'] = 'PE 실행 파일 표시가 없습니다. 빌드된 실행 파일을 선택하세요.'
        elif subsystem == 2:
            result['reason'] = 'Windows GUI 앱입니다. 화면 조작·관찰을 포함한 동작 비교는 지원하지 않습니다.'
        elif machine != 0x8664 or magic != 0x20B:
            result['reason'] = f"{result['architecture']} 아키텍처는 현재 실행 비교에서 지원하지 않습니다. x64 네이티브 콘솔 파일을 선택하세요."
        elif subsystem != 3:
            result['reason'] = '일반 콘솔 프로그램이 아닙니다. 드라이버·서비스 등의 실행 비교는 지원하지 않습니다.'
        else:
            result['reason'] = 'PE x64 콘솔 형식입니다. Windows 격리 실행 환경은 별도 검증이 필요하며 현재 동봉 환경에서는 실행 비교가 준비되지 않았습니다.'
    # Keep the historical field as executable eligibility for older callers.
    # Static admission must never enable a target's stdin/stdout execution route.
    result['execution_supported'] = result['supported']
    result['execution_reason'] = result['reason']
    return result
