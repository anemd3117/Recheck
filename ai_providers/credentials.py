"""프로필·인증 origin에 결합한 Windows 사용자 DPAPI 저장소. 평문 대체 저장은 없다."""

from __future__ import annotations

import asyncio
import base64
from contextlib import contextmanager
import ctypes
from dataclasses import dataclass
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
from urllib.parse import urlsplit
import uuid

from pydantic import SecretStr


class AuthError(RuntimeError):
    def __init__(self, code, message, details=None):
        self.code, self.details = code, details or {}
        super().__init__(message)


def canonical_origin(value):
    try:
        if not isinstance(value, str) or any(c in value for c in '\r\n\x00\\'):
            raise ValueError()
        parsed = urlsplit(value)
        if (
            parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError()
        host = (parsed.hostname or '').encode('idna').decode('ascii').lower()
        if not host or parsed.scheme not in ('https', 'http'):
            raise ValueError()
        if parsed.scheme == 'http':
            try:
                local = ipaddress.ip_address(host).is_loopback
            except ValueError:
                local = host == 'localhost'
            if not local:
                raise ValueError()
        port = parsed.port
        if ':' in host:
            host = '[' + host + ']'
        suffix = (
            ''
            if port is None or port == (443 if parsed.scheme == 'https' else 80)
            else ':' + str(port)
        )
        return parsed.scheme + '://' + host + suffix
    except (ValueError, UnicodeError):
        raise AuthError(
            'credential_origin',
            '인증 대상은 사용자 정보와 쿼리가 없는 HTTPS 주소 또는 로컬 loopback 주소여야 합니다.',
        ) from None


class DPAPIProtector:
    """CRYPTPROTECT_LOCAL_MACHINE을 사용하지 않는 현재 Windows 사용자 보호."""

    def __init__(self):
        if sys.platform != 'win32':
            raise AuthError(
                'secure_store_unavailable', '이 저장소에는 Windows 사용자 DPAPI가 필요합니다.'
            )
        from ctypes import wintypes

        class Blob(ctypes.Structure):
            _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]

        self.Blob = Blob
        self.crypt = ctypes.WinDLL('crypt32', use_last_error=True)
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.crypt.CryptProtectData.argtypes = [
            ctypes.POINTER(Blob),
            wintypes.LPCWSTR,
            ctypes.POINTER(Blob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(Blob),
        ]
        self.crypt.CryptProtectData.restype = wintypes.BOOL
        self.crypt.CryptUnprotectData.argtypes = [
            ctypes.POINTER(Blob),
            ctypes.c_void_p,
            ctypes.POINTER(Blob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(Blob),
        ]
        self.crypt.CryptUnprotectData.restype = wintypes.BOOL
        self.kernel.LocalFree.argtypes = [ctypes.c_void_p]
        self.kernel.LocalFree.restype = ctypes.c_void_p

    def _run(self, data, entropy, protect):
        buffers = [ctypes.create_string_buffer(data), ctypes.create_string_buffer(entropy)]
        source = self.Blob(len(data), ctypes.cast(buffers[0], ctypes.POINTER(ctypes.c_ubyte)))
        extra = self.Blob(len(entropy), ctypes.cast(buffers[1], ctypes.POINTER(ctypes.c_ubyte)))
        result = self.Blob()
        try:
            if protect:
                ok = self.crypt.CryptProtectData(
                    ctypes.byref(source),
                    'Recheck credential',
                    ctypes.byref(extra),
                    None,
                    None,
                    1,
                    ctypes.byref(result),
                )
            else:
                ok = self.crypt.CryptUnprotectData(
                    ctypes.byref(source),
                    None,
                    ctypes.byref(extra),
                    None,
                    None,
                    1,
                    ctypes.byref(result),
                )
            if not ok:
                raise AuthError(
                    'credential_protection',
                    'Windows 사용자 보안 저장소에서 자격증명을 보호하거나 확인하지 못했습니다.',
                )
            return ctypes.string_at(result.pbData, result.cbData)
        finally:
            for buffer in buffers:
                ctypes.memset(buffer, 0, ctypes.sizeof(buffer))
            if result.pbData:
                self.kernel.LocalFree(ctypes.cast(result.pbData, ctypes.c_void_p))

    def protect(self, data, entropy):
        return self._run(data, entropy, True)

    def unprotect(self, data, entropy):
        return self._run(data, entropy, False)


@dataclass(frozen=True)
class CredentialRecord:
    ref: str
    owner: str
    origin: str
    kind: str
    version: int
    value: SecretStr


_MUTEXES = {}
_MUTEX_GUARD = threading.Lock()
_LOCAL = threading.local()


class CredentialStore:
    def __init__(self, root, *, protector=None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.protector = protector if protector is not None else DPAPIProtector()

    @staticmethod
    def _owner(owner):
        if (
            not isinstance(owner, str)
            or not 1 <= len(owner) <= 200
            or any(c in owner for c in '\r\n\0')
        ):
            raise AuthError('credential_owner', '자격증명 소유 프로필을 확인하세요.')
        return owner

    @staticmethod
    def _reference(ref):
        if not isinstance(ref, str) or not re.fullmatch(r'cred_[a-f0-9]{32}', ref):
            raise AuthError('credential_reference', '자격증명 참조 형식이 잘못되었습니다.')
        return ref

    @staticmethod
    def _reparse(path):
        if path.exists() and (
            path.is_symlink() or getattr(path.lstat(), 'st_file_attributes', 0) & 0x400
        ):
            raise AuthError(
                'credential_path', '보안 저장소 파일의 링크 또는 재분석 경로는 사용할 수 없습니다.'
            )

    @contextmanager
    def transaction(self, ref, owner, *, timeout=10):
        self._owner(owner)
        slot = self._reference(ref) if ref is not None else 'store'
        if not isinstance(timeout, (int, float)) or timeout < 0 or timeout > 60:
            raise AuthError('credential_lock', '보안 저장소 잠금 시간을 확인하세요.')
        key = (str(self.root), slot)
        with _MUTEX_GUARD:
            mutex = _MUTEXES.setdefault(key, threading.RLock())
        try:
            task = asyncio.current_task()
        except RuntimeError:
            task = None
        depth_key = (key, id(task) if task is not None else None)
        depths = getattr(_LOCAL, 'depths', None)
        if depths is None:
            depths = {}
            _LOCAL.depths = depths
        if depths.get(depth_key, 0):
            depths[depth_key] += 1
            try:
                yield
            finally:
                depths[depth_key] -= 1
            return
        deadline = time.monotonic() + timeout
        if not mutex.acquire(timeout=timeout):
            raise AuthError('credential_busy', '다른 작업이 보안 저장소를 사용 중입니다.')
        stream = None
        locked = False
        try:
            folder = self.root / '.locks'
            folder.mkdir(exist_ok=True)
            self._reparse(folder)
            path = folder / (slot + '.lock')
            self._reparse(path)
            stream = path.open('a+b')
            if stream.seek(0, 2) == 0:
                stream.write(b'\0')
                stream.flush()
            while True:
                try:
                    stream.seek(0)
                    if sys.platform == 'win32':
                        import msvcrt

                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise AuthError(
                            'credential_busy', '다른 작업이 보안 저장소를 사용 중입니다.'
                        ) from None
                    time.sleep(min(0.02, max(0, deadline - time.monotonic())))
            depths[depth_key] = 1
            yield
        finally:
            depths.pop(depth_key, None)
            if stream is not None:
                if locked:
                    stream.seek(0)
                    if sys.platform == 'win32':
                        import msvcrt

                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                stream.close()
            mutex.release()

    @staticmethod
    def _entropy(meta):
        bound = {key: meta[key] for key in ('ref', 'owner', 'origin', 'kind', 'version')}
        return hashlib.sha256(
            b'Rebuild-user-credential-v1\0'
            + json.dumps(bound, sort_keys=True, separators=(',', ':')).encode('utf-8')
        ).digest()

    def _outer(self, ref, owner):
        path = self.root / (self._reference(ref) + '.json')
        self._reparse(path)
        try:
            if path.stat().st_size > 2 * 1024 * 1024:
                raise ValueError()
            meta = json.loads(path.read_text(encoding='utf-8'))
            if (
                meta.get('schema') != 1
                or meta.get('ref') != ref
                or meta.get('owner') != owner
                or meta.get('protection') != 'windows-user-dpapi'
                or isinstance(meta.get('version'), bool)
                or not isinstance(meta.get('version'), int)
                or meta['version'] < 1
            ):
                raise ValueError()
            return meta
        except FileNotFoundError:
            raise AuthError('credential_missing', '저장된 자격증명을 찾지 못했습니다.') from None
        except (OSError, ValueError, TypeError):
            raise AuthError(
                'credential_binding', '자격증명 소유권 또는 보호 기록을 확인하지 못했습니다.'
            ) from None

    def _read(self, ref, owner, origin=None):
        meta = self._outer(ref, owner)
        if origin is not None and meta.get('origin') != origin:
            raise AuthError('credential_binding', '자격증명의 인증 대상이 현재 프로필과 다릅니다.')
        try:
            plain = self.protector.unprotect(
                base64.b64decode(meta['ciphertext'], validate=True), self._entropy(meta)
            )
            decoded = json.loads(plain.decode('utf-8'))
            expected = {
                key: meta[key]
                for key in ('ref', 'owner', 'origin', 'kind', 'version', 'created', 'updated')
            }
            if decoded.get('metadata') != expected or not isinstance(decoded.get('value'), str):
                raise ValueError()
            return meta, decoded['value']
        except Exception:
            raise AuthError(
                'credential_protection',
                '자격증명 보호 기록을 확인하지 못했습니다. 평문 저장으로 전환하지 않습니다.',
            ) from None

    def put(self, owner, origin, kind, value, ref=None, expected_version=None):
        self._owner(owner)
        origin = canonical_origin(origin)
        if not isinstance(kind, str) or not re.fullmatch(r'[a-z][a-z0-9_.-]{0,63}', kind):
            raise AuthError('credential_kind', '자격증명 종류를 확인하세요.')
        try:
            secret = (
                json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
                if isinstance(value, dict)
                else value.get_secret_value() if isinstance(value, SecretStr) else value
            )
            if (
                not isinstance(secret, str)
                or not 1 <= len(secret.encode('utf-8')) <= 512 * 1024
                or '\0' in secret
            ):
                raise ValueError()
        except Exception:
            raise AuthError(
                'credential_value', '보호할 자격증명 값의 형식이나 크기를 확인하세요.'
            ) from None
        ref = self._reference(ref) if ref is not None else 'cred_' + uuid.uuid4().hex
        with self.transaction(ref, owner):
            path = self.root / (ref + '.json')
            self._reparse(path)
            previous = None
            if path.exists():
                previous, _ = self._read(ref, owner, origin)
                if previous['kind'] != kind:
                    raise AuthError(
                        'credential_binding', '다른 인증 종류의 저장 기록을 덮어쓸 수 없습니다.'
                    )
            current = previous['version'] if previous else 0
            if expected_version is not None and (
                isinstance(expected_version, bool) or expected_version != current
            ):
                raise AuthError(
                    'credential_conflict',
                    '다른 작업에서 자격증명이 변경되었습니다. 최신 상태를 다시 읽으세요.',
                )
            stamp = time.time()
            meta = dict(
                ref=ref,
                owner=owner,
                origin=origin,
                kind=kind,
                version=current + 1,
                created=previous['created'] if previous else stamp,
                updated=stamp,
            )
            try:
                payload = json.dumps(
                    dict(metadata=meta, value=secret), ensure_ascii=False, separators=(',', ':')
                ).encode('utf-8')
                cipher = self.protector.protect(payload, self._entropy(meta))
                envelope = dict(
                    schema=1,
                    protection='windows-user-dpapi',
                    **meta,
                    ciphertext=base64.b64encode(cipher).decode('ascii'),
                )
            except Exception:
                raise AuthError(
                    'credential_protection',
                    '자격증명을 암호화하지 못했습니다. 평문 저장으로 전환하지 않습니다.',
                ) from None
            temporary = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
            try:
                with temporary.open('x', encoding='utf-8') as stream:
                    json.dump(envelope, stream, ensure_ascii=False, separators=(',', ':'))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            except OSError:
                raise AuthError(
                    'credential_storage',
                    '보호된 자격증명 저장에 실패했습니다. 이전 기록을 유지합니다.',
                ) from None
            finally:
                temporary.unlink(missing_ok=True)
            return dict(ref=ref, version=meta['version'])

    def get(self, ref, owner, origin):
        self._owner(owner)
        origin = canonical_origin(origin)
        with self.transaction(ref, owner):
            meta, value = self._read(ref, owner, origin)
            return CredentialRecord(
                ref, owner, origin, meta['kind'], meta['version'], SecretStr(value)
            )

    def metadata(self, ref, owner):
        self._owner(owner)
        with self.transaction(ref, owner):
            meta, _ = self._read(ref, owner)
            return {
                key: meta[key]
                for key in (
                    'ref',
                    'owner',
                    'origin',
                    'kind',
                    'version',
                    'created',
                    'updated',
                    'protection',
                )
            }

    def delete(self, ref, owner):
        self._owner(owner)
        with self.transaction(ref, owner):
            path = self.root / (ref + '.json')
            if not path.exists():
                return False
            self._read(ref, owner)
            try:
                path.unlink()
            except OSError:
                raise AuthError(
                    'credential_storage', '보호된 자격증명을 삭제하지 못했습니다.'
                ) from None
            return True
