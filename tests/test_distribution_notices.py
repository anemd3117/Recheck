"""Real staged files, source archives and licenses stay checksum bound to a release."""

import hashlib
import importlib
import io
import json
from pathlib import Path
import shutil
import sys
import tarfile
from types import SimpleNamespace

import pytest


def notice_module():
    assert importlib.util.find_spec('scripts.collect_distribution_notices'), 'license collector missing'
    return importlib.import_module('scripts.collect_distribution_notices')


@pytest.fixture
def inputs(tmp_path):
    stage = tmp_path / 'stage'
    (stage / '_internal/PySide6').mkdir(parents=True)
    (stage / 'Recheck.exe').write_bytes(b'MZ executable fixture')
    (stage / '_internal/PySide6/Qt6Core.dll').write_bytes(b'replaceable Qt fixture')
    root = tmp_path / 'source'
    (root / 'docs').mkdir(parents=True)
    (root / 'LICENSE').write_text('Recheck rights; third-party rights preserved')
    (root / 'THIRD_PARTY_NOTICES.md').write_text('Third-party notices')
    (root / 'docs/BINARY_DISTRIBUTION.md').write_text('Replacement instructions')
    prefix = tmp_path / 'python'
    prefix.mkdir()
    (prefix / 'LICENSE.txt').write_text('Python runtime license')
    cache = tmp_path / 'cache'
    cache.mkdir()
    archive = cache / 'qtbase-everywhere-src-6.12.0.tar.xz'
    with tarfile.open(archive, 'w:xz') as stream:
        payload = b'LGPL text preserved byte for byte\n'
        info = tarfile.TarInfo('qtbase-6.12.0/LICENSES/LGPL-3.0-only.txt')
        info.size = len(payload)
        stream.addfile(info, io.BytesIO(payload))
        attribution = b'Upstream third-party copyright and distribution conditions\n'
        info = tarfile.TarInfo('qtbase-6.12.0/src/3rdparty/jpeg/README.md')
        info.size = len(attribution)
        stream.addfile(info, io.BytesIO(attribution))
        patent = b'Original codec patent grant and conditions\n'
        info = tarfile.TarInfo('qtbase-6.12.0/src/3rdparty/codec/PATENTS')
        info.size = len(patent)
        stream.addfile(info, io.BytesIO(patent))
    manifest = root / 'source-manifest.json'
    manifest.write_text(json.dumps({'version': 1, 'sources': [{
        'component': 'qtbase', 'version': '6.12.0', 'filename': archive.name,
        'url': 'https://download.qt.io/example/' + archive.name,
        'sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
        'size_bytes': archive.stat().st_size,
    }]}))
    pkgroot = tmp_path / 'packages'
    package_license = Path('fixture-1.0.dist-info/licenses/LICENSE')
    (pkgroot / package_license).parent.mkdir(parents=True)
    (pkgroot / package_license).write_text('Package copyright and license')
    dist = SimpleNamespace(
        metadata={'Name': 'fixture', 'Version': '1.0', 'License': 'MIT'},
        version='1.0', files=[package_license],
        locate_file=lambda path: pkgroot / path,
    )
    return SimpleNamespace(stage=stage, root=root, prefix=prefix, cache=cache,
                           manifest=manifest, dist=dist, archive=archive)


def collect(m, values):
    return m.collect_notices(values.stage, source_cache=values.cache, root=values.root,
                             manifest_path=values.manifest, python_prefix=values.prefix,
                             distributions=[values.dist])


def test_new_distribution_preserves_python_package_notices_and_exact_qt_source(inputs):
    m = notice_module()
    collect(m, inputs)
    assert (inputs.stage / 'licenses/CPython/LICENSE.txt').read_text() == 'Python runtime license'
    assert (inputs.stage / 'licenses/packages/fixture-1.0/fixture-1.0.dist-info/licenses/LICENSE').read_text() == 'Package copyright and license'
    assert (inputs.stage / 'third-party-sources' / inputs.archive.name).read_bytes() == inputs.archive.read_bytes()
    assert (inputs.stage / 'licenses/qtbase/qtbase-6.12.0/LICENSES/LGPL-3.0-only.txt').read_bytes() == b'LGPL text preserved byte for byte\n'
    assert (inputs.stage / 'licenses/REPLACEMENT.md').read_text() == 'Replacement instructions'
    assert m.verify_notices(inputs.stage) is True
    serialized = (inputs.stage / 'licenses/manifest.json').read_text()
    assert str(inputs.prefix) not in serialized
    assert str(inputs.root) not in serialized


