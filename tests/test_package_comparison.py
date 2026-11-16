"""Package comparison measures bounded member content, never program behavior."""

import hashlib
import importlib
import importlib.util
from pathlib import Path
import stat
import struct
import threading
import warnings
import zipfile

import pytest


def package_api():
    assert importlib.util.find_spec('recheck.package_comparison') is not None, (
        'Package inventory comparison is not implemented'
    )
    return importlib.import_module('recheck.package_comparison')


def archive(path, entries, *, compression=zipfile.ZIP_STORED):
    with zipfile.ZipFile(path, 'w', compression=compression) as stream:
        for name, payload in entries:
            stream.writestr(name, payload)
    return path


def patch_headers(path, offset, value):
    data = bytearray(path.read_bytes())
    for signature, delta in ((b'PK\x03\x04', offset[0]), (b'PK\x01\x02', offset[1])):
        struct.pack_into('<H', data, data.index(signature) + delta, value)
    path.write_bytes(data)


def test_real_zip_diff_has_independent_content_verdicts_and_no_execution(tmp_path):
    original = archive(tmp_path / 'before.apk', [
        ('same.txt', b'abc'), ('changed.txt', b'before'), ('removed.txt', b'remove'),
        ('kind/', b''), ('nested.zip', b'not opened as a nested archive'),
    ])
    changed = archive(tmp_path / 'after.msix', [
        ('added.txt', b'new'), ('same.txt', b'abc'), ('changed.txt', b'after'),
        ('kind', b''), ('nested.zip', b'not opened as a nested archive'),
    ], compression=zipfile.ZIP_DEFLATED)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    result = package_api().compare_packages(original, changed)
    assert result['status'] == 'success' and result['scope'] == 'package_inventory'
    assert result['comparison'] == {
        'added': ['added.txt'], 'removed': ['removed.txt'],
        'changed': ['changed.txt', 'kind'], 'unchanged': ['nested.zip', 'same.txt'],
        'inventory_equal': False,
    }
    assert result['original']['members'][0]['path'] == 'changed.txt'
    same = next(row for row in result['original']['members'] if row['path'] == 'same.txt')
    assert same == {'path': 'same.txt', 'kind': 'file', 'size': 3,
                    'sha256': 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad'}
    assert result['original']['container'] == result['reimplementation']['container'] == 'zip'
    assert result['original']['extension_hint'] == '.apk'
    assert result['reimplementation']['extension_hint'] == '.msix'
    assert 'behavior' not in result and 'score' not in result and 'equivalent' not in result
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir()}


def test_inventory_ignores_archive_order_compression_timestamps_and_comment(tmp_path):
    one = archive(tmp_path / 'one.zip', [('z', b'z'), ('a', b'a')])
    two = archive(tmp_path / 'two.zip', [('a', b'a'), ('z', b'z')],
                  compression=zipfile.ZIP_DEFLATED)
    with zipfile.ZipFile(two, 'a') as stream:
        stream.comment = b'different archive metadata'
    result = package_api().compare_packages(one, two)
    assert result['comparison'] == {'added': [], 'removed': [], 'changed': [],
                                    'unchanged': ['a', 'z'], 'inventory_equal': True}


@pytest.mark.parametrize('suffix', ['.zip', '.jar', '.apk', '.msix', '.ipa', '.unknown'])
def test_detects_real_zip_payload_without_claiming_application_format_support(tmp_path, suffix):
    path = archive(tmp_path / ('sample' + suffix), [('x', b'x')])
    report = package_api().inventory_package(path)
    assert report['status'] == 'success' and report['complete'] is True
    assert report['container'] == 'zip' and report['scope'] == 'package_inventory'
    assert report['extension_hint'] == suffix


def test_empty_valid_zip_is_a_complete_empty_inventory(tmp_path):
    path = archive(tmp_path / 'empty.zip', [])
    report = package_api().compare_packages(path, path)
    assert report['status'] == 'success'
    assert report['comparison']['inventory_equal'] is True
    assert report['original']['complete'] is True and report['original']['members'] == []


