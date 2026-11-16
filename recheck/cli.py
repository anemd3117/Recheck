import argparse
import asyncio
import json
import os
import signal
import re
import sys
import tempfile
import threading
from pathlib import Path
from .models import load_settings, inspect_binary
from .engine import Engine


class CLIError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


class UsageError(CLIError):
    pass


class HelpRequest(Exception):
    def __init__(self, text):
        self.text = text


class SafeParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault('allow_abbrev', False)
        super().__init__(*args, **kwargs)

    def error(self, message):
        raise UsageError('invalid_arguments', '명령과 옵션 형식을 확인하세요.')

    def print_help(self, file=None):
        self._requested_help = self.format_help()

    def exit(self, status=0, message=None):
        if status == 0:
            raise HelpRequest(getattr(self, '_requested_help', self.format_help()))
        raise UsageError('invalid_arguments', '명령과 옵션 형식을 확인하세요.')


def extract_json_output(argv):
    """The output flag is global, including after nested subcommands."""
    result = []
    output = None
    index = 0
    while index < len(argv):
        value = argv[index]
        if value == '--':
            result.extend(argv[index:])
            break
        if value == '--json-output' or value.startswith('--json-output='):
            if output is not None:
                raise CLIError('invalid_arguments', 'JSON 출력 파일은 한 번만 지정하세요.')
            if value == '--json-output':
                index += 1
                if index >= len(argv) or argv[index].startswith('--'):
                    raise CLIError('invalid_arguments', '새 JSON 출력 파일을 지정하세요.')
                output = argv[index]
            else:
                output = value.split('=', 1)[1]
            if not output or '\0' in output:
                raise CLIError('invalid_arguments', '새 JSON 출력 파일을 지정하세요.')
        else:
            result.append(value)
        index += 1
    return result, Path(output) if output is not None else None


def prepare_json_output(path):
    if path is None:
        return
    if path.exists() or path.is_symlink():
        raise CLIError('output_conflict', '기존 JSON 출력 파일을 덮어쓰지 않습니다.')
    path.parent.mkdir(parents=True, exist_ok=True)


def _stdout(text, fallback=None):
    if sys.stdout is None:
        return
    try:
        sys.stdout.write(text + '\n')
    except UnicodeEncodeError:
        sys.stdout.write(
            (fallback() if fallback else text.encode('ascii', 'backslashreplace').decode('ascii'))
            + '\n'
        )


def emit_json_result(result, path=None):
    text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    if path is not None:
        path = Path(path)
        descriptor, temporary = tempfile.mkstemp(
            prefix='.recheck-json-', suffix='.tmp', dir=path.parent
        )
        temporary = Path(temporary)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as stream:
                stream.write(text + '\n')
                stream.flush()
                os.fsync(stream.fileno())
            # Windows rename refuses an existing destination. POSIX link gives
            # the same atomic no-replacement contract without rename's overwrite.
            if os.name == 'nt':
                os.rename(temporary, path)
            else:
                os.link(temporary, path)
        except FileExistsError:
            raise CLIError('output_conflict', '기존 JSON 출력 파일을 덮어쓰지 않습니다.') from None
        finally:
            if temporary.exists():
                temporary.unlink()
    _stdout(text, lambda: json.dumps(result, ensure_ascii=True, indent=2, default=str))


