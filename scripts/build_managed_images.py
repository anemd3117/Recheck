import argparse
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = ('static', 'execution', 'ollama')


def run(*arguments, capture=False):
    return subprocess.run(
        arguments, cwd=ROOT, check=True, text=True, encoding='utf-8', capture_output=capture
    ).stdout


def main():
    parser = argparse.ArgumentParser(
        description='Build the reviewed, pinned managed runtime images. This developer command is never run by Recheck.'
    )
    parser.add_argument('--components', nargs='+', choices=COMPONENTS, default=list(COMPONENTS))
    parser.add_argument(
        '--write-manifest',
        action='store_true',
        help='Explicitly refresh local image IDs in resources/runtime/environment.json; remove stale archive identities.',
    )
    parser.add_argument(
        '--output-json', type=Path, help='Write the build receipts for release packaging.'
    )
    args = parser.parse_args()
    receipts = {}
    for component in dict.fromkeys(args.components):
        reference = f'recheck-managed-{component}:1'
        run(
            'docker',
            'build',
            '--platform',
            'linux/amd64',
            '--provenance=false',
            '--tag',
            reference,
            str(ROOT / 'resources' / 'docker' / component),
        )
        info = json.loads(run('docker', 'image', 'inspect', reference, capture=True))[0]
        if info['Os'] != 'linux' or info['Architecture'] != 'amd64':
            raise RuntimeError(f'{component}: unexpected image platform')
        if info.get('Config', {}).get('Labels', {}).get('org.recheck.component') != component:
            raise RuntimeError(f'{component}: missing managed component label')
        receipts[component] = {
            'ref': reference,
            'image_id': info['Id'],
            'platform': 'linux/amd64',
            'size_bytes': info['Size'],
        }
    if args.write_manifest:
        path = ROOT / 'resources' / 'runtime' / 'environment.json'
        manifest = json.loads(path.read_text(encoding='utf-8-sig'))
        for component, receipt in receipts.items():
            spec = manifest['images'][component]
            if spec.get('image_id') != receipt['image_id']:
                spec.pop('archive', None)
            spec.update({key: receipt[key] for key in ('ref', 'image_id', 'platform')})
        manifest['release'] = 'local-developer-build'
        path.write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    result = json.dumps({'worker_contract': 1, 'images': receipts}, indent=2)
    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(result + '\n', encoding='utf-8')
    print(result)


if __name__ == '__main__':
    main()
