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
import zlib

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


@pytest.mark.parametrize('name', [
    'stored_size_contradiction.zip', 'deflate_size_contradiction.zip',
    'stored_zero_contradiction.zip', 'local_header_size_mismatch.zip',
])
def test_retained_contradictory_payloads_cannot_become_equal_inventories(name):
    path = Path(__file__).parent / 'fixtures' / 'package-integrity' / name
    result = package_api().compare_packages(path, path)
    assert result['status'] == 'failed'
    assert result['code'] == 'package_invalid'
    assert result['original']['complete'] is False
    assert result['comparison'] is None


def wire_archive(payload=b'payload', *, method=8, descriptor=True, signed=True,
                 compressed=None, local_values=None, descriptor_values=None):
    """A single-entry ZIP with independent raw header/descriptor control."""
    name = b'abc'
    if compressed is None:
        compressed = (zlib.compress(payload)[2:-4] if method == 8 else payload)
    values = (zlib.crc32(payload), len(compressed), len(payload))
    flags = 8 if descriptor else 0
    local_values = local_values if local_values is not None else (
        (0, 0, 0) if descriptor else values)
    local = struct.pack('<4s5H3I2H', b'PK\x03\x04', 20, flags, method, 0, 0,
                        *local_values, len(name), 0) + name
    trailer = b''
    if descriptor:
        trailer = (b'PK\x07\x08' if signed else b'') + struct.pack(
            '<3I', *(descriptor_values if descriptor_values is not None else values))
    central = struct.pack('<4s6H3I5H2I', b'PK\x01\x02', 20, 20, flags, method, 0, 0,
                          *values, len(name), 0, 0, 0, 0, 0, 0) + name
    end = struct.pack('<4s4H2IH', b'PK\x05\x06', 0, 0, 1, 1, len(central),
                      len(local) + len(compressed) + len(trailer), 0)
    return local + compressed + trailer + central + end


@pytest.mark.parametrize('method', [0, 8])
@pytest.mark.parametrize('signed', [False, True])
@pytest.mark.parametrize('payload', [b'', b'payload'])
def test_standard_data_descriptors_preserve_actual_content(tmp_path, method, signed, payload):
    path = tmp_path / 'descriptor.zip'
    path.write_bytes(wire_archive(payload, method=method, signed=signed))
    result = package_api().inventory_package(path)
    assert result['status'] == 'success' and result['complete'] is True
    assert result['members'] == [dict(path='abc', kind='file', size=len(payload),
                                      sha256=hashlib.sha256(payload).hexdigest())]


@pytest.mark.parametrize('kind', ['crc', 'compressed-size', 'size', 'missing'])
def test_data_descriptor_must_match_central_and_observed_payload(tmp_path, kind):
    data = bytearray(wire_archive())
    descriptor = data.index(b'PK\x07\x08')
    if kind == 'missing':
        # Preserve the central-directory location while removing its descriptor.
        del data[descriptor:descriptor + 16]
        end = data.index(b'PK\x05\x06')
        struct.pack_into('<I', data, end + 16, descriptor)
    else:
        offset = {'crc': 4, 'compressed-size': 8, 'size': 12}[kind]
        struct.pack_into('<I', data, descriptor + offset, 123)
    path = tmp_path / 'bad-descriptor.zip'
    path.write_bytes(data)
    result = package_api().compare_packages(path, path)
    assert result['status'] == 'failed' and result['code'] == 'package_invalid'
    assert result['original']['complete'] is False and result['comparison'] is None


@pytest.mark.parametrize('kind', ['unfinished', 'trailing-bytes', 'second-stream'])
def test_deflate_must_end_once_at_the_exact_compressed_boundary(tmp_path, kind):
    compressed = zlib.compress(b'payload')[2:-4]
    compressed = {'unfinished': compressed[:-1], 'trailing-bytes': compressed + b'junk',
                  'second-stream': compressed + compressed}[kind]
    path = tmp_path / 'bad-stream.zip'
    path.write_bytes(wire_archive(compressed=compressed, descriptor=False))
    result = package_api().compare_packages(path, path)
    assert result['status'] == 'failed' and result['code'] == 'package_invalid'
    assert result['comparison'] is None


@pytest.mark.parametrize('offset,value', [(6, 8), (8, 0), (14, 0), (18, 1), (22, 1)])
def test_local_metadata_must_match_central_without_descriptor(tmp_path, offset, value):
    data = bytearray(wire_archive(descriptor=False))
    struct.pack_into('<H' if offset < 10 else '<I', data, offset, value)
    path = tmp_path / 'contradictory-local.zip'
    path.write_bytes(data)
    result = package_api().compare_packages(path, path)
    assert result['status'] == 'failed' and result['code'] == 'package_invalid'
    assert result['comparison'] is None


