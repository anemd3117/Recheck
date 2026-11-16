"""Export reviewed immutable Docker images into an offline Recheck distribution.

Only image layers are saved. Containers, model volumes, credentials and workstation
configuration are outside this command's input set. Recheck never runs this script.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = ('static', 'execution', 'ollama')
IDENTITY = re.compile(r'sha256:[0-9a-f]{64}\Z')


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _temporary(directory: Path, suffix: str) -> Path:
    descriptor, name = tempfile.mkstemp(prefix='.recheck-export-', suffix=suffix, dir=directory)
    os.close(descriptor)
    return Path(name)


def _stage_json(directory: Path, data: dict) -> Path:
    path = _temporary(directory, '.json')
    try:
        with path.open('w', encoding='utf-8', newline='\n') as stream:
            json.dump(data, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def export_environment(
    manifest_path, output_dir, *, include_ollama=False, update_manifest=False, runner=None
):
    """Verify every identity, stage every save, then publish owned files atomically.

    The resource manifest changes only with explicit ``update_manifest=True``.
    Existing archives require a matching previous distribution manifest and hash.
    """
    runner = runner or subprocess.run
    manifest_path = Path(manifest_path).expanduser().resolve()
    output = Path(output_dir).expanduser().resolve()
    if output == Path(output.anchor):
        raise ValueError('The filesystem root cannot be a distribution output directory')
    original = manifest_path.read_bytes()
    manifest = json.loads(original.decode('utf-8-sig'))
    if manifest.get('version') != 1 or not isinstance(manifest.get('images'), dict):
        raise ValueError('Unsupported environment manifest')
    selected = COMPONENTS if include_ollama else COMPONENTS[:2]
    specs = {}
    for component in selected:
        spec = manifest['images'].get(component, {})
        identity = spec.get('image_id', '')
        if (
            not isinstance(identity, str)
            or not IDENTITY.fullmatch(identity)
            or identity == 'sha256:' + '0' * 64
        ):
            raise ValueError(f'{component}: a valid immutable image identity is required')
        if spec.get('platform') != 'linux/amd64':
            raise ValueError(f'{component}: unsupported manifest platform')
        specs[component] = spec

    # Complete preflight for all requested images before Docker writes any archive.
    for component, spec in specs.items():
        result = runner(
            ['docker', 'image', 'inspect', spec['image_id']],
            check=True,
            capture_output=True,
            text=True,
            encoding='utf-8',
        )
        observed = json.loads(result.stdout)
        if not isinstance(observed, list) or len(observed) != 1:
            raise ValueError(f'{component}: Docker returned an invalid image identity record')
        image = observed[0]
        if image.get('Id') != spec['image_id']:
            raise ValueError(f'{component}: Docker image identity does not match the manifest')
        if image.get('Os') != 'linux' or image.get('Architecture') != 'amd64':
            raise ValueError(f'{component}: Docker image platform does not match linux/amd64')
        if image.get('Config', {}).get('Labels', {}).get('org.recheck.component') != component:
            raise ValueError(f'{component}: Docker image component label does not match')

    output.mkdir(parents=True, exist_ok=True)
    destination_manifest = output / 'environment-manifest.json'
    if destination_manifest.is_symlink():
        raise ValueError('Existing distribution manifest must be an owned regular file')
    previous_bytes = destination_manifest.read_bytes() if destination_manifest.exists() else None
    previous = json.loads(previous_bytes.decode('utf-8-sig')) if previous_bytes else {}
    filenames = {component: f'recheck-managed-{component}-1.tar' for component in selected}
    for component, filename in filenames.items():
        archive_path = output / filename
        if archive_path.is_symlink():
            raise ValueError(f'{component}: refusing to replace an existing symbolic link')
        if archive_path.exists():
            ownership = previous.get('images', {}).get(component, {}).get('archive', {})
            if (
                not archive_path.is_file()
                or ownership.get('filename') != filename
                or ownership.get('sha256') != sha256_file(archive_path)
            ):
                raise ValueError(
                    f'{component}: existing archive is not verified as owned by this distribution'
                )

    staged = {}
    backups = {}
    published = []
    source_staged = None
    committed = False
    try:
        result_manifest = copy.deepcopy(manifest)
        for component in COMPONENTS:
            if component not in selected and component in result_manifest['images']:
                result_manifest['images'][component].pop('archive', None)
        for component, spec in specs.items():
            temporary = _temporary(output, '.tar')
            staged[output / filenames[component]] = temporary
            runner(
                ['docker', 'image', 'save', '--output', str(temporary), spec['image_id']],
                check=True,
                capture_output=True,
                text=True,
                encoding='utf-8',
            )
            size = temporary.stat().st_size
            if not size:
                raise ValueError(f'{component}: Docker produced an empty image archive')
            archive = {
                'filename': filenames[component],
                'sha256': sha256_file(temporary),
                'size_bytes': size,
            }
            result_manifest['images'][component]['archive'] = archive
        staged[destination_manifest] = _stage_json(output, result_manifest)

        if update_manifest:
            source_manifest = copy.deepcopy(manifest)
            for component in selected:
                source_manifest['images'][component]['archive'] = result_manifest['images'][
                    component
                ]['archive']
            source_staged = _stage_json(manifest_path.parent, source_manifest)
            if manifest_path.read_bytes() != original:
                raise RuntimeError(
                    'Resource manifest changed during export; no distribution was published'
                )
        observed_previous = (
            destination_manifest.read_bytes() if destination_manifest.exists() else None
        )
        if observed_previous != previous_bytes:
            raise RuntimeError('Existing distribution manifest changed during export')

        # All Docker saves and checksums succeeded. Each archive and manifest now
        # replaces one known owned filename; backups allow rollback on I/O failure.
        for final, temporary in staged.items():
            if final.exists():
                backup = _temporary(output, '.backup')
                os.replace(final, backup)
                backups[final] = backup
            os.replace(temporary, final)
            published.append(final)
        if source_staged:
            os.replace(source_staged, manifest_path)
        committed = True
        return result_manifest
    except BaseException:
        for final in reversed(published):
            final.unlink(missing_ok=True)
        for final, backup in backups.items():
            if backup.exists():
                os.replace(backup, final)
        raise
    finally:
        for temporary in staged.values():
            temporary.unlink(missing_ok=True)
        if source_staged:
            source_staged.unlink(missing_ok=True)
        if committed:
            for backup in backups.values():
                backup.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'environment')
    parser.add_argument(
        '--manifest', type=Path, default=ROOT / 'resources/runtime/environment.json'
    )
    parser.add_argument(
        '--include-ollama',
        action='store_true',
        help='Include the optional service image; model volumes are never exported',
    )
    parser.add_argument(
        '--update-manifest',
        action='store_true',
        help='Explicitly publish the verified archive identities into the resource manifest too',
    )
    args = parser.parse_args()
    result = export_environment(
        args.manifest,
        args.output_dir,
        include_ollama=args.include_ollama,
        update_manifest=args.update_manifest,
    )
    print(
        json.dumps(
            {
                'output_dir': str(args.output_dir.expanduser().resolve()),
                'archives': {
                    component: spec['archive']
                    for component, spec in result['images'].items()
                    if 'archive' in spec
                },
            },
            indent=2,
        )
    )


if __name__ == '__main__':
    main()
