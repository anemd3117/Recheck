"""Publication preserves old releases and never promotes untested/partial copies."""
import importlib
import json
from types import SimpleNamespace
from pathlib import Path

import pytest


@pytest.fixture
def release(tmp_path):
    assert importlib.util.find_spec('scripts.release_distribution'), 'release transaction missing'
    module = importlib.import_module('scripts.release_distribution')
    stage = tmp_path / 'stage'
    (stage / '_internal').mkdir(parents=True)
    (stage / 'Recheck.exe').write_bytes(b'verified executable')
    (stage / '_internal/RECHECK_SOURCE_IDENTITY.json').write_text(
        json.dumps({'aggregate_sha256': 'a' * 64})
    )
    proof = tmp_path / 'proof.json'
    proof.write_text(json.dumps({
        'status': 'verified', 'inventory': module.inventory(stage),
        'source_aggregate': 'a' * 64,
        'checks': {'native_smoke': True, 'frozen_dynamic': True, 'literal_observations': True},
    }))
    targets = [tmp_path / 'dist/Recheck', tmp_path / 'user/Recheck']
    for target in targets:
        target.mkdir(parents=True)
        (target / 'Recheck.exe').write_bytes(b'previous executable')
        (target / 'personal.txt').write_text('preserve this')
    return module, stage, proof, targets, tmp_path / 'transaction'


def test_publication_produces_identical_folders_zip_and_full_backups(release):
    m, stage, proof, targets, record = release
    result = m.publish(stage, proof, targets, record)
    for target in targets:
        assert (target / 'Recheck.exe').read_bytes() == b'verified executable'
        assert not (target / 'personal.txt').exists()
    for row in result['targets']:
        assert (Path(row['backup']) / 'personal.txt').read_text() == 'preserve this'
    assert m.verify_outputs(targets, result['zip'], result['inventory'])


def test_changed_payload_cannot_replace_existing_release(release):
    m, stage, proof, targets, record = release
    (stage / 'Recheck.exe').write_bytes(b'changed after native test')
    with pytest.raises(ValueError, match='verification|changed'):
        m.publish(stage, proof, targets, record)
    assert all((p / 'personal.txt').is_file() for p in targets)


def test_failed_second_swap_restores_both_previous_releases(release, monkeypatch):
    m, stage, proof, targets, record = release
    rename = Path.rename

    def fail_second(path, dest):
        if path.name.startswith('.recheck-incoming-') and Path(dest) == targets[1]:
            raise PermissionError('simulated Windows file lock')
        return rename(path, dest)

    monkeypatch.setattr(Path, 'rename', fail_second)
    with pytest.raises(PermissionError):
        m.publish(stage, proof, targets, record)
    assert all((p / 'Recheck.exe').read_bytes() == b'previous executable' for p in targets)
    assert all((p / 'personal.txt').is_file() for p in targets)
    assert json.loads((record / 'journal.json').read_text())['state'] == 'rolled_back'


def test_incomplete_native_proof_preserves_previous_release(release):
    m, stage, proof, targets, record = release
    data = json.loads(proof.read_text())
    data['checks']['frozen_dynamic'] = False
    proof.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='verification'):
        m.publish(stage, proof, targets, record)
    assert all((p / 'personal.txt').is_file() for p in targets)


def test_crash_between_rename_and_journal_update_can_be_recovered(release, monkeypatch):
    m, stage, proof, targets, record = release
    rename = Path.rename

    def crash_after_swap(path, dest):
        result = rename(path, dest)
        if path.name.startswith('.recheck-incoming-'):
            raise KeyboardInterrupt('process stopped before receipt')
        return result

    monkeypatch.setattr(Path, 'rename', crash_after_swap)
    with pytest.raises(KeyboardInterrupt):
        m.publish(stage, proof, targets, record)
    monkeypatch.setattr(Path, 'rename', rename)
    m.recover(record)
    assert all((p / 'Recheck.exe').read_bytes() == b'previous executable' for p in targets)
    assert all((p / 'personal.txt').is_file() for p in targets)


def test_zip_tamper_is_detected(release):
    m, stage, proof, targets, record = release
    result = m.publish(stage, proof, targets, record)
    import zipfile
    with zipfile.ZipFile(result['zip'], 'a') as stream:
        stream.writestr('Recheck/unreviewed.txt', 'unreviewed payload')
    with pytest.raises(ValueError, match='ZIP'):
        m.verify_outputs(targets, result['zip'], result['inventory'])