def test_member_disk_number_cannot_claim_a_complete_single_volume(tmp_path):
    data = bytearray(wire_archive(descriptor=False))
    struct.pack_into('<H', data, data.index(b'PK\x01\x02') + 34, 1)
    path = tmp_path / 'split-member.zip'
    path.write_bytes(data)
    result = package_api().compare_packages(path, path)
    assert result['status'] == 'failed' and result['code'] == 'package_multidisk'
    assert result['comparison'] is None


def test_deflate_validation_streams_more_than_one_output_chunk(tmp_path):
    payload = bytes(range(256)) * 500
    path = tmp_path / 'chunked-deflate.zip'
    path.write_bytes(wire_archive(payload))
    result = package_api().inventory_package(path)
    assert result['status'] == 'success' and result['complete'] is True
    assert result['total_bytes_read'] == 128000
    assert result['members'][0]['sha256'] == hashlib.sha256(payload).hexdigest()


def test_descriptor_accepts_populated_matching_local_values(tmp_path):
    compressed = zlib.compress(b'payload')[2:-4]
    path = tmp_path / 'populated-local.zip'
    path.write_bytes(wire_archive(local_values=(zlib.crc32(b'payload'),
                                               len(compressed), 7)))
    result = package_api().inventory_package(path)
    assert result['status'] == 'success' and result['complete'] is True


def test_forged_small_size_cannot_bypass_actual_decode_limit(tmp_path):
    data = bytearray(wire_archive(b'a' * 100000, descriptor=False))
    for signature, offset in ((b'PK\x03\x04', 22), (b'PK\x01\x02', 24)):
        struct.pack_into('<I', data, data.index(signature) + offset, 1)
    path = tmp_path / 'forged-bomb.zip'
    path.write_bytes(data)
    result = package_api().compare_packages(path, path,
                                            limits=package_api().PackageLimits(max_member_bytes=2))
    assert result['status'] == 'failed' and result['code'] == 'package_member_size'
    assert result['comparison'] is None


@pytest.mark.parametrize('length', [65535, 65536, 65537, 131072, 262145])
@pytest.mark.parametrize('level', [1, 6, 9])
def test_normal_deflate_can_drain_buffered_output_at_chunk_boundary(tmp_path, length, level):
    payload = b'a' * length
    path = tmp_path / 'chunk-boundary.zip'
    path.write_bytes(wire_archive(payload, compressed=zlib.compress(payload, level)[2:-4]))
    report = package_api().inventory_package(
        path, limits=package_api().PackageLimits(max_ratio=1000000))
    assert report['status'] == 'success' and report['complete'] is True
    assert report['total_bytes_read'] == length
    assert report['members'][0]['sha256'] == hashlib.sha256(payload).hexdigest()


def test_buffered_final_byte_cannot_exceed_actual_decode_limit(tmp_path):
    payload = b'a' * 65537
    data = bytearray(wire_archive(payload, descriptor=False))
    for signature, crc_offset, size_offset in ((b'PK\x03\x04', 14, 22),
                                              (b'PK\x01\x02', 16, 24)):
        start = data.index(signature)
        struct.pack_into('<I', data, start + crc_offset, zlib.crc32(payload[:-1]))
        struct.pack_into('<I', data, start + size_offset, 65536)
    path = tmp_path / 'buffered-overflow.zip'
    path.write_bytes(data)
    report = package_api().compare_packages(path, path, limits=package_api().PackageLimits(
        max_member_bytes=65536, max_ratio=1000000))
    assert report['status'] == 'failed' and report['code'] == 'package_member_size'
    assert report['comparison'] is None


def test_cancel_between_bounded_decode_chunks_keeps_inventory_incomplete(tmp_path, monkeypatch):
    payload = b'a' * 65537
    path = tmp_path / 'cancel-buffered.zip'
    path.write_bytes(wire_archive(payload))
    cancel = threading.Event()
    original = zlib.decompressobj

    class CancelAfterActualOutput:
        def __init__(self, *args, **kwargs):
            self.decoder = original(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self.decoder, name)

        def decompress(self, *args):
            output = self.decoder.decompress(*args)
            # zlib versions consume the final input bytes differently. Cancel
            # after actual output, independently of its internal tail/EOF shape.
            if output:
                cancel.set()
            return output

    monkeypatch.setattr(zlib, 'decompressobj', CancelAfterActualOutput)
    report = package_api().compare_packages(path, path, cancel=cancel,
                                            limits=package_api().PackageLimits(max_ratio=1000000))
    assert report['status'] == 'cancelled' and report['code'] == 'cancelled'
    assert report['original']['complete'] is False and report['comparison'] is None
