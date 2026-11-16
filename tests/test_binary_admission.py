"""Header admission must reject malformed objects before any tool execution."""

import struct

import pytest

from recheck.models import inspect_binary


def portable_executable(*, machine=0x8664, magic=0x20B, subsystem=3, flags=2, clr=False):
    data = bytearray(1024)
    data[:2] = b'MZ'
    struct.pack_into('<I', data, 60, 128)
    data[128:132] = b'PE\0\0'
    struct.pack_into('<HH', data, 132, machine, 1)
    optional_size = 240 if magic == 0x20B else 224
    struct.pack_into('<HH', data, 148, optional_size, flags)
    struct.pack_into('<H', data, 152, magic)
    struct.pack_into('<H', data, 220, subsystem)
    directories = 112 if magic == 0x20B else 96
    struct.pack_into('<I', data, 152 + directories - 4, 16)
    if clr:
        struct.pack_into('<II', data, 152 + directories + 14 * 8, 0x2000, 72)
    section = 152 + optional_size
    data[section:section + 5] = b'.text'
    struct.pack_into('<II', data, section + 16, 512, 512)
    return data


def inspect(tmp_path, data):
    path = tmp_path / '형식을 바꾼 이름.txt'
    path.write_bytes(data)
    return inspect_binary(path)


@pytest.mark.parametrize('fault', ['no_optional', 'short_optional', 'truncated_section', 'raw_outside', 'too_many_directories'])
def test_malformed_pe_is_rejected_before_it_is_registered(tmp_path, fault):
    data = portable_executable()
    if fault == 'no_optional':
        struct.pack_into('<H', data, 148, 0)
    elif fault == 'short_optional':
        struct.pack_into('<H', data, 148, 96)
    elif fault == 'truncated_section':
        data = data[:400]
    elif fault == 'raw_outside':
        struct.pack_into('<I', data, 392 + 20, 1024)
    elif fault == 'too_many_directories':
        struct.pack_into('<I', data, 260, 17)
    with pytest.raises(ValueError, match='PE'):
        inspect(tmp_path, data)


def test_pe_object_without_executable_flag_is_not_admitted(tmp_path):
    value = inspect(tmp_path, portable_executable(flags=0))
    assert value['supported'] is False
    assert '실행' in value['reason']


@pytest.mark.parametrize(
    'options,kind,reason',
    [
        ({'flags': 0x2002}, 'dll', 'DLL'),
        ({'subsystem': 2}, 'gui', 'GUI'),
        ({'clr': True}, 'managed', '.NET'),
        ({'machine': 0x14C, 'magic': 0x10B}, 'console', 'x86'),
        ({'machine': 0xAA64}, 'console', '아키텍처'),
    ],
)
def test_unsupported_pe_retains_specific_kind_and_action(tmp_path, options, kind, reason):
    value = inspect(tmp_path, portable_executable(**options))
    assert value['supported'] is False
    assert value['kind'] == kind
    assert reason in value['reason']


def test_managed_pe32_is_identified_using_its_own_directory_offset(tmp_path):
    value = inspect(tmp_path, portable_executable(machine=0x14C, magic=0x10B, clr=True))
    assert value['managed'] is True
    assert value['kind'] == 'managed' and value['supported'] is False


def test_native_pe_candidate_does_not_claim_a_ready_windows_runtime(tmp_path):
    value = inspect(tmp_path, portable_executable())
    assert value['supported'] is True and value['format'] == 'PE'
    assert value['execution_validation'] == 'unverified'
    assert '검증' in value['reason']


def test_elf_shared_library_without_entrypoint_is_not_a_console_target(tmp_path):
    data = bytearray(64)
    data[:7] = b'\x7fELF\x02\x01\x01'
    struct.pack_into('<HH', data, 16, 3, 62)
    value = inspect(tmp_path, data)
    assert value['supported'] is False
    assert '공유 라이브러리' in value['reason']


def test_elf_pie_with_entrypoint_retains_linux_execution_admission(tmp_path):
    data = bytearray(64)
    data[:7] = b'\x7fELF\x02\x01\x01'
    struct.pack_into('<HH', data, 16, 3, 62)
    struct.pack_into('<Q', data, 24, 0x1040)
    value = inspect(tmp_path, data)
    assert value['supported'] is True and value['os'] == 'linux'
