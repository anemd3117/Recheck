"""Real staged files, source archives and licenses stay checksum bound to a release."""

import hashlib
import importlib
import io
import json
from pathlib import Path
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