def safe_error(error):
    from ai_providers.contracts import ProviderError

    code = getattr(error, 'code', '')
    if not isinstance(code, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', code):
        code = (
            'cli_io'
            if isinstance(error, OSError)
            else (
                'input_format' if isinstance(error, (ValueError, TypeError)) else 'operation_failed'
            )
        )
    message = (
        str(error)
        if isinstance(error, CLIError)
        else (
            '파일 접근에 실패했습니다.'
            if isinstance(error, OSError)
            else (
                'AI 제공자 작업을 완료하지 못했습니다.'
                if isinstance(error, ProviderError)
                else '입력 형식 또는 작업 상태를 확인하세요.'
            )
        )
    )
    return dict(status='failed', code=code, message=message)


def result_code(result):
    return (
        2
        if isinstance(result, dict)
        and str(result.get('status', '')).lower()
        in ('failed', 'blocked', 'partial', 'cancelled', 'error')
        else 0
    )


def public_result(result):
    if not result_code(result):
        return result
    code = (
        result.get('code')
        or result.get('error_code')
        or result.get('completion_reason')
        or 'operation_failed'
    )
    if not isinstance(code, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', code):
        code = 'operation_failed'
    summary = dict(
        status=result['status'],
        code=code,
        message='작업이 완료되지 않았습니다. 저장된 보고서와 상태 코드를 확인하세요.',
    )
    for key in ('run_id', 'workflow_id', 'report', 'comparison_report', 'directory'):
        if isinstance(result.get(key), str):
            summary[key] = result[key]
    return summary


def load_json(path):
    path = Path(path)
    if path.stat().st_size > 64 * 1024 * 1024:
        raise CLIError('input_format', 'JSON 입력 파일의 크기 제한을 초과했습니다.')
    return json.loads(path.read_text(encoding='utf-8-sig'))


def _ai_result(args, cancel):
    from ai_providers.contracts import ProviderError, ProviderProfile
    from ai_providers.profiles import ProfileManager

    manager = ProfileManager()
    operation = args.ai_command
    if operation == 'profiles':
        return [profile.model_dump() for profile in manager.list_profiles()]
    if operation == 'import':
        path = Path(args.file)
        if path.stat().st_size > 65536:
            raise ProviderError('profile_configuration', '프로필 JSON은 64KB 이하여야 합니다')
        try:
            profile = ProviderProfile.model_validate_json(path.read_bytes())
        except ValueError:
            raise ProviderError(
                'profile_configuration', '프로필 형식을 확인하세요. API 키 값은 파일에 넣지 마세요'
            ) from None
        return manager.save_profile(profile).model_dump()
    if operation == 'select':
        return manager.select_profile(args.profile_id, role=args.role).model_dump()
    if operation == 'status':
        return manager.account_status(args.profile_id)
    if operation == 'delete':
        manager.delete_profile(args.profile_id)
        return {'deleted': True, 'profile_id': args.profile_id}
    if operation == 'login':
        return asyncio.run(
            manager.login(
                args.profile_id,
                {
                    'client_id': args.client_id,
                    'scopes': args.scope,
                    'google_project_id': args.project_id,
                },
                cancel=cancel,
            )
        )
    method = {
        'connection': 'probe_connection',
        'models': 'list_models',
        'test': 'probe_capabilities',
        'logout': 'logout',
    }[operation]
    result = asyncio.run(getattr(manager, method)(args.profile_id, cancel=cancel))
    return [model.model_dump() for model in result] if operation == 'models' else result


def _selected_settings(args):
    from ai_providers.contracts import ProviderError
    from ai_providers.profiles import resolve_profile

    settings = load_settings().model_copy(deep=True)
    values = settings.model_dump()
    if args.ai_profile:
        values['active_ai_profile'] = args.ai_profile
    for role in ('generation', 'review'):
        if getattr(args, role + '_profile', None):
            values['ai_role_profiles'][role] = getattr(args, role + '_profile', None)
    try:
        settings = type(settings).model_validate(values)
    except ValueError:
        raise ProviderError('profile_missing', '명시한 역할의 AI 프로필이 없습니다') from None
    if args.ai_key_env:
        profile = resolve_profile(settings)
        if profile.auth_mode != 'api_key':
            raise ProviderError(
                'credential_auth', '환경 키 참조는 API 키 인증 프로필에만 사용할 수 있습니다'
            )
        try:
            changed = type(profile).model_validate(
                {
                    **profile.model_dump(),
                    'credential_ref': 'env:' + args.ai_key_env,
                    'credential_origin': profile.origin,
                    'credential_version': 0,
                }
            )
        except ValueError:
            raise ProviderError(
                'credential_binding', '명시할 환경변수 이름 형식을 확인하세요'
            ) from None
        settings.ai_profiles = [
            changed if item.profile_id == profile.profile_id else item
            for item in settings.ai_profiles
        ]
    if args.command == 'command' or (args.command == 'analyze' and args.mode == 'agent'):
        from ai_providers.profiles import ProfileManager

        proof = ProfileManager(settings=settings).cached_capabilities(settings.active_ai_profile)
        if proof:
            settings.capabilities = proof
    return settings


def _parser():
    parser = SafeParser(description='Recheck CLI (GUI와 동일 엔진)')
    parser.add_argument(
        '--json-output',
        help='기존 파일을 덮어쓰지 않는 새 UTF-8 JSON 출력 파일; 모든 명령 위치에서 사용 가능',
    )
    parser.add_argument('--data-dir')
    parser.add_argument('--settings-dir', help='설정·보안 저장소 폴더; 결과 폴더와 별도')
    parser.add_argument('--ai-profile', help='이번 실행의 분석 AI 프로필 ID; 저장하지 않음')
    parser.add_argument(
        '--ai-key-env', help='선택 API 프로필에 사용할 명시 환경변수 이름; 키 값 인자 금지'
    )
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('environment')
    ai = sub.add_parser('ai', help='AI 연결 관리; test 명령만 실제 추론·비용 발생 가능')
    ai_sub = ai.add_subparsers(dest='ai_command', required=True)
    ai_sub.add_parser('profiles')
    ai_import = ai_sub.add_parser('import')
    ai_import.add_argument('file', help='비밀값 없는 프로필 JSON')
    for operation in (
        'select',
        'status',
        'delete',
        'connection',
        'models',
        'test',
        'login',
        'logout',
    ):
        item = ai_sub.add_parser(operation)
        item.add_argument('profile_id')
        if operation == 'select':
            item.add_argument('--role', choices=['analysis'], default='analysis')
        if operation == 'login':
            item.add_argument('--client-id', default='')
            item.add_argument('--scope', action='append', default=[])
            item.add_argument('--project-id', default='')
    sub.add_parser('history')
    inspect = sub.add_parser('inspect')
    inspect.add_argument('path')
    analyze = sub.add_parser('analyze')
    analyze.add_argument('--original', required=True)
    analyze.add_argument('--reimplementation', required=True)
    analyze.add_argument('--tests')
    analyze.add_argument(
        '--mode', choices=['automatic', 'agent', 'static', 'dynamic'], default='automatic'
    )
    for name in ('reinterpret', 'recover', 'report', 'diagnostic-zip'):
        p = sub.add_parser(name)
        p.add_argument('run_id')
    question = sub.add_parser('question')
    question.add_argument('run_id')
    question.add_argument('question')
    command_parser = sub.add_parser('command')
    command_parser.add_argument('run_id')
    command_parser.add_argument('instruction')
    for name in ('reconstruct', 'modernize'):
        workflow = sub.add_parser(name)
        workflow.add_argument('--original', required=True)
        workflow.add_argument('--tests', required=True)
        workflow.add_argument('--requirements', required=True)
        workflow.add_argument('--source', required=name == 'modernize')
        workflow.add_argument('--language', choices=['c', 'cpp'], default='c')
        workflow.add_argument('--approve-build', action='store_true')
        workflow.add_argument('--max-repairs', type=int, choices=range(3), default=1)
    sub.add_parser('workflows')
    workflow_info = sub.add_parser('workflow')
    workflow_info.add_argument('workflow_id')
    return parser


def _execute(args, cancel):
    if args.command == 'ai':
        return _ai_result(args, cancel)
    if args.command == 'inspect':
        return inspect_binary(args.path)
    engine = Engine(_selected_settings(args), data_dir=args.data_dir)
    command = args.command
    if command == 'environment':
        result = asyncio.run(engine.environment(cancel))
    elif command == 'history':
        result = engine.store.list_runs()
    elif command == 'analyze':
        tests = (
            load_json(args.tests)
            if args.tests
            else [
                dict(
                    id='baseline',
                    description='입력 없는 기본 실행',
                    args=[],
                    stdin='',
                    source='user',
                )
            ]
        )
        result = asyncio.run(
            engine.analyze(args.original, args.reimplementation, tests, args.mode, cancel)
        )
    elif command == 'question':
        result = asyncio.run(engine.question(args.run_id, args.question, cancel))
    elif command == 'command':
        result = asyncio.run(engine.command(args.run_id, args.instruction, cancel))
    elif command == 'workflows':
        result = engine.store.list_workflows()
    elif command == 'workflow':
        result = engine.store.load_workflow(args.workflow_id)
    elif command in ('reconstruct', 'modernize'):
        from .reconstruction import ReconstructionService

        tests = json.loads(Path(args.tests).read_text(encoding='utf-8-sig'))
        result = asyncio.run(
            ReconstructionService(engine.settings, data_dir=args.data_dir).run(
                args.original,
                tests,
                args.requirements,
                mode='modernization' if command == 'modernize' else 'reconstruction',
                source_path=args.source,
                language=args.language,
                approve_build=args.approve_build,
                max_repairs=args.max_repairs,
                cancel=cancel,
            )
        )
    elif command in ('recover', 'reinterpret'):
        result = asyncio.run(getattr(engine, command)(args.run_id, cancel))
    elif command == 'report':
        result = str(engine.report(args.run_id))
    else:
        result = str(engine.diagnostic_zip(args.run_id))
    return result


def main(argv=None):
    output = None
    handler = None
    parse_failure = False
    try:
        args, output = extract_json_output(list(sys.argv[1:] if argv is None else argv))
        prepare_json_output(output)
        args = _parser().parse_args(args)
        if args.settings_dir:
            os.environ['RECHECK_DATA_DIR'] = str(Path(args.settings_dir).resolve())
        cancel = threading.Event()
        if threading.current_thread() is threading.main_thread():
            handler = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT, lambda *_: cancel.set())
        result = _execute(args, cancel)
        code = result_code(result)
        emit_json_result(public_result(result), output)
        return code
    except HelpRequest as help_request:
        if output is not None:
            emit_json_result({'status': 'success', 'help': help_request.text}, output)
        else:
            _stdout(help_request.text.rstrip())
        return 0
    except (KeyboardInterrupt, asyncio.CancelledError):
        result = dict(status='cancelled', code='cancelled', message='작업이 취소되었습니다.')
    except Exception as exc:
        parse_failure = isinstance(exc, UsageError)
        result = safe_error(exc)
        if result['code'] == 'output_conflict':
            output = None
    finally:
        if handler is not None:
            signal.signal(signal.SIGINT, handler)
    try:
        emit_json_result(result, output)
    except (OSError, CLIError) as exc:
        emit_json_result(safe_error(exc))
    if parse_failure:
        raise SystemExit(2)
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