def test_third_party_readme_notices_are_preserved_even_when_not_named_license(inputs):
    m = notice_module()
    collect(m, inputs)
    assert (inputs.stage / 'licenses/qtbase/qtbase-6.12.0/src/3rdparty/jpeg/README.md').read_bytes() == b'Upstream third-party copyright and distribution conditions\n'


def test_third_party_codec_patent_terms_are_preserved(inputs):
    m = notice_module()
    collect(m, inputs)
    assert (inputs.stage / 'licenses/qtbase/qtbase-6.12.0/src/3rdparty/codec/PATENTS').read_bytes() == b'Original codec patent grant and conditions\n'


def test_wrong_source_archive_is_rejected_before_license_tree_is_written(inputs):
    m = notice_module()
    inputs.archive.write_bytes(b'tampered source')
    with pytest.raises(ValueError, match='checksum|size'):
        collect(m, inputs)
    assert not (inputs.stage / 'licenses').exists()


def test_source_filename_cannot_escape_cache_or_stage(inputs):
    m = notice_module()
    data = json.loads(inputs.manifest.read_text())
    data['sources'][0]['filename'] = '../credentials/private.tar.xz'
    inputs.manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='filename|path'):
        collect(m, inputs)
    assert not (inputs.stage / 'licenses').exists()


def test_source_license_member_cannot_escape_stage(inputs):
    m = notice_module()
    with tarfile.open(inputs.archive, 'w:xz') as stream:
        item = tarfile.TarInfo('../../LICENSE')
        item.size = 3
        stream.addfile(item, io.BytesIO(b'bad'))
    data = json.loads(inputs.manifest.read_text())
    data['sources'][0]['sha256'] = hashlib.sha256(inputs.archive.read_bytes()).hexdigest()
    data['sources'][0]['size_bytes'] = inputs.archive.stat().st_size
    inputs.manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='member|path'):
        collect(m, inputs)
    assert not (inputs.stage.parent / 'LICENSE').exists()


def test_notice_tamper_after_collection_is_detected(inputs):
    m = notice_module()
    collect(m, inputs)
    (inputs.stage / 'licenses/CPython/LICENSE.txt').write_text('wrong license')
    with pytest.raises(ValueError, match='checksum|inventory'):
        m.verify_notices(inputs.stage)


def test_corresponding_source_removal_is_detected(inputs):
    m = notice_module()
    collect(m, inputs)
    (inputs.stage / 'third-party-sources' / inputs.archive.name).unlink()
    with pytest.raises(ValueError, match='missing|inventory'):
        m.verify_notices(inputs.stage)


def test_unreviewed_qt_renderer_is_rejected(inputs):
    m = notice_module()
    (inputs.stage / '_internal/PySide6/opengl32sw.dll').write_bytes(b'Mesa fixture')
    with pytest.raises(ValueError, match='renderer|OpenGL|scope'):
        collect(m, inputs)


@pytest.mark.parametrize('filename', ['Qt6Pdf.dll', 'plugins/imageformats/qwebp.dll',
                                      'translations/qtbase_ko.qm'])
def test_unreviewed_qt_module_plugin_or_translation_is_rejected(inputs, filename):
    m = notice_module()
    path = inputs.stage / '_internal/PySide6' / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'unreviewed component fixture')
    with pytest.raises(ValueError, match='scope'):
        collect(m, inputs)


def test_package_without_any_license_cannot_be_silently_certified(inputs):
    m = notice_module()
    inputs.dist.files = []
    with pytest.raises(ValueError, match='license|notice'):
        collect(m, inputs)


def test_application_license_tamper_is_detected_with_dependency_notices(inputs):
    m = notice_module()
    collect(m, inputs)
    (inputs.stage / 'LICENSE').write_text('wrong application terms')
    with pytest.raises(ValueError, match='checksum|inventory'):
        m.verify_notices(inputs.stage)


def test_source_fetch_does_not_delete_an_existing_partial_file(inputs, monkeypatch):
    m = notice_module()
    payload = inputs.archive.read_bytes()
    inputs.archive.unlink()
    partial = inputs.archive.with_name(inputs.archive.name + '.partial')
    partial.write_bytes(b'another download owns this file')
    monkeypatch.setattr(m.urllib.request, 'urlopen', lambda *a, **k: io.BytesIO(payload))
    with pytest.raises(FileExistsError):
        m.fetch_sources(inputs.cache, inputs.manifest)
    assert partial.read_bytes() == b'another download owns this file'


def test_source_fetch_verifies_bytes_before_publishing_the_cache_entry(inputs, monkeypatch):
    m = notice_module()
    payload = inputs.archive.read_bytes()
    inputs.archive.unlink()
    monkeypatch.setattr(m.urllib.request, 'urlopen', lambda *a, **k: io.BytesIO(payload))
    m.fetch_sources(inputs.cache, inputs.manifest)
    assert inputs.archive.read_bytes() == payload
    assert not inputs.archive.with_name(inputs.archive.name + '.partial').exists()


