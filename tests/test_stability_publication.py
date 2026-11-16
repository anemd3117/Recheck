"""배포 게이트 실패 시 정상 배포를 유지하고 성공 시 이전 전체를 보관한다."""

import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    'publish_stability', Path(__file__).resolve().parents[1] / 'scripts/publish_stability.py'
)


def load_module():
    module = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(module)
    return module


def fixture(tmp_path):
    release = tmp_path / 'working' / 'verification' / 'release'
    staged = release / 'staged-dist' / 'Recheck'
    staged.mkdir(parents=True)
    (staged / 'Recheck.exe').write_bytes(b'new verified candidate')
    source = tmp_path / 'working' / 'recheck' / 'engine.py'
    source.parent.mkdir()
    source.write_bytes(b'verified source')
    import hashlib

    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    exe_sha = hashlib.sha256((staged / 'Recheck.exe').read_bytes()).hexdigest()
    source_result = tmp_path / 'source-summary.json'
    source_result.write_text(
        json.dumps(
            {
                'status': 'VERIFIED',
                'engine_sha256': source_sha,
                'source_root': str(release.parents[1]),
                'settings': {'model': 'selected'},
                'settings_preserved': True,
                'source_preserved': True,
                'records': [
                    {
                        'sample_id': sample,
                        'status': 'VERIFIED',
                        'run_id': 'run_' + format(index + 1, '032x'),
                        'attempt_id': 'attempt_' + format(index + 1, '032x'),
                        'analysis_reuse': (
                            {'source_run_id': 'run_' + format(index, '032x')}
                            if index in (1, 3)
                            else None
                        ),
                    }
                    for index, sample in enumerate(
                        (
                            'gcd_algorithms',
                            'gcd_algorithms',
                            'range_overlap',
                            'range_overlap',
                            'discount_boundary',
                        )
                    )
                ],
            }
        )
    )
    package_result = tmp_path / 'package-summary.json'
    package_result.write_text(
        json.dumps(
            {
                'status': 'VERIFIED',
                'executable_sha256': exe_sha,
                'settings': {'model': 'selected'},
                'user_settings_unchanged': True,
                'checks': [{'kind': 'smoke', 'status': 'VERIFIED'}]
                + [
                    {'kind': 'actual_packaged_gui', 'sample_id': sample, 'status': 'VERIFIED'}
                    for sample in ('gcd_algorithms', 'range_overlap', 'discount_boundary')
                ],
            }
        )
    )
    (release / 'source-identity.json').write_text(json.dumps({'recheck/engine.py': source_sha}))
    (release / 'staged-inventory.json').write_text(
        json.dumps(
            [
                {
                    'path': 'Recheck.exe',
                    'size': (staged / 'Recheck.exe').stat().st_size,
                    'sha256': exe_sha,
                }
            ]
        )
    )
    target_root = tmp_path / 'original' / 'Recheck'
    target = target_root / 'dist' / 'Recheck'
    target.mkdir(parents=True)
    (target / 'Recheck.exe').write_bytes(b'previous good release')
    return release, source_result, package_result, target_root


def test_rejected_gate_preserves_previous_release(tmp_path):
    module = load_module()
    release, source, package, target_root = fixture(tmp_path)
    data = json.loads(package.read_text())
    data['checks'][1]['status'] = 'FAILED'
    package.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        module.publish(release, source, package, target_root)
    assert (target_root / 'dist/Recheck/Recheck.exe').read_bytes() == b'previous good release'


def test_success_archives_previous_and_copies_verified_bytes(tmp_path):
    module = load_module()
    release, source, package, target_root = fixture(tmp_path)
    result = module.publish(release, source, package, target_root)
    assert (target_root / 'dist/Recheck/Recheck.exe').read_bytes() == b'new verified candidate'
    assert (
        Path(result['previous_distribution']) / 'Recheck.exe'
    ).read_bytes() == b'previous good release'
    assert (release / 'publication.json').is_file()


