"""검증된 안정화 패키지를 원래 Recheck 배포에 보관·복구 가능하게 반영한다."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import re
from pathlib import Path
import shutil
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from recheck.models import atomic_json


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def inventory(folder):
    return [
        dict(path=str(path.relative_to(folder)), size=path.stat().st_size, sha256=sha(path))
        for path in sorted(Path(folder).rglob('*'))
        if path.is_file()
    ]


def publish(release, source_summary, package_summary, target_root):
    target_root = Path(target_root).resolve(strict=True)
    lock = target_root / 'dist/.recheck-publication.lock'
    lock.parent.mkdir(parents=True, exist_ok=True)
    owner = uuid.uuid4().hex
    try:
        with lock.open('x', encoding='utf-8') as stream:
            stream.write(owner)
    except FileExistsError as exc:
        raise ValueError('다른 배포 작업 또는 중단 기록이 있습니다: ' + str(lock)) from exc
    try:
        return _publish_verified(release, source_summary, package_summary, target_root)
    finally:
        if lock.is_file() and lock.read_text(encoding='utf-8') == owner:
            lock.unlink()


def _publish_verified(release, source_summary, package_summary, target_root):
    release, target_root = Path(release).resolve(strict=True), Path(target_root).resolve(
        strict=True
    )
    source, package = read(source_summary), read(package_summary)
    required = Counter(gcd_algorithms=2, range_overlap=2, discount_boundary=1)
    records = source.get('records', [])
    run_ids = [row.get('run_id') for row in records]
    attempt_ids = [row.get('attempt_id') for row in records]
    primary = Counter(row.get('sample_id') for row in records if not row.get('analysis_reuse'))
    extras = Counter(row.get('sample_id') for row in records if row.get('analysis_reuse'))
    if (
        source.get('status') != 'VERIFIED'
        or not source.get('settings_preserved')
        or not source.get('source_preserved')
        or not records
        or any(row.get('status') != 'VERIFIED' for row in records)
        or any(
            Counter(row.get('sample_id') for row in records)[key] < count
            for key, count in required.items()
        )
        or len(set(run_ids)) != len(records)
        or len(set(attempt_ids)) != len(records)
        or any(not re.fullmatch(r'run_[a-f0-9]{32}', value or '') for value in run_ids)
        or any(not re.fullmatch(r'attempt_[a-f0-9]{32}', value or '') for value in attempt_ids)
        or any(primary[key] != 1 for key in required)
        or any(extras[key] < 1 for key in ('gcd_algorithms', 'range_overlap'))
    ):
        raise ValueError('세 사례와 두 독립 추가 AI의 소스 검증을 먼저 통과해야 합니다.')
    checks = package.get('checks', [])
    if (
        package.get('status') != 'VERIFIED'
        or not package.get('user_settings_unchanged')
        or len(checks) != 4
        or any(row.get('status') != 'VERIFIED' for row in checks)
        or Counter(row.get('sample_id') for row in checks if row.get('sample_id'))
        != Counter(required.keys())
        or sum(row.get('kind') == 'smoke' for row in checks) != 1
        or any(row.get('sample_id') for row in checks if row.get('kind') == 'smoke')
        or any(row.get('kind') != 'actual_packaged_gui' for row in checks if row.get('sample_id'))
        or package.get('settings') != source.get('settings')
    ):
        raise ValueError('같은 실행 설정의 패키지 시작·세 사례 검증을 먼저 통과해야 합니다.')
    root = Path(source['source_root']).resolve(strict=True)
    identities = read(release / 'source-identity.json')
    if not re.fullmatch(r'[a-fA-F0-9]{64}', identities.get('recheck/engine.py', '')) or identities[
        'recheck/engine.py'
    ] != source.get('engine_sha256'):
        raise ValueError('패키지 소스와 분석한 엔진 해시가 다릅니다.')
    for relative, expected in identities.items():
        path = (root / relative).resolve(strict=True)
        if root not in path.parents or sha(path) != expected:
            raise ValueError('빌드·검증 후 소스가 변경되었습니다: ' + relative)
    staged = (release / 'staged-dist/Recheck').resolve(strict=True)
    if release not in staged.parents or sha(staged / 'Recheck.exe') != package.get(
        'executable_sha256'
    ):
        raise ValueError('패키지 실행 파일이 검증 후 변경되었습니다.')
    actual = inventory(staged)
    if actual != read(release / 'staged-inventory.json'):
        raise ValueError('패키지 전체 파일이 검증 후 변경되었습니다.')
    target = (target_root / 'dist/Recheck').resolve()
    archive = (release / 'previous-distribution/Recheck').resolve()
    incoming = (target_root / 'dist' / ('Recheck.verified-' + uuid.uuid4().hex)).resolve()
    local_previous = (target_root / 'dist' / ('Recheck.rollback-' + uuid.uuid4().hex)).resolve()
    if (
        target.parent != target_root / 'dist'
        or incoming.parent != target.parent
        or local_previous.parent != target.parent
        or release not in archive.parents
        or archive.exists()
        or (release / 'publication.json').exists()
    ):
        raise ValueError('배포·보관 경로 또는 중복 반영 상태를 확인하세요.')
    previous = inventory(target) if target.exists() else []
    atomic_json(release / 'previous-distribution-manifest.json', previous)
    shutil.copytree(staged, incoming)
    if inventory(incoming) != actual:
        raise ValueError('새 배포 사본 해시가 다릅니다. 기존 배포는 유지됩니다.')
    # 다른 볼륨의 보관 폴더로 이동하기 전에 검증된 사본을 확보한다.
    # 실제 교체는 동일 dist 부모 안의 이름 변경만 사용하며 구본도 보존한다.
    if target.exists():
        archive.parent.mkdir(parents=True)
        shutil.copytree(target, archive)
        if inventory(archive) != previous or inventory(target) != previous:
            raise ValueError('기존 배포 백업의 해시가 다릅니다. 원래 배포를 유지합니다.')
    moved, installed = False, False
    try:
        if target.exists():
            shutil.move(str(target), str(local_previous))
            moved = True
        shutil.move(str(incoming), str(target))
        installed = True
        if inventory(target) != actual:
            raise ValueError('반영한 배포의 전체 해시가 다릅니다.')
        result = dict(
            executable=str(target / 'Recheck.exe'),
            sha256=sha(target / 'Recheck.exe'),
            files=len(actual),
            previous_distribution=str(archive),
            local_previous_distribution=str(local_previous) if moved else None,
            source_summary=str(Path(source_summary).resolve()),
            package_summary=str(Path(package_summary).resolve()),
            source_identity=identities,
            published_inventory=actual,
        )
        atomic_json(release / 'publication.json', result)
    except Exception:
        if (moved or installed) and target.exists():
            shutil.move(str(target), str(release / 'publication-failed-copy'))
        if moved:
            shutil.move(str(local_previous), str(target))
        raise
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--release', required=True)
    parser.add_argument('--source-summary', required=True)
    parser.add_argument('--package-summary', required=True)
    parser.add_argument('--target-root', required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            publish(args.release, args.source_summary, args.package_summary, args.target_root),
            ensure_ascii=False,
            indent=2,
        )
    )