@pytest.mark.parametrize('filename', [
    'Qt6Network.dll', 'Qt6WebEngineCore.dll', 'Qt6WebEngineWidgets.dll',
    'Qt6Positioning.dll', 'Qt6Qml.dll', 'Qt6Quick.dll', 'Qt6WebChannel.dll',
    'Qt6PrintSupport.dll', 'Qt6QuickWidgets.dll', 'Qt6OpenGL.dll',
    'Qt6QmlMeta.dll', 'Qt6QmlModels.dll', 'Qt6QmlWorkerScript.dll',
    'QtNetwork.pyd', 'QtWebEngineCore.pyd', 'QtWebEngineWidgets.pyd',
    'QtWebEngineProcess.exe', 'qt.conf',
    'plugins/tls/qschannelbackend.dll', 'plugins/tls/qcertonlybackend.dll',
    'resources/icudtl.dat', 'resources/qtwebengine_resources.pak',
    'resources/qtwebengine_resources_100p.pak',
    'resources/qtwebengine_resources_200p.pak',
    'resources/qtwebengine_devtools_resources.pak',
    'resources/v8_context_snapshot.bin',
    'translations/qtwebengine_locales/en-US.pak',
    'translations/qtwebengine_locales/ko.pak',
])
def test_browser_runtime_survives_binary_review(filename):
    assert notice_module().reviewed_qt_binary('PySide6/' + filename)


@pytest.mark.parametrize('filename', [
    'Qt6Pdf.dll', 'Qt6Charts.dll', 'QtPdf.pyd',
    'QtWebEngineQuick.pyd', 'plugins/tls/qopensslbackend.dll',
    'resources/v8_context_snapshot.debug.bin',
    'resources/qtwebengine_resources.debug.pak',
    'resources/unknown.pak', 'translations/qtwebengine_locales/unknown.pak',
    'translations/qtwebengine_locales/ko.debug.pak',
    'translations/qtwebengine_locales/en-US.pak/extra',
    'qml/QtQuick/qtquick2plugin.dll',
])
def test_unreviewed_browser_payload_still_fails_binary_review(filename):
    assert not notice_module().reviewed_qt_binary('PySide6/' + filename)


def browser_manifest(values):
    data = json.loads(values.manifest.read_text())
    data['qt_runtime'] = {'files': {
        'PySide6/QtWebEngineProcess.exe': 'qtbase',
        'PySide6/resources/icudtl.dat': 'qtbase',
    }}
    values.manifest.write_text(json.dumps(data))
    for name in data['qt_runtime']['files']:
        target = values.stage / '_internal' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'reviewed runtime fixture')
    return data


def test_missing_browser_helper_cannot_be_certified(inputs):
    m = notice_module()
    browser_manifest(inputs)
    (inputs.stage / '_internal/PySide6/QtWebEngineProcess.exe').unlink()
    with pytest.raises(ValueError, match='runtime|missing'):
        collect(m, inputs)
    assert not (inputs.stage / 'licenses').exists()


def test_runtime_without_its_corresponding_source_cannot_be_certified(inputs):
    m = notice_module()
    data = browser_manifest(inputs)
    data['qt_runtime']['files']['PySide6/QtWebEngineProcess.exe'] = 'qtwebengine'
    inputs.manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='source|component'):
        collect(m, inputs)
    assert not (inputs.stage / 'licenses').exists()


def test_collected_runtime_scope_is_rechecked_before_publication(inputs):
    m = notice_module()
    browser_manifest(inputs)
    collect(m, inputs)
    (inputs.stage / '_internal/PySide6/resources/icudtl.dat').unlink()
    with pytest.raises(ValueError, match='runtime|missing'):
        m.verify_notices(inputs.stage)


def test_unreviewed_qt_file_added_after_collection_is_rejected(inputs):
    m = notice_module()
    collect(m, inputs)
    (inputs.stage / '_internal/PySide6/Qt6Pdf.dll').write_bytes(b'unreviewed runtime')
    with pytest.raises(ValueError, match='scope'):
        m.verify_notices(inputs.stage)


def test_browser_binary_checksum_is_rechecked_before_publication(inputs):
    m = notice_module()
    data = browser_manifest(inputs)
    data['qt_runtime']['sha256'] = {
        'PySide6/QtWebEngineProcess.exe': hashlib.sha256(b'reviewed runtime fixture').hexdigest(),
    }
    inputs.manifest.write_text(json.dumps(data))
    collect(m, inputs)
    (inputs.stage / '_internal/PySide6/QtWebEngineProcess.exe').write_bytes(b'other browser build')
    with pytest.raises(ValueError, match='checksum|runtime'):
        m.verify_notices(inputs.stage)