def test_user_data_in_staging_is_not_admitted(release):
    m, stage, proof, targets, record = release
    (stage / 'auth.json').write_text('private')
    with pytest.raises(ValueError):
        m.publish(stage, proof, targets, record)
    assert all((p / 'personal.txt').exists() for p in targets)


def test_recovery_releases_only_its_own_stale_publication_locks(release, monkeypatch):
    m, stage, proof, targets, record = release
    rename = Path.rename

    def crash(path, dest):
        value = rename(path, dest)
        if path.name.startswith('.recheck-incoming-'):
            raise KeyboardInterrupt()
        return value

    monkeypatch.setattr(Path, 'rename', crash)
    with pytest.raises(KeyboardInterrupt):
        m.publish(stage, proof, targets, record)
    monkeypatch.setattr(Path, 'rename', rename)
    for target in targets:
        (target.parent / '.recheck-publication.lock').write_text(str(record))
    m.recover(record)
    assert all(not (p.parent / '.recheck-publication.lock').exists() for p in targets)


def test_same_parent_targets_are_rejected_before_any_replacement(release):
    m, stage, proof, targets, record = release
    with pytest.raises(ValueError, match='parent'):
        m.publish(stage, proof, [targets[0], targets[0].with_name('Another')], record)
    assert (targets[0] / 'personal.txt').exists()


def test_recover_committed_publication_releases_own_stale_lock(release):
    m, stage, proof, targets, record = release
    m.publish(stage, proof, targets, record)
    for target in targets:
        (target.parent / '.recheck-publication.lock').write_text(str(record))
    m.recover(record)
    assert all(not (p.parent / '.recheck-publication.lock').exists() for p in targets)
    assert all((p / 'Recheck.exe').read_bytes() == b'verified executable' for p in targets)


def test_preparing_recovery_never_moves_a_target_owned_by_another_process(release):
    m, stage, proof, targets, record = release
    record.mkdir()
    new_target = targets[0].with_name('new-target')
    new_target.mkdir()
    (new_target / 'user.txt').write_text('another owner')
    row = dict(target=str(new_target), existed=False, previous_inventory=[],
               incoming=str(new_target.parent / '.recheck-incoming-abc'),
               backup=str(new_target.parent / '.recheck-previous-abc'))
    (record / 'journal.json').write_text(json.dumps(
        dict(id='abc', state='preparing', targets=[row])))
    m.recover(record)
    assert (new_target / 'user.txt').read_text() == 'another owner'


def test_all_recovery_paths_are_checked_before_any_folder_is_moved(release):
    m, stage, proof, targets, record = release
    record.mkdir()
    backup = targets[0].parent / '.recheck-previous-abc'
    backup.mkdir()
    (backup / 'Recheck.exe').write_bytes(b'old')
    row = dict(target=str(targets[0]), existed=True,
               previous_inventory=m.inventory(backup), backup=str(backup),
               incoming=str(targets[0].parent / '.recheck-incoming-abc'))
    invalid = dict(row, backup=str(targets[1].parent / 'arbitrary-user-folder'))
    (record / 'journal.json').write_text(json.dumps(
        dict(id='abc', state='applying', targets=[invalid, row])))
    with pytest.raises(ValueError, match='paths'):
        m.recover(record)
    assert (targets[0] / 'Recheck.exe').read_bytes() == b'previous executable'
    assert (backup / 'Recheck.exe').read_bytes() == b'old'


def test_legacy_publish_entrypoint_cannot_bypass_transactional_publication():
    m = importlib.import_module('scripts.package_verified')
    with pytest.raises(ValueError, match='release_distribution'):
        m.main(SimpleNamespace(output=str(m.ROOT / 'verification/unused-test'), publish=True))


def test_missing_backup_cannot_be_reported_as_successful_rollback(release):
    m, stage, proof, targets, record = release
    result = m.publish(stage, proof, targets, record)
    journal = json.loads((record / 'journal.json').read_text())
    journal['state'] = 'applying'
    (record / 'journal.json').write_text(json.dumps(journal))
    backup = Path(result['targets'][0]['backup'])
    backup.rename(record / 'manually-moved-backup')
    with pytest.raises(ValueError, match='backup'):
        m.recover(record)
    assert json.loads((record / 'journal.json').read_text())['state'] == 'applying'


def test_publication_requires_the_literal_observation_gate(release):
    m, stage, proof, targets, record = release
    data = json.loads(proof.read_text())
    del data['checks']['literal_observations']
    proof.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='verification'):
        m.publish(stage, proof, targets, record)
    assert all((target / 'personal.txt').exists() for target in targets)