def test_changed_source_after_verification_blocks_publication(tmp_path):
    module = load_module()
    release, source, package, target_root = fixture(tmp_path)
    (release.parents[1] / 'recheck/engine.py').write_bytes(b'changed after verification')
    with pytest.raises(ValueError):
        module.publish(release, source, package, target_root)
    assert (target_root / 'dist/Recheck/Recheck.exe').read_bytes() == b'previous good release'


def test_archive_move_failure_leaves_original_release_in_place(tmp_path, monkeypatch):
    module = load_module()
    release, source, package, target_root = fixture(tmp_path)
    move = module.shutil.move
    target = target_root / 'dist/Recheck'

    def fail_archive(src, dst):
        if Path(src) == target and (
            Path(dst) == release / 'previous-distribution/Recheck'
            or Path(dst).name.startswith('Recheck.rollback-')
        ):
            raise OSError('보관 장치 쓰기 실패')
        return move(src, dst)

    monkeypatch.setattr(module.shutil, 'move', fail_archive)
    with pytest.raises(OSError):
        module.publish(release, source, package, target_root)
    assert (target / 'Recheck.exe').read_bytes() == b'previous good release'


def test_new_release_move_failure_restores_archived_original(tmp_path, monkeypatch):
    module = load_module()
    release, source, package, target_root = fixture(tmp_path)
    move = module.shutil.move

    def fail_install(src, dst):
        if Path(src).name.startswith('Recheck.verified-'):
            raise OSError('새 배포 이름 변경 실패')
        return move(src, dst)

    monkeypatch.setattr(module.shutil, 'move', fail_install)
    with pytest.raises(OSError):
        module.publish(release, source, package, target_root)
    assert (target_root / 'dist/Recheck/Recheck.exe').read_bytes() == b'previous good release'


def test_publication_receipt_failure_restores_original(tmp_path, monkeypatch):
    module = load_module()
    release, source, package, target_root = fixture(tmp_path)
    write = module.atomic_json

    def fail_receipt(path, value):
        if Path(path).name == 'publication.json':
            raise OSError('반영 기록 저장 실패')
        return write(path, value)

    monkeypatch.setattr(module, 'atomic_json', fail_receipt)
    with pytest.raises(OSError):
        module.publish(release, source, package, target_root)
    assert (target_root / 'dist/Recheck/Recheck.exe').read_bytes() == b'previous good release'


@pytest.mark.parametrize(
    'failure', ['no_engine_identity', 'duplicate_run', 'smoke_masquerading_as_sample']
)
def test_incomplete_or_duplicate_proof_cannot_satisfy_release_gate(tmp_path, failure):
    module = load_module()
    release, source, package, target_root = fixture(tmp_path)
    if failure == 'no_engine_identity':
        (release / 'source-identity.json').write_text('{}')
        value = json.loads(source.read_text())
        value.pop('engine_sha256')
        source.write_text(json.dumps(value))
    elif failure == 'duplicate_run':
        value = json.loads(source.read_text())
        value['records'][1] = dict(value['records'][0])
        source.write_text(json.dumps(value))
    else:
        value = json.loads(package.read_text())
        value['checks'][0]['sample_id'] = 'gcd_algorithms'
        value['checks'][1] = {'status': 'VERIFIED'}
        package.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        module.publish(release, source, package, target_root)
    assert (target_root / 'dist/Recheck/Recheck.exe').read_bytes() == b'previous good release'


def test_another_publication_lock_blocks_replacement(tmp_path):
    module = load_module()
    release, source, package, target_root = fixture(tmp_path)
    lock = target_root / 'dist/.recheck-publication.lock'
    lock.write_text('another publication owns this lock')
    with pytest.raises(ValueError):
        module.publish(release, source, package, target_root)
    assert lock.read_text() == 'another publication owns this lock'
    assert (target_root / 'dist/Recheck/Recheck.exe').read_bytes() == b'previous good release'