def test_host_tls_libraries_are_dropped_but_cpython_tls_is_preserved(tmp_path):
    m = notice_module()
    prefix = tmp_path / 'python'
    own = prefix / 'DLLs/libssl-4.dll'
    host = tmp_path / 'another-app/libssl-3-x64.dll'
    package = prefix / 'Lib/site-packages/other/libcrypto-3.dll'
    for source in (own, host, package):
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b'TLS fixture')
    assert m.reviewed_binary_entry(('libssl-4.dll', str(own), 'BINARY'), python_prefix=prefix)
    assert not m.reviewed_binary_entry(('libssl-3-x64.dll', str(host), 'BINARY'), python_prefix=prefix)
    assert not m.reviewed_binary_entry(('libcrypto-3.dll', str(package), 'BINARY'), python_prefix=prefix)


@pytest.fixture
def cpython_ssl_files(inputs):
    if sys.platform != 'win32':
        pytest.skip('The frozen Windows build uses native CPython PE dependencies')
    native_ssl = Path(importlib.util.find_spec('_ssl').origin)
    native_prefix = Path(sys.base_prefix).resolve()
    assert native_ssl.parent in (native_prefix, native_prefix / 'DLLs')
    # Real _ssl PE imports, controlled CPython/host DLL payloads. The test is
    # about preserving source selection after PyInstaller's basename collision.
    folder = inputs.prefix / 'DLLs'
    folder.mkdir()
    shutil.copyfile(native_ssl, folder / '_ssl.pyd')
    import pefile
    with pefile.PE(str(native_ssl), fast_load=True) as binary:
        binary.parse_data_directories(directories=[1])
        names = [entry.dll.decode('ascii') for entry in binary.DIRECTORY_ENTRY_IMPORT
                 if entry.dll.lower().startswith((b'libssl-', b'libcrypto-'))]
    assert len(names) == 2
    for name in names:
        (folder / name).write_bytes(('CPython owns ' + name).encode())
    return inputs, folder, names


def test_cpython_ssl_dependencies_replace_a_host_basename_collision(cpython_ssl_files):
    values, folder, names = cpython_ssl_files
    host = values.root / 'other-app'
    host.mkdir()
    entries = [('_ssl.pyd', str(folder / '_ssl.pyd'), 'BINARY')]
    for name in names:
        (host / name).write_bytes(b'host TLS build')
        entries.append((name, str(host / name), 'BINARY'))
    # This mirrors the observed Analysis TOC: only the host row survives its
    # initial basename deduplication. Merely dropping it leaves _ssl unloadable.
    selected = notice_module().pin_cpython_ssl_binaries(entries, python_prefix=values.prefix)
    sources = {row[0]: Path(row[1]) for row in selected}
    assert sources['_ssl.pyd'] == folder / '_ssl.pyd'
    assert next(row[2] for row in selected if row[0] == '_ssl.pyd') == 'EXTENSION'
    for name in names:
        assert sources[name] == folder / name
        assert sources[name].read_bytes() == ('CPython owns ' + name).encode()


def test_missing_cpython_ssl_dependency_stops_packaging(cpython_ssl_files):
    values, folder, names = cpython_ssl_files
    (folder / names[0]).unlink()
    with pytest.raises(ValueError, match='CPython|dependency'):
        notice_module().pin_cpython_ssl_binaries([], python_prefix=values.prefix)


def cpython_tls_stage(values, folder, names):
    data = json.loads(values.manifest.read_text())
    data['build_versions'] = {'CPython': '.'.join(map(str, sys.version_info[:3]))}
    values.manifest.write_text(json.dumps(data))
    for name in ['_ssl.pyd', *names]:
        shutil.copyfile(folder / name, values.stage / '_internal' / name)


def test_missing_staged_cpython_ssl_dependency_is_rejected(cpython_ssl_files):
    values, folder, names = cpython_ssl_files
    cpython_tls_stage(values, folder, names)
    (values.stage / '_internal' / names[0]).unlink()
    with pytest.raises(ValueError, match='CPython|TLS|runtime'):
        collect(notice_module(), values)
    assert not (values.stage / 'licenses').exists()


def test_cpython_ssl_dependency_hashes_are_rechecked_for_publication(cpython_ssl_files):
    values, folder, names = cpython_ssl_files
    cpython_tls_stage(values, folder, names)
    m = notice_module()
    collect(m, values)
    (values.stage / '_internal' / names[0]).write_bytes(b'host TLS build')
    with pytest.raises(ValueError, match='CPython|TLS|runtime|checksum'):
        m.verify_notices(values.stage)
