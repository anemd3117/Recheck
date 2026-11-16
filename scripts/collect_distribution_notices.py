"""Collect exact dependency notices and corresponding Qt sources for a portable build.

No installed paths are written to the distributable manifest. Archive members are
read as data; no archive is extracted with extractall and no downloaded code runs.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / 'resources/distribution/notice-sources.json'
DEFAULT_CACHE = ROOT / 'verification/portable-prerequisites-20261116/packaging/source-cache'
REVIEWED_QT_LIBRARIES = frozenset(('qt6core.dll', 'qt6gui.dll', 'qt6widgets.dll', 'qt6svg.dll'))
REVIEWED_QT_PLUGINS = frozenset((
    'platforms/qwindows.dll', 'platforms/qoffscreen.dll', 'platforms/qminimal.dll',
    'styles/qmodernwindowsstyle.dll', 'iconengines/qsvgicon.dll',
    'imageformats/qsvg.dll', 'imageformats/qgif.dll', 'imageformats/qico.dll',
    'imageformats/qjpeg.dll',
))


def reviewed_qt_binary(filename):
    """Keep Qt scope within qtbase/qtsvg and avoid incidental host TLS libraries."""
    filename = str(filename).replace('\\', '/').casefold()
    basename = filename.rsplit('/', 1)[-1]
    if basename in ('opengl32sw.dll', 'qtnetwork.pyd'):
        return False
    if basename.startswith('qt6') and basename.endswith('.dll'):
        return basename in REVIEWED_QT_LIBRARIES
    if filename.startswith('pyside6/plugins/'):
        return filename.removeprefix('pyside6/plugins/') in REVIEWED_QT_PLUGINS
    if filename.startswith('pyside6/translations/'):
        return False
    return True


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def safe_relative(value):
    path = PurePosixPath(str(value).replace('\\', '/'))
    if path.is_absolute() or any(part in ('..', '') for part in path.parts) or ':' in str(path):
        raise ValueError('Unsafe source/member path: ' + str(value))
    return path


def _notice_name(name):
    name = name.casefold()
    return any(term in name for term in ('license', 'copying', 'notice', 'copyright')) or name in (
        'authors', 'authors.txt', 'qt_attributions.json', 'copyrights',
    )


def _source_rows(manifest, cache):
    if manifest.get('version') != 1 or not manifest.get('sources'):
        raise ValueError('Missing corresponding-source manifest')
    rows = []
    seen = set()
    for row in manifest['sources']:
        filename = safe_relative(row.get('filename', ''))
        if len(filename.parts) != 1 or filename.name in seen:
            raise ValueError('Unsafe or duplicate source filename')
        seen.add(filename.name)
        if not re.fullmatch(r'[a-z0-9-]+', row.get('component', '')):
            raise ValueError('Unsafe source component path')
        if not re.fullmatch(r'[0-9a-f]{64}', row.get('sha256', '')):
            raise ValueError('Missing source checksum')
        if type(row.get('size_bytes')) is not int or row['size_bytes'] <= 0:
            raise ValueError('Missing source size')
        source = cache / filename.name
        if source.is_symlink() or not source.is_file():
            raise ValueError('Missing source archive: ' + filename.name)
        if source.stat().st_size != row['size_bytes'] or digest(source) != row['sha256']:
            raise ValueError('Source archive size/checksum mismatch: ' + filename.name)
        notices = []
        with tarfile.open(source, 'r:*') as stream:
            for member in stream:
                relative = safe_relative(member.name)
                third_party_readme = relative.name.casefold().startswith('readme') and any(
                    part.casefold() in ('3rdparty', 'third_party', 'vendor', 'external') for part in relative.parts
                )
                if not _notice_name(relative.name) and 'LICENSES' not in relative.parts and not third_party_readme:
                    continue
                if not member.isfile() or member.size > 8 * 1024 * 1024:
                    if member.isdir():
                        continue
                    raise ValueError('Unsafe license archive member: ' + member.name)
                notices.append((relative, stream.extractfile(member).read()))
        if not notices:
            raise ValueError('No notices found in source archive: ' + filename.name)
        rows.append((row, source, notices))
    return rows


def fetch_sources(cache=DEFAULT_CACHE, manifest_path=DEFAULT_MANIFEST):
    """Download pinned archives only, with bounded size and checksum validation."""
    cache = Path(cache).resolve()
    manifest = json.loads(Path(manifest_path).read_text(encoding='utf-8'))
    cache.mkdir(parents=True, exist_ok=True)
    for row in manifest['sources']:
        filename = safe_relative(row['filename'])
        if len(filename.parts) != 1 or not row['url'].startswith('https://'):
            raise ValueError('Unsafe source filename/URL')
        target = cache / filename.name
        if target.is_file() and target.stat().st_size == row['size_bytes'] and digest(target) == row['sha256']:
            continue
        if target.exists():
            raise ValueError('Existing source cache entry has a wrong checksum: ' + filename.name)
        partial = target.with_name(target.name + '.partial')
        count = 0
        partial_created = False
        try:
            with urllib.request.urlopen(row['url'], timeout=45) as response, partial.open('xb') as out:
                partial_created = True
                while chunk := response.read(1024 * 1024):
                    count += len(chunk)
                    if count > row['size_bytes']:
                        raise ValueError('Downloaded source exceeds pinned size')
                    out.write(chunk)
            if count != row['size_bytes'] or digest(partial) != row['sha256']:
                raise ValueError('Downloaded source size/checksum mismatch')
            partial.rename(target)
        finally:
            if partial_created and partial.exists():
                partial.unlink()
    _source_rows(manifest, cache)
    return cache


def collect_notices(staged, *, source_cache=DEFAULT_CACHE, root=ROOT,
                    manifest_path=DEFAULT_MANIFEST, python_prefix=None, distributions=None):
    staged, root, cache = Path(staged).resolve(strict=True), Path(root).resolve(), Path(source_cache).resolve()
    if staged == root or not (staged / 'Recheck.exe').is_file():
        raise ValueError('A separate staged Recheck executable is required')
    if (staged / 'licenses').exists() or (staged / 'third-party-sources').exists():
        raise ValueError('Use a fresh staging folder; existing license/source folders are not merged')
    internal = staged / '_internal'
    for path in internal.rglob('*'):
        if path.is_file() and not reviewed_qt_binary(path.relative_to(internal).as_posix()):
            raise ValueError('Unreviewed optional renderer/Qt component scope: ' + path.name)
    manifest = json.loads(Path(manifest_path).read_text(encoding='utf-8'))
    sources = _source_rows(manifest, cache)
    prefix = Path(python_prefix) if python_prefix is not None else Path(sys.base_prefix)
    python_license = prefix / 'LICENSE.txt'
    if not python_license.is_file():
        raise ValueError('Missing original CPython license')
    if distributions is None:
        names = [line.split('==')[0] for line in (root / 'requirements-lock.txt').read_text().splitlines()
                 if line.strip() and not line.startswith('#')]
        distributions = [importlib.metadata.distribution(name) for name in names]
    packages = []
    copies = [(python_license, Path('licenses/CPython/LICENSE.txt'))]
    for dist in distributions:
        name, version = dist.metadata['Name'], dist.version
        component = re.sub(r'[-_.]+', '-', name).casefold() + '-' + version
        if not re.fullmatch(r'[a-z0-9.+-]+', component):
            raise ValueError('Unsafe package notice path')
        files = []
        for value in dist.files or []:
            if not _notice_name(PurePosixPath(str(value)).name):
                continue
            relative = safe_relative(value)
            if _notice_name(relative.name):
                source = Path(dist.locate_file(value))
                if source.is_symlink() or not source.is_file():
                    raise ValueError('Missing original package license: ' + name)
                destination = Path('licenses/packages') / component / Path(relative)
                copies.append((source, destination))
                files.append(destination.as_posix())
        if not files:
            raise ValueError('No original license/notice found for package: ' + name)
        packages.append(dict(name=name, version=version, files=files))
    required_versions = manifest.get('build_versions', {})
    actual_versions = {package['name'].casefold(): package['version'] for package in packages}
    actual_versions['cpython'] = '.'.join(map(str, sys.version_info[:3]))
    for name, version in required_versions.items():
        if actual_versions.get(name.casefold()) != version:
            raise ValueError('Source/build component version differs: ' + name)
    for name in ('LICENSE', 'THIRD_PARTY_NOTICES.md'):
        source = root / name
        if not source.is_file():
            raise ValueError('Missing reviewed distribution notice: ' + name)
        copies.append((source, Path(name)))
    copies.append((root / 'docs/BINARY_DISTRIBUTION.md', Path('licenses/REPLACEMENT.md')))
    notice_root = root / 'resources/distribution/notices'
    for row in manifest.get('notice_files', []):
        relative = safe_relative(row['filename'])
        source = notice_root / Path(relative)
        if not source.is_file() or digest(source) != row['sha256']:
            raise ValueError('Original extra notice missing/checksum mismatch: ' + str(relative))
        copies.append((source, Path('licenses/upstream') / Path(relative)))
    for source, destination in copies:
        if not source.is_file():
            raise ValueError('Missing notice: ' + str(destination))
    for source, destination in copies:
        target = staged / destination
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    for row, source, notices in sources:
        target = staged / 'third-party-sources' / row['filename']
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        for relative, payload in notices:
            target = staged / 'licenses' / row['component'] / Path(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
    files = [dict(path=path.relative_to(staged).as_posix(), size_bytes=path.stat().st_size,
                  sha256=digest(path)) for folder in ('licenses', 'third-party-sources')
             for path in sorted((staged / folder).rglob('*')) if path.is_file()]
    files += [dict(path=name, size_bytes=(staged / name).stat().st_size,
                   sha256=digest(staged / name)) for name in ('LICENSE', 'THIRD_PARTY_NOTICES.md')]
    record = dict(version=1, cpython_version=actual_versions['cpython'], packages=packages,
                  sources=manifest['sources'], files=files,
                  library_replacement='REPLACEMENT.md',
                  review_scope='portable-application-only',
                  bundled_environment_images=(staged / 'environment').exists())
    (staged / 'licenses/manifest.json').write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    verify_notices(staged)
    return record


def verify_notices(staged):
    staged = Path(staged).resolve(strict=True)
    marker = staged / 'licenses/manifest.json'
    if not marker.is_file():
        raise ValueError('Missing distribution license/source inventory')
    record = json.loads(marker.read_text(encoding='utf-8'))
    if record.get('version') != 1 or not record.get('files') or not record.get('sources'):
        raise ValueError('Missing license/source inventory')
    expected = set()
    for row in record['files']:
        relative = safe_relative(row['path'])
        if relative.parts[0] not in ('licenses', 'third-party-sources') and relative.as_posix() not in ('LICENSE', 'THIRD_PARTY_NOTICES.md'):
            raise ValueError('Unsafe license inventory path')
        path = staged / Path(relative)
        expected.add(relative.as_posix())
        if not path.is_file() or path.is_symlink():
            raise ValueError('Missing license/source inventory file: ' + str(relative))
        if path.stat().st_size != row['size_bytes'] or digest(path) != row['sha256']:
            raise ValueError('License/source inventory checksum mismatch: ' + str(relative))
    actual = {path.relative_to(staged).as_posix() for folder in ('licenses', 'third-party-sources')
              for path in (staged / folder).rglob('*') if path.is_file() and path != marker}
    actual.update(name for name in ('LICENSE', 'THIRD_PARTY_NOTICES.md') if (staged / name).is_file())
    if actual != expected:
        raise ValueError('License/source inventory differs')
    return True


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage')
    parser.add_argument('--source-cache', default=str(DEFAULT_CACHE))
    parser.add_argument('--fetch-sources', action='store_true')
    parser.add_argument('--verify-only', action='store_true')
    args = parser.parse_args()
    if args.fetch_sources:
        fetch_sources(args.source_cache)
    if args.stage:
        if args.verify_only:
            verify_notices(args.stage)
        else:
            collect_notices(args.stage, source_cache=args.source_cache)
        print('Distribution notices and corresponding-source checks passed.')
