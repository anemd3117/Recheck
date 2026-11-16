"""Probe Docker-hosted Windows VM prerequisites without installing a guest.

No image download, kernel-module load, guest boot, Docker-mode change or Windows
installation occurs. --probe-image opts into creating and closing an empty KVM
VM inside a disposable local Python 3 container. It never executes guest code.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import uuid

IMAGE_ID = re.compile(r'sha256:[0-9a-f]{64}\Z')
KVM_PROGRAM = '''import os, fcntl, json
fd = os.open('/dev/kvm', os.O_RDWR | os.O_CLOEXEC)
try:
    api = fcntl.ioctl(fd, 0xAE00, 0)
    vm = fcntl.ioctl(fd, 0xAE01, 0)
    os.close(vm)
    print(json.dumps(dict(kvm_api_version=api, empty_vm_created=True,
                         target_executed=False)))
finally:
    os.close(fd)
'''
DEVICE_COMMAND = (
    '''printf '{"kvm":'; if test -c /dev/kvm; then printf true; else printf false; fi; '''
    '''printf ',"tun":'; if test -c /dev/net/tun; then printf true; else printf false; fi; '''
    '''printf '}\\n' '''
)


def local_endpoint(endpoint):
    return isinstance(endpoint, str) and endpoint.startswith(('npipe:////./pipe/', 'unix:///'))


def assess(endpoint, engine, devices, kvm=None):
    result = dict(status='blocked', reason_code='nonlocal_engine',
                  windows_execution_verified=False)
    if not local_endpoint(endpoint):
        return result
    if (not isinstance(engine, dict) or engine.get('OSType') != 'linux'
            or engine.get('Architecture') not in ('amd64', 'x86_64')):
        result['reason_code'] = 'linux_amd64_engine_required'
    elif not isinstance(devices, dict) or not all(
            isinstance(devices.get(key), bool) for key in ('kvm', 'tun')):
        result['reason_code'] = 'device_probe_failed'
    elif not devices['kvm']:
        result['reason_code'] = 'kvm_device_missing'
    elif not devices['tun']:
        result['reason_code'] = 'tun_device_missing'
    elif kvm is None:
        result.update(status='candidate', reason_code='kvm_ioctl_not_tested')
    elif (isinstance(kvm, dict) and kvm.get('kvm_api_version') == 12
          and kvm.get('empty_vm_created') is True and kvm.get('target_executed') is False):
        result.update(status='kvm_verified', reason_code='guest_not_installed_or_tested')
    else:
        result['reason_code'] = 'kvm_ioctl_failed'
    return result


def run_command(args, *, stdin=None):
    try:
        result = subprocess.run(args, input=stdin, capture_output=True, text=True,
                                encoding='utf-8', errors='replace', timeout=30)
        return dict(exit_code=result.returncode, stdout=result.stdout.replace('\x00', ''),
                    stderr=result.stderr.replace('\x00', ''))
    except (OSError, subprocess.TimeoutExpired) as exc:
        return dict(exit_code=None, stdout='', stderr=str(exc))


def parsed(result):
    if result.get('exit_code') == 0:
        try:
            return json.loads(result['stdout'])
        except (ValueError, TypeError, KeyError):
            pass
    return None


def collect(context='', distro='docker-desktop', probe_image=None, runner=None):
    runner = runner or run_command
    context = context or 'desktop-linux'
    report = dict(recorded_at=datetime.now(timezone.utc).isoformat(), commands=[],
                  windows_execution_verified=False, guest_installed=False)
    if probe_image is not None and not IMAGE_ID.fullmatch(probe_image):
        report['assessment'] = dict(status='blocked',
                                    reason_code='immutable_probe_image_required',
                                    windows_execution_verified=False)
        return report
    docker = ['docker', '--context', context]

    def call(args, **kwargs):
        result = runner(args, **kwargs)
        report['commands'].append(dict(argv=args, **result))
        return result

    inspected = call(docker + ['context', 'inspect'] + ([context] if context else [])
                     + ['--format', '{{.Endpoints.docker.Host}}'])
    endpoint = inspected.get('stdout', '').strip() if inspected.get('exit_code') == 0 else ''
    report['endpoint'] = endpoint
    if not local_endpoint(endpoint):
        report['assessment'] = assess(endpoint, None, None)
        return report
    fields = ('{"OSType":{{json .OSType}},"Architecture":{{json .Architecture}},'
              '"KernelVersion":{{json .KernelVersion}},"ServerVersion":{{json .ServerVersion}}}')
    engine = parsed(call(docker + ['info', '--format', fields]))
    report['engine'] = engine
    assessment = assess(endpoint, engine, None)
    if assessment['reason_code'] == 'linux_amd64_engine_required':
        report['assessment'] = assessment
        return report
    devices = parsed(call(['wsl', '-d', distro, '--', 'sh', '-c', DEVICE_COMMAND]))
    report['devices'] = devices
    report['assessment'] = assess(endpoint, engine, devices)
    if report['assessment']['status'] != 'candidate' or probe_image is None:
        return report
    image = parsed(call(docker + ['image', 'inspect', probe_image, '--format',
                                  '{"Id":{{json .Id}},"Os":{{json .Os}},'
                                  '"Architecture":{{json .Architecture}}}']))
    if (not isinstance(image, dict) or image.get('Id') != probe_image
            or image.get('Os') != 'linux' or image.get('Architecture') != 'amd64'):
        report['assessment'].update(status='blocked', reason_code='local_probe_image_invalid')
        return report
    name = 'recheck-kvm-probe-' + uuid.uuid4().hex
    report['probe_image_id'] = probe_image
    executed = call(docker + ['run', '--rm', '-i', '--pull', 'never', '--name', name,
                              '--label', 'org.recheck.purpose=kvm-probe', '--network', 'none',
                              '--read-only', '--cap-drop', 'ALL', '--pids-limit', '16',
                              '--memory', '128m', '--device', '/dev/kvm',
                              '--entrypoint', 'python3', probe_image, '-'], stdin=KVM_PROGRAM)
    kvm = parsed(executed)
    report['kvm'] = kvm
    report['assessment'] = assess(endpoint, engine, devices, kvm if kvm else {})
    if executed.get('exit_code') != 0:
        call(docker + ['rm', '--force', name])
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--context', default='')
    parser.add_argument('--wsl-distro', default='docker-desktop')
    parser.add_argument('--probe-image', help='Existing local immutable sha256 ID with Python 3')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    report = collect(args.context, args.wsl_distro, args.probe_image)
    rendered = json.dumps(report, indent=2, ensure_ascii=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + '\n', encoding='utf-8')
    print(rendered)
    return 2 if report['assessment']['status'] == 'blocked' else 0


if __name__ == '__main__':
    raise SystemExit(main())
