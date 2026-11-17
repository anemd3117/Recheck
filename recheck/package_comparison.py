"""Bounded ZIP member inventories; no extraction, execution, or semantic verdict.

ZIP-based suffixes such as APK/JAR/MSIX/IPA are hints, not evidence of a valid
application package. Only member paths, kinds, lengths and uncompressed SHA-256
are compared. ZIP compression integrity is validated; timestamps, permissions
and application signatures are not compared. Nested archives remain byte content.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
import struct
import unicodedata
import zipfile
import zlib

from .paths import filesystem_path

CHUNK_BYTES = 64 * 1024
SCOPE = 'package_inventory'


@dataclass(frozen=True)
class PackageLimits:
    max_archive_bytes: int = 512 * 1024 * 1024
    max_members: int = 10000
    max_member_bytes: int = 128 * 1024 * 1024
    max_total_bytes: int = 512 * 1024 * 1024
    max_ratio: int = 200
    max_name_bytes: int = 4096
    max_directory_bytes: int = 16 * 1024 * 1024

    def __post_init__(self):
        if any(type(value) is not int or value <= 0 for value in vars(self).values()):
            raise ValueError('Package inventory limits must be positive integers')


class PackageError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise PackageError('cancelled')


def _empty_report(path):
    return dict(status='blocked', code='package_not_inspected', scope=SCOPE,
                container='zip', extension_hint=Path(path).suffix.lower(),
                complete=False, members=[], total_bytes_read=0, declared_members=None)


def _preflight(stream, limits, cancel):
    """Bound central-directory allocation before ZipFile loads its entries.

    Multipart and ZIP64 directory records are deliberately unsupported in this
    first bounded inventory reader, even when the payload happens to be small.
    """
    initial = os.fstat(stream.fileno())
    if not stat.S_ISREG(initial.st_mode):
        raise PackageError('package_io')
    if initial.st_size > limits.max_archive_bytes:
        raise PackageError('package_archive_limit')
    stream.seek(max(0, initial.st_size - (65535 + 22)))
    tail = stream.read(65535 + 22)
    index = tail.rfind(b'PK\x05\x06')
    if index < 0 or len(tail) - index < 22:
        raise PackageError('package_invalid')
    record = struct.unpack_from('<4s4H2LH', tail, index)
    _, disk, directory_disk, disk_count, count, directory_size, directory_offset, comment = record
    if index + 22 + comment != len(tail):
        raise PackageError('package_invalid')
    if disk or directory_disk or disk_count != count:
        raise PackageError('package_multidisk')
    if count == 65535 or directory_size == 0xffffffff or directory_offset == 0xffffffff:
        raise PackageError('package_zip64')
    if count > limits.max_members:
        raise PackageError('package_member_limit')
    if directory_size > limits.max_directory_bytes:
        raise PackageError('package_directory_limit')
    if directory_size + directory_offset > initial.st_size - 22 - comment:
        raise PackageError('package_invalid')
    end_offset = initial.st_size - len(tail) + index
    if end_offset >= 20:
        stream.seek(end_offset - 20)
        if stream.read(4) == b'PK\x06\x07':
            raise PackageError('package_zip64')
    directory_start = end_offset - directory_size
    if directory_start < 0 or directory_offset != directory_start:
        raise PackageError('package_invalid')
    stream.seek(directory_start)
    remaining, observed_count = directory_size, 0
    while remaining:
        _check_cancel(cancel)
        observed_count += 1
        if observed_count > limits.max_members:
            raise PackageError('package_member_limit')
        if remaining < 46:
            raise PackageError('package_invalid')
        header = stream.read(46)
        if len(header) != 46 or header[:4] != b'PK\x01\x02':
            raise PackageError('package_invalid')
        name_length, extra_length, comment_length = struct.unpack_from('<HHH', header, 28)
        variable_size = name_length + extra_length + comment_length
        if variable_size > remaining - 46:
            raise PackageError('package_invalid')
        if not name_length or name_length > limits.max_name_bytes:
            raise PackageError('package_member_path')
        raw_name = stream.read(name_length)
        if b'\\' in raw_name or b'\x00' in raw_name:
            raise PackageError('package_member_path')
        stream.seek(extra_length + comment_length, 1)
        remaining -= 46 + variable_size
    if observed_count != count:
        raise PackageError('package_invalid')
    stream.seek(0)
    return initial, count, directory_start


def _member_path(info, limits):
    name = info.orig_filename
    if (not name or len(name.encode('utf-8')) > limits.max_name_bytes
            or name.startswith('/') or '\\' in name or ':' in name
            or any(ord(char) < 32 or ord(char) == 127 for char in name)):
        raise PackageError('package_member_path')
    normalized = unicodedata.normalize('NFC', name[:-1] if name.endswith('/') else name)
    parts = normalized.split('/')
    if (not normalized or any(part in ('', '.', '..') or part.rstrip(' .') != part
                              for part in parts)):
        raise PackageError('package_member_path')
    return normalized


def _member_kind(info):
    mode = stat.S_IFMT(info.external_attr >> 16)
    if mode not in (0, stat.S_IFREG, stat.S_IFDIR):
        raise PackageError('package_member_kind')
    directory = info.is_dir()
    if (mode == stat.S_IFDIR and not directory
            or mode == stat.S_IFREG and directory
            or info.external_attr & 0x10 and not directory):
        raise PackageError('package_member_kind')
    if directory and info.file_size:
        raise PackageError('package_member_kind')
    return 'directory' if directory else 'file'


def _checked_members(entries, limits, cancel):
    seen = set()
    checked = []
    total = 0
    for info in entries:
        _check_cancel(cancel)
        path = _member_path(info, limits)
        if path in seen:
            raise PackageError('package_duplicate_path')
        seen.add(path)
        kind = _member_kind(info)
        if info.volume:
            raise PackageError('package_multidisk')
        if info.flag_bits & (1 | 64):
            raise PackageError('package_encrypted')
        if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise PackageError('package_compression')
        if info.file_size < 0 or info.compress_size < 0:
            raise PackageError('package_invalid')
        if info.file_size > limits.max_member_bytes:
            raise PackageError('package_member_size')
        total += info.file_size
        if total > limits.max_total_bytes:
            raise PackageError('package_total_size')
        if info.file_size > max(1, info.compress_size) * limits.max_ratio:
            raise PackageError('package_ratio')
        checked.append((info, path, kind))
    kinds = {path: kind for _, path, kind in checked}
    for _, path, _ in checked:
        parts = path.split('/')
        if any(kinds.get('/'.join(parts[:index]), 'directory') != 'directory'
               for index in range(1, len(parts))):
            raise PackageError('package_member_path')
    return checked


def _check_extra(extra):
    """Reject truncated fields and ZIP64 even when its declared sizes are small."""
    offset = 0
    while offset < len(extra):
        if len(extra) - offset < 4:
            raise PackageError('package_invalid')
        kind, length = struct.unpack_from('<HH', extra, offset)
        offset += 4
        if length > len(extra) - offset:
            raise PackageError('package_invalid')
        if kind == 1:
            raise PackageError('package_zip64')
        offset += length


def _member_boundaries(entries, directory_start):
    """Physical member extents prevent overlaps, hidden gaps and trailing data."""
    offsets = sorted(info.header_offset for info in entries)
    if (len(set(offsets)) != len(offsets)
            or (offsets[0] if offsets else directory_start) != 0
            or any(offset < 0 or offset >= directory_start for offset in offsets)):
        raise PackageError('package_invalid')
    return dict(zip(offsets, offsets[1:] + [directory_start]))


def _member_layout(source, info, boundary):
    """Validate local/central/descriptor metadata before trusting member output."""
    _check_extra(info.extra)
    if info.header_offset + 30 > boundary:
        raise PackageError('package_invalid')
    source.seek(info.header_offset)
    header = source.read(30)
    if len(header) != 30 or header[:4] != b'PK\x03\x04':
        raise PackageError('package_invalid')
    _, version, flags, method, _, _, crc, compressed, size, names, extras = struct.unpack(
        '<4s5H3I2H', header)
    # Only deflate options, a data descriptor and UTF-8 names are supported.
    allowed_flags = 0x808 | (6 if method == zipfile.ZIP_DEFLATED else 0)
    if (version != (info.extract_version | (info.reserved << 8))
            or flags != info.flag_bits or flags & ~allowed_flags
            or method != info.compress_type):
        raise PackageError('package_invalid')
    data_start = info.header_offset + 30 + names + extras
    if data_start > boundary:
        raise PackageError('package_invalid')
    name = source.read(names).decode('utf-8' if flags & 0x800 else 'cp437')
    if name != info.orig_filename:
        raise PackageError('package_invalid')
    _check_extra(source.read(extras))
    if compressed == 0xffffffff or size == 0xffffffff:
        raise PackageError('package_zip64')
    expected = (info.CRC, info.compress_size, info.file_size)
    local = (crc, compressed, size)
    if method == zipfile.ZIP_STORED and info.compress_size != info.file_size:
        raise PackageError('package_invalid')
    data_end = data_start + info.compress_size
    if data_end > boundary:
        raise PackageError('package_invalid')
    if flags & 8:
        if any(value not in (0, actual) for value, actual in zip(local, expected)):
            raise PackageError('package_invalid')
        descriptor_size = boundary - data_end
        if descriptor_size not in (12, 16):
            raise PackageError('package_invalid')
        source.seek(data_end)
        descriptor = source.read(descriptor_size)
        if descriptor_size == 16:
            if descriptor[:4] != b'PK\x07\x08':
                raise PackageError('package_invalid')
            descriptor = descriptor[4:]
        if len(descriptor) != 12 or struct.unpack('<3I', descriptor) != expected:
            raise PackageError('package_invalid')
    elif local != expected or data_end != boundary:
        raise PackageError('package_invalid')
    return data_start


def _verify_member_stream(source, info, start, limits, prior_bytes, cancel):
    """Decode raw compressed bytes independently of ZipExtFile's declared cap.

    Every decompression call caps its output. A forged size cannot hide payload
    bytes, an unfinished stream, a second stream or bytes after DEFLATE EOF.
    """
    source.seek(start)
    remaining, observed, crc = info.compress_size, 0, 0
    digest = hashlib.sha256()
    decoder = (zlib.decompressobj(-zlib.MAX_WBITS)
               if info.compress_type == zipfile.ZIP_DEFLATED else None)
    while remaining:
        _check_cancel(cancel)
        chunk = source.read(min(remaining, CHUNK_BYTES))
        if not chunk:
            raise PackageError('package_invalid')
        remaining -= len(chunk)
        pending = chunk
        while True:
            _check_cancel(cancel)
            output = decoder.decompress(pending, CHUNK_BYTES) if decoder else pending
            pending = decoder.unconsumed_tail if decoder else b''
            observed += len(output)
            if observed > limits.max_member_bytes:
                raise PackageError('package_member_size')
            if prior_bytes + observed > limits.max_total_bytes:
                raise PackageError('package_total_size')
            if observed > info.file_size:
                raise PackageError('package_invalid')
            digest.update(output)
            crc = zlib.crc32(output, crc)
            if decoder and (decoder.unused_data or decoder.eof and (pending or remaining)):
                raise PackageError('package_invalid')
            # max_length can leave output buffered after all input was consumed.
            # Drain with empty input under the same byte/cancellation limits.
            if decoder is None or decoder.eof or not (pending or output):
                break
    if (decoder is not None and not decoder.eof
            or observed != info.file_size or crc != info.CRC):
        raise PackageError('package_invalid')
    return digest.hexdigest()


def inventory_package(path, *, limits=None, cancel=None):
    """Read an entire bounded inventory, retaining measured rows on read failure.

    A complete inventory is required before comparisons. A partial inventory is
    diagnostic evidence only: missing rows must never be reported as removals.
    """
    limits = limits or PackageLimits()
    report = _empty_report(path)
    try:
        _check_cancel(cancel)
        with filesystem_path(path).open('rb') as source:
            initial, count, directory_start = _preflight(source, limits, cancel)
            report['declared_members'] = count
            _check_cancel(cancel)
            with zipfile.ZipFile(source, 'r') as package:
                entries = package.infolist()
                if len(entries) != count:
                    raise PackageError('package_invalid')
                checked = _checked_members(entries, limits, cancel)
                boundaries = _member_boundaries(entries, directory_start)
                for info, member_path, kind in checked:
                    _check_cancel(cancel)
                    start = _member_layout(source, info, boundaries[info.header_offset])
                    raw_digest = _verify_member_stream(
                        source, info, start, limits, report['total_bytes_read'], cancel)
                    digest, observed = hashlib.sha256(), 0
                    # Opening directory entries also checks malformed headers/CRC.
                    with package.open(info, 'r') as member:
                        while True:
                            _check_cancel(cancel)
                            chunk = member.read(CHUNK_BYTES)
                            if not chunk:
                                break
                            observed += len(chunk)
                            report['total_bytes_read'] += len(chunk)
                            if observed > limits.max_member_bytes or observed > info.file_size:
                                raise PackageError('package_member_size')
                            if report['total_bytes_read'] > limits.max_total_bytes:
                                raise PackageError('package_total_size')
                            digest.update(chunk)
                    if observed != info.file_size or digest.hexdigest() != raw_digest:
                        raise PackageError('package_invalid')
                    report['members'].append(dict(path=member_path, kind=kind, size=observed,
                                                  sha256=digest.hexdigest()))
            _check_cancel(cancel)
            final = os.fstat(source.fileno())
            if (initial.st_size, initial.st_mtime_ns, initial.st_ino) != (
                    final.st_size, final.st_mtime_ns, final.st_ino):
                raise PackageError('package_changed')
        report.update(status='success', code='', complete=True)
    except PackageError as error:
        report.update(status='cancelled' if error.code == 'cancelled' else
                      'partial' if report['members'] else 'failed', code=error.code)
    except OSError:
        report.update(status='partial' if report['members'] else 'failed', code='package_io')
    except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, ValueError,
            RuntimeError, NotImplementedError, UnicodeError, zlib.error):
        report.update(status='partial' if report['members'] else 'failed', code='package_invalid')
    report['members'].sort(key=lambda row: row['path'])
    return report


def compare_packages(original, reimplementation, *, limits=None, cancel=None):
    """Compare complete member inventories; status success includes differences."""
    before = inventory_package(original, limits=limits, cancel=cancel)
    after = (inventory_package(reimplementation, limits=limits, cancel=cancel)
             if before['complete'] else _empty_report(reimplementation))
    result = dict(status='success', code='', scope=SCOPE, original=before,
                  reimplementation=after, comparison=None,
                  scope_note='패키지 구성 파일의 경로·종류·크기·내용 해시 비교입니다. '
                             '코드 의미·서명 유효성·실행 동작의 동등성을 판정하지 않습니다.')
    incomplete = next((item for item in (before, after) if not item['complete']), None)
    if incomplete is not None:
        result.update(status=incomplete['status'], code=incomplete['code'])
        return result
    try:
        _check_cancel(cancel)
    except PackageError:
        result.update(status='cancelled', code='cancelled')
        return result
    left = {row['path']: row for row in before['members']}
    right = {row['path']: row for row in after['members']}
    common = left.keys() & right.keys()
    changed = sorted(path for path in common if left[path] != right[path])
    unchanged = sorted(path for path in common if left[path] == right[path])
    added, removed = sorted(right.keys() - left.keys()), sorted(left.keys() - right.keys())
    result['comparison'] = dict(added=added, removed=removed, changed=changed,
                                unchanged=unchanged,
                                inventory_equal=not (added or removed or changed))
    return result
