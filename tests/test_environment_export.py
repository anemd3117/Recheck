"""Release archive publication checks; Docker I/O is mocked, file policy stays real."""

import importlib
import json
import hashlib
import subprocess
from pathlib import Path

import pytest

IDS = {
    'static': 'sha256:' + '1' * 64,
    'execution': 'sha256:' + '2' * 64,
    'ollama': 'sha256:' + '3' * 64,
}


@pytest.fixture
def source(tmp_path):
    data = {
        'version': 1,
        'release': 'test',
        'images': {
            c: {'ref': f'recheck-managed-{c}:1', 'image_id': v, 'platform': 'linux/amd64'}
            for c, v in IDS.items()
        },
    }
    path = tmp_path / 'source.json'
    path.write_text(json.dumps(data), encoding='utf-8')
    return path


@pytest.fixture
def exporter():
    assert importlib.util.find_spec(
        'scripts.export_managed_environment'
    ), 'Distribution exporter is not implemented'
    return importlib.import_module('scripts.export_managed_environment')


class Docker:
    def __init__(self, bad=None, fail_save=None):
        self.calls = []
        self.bad = bad
        self.fail_save = fail_save

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        component = next(c for c, v in IDS.items() if args[-1] == v)
        if args[1:3] == ['image', 'inspect']:
            value = {
                'Id': IDS[component],
                'Os': 'linux',
                'Architecture': 'amd64',
                'Config': {'Labels': {'org.recheck.component': component}},
            }
            if self.bad:
                self.bad(component, value)
            return subprocess.CompletedProcess(args, 0, json.dumps([value]), '')
        assert args[1:3] == ['image', 'save']
        if component == self.fail_save:
            raise subprocess.CalledProcessError(1, args, stderr='save failed')
        Path(args[args.index('--output') + 1]).write_bytes(
            (component + ' exported immutable image').encode()
        )
        return subprocess.CompletedProcess(args, 0, '', '')


def test_exports_required_images_by_immutable_id_and_records_real_hashes(
    exporter, source, tmp_path
):
    docker = Docker()
    original = source.read_bytes()
    out = tmp_path / 'delivery'
    result = exporter.export_environment(source, out, runner=docker)
    assert (
        result['images']['static']['archive']['sha256']
        == hashlib.sha256((out / 'recheck-managed-static-1.tar').read_bytes()).hexdigest()
    )
    assert source.read_bytes() == original
    saved = [c[-1] for c in docker.calls if c[1:3] == ['image', 'save']]
    assert saved == [IDS['static'], IDS['execution']]
    assert 'archive' not in result['images']['ollama']
    assert json.loads((out / 'environment-manifest.json').read_text()) == result


@pytest.mark.parametrize('mismatch', ['Id', 'Os', 'Architecture', 'label'])
def test_rejects_inspect_mismatch_before_any_save(exporter, source, tmp_path, mismatch):
    def bad(component, value):
        if component == 'execution':
            if mismatch == 'label':
                value['Config']['Labels']['org.recheck.component'] = 'unowned'
            else:
                value[mismatch] = 'wrong'

    docker = Docker(bad=bad)
    with pytest.raises((ValueError, RuntimeError), match='identity|platform|label|component'):
        exporter.export_environment(source, tmp_path / 'delivery', runner=docker)
    assert not any(c[1:3] == ['image', 'save'] for c in docker.calls)


def test_failed_second_save_does_not_publish_or_change_manifest(exporter, source, tmp_path):
    out = tmp_path / 'delivery'
    out.mkdir()
    existing = out / 'recheck-managed-static-1.tar'
    existing.write_bytes(b'previous release')
    old = {
        'version': 1,
        'images': {
            'static': {
                'archive': {
                    'filename': existing.name,
                    'sha256': hashlib.sha256(existing.read_bytes()).hexdigest(),
                }
            }
        },
    }
    marker = out / 'environment-manifest.json'
    marker.write_text(json.dumps(old))
    original = source.read_bytes()
    marker_original = marker.read_bytes()
    with pytest.raises(subprocess.CalledProcessError):
        exporter.export_environment(
            source, out, runner=Docker(fail_save='execution'), update_manifest=True
        )
    assert existing.read_bytes() == b'previous release'
    assert marker.read_bytes() == marker_original
    assert source.read_bytes() == original
    assert set(p.name for p in out.iterdir()) == {existing.name, marker.name}


def test_preserves_unowned_existing_filename(exporter, source, tmp_path):
    out = tmp_path / 'delivery'
    out.mkdir()
    existing = out / 'recheck-managed-static-1.tar'
    existing.write_bytes(b'user data')
    with pytest.raises((ValueError, RuntimeError), match='owned|existing'):
        exporter.export_environment(source, out, runner=Docker())
    assert existing.read_bytes() == b'user data'


def test_optional_ollama_and_explicit_manifest_update(exporter, source, tmp_path):
    docker = Docker()
    result = exporter.export_environment(
        source, tmp_path / 'delivery', runner=docker, include_ollama=True, update_manifest=True
    )
    updated = json.loads(source.read_text())
    assert all(result['images'][c]['archive']['size_bytes'] > 0 for c in IDS)
    assert all(updated['images'][c]['archive'] == result['images'][c]['archive'] for c in IDS)
    assert [c[-1] for c in docker.calls if c[1:3] == ['image', 'save']] == list(IDS.values())


def test_invalid_manifest_identity_cannot_reach_docker(exporter, source, tmp_path):
    value = json.loads(source.read_text())
    value['images']['static']['image_id'] = 'recheck-managed-static:1'
    source.write_text(json.dumps(value))
    docker = Docker()
    with pytest.raises(ValueError, match='immutable|identity|image'):
        exporter.export_environment(source, tmp_path / 'delivery', runner=docker)
    assert docker.calls == []


def test_publication_io_failure_restores_previous_owned_release(
    exporter, source, tmp_path, monkeypatch
):
    out = tmp_path / 'delivery'
    exporter.export_environment(source, out, runner=Docker())
    before = {path.name: path.read_bytes() for path in out.iterdir()}
    original_replace = exporter.os.replace
    failed = False

    def replace(src, dst):
        nonlocal failed
        if Path(dst).name == 'recheck-managed-execution-1.tar' and not failed:
            failed = True
            raise OSError('simulated archive publication failure')
        return original_replace(src, dst)

    monkeypatch.setattr(exporter.os, 'replace', replace)
    with pytest.raises(OSError, match='publication failure'):
        exporter.export_environment(source, out, runner=Docker())
    assert {path.name: path.read_bytes() for path in out.iterdir()} == before