@pytest.mark.parametrize('name', [
    '../escape', 'nested/../../escape', '/absolute', 'C:/drive', 'a\\b',
    './alias', 'a//alias', 'a/./b', 'x:stream', 'control\x01', 'trail. /x',
])
def test_unsafe_or_ambiguous_member_names_are_not_read_as_success(tmp_path, name):
    # ZipInfo normalizes os.sep when writing on Windows; patch raw names in
    # both ZIP headers to exercise an actual backslash received on disk.
    path = archive(tmp_path / 'unsafe.zip', [(name.replace('\\', '/'), b'payload')])
    if '\\' in name:
        path.write_bytes(path.read_bytes().replace(name.replace('\\', '/').encode(), name.encode()))
    report = package_api().inventory_package(path)
    assert report['status'] == 'failed' and report['code'] == 'package_member_path'
    assert report['complete'] is False and report['members'] == []
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize('names', [('same', 'same'), ('folder/', 'folder'), ('é', 'e\u0301')])
def test_duplicate_normalized_paths_never_overwrite_an_inventory_row(tmp_path, names):
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', UserWarning)
        path = archive(tmp_path / 'duplicate.zip', [
            (name, b'' if name.endswith('/') else b'x') for name in names
        ])
    report = package_api().inventory_package(path)
    assert report['status'] == 'failed' and report['code'] == 'package_duplicate_path'
    assert report['members'] == [] and report['complete'] is False


@pytest.mark.parametrize('mode', [stat.S_IFLNK | 0o777, stat.S_IFIFO | 0o600])
def test_link_and_special_file_entries_are_rejected_without_following_targets(tmp_path, mode):
    info = zipfile.ZipInfo('link')
    info.create_system = 3
    info.external_attr = mode << 16
    path = archive(tmp_path / 'links.zip', [(info, b'../outside')])
    report = package_api().inventory_package(path)
    assert report['status'] == 'failed' and report['code'] == 'package_member_kind'
    assert report['members'] == []


def test_encryption_is_explicitly_unsupported_and_does_not_prompt(tmp_path):
    path = archive(tmp_path / 'encrypted.zip', [('secret', b'payload')])
    patch_headers(path, (6, 8), 1)
    report = package_api().inventory_package(path)
    assert report['status'] == 'failed' and report['code'] == 'package_encrypted'


def test_unsupported_compression_fails_before_member_read(tmp_path):
    path = archive(tmp_path / 'unknown.zip', [('x', b'payload')])
    patch_headers(path, (8, 10), 99)
    report = package_api().inventory_package(path)
    assert report['status'] == 'failed' and report['code'] == 'package_compression'


@pytest.mark.parametrize('setting,entries,compression,code', [
    ({'max_archive_bytes': 20}, [('a', b'x')], zipfile.ZIP_STORED, 'package_archive_limit'),
    ({'max_members': 1}, [('a', b'x'), ('b', b'y')], zipfile.ZIP_STORED, 'package_member_limit'),
    ({'max_member_bytes': 2}, [('a', b'abc')], zipfile.ZIP_STORED, 'package_member_size'),
    ({'max_total_bytes': 5}, [('a', b'abc'), ('b', b'def')], zipfile.ZIP_STORED, 'package_total_size'),
    ({'max_ratio': 2}, [('a', b'x' * 4096)], zipfile.ZIP_DEFLATED, 'package_ratio'),
    ({'max_name_bytes': 3}, [('longname', b'x')], zipfile.ZIP_STORED, 'package_member_path'),
    ({'max_directory_bytes': 20}, [('a', b'x')], zipfile.ZIP_STORED, 'package_directory_limit'),
])
def test_independent_resource_limits_block_instead_of_returning_empty_match(
    tmp_path, setting, entries, compression, code
):
    path = archive(tmp_path / 'limited.zip', entries, compression=compression)
    limits = package_api().PackageLimits(**setting)
    report = package_api().compare_packages(path, path, limits=limits)
    assert report['status'] == 'failed' and report['code'] == code
    assert report['comparison'] is None and report['original']['complete'] is False


def test_corrupt_member_crc_preserves_only_prior_measured_rows_as_partial(tmp_path):
    path = archive(tmp_path / 'crc.zip', [('a', b'first'), ('b', b'CORRUPT-ME')])
    data = path.read_bytes()
    path.write_bytes(data.replace(b'CORRUPT-ME', b'CORRUPT-XX', 1))
    report = package_api().inventory_package(path)
    assert report['status'] == 'partial' and report['code'] == 'package_invalid'
    assert [row['path'] for row in report['members']] == ['a']
    assert report['complete'] is False
    result = package_api().compare_packages(path, path)
    assert result['status'] == 'partial' and result['comparison'] is None


