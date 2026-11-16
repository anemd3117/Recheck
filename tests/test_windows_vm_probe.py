"""A device node is a prerequisite, never evidence of Windows execution."""
from scripts import probe_windows_vm as probe


def test_missing_kvm_blocks_candidate_even_with_nested_cpu_flag():
    result = probe.assess('npipe:////./pipe/dockerDesktopLinuxEngine',
                          {'OSType': 'linux', 'Architecture': 'x86_64'},
                          {'kvm': False, 'tun': True, 'svm': True})
    assert result['status'] == 'blocked'
    assert result['reason_code'] == 'kvm_device_missing'
    assert result['windows_execution_verified'] is False


def test_device_presence_only_identifies_candidate_not_working_guest():
    result = probe.assess('npipe:////./pipe/dockerDesktopLinuxEngine',
                          {'OSType': 'linux', 'Architecture': 'x86_64'},
                          {'kvm': True, 'tun': True})
    assert result['status'] == 'candidate'
    assert result['windows_execution_verified'] is False


def test_remote_endpoint_is_rejected_before_distro_probe():
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return {'exit_code': 0, 'stdout': 'tcp://10.0.0.2:2376', 'stderr': ''}

    result = probe.collect(context='remote', runner=run)
    assert result['assessment']['reason_code'] == 'nonlocal_engine'
    assert len(calls) == 1


def test_windows_engine_cannot_host_linux_qemu_image():
    result = probe.assess('npipe:////./pipe/docker_engine',
                          {'OSType': 'windows', 'Architecture': 'x86_64'},
                          {'kvm': True, 'tun': True})
    assert result['reason_code'] == 'linux_amd64_engine_required'


def test_malformed_device_probe_is_not_treated_as_success():
    calls = iter([
        {'exit_code': 0, 'stdout': 'npipe:////./pipe/dockerDesktopLinuxEngine', 'stderr': ''},
        {'exit_code': 0, 'stdout': '{"OSType":"linux","Architecture":"x86_64"}', 'stderr': ''},
        {'exit_code': 0, 'stdout': 'unexpected output', 'stderr': ''},
    ])
    result = probe.collect(runner=lambda args, **kwargs: next(calls))
    assert result['assessment']['reason_code'] == 'device_probe_failed'


def test_successful_empty_kvm_vm_is_not_a_windows_guest_test():
    result = probe.assess('npipe:////./pipe/dockerDesktopLinuxEngine',
                          {'OSType': 'linux', 'Architecture': 'x86_64'},
                          {'kvm': True, 'tun': True},
                          {'kvm_api_version': 12, 'empty_vm_created': True,
                           'target_executed': False})
    assert result['status'] == 'kvm_verified'
    assert result['windows_execution_verified'] is False


def test_mutable_probe_image_is_rejected_before_docker_operation():
    result = probe.collect(probe_image='ubuntu:latest',
                           runner=lambda *args, **kwargs: (_ for _ in ()).throw(
                               AssertionError('Docker must not be called')))
    assert result['assessment']['reason_code'] == 'immutable_probe_image_required'


def test_remote_named_pipe_does_not_pass_local_endpoint_gate():
    assert probe.local_endpoint('npipe:////remote-machine/pipe/docker_engine') is False