@pytest.mark.parametrize('payload', [b'not a zip', b'PK\x03\x04truncated'])
def test_invalid_or_nonzip_inputs_are_failed_not_unchanged(tmp_path, payload):
    path = tmp_path / 'misleading.apk'
    path.write_bytes(payload)
    report = package_api().compare_packages(path, path)
    assert report['status'] == 'failed' and report['code'] == 'package_invalid'
    assert report['comparison'] is None


def test_missing_input_and_directory_have_safe_failure_codes(tmp_path):
    for path in (tmp_path / 'missing.zip', tmp_path):
        report = package_api().inventory_package(path)
        assert report['status'] == 'failed' and report['code'] == 'package_io'
        assert report['members'] == []


def test_preexisting_cancel_does_not_open_the_input(tmp_path):
    event = threading.Event()
    event.set()
    report = package_api().compare_packages(tmp_path / 'missing', tmp_path / 'missing', cancel=event)
    assert report['status'] == 'cancelled' and report['code'] == 'cancelled'
    assert report['comparison'] is None


def test_cancel_during_streaming_does_not_mark_unfinished_member_complete(tmp_path, monkeypatch):
    path = archive(tmp_path / 'large.zip', [('a', b'a' * 1048576)])
    event = threading.Event()
    real_read = zipfile.ZipExtFile.read

    def cancel_after_real_chunk(stream, length):
        chunk = real_read(stream, length)
        if chunk:
            event.set()
        return chunk

    monkeypatch.setattr(zipfile.ZipExtFile, 'read', cancel_after_real_chunk)
    report = package_api().inventory_package(path, cancel=event)
    assert report['status'] == 'cancelled' and report['members'] == []
    assert report['complete'] is False and report['code'] == 'cancelled'
    assert 0 < report['total_bytes_read'] < 1048576


def test_null_name_in_central_directory_is_not_silently_truncated(tmp_path):
    path = archive(tmp_path / 'null.zip', [('abc', b'data')])
    path.write_bytes(path.read_bytes().replace(b'abc', b'a\x00c'))
    report = package_api().inventory_package(path)
    assert report['status'] == 'failed' and report['code'] == 'package_member_path'


def test_single_member_digest_streaming_result_matches_literal_ground_truth(tmp_path):
    path = archive(tmp_path / 'stream.zip', [('x', b'abc' * 100000)])
    report = package_api().inventory_package(path)
    assert report['status'] == 'success' and report['total_bytes_read'] == 300000
    assert report['members'][0]['sha256'] == hashlib.sha256(b'abc' * 100000).hexdigest()


def test_actual_directory_count_is_bounded_before_zipfile_allocates_members(tmp_path, monkeypatch):
    path = archive(tmp_path / 'lying-count.zip', [('a', b'a'), ('b', b'b')])
    data = bytearray(path.read_bytes())
    end = data.rfind(b'PK\x05\x06')
    struct.pack_into('<HH', data, end + 8, 1, 1)
    path.write_bytes(data)

    def forbidden(*args, **kwargs):
        pytest.fail('Untrusted directory entries must be counted before ZipFile allocation')

    api = package_api()
    monkeypatch.setattr(zipfile, 'ZipFile', forbidden)
    report = api.inventory_package(path, limits=api.PackageLimits(max_members=1))
    assert report['status'] == 'failed' and report['code'] == 'package_member_limit'


def test_file_parent_cannot_also_be_a_directory_namespace(tmp_path):
    path = archive(tmp_path / 'conflict.zip', [('a', b'file'), ('a/b', b'child')])
    report = package_api().inventory_package(path)
    assert report['status'] == 'failed' and report['code'] == 'package_member_path'


@pytest.mark.parametrize('field,value', [('max_members', 0), ('max_ratio', True),
                                        ('max_total_bytes', -1), ('max_ratio', 1.5)])
def test_invalid_resource_limits_are_not_silently_accepted(field, value):
    with pytest.raises(ValueError):
        package_api().PackageLimits(**{field: value})


def test_invalid_deflate_stream_is_a_safe_failure_not_an_uncaught_decoder_error(tmp_path):
    path = archive(tmp_path / 'deflate.zip', [('a', b'payload' * 100)],
                   compression=zipfile.ZIP_DEFLATED)
    data = bytearray(path.read_bytes())
    local = data.index(b'PK\x03\x04')
    name_length, extra_length = struct.unpack_from('<HH', data, local + 26)
    data[local + 30 + name_length + extra_length] = 7  # invalid DEFLATE block type
    path.write_bytes(data)
    report = package_api().inventory_package(path)
    assert report['status'] == 'failed' and report['code'] == 'package_invalid'
