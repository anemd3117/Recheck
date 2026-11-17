"""Immutable saved problem inputs and links to fresh pair comparisons.

Saved cases are not correctness oracles. A later ``resolved`` observation means
the two selected programs now agree under the saved comparison policy; it does
not prove that either program preserves all behavior of the original version.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from .comparison import compare_behavior
from .models import Settings, validate_tests
from .storage import Store, new_id, now

MAX_SUITE_BYTES = 32 * 1024 * 1024
MAX_AUXILIARY_FILE_BYTES = 8 * 1024 * 1024
MAX_AUXILIARY_TOTAL_BYTES = 32 * 1024 * 1024
MAX_SUITES = 1000
POLICY_FIELDS = ('normalize_newlines', 'trim_trailing_whitespace')
FILE_FIELDS = ('name', 'path', 'snapshot', 'sha256', 'format', 'os', 'architecture')


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def _link(path):
    return path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction())


def _policy(value):
    if not isinstance(value, dict) or any(key not in value for key in POLICY_FIELDS):
        raise ValueError('저장된 출력 정규화 정책을 확인할 수 없습니다.')
    result = {key: value[key] for key in POLICY_FIELDS}
    if any(type(item) is not bool for item in result.values()):
        raise ValueError('저장된 출력 정규화 정책을 확인할 수 없습니다.')
    return result


def _recorded_policy(run):
    settings = run.get('settings')
    if isinstance(settings, dict) and all(key in settings for key in POLICY_FIELDS):
        return _policy(settings)
    recorded = (run.get('behavior') or {}).get('normalization')
    if isinstance(recorded, dict):
        return _policy(dict(normalize_newlines=recorded.get('newlines'),
                            trim_trailing_whitespace=recorded.get('trailing_whitespace')))
    raise ValueError('분석 당시 출력 정규화 정책을 확인할 수 없어 저장하지 않았습니다.')


def _test_inputs(values):
    try:
        return {test.id: test.model_dump() for test in validate_tests(values)}
    except (ValueError, TypeError):
        return {}


def _same_input(expected, actual):
    if not isinstance(actual, dict):
        return False
    parsed = _test_inputs([actual]).get(expected['id'])
    return parsed is not None and (
        {Path(value).name for value in parsed['aux_files']}
        == {Path(value).name for value in expected['aux_files']}
    ) and all(
        parsed[key] == expected[key]
        for key in ('args', 'stdin', 'env', 'timeout_seconds', 'limits')
    )


def _actual_input_matches(expected, observation):
    if not isinstance(observation, dict):
        return False
    # Older adapters may only record the engine's row input. When actual
    # execution provenance exists, it must not contradict that registered input.
    if 'actual_input' in observation:
        actual = observation['actual_input']
        if not isinstance(actual, dict) or any(
            actual.get(key) != expected[key] for key in ('args', 'stdin', 'env')
        ):
            return False
        if not expected['aux_files'] and actual.get('auxiliary_files', []) != []:
            return False
    if 'limits' in observation:
        limits = observation['limits']
        required = {key: value for key, value in expected['limits'].items() if value is not None}
        required['timeout_seconds'] = expected['timeout_seconds']
        if not isinstance(limits, dict) or any(
            limits.get(key) != value for key, value in required.items()
        ):
            return False
    return True


def _read_auxiliary(path, expected_sha256):
    path = Path(path)
    if _link(path) or not path.is_file():
        raise ValueError('보조 파일이 없거나 링크입니다.')
    before = path.stat()
    if before.st_size > MAX_AUXILIARY_FILE_BYTES:
        raise ValueError('보조 파일은 하나당 8 MiB까지 저장할 수 있습니다.')
    with path.open('rb') as stream:
        data = stream.read(MAX_AUXILIARY_FILE_BYTES + 1)
    after = path.stat()
    if (len(data) != before.st_size or len(data) > MAX_AUXILIARY_FILE_BYTES
            or (before.st_size, before.st_mtime_ns, before.st_ino)
            != (after.st_size, after.st_mtime_ns, after.st_ino)
            or hashlib.sha256(data).hexdigest() != expected_sha256):
        raise ValueError('보조 파일이 분석 당시의 내용과 다르거나 읽는 동안 변경되었습니다.')
    return data


def _new_json(path, value):
    """Publish one new manifest atomically without replacing an existing file."""
    data = json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8')
    if len(data) > MAX_SUITE_BYTES:
        raise ValueError('저장할 회귀 테스트가 32 MiB 한도를 넘습니다.')
    descriptor, name = tempfile.mkstemp(prefix='.suite-', suffix='.tmp', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name == 'nt':
            temporary.rename(path)
        else:
            os.link(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


class RegressionLibrary:
    def __init__(self, root=None):
        self.store = Store(root)
        self.directory = self.store.root / 'regressions'
        if _link(self.directory):
            raise ValueError('회귀 테스트 저장 폴더에 링크를 사용할 수 없습니다.')
        self.directory.mkdir(parents=True, exist_ok=True)

    def _folder(self, suite_id):
        if not isinstance(suite_id, str) or not re.fullmatch(r'suite_[a-f0-9]{32}', suite_id):
            raise ValueError('회귀 테스트 ID가 올바르지 않습니다.')
        path = self.directory / suite_id
        if _link(path):
            raise ValueError('회귀 테스트 저장 폴더에 링크를 사용할 수 없습니다.')
        return path

    def load_suite(self, suite_id):
        path = self._folder(suite_id) / 'suite.json'
        try:
            if _link(path) or not path.is_file() or path.stat().st_size > MAX_SUITE_BYTES:
                raise ValueError('Invalid suite manifest')
            value = json.loads(path.read_text(encoding='utf-8'))
            if (not isinstance(value, dict) or value.get('schema_version') != 1
                    or value.get('suite_id') != suite_id):
                raise ValueError('Invalid suite identity')
            checksum = value.get('payload_sha256')
            unsigned = {key: item for key, item in value.items() if key != 'payload_sha256'}
            if checksum != _digest(unsigned):
                raise ValueError('Suite contents changed')
            tests = validate_tests(value['tests'])
            if not isinstance(value.get('name'), str) or len(value['name']) > 200:
                raise ValueError('Invalid suite name')
            if not re.fullmatch(r'run_[a-f0-9]{32}', value['source_run_id']):
                raise ValueError('Invalid source identity')
            if _policy(value['comparison_policy']) != value['comparison_policy']:
                raise ValueError('Invalid policy')
            if (not isinstance(value.get('cases'), list)
                    or [case['test_id'] for case in value['cases']] != [test.id for test in tests]
                    or not isinstance(value.get('source_files'), dict)):
                raise ValueError('Invalid saved cases')
            for case, test in zip(value['cases'], tests):
                if (case['baseline'].get('execution_status') not in ('measured', 'unmeasured')
                        or case['baseline'].get('match') is True
                        or not isinstance(case.get('auxiliary'), list)
                        or len(case['auxiliary']) != len(test.aux_files)):
                    raise ValueError('Invalid baseline observation')
            return value
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raise ValueError('저장된 회귀 테스트를 읽거나 검증할 수 없습니다. 원본은 보존했습니다.') from None

    def list_suites(self):
        result = []
        for path in sorted(self.directory.glob('suite_*'))[:MAX_SUITES]:
            if not (path / 'suite.json').exists():
                continue
            try:
                suite = self.load_suite(path.name)
                result.append(dict(suite_id=suite['suite_id'], name=suite['name'],
                                   created=suite['created'], source_run_id=suite['source_run_id'],
                                   case_count=len(suite['tests']), status='ready'))
            except ValueError as exc:
                result.append(dict(suite_id=path.name, name='읽을 수 없는 회귀 테스트',
                                   created='', case_count=0, status='unreadable', reason=str(exc)))
        return sorted(result, key=lambda item: item['created'], reverse=True)

    def _historical_auxiliary(self, run, row, source):
        name = Path(source).name
        if not name or name in ('.', '..', 'target', 'target.exe') or any(c in name for c in '/\\:\0'):
            raise ValueError('보조 파일 이름이 올바르지 않습니다.')
        candidates, hashes = [], set()
        run_folder = self.store.run_dir(run['run_id']).resolve()
        for side in ('original', 'reimplementation'):
            observation = row.get(side) or {}
            if not isinstance(observation, dict):
                continue
            actual = observation.get('actual_input') or {}
            for item in actual.get('auxiliary_files', []):
                if item.get('name') == name:
                    digest = item.get('sha256')
                    if not isinstance(digest, str) or not re.fullmatch('[a-f0-9]{64}', digest):
                        raise ValueError('보조 파일의 분석 당시 해시가 올바르지 않습니다.')
                    hashes.add(digest)
            staging = (observation.get('input_staging') or {}).get('artifact')
            if staging:
                staged = Path(staging)
                if not _link(staged) and staged.resolve().is_relative_to(run_folder):
                    candidates.append(staged / name)
        if len(hashes) != 1:
            raise ValueError('보조 파일의 분석 당시 내용을 확인할 수 없습니다. 원본 입력을 다시 분석하세요.')
        expected = next(iter(hashes))
        candidates.append(Path(source))
        for candidate in candidates:
            try:
                return name, expected, _read_auxiliary(candidate, expected)
            except (OSError, ValueError):
                continue
        raise ValueError('보조 파일이 변경되었거나 없어 분석 당시 입력을 저장할 수 없습니다.')

    def save_from_run(self, run_id, test_ids=None, name=''):
        if not isinstance(name, str) or len(name) > 200 or any(ord(c) < 32 for c in name):
            raise ValueError('회귀 테스트 이름은 제어 문자 없이 200자 이내로 입력하세요.')
        run = self.store.load_run(run_id)
        scope = run.get('analysis_mode') or ('static' if run.get('mode') == 'static' else 'both')
        if scope == 'static':
            raise ValueError('동적분석에서 발견한 문제 입력만 회귀 테스트로 저장할 수 있습니다.')
        tests = [test.model_dump() for test in validate_tests(run.get('tests', []))]
        policy = _recorded_policy(run)
        rows = run.get('dynamic') or []
        known = {test['id'] for test in tests}
        if (not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows)
                or any(not isinstance(row.get('test_id'), str) or row['test_id'] not in known for row in rows)
                or len({row.get('test_id') for row in rows}) != len(rows)):
            raise ValueError('원본 실행 관찰의 입력 ID를 확인할 수 없습니다.')
        by_id = {row['test_id']: row for row in rows}
        observed = {row['test_id']: row for row in compare_behavior(rows, Settings(**policy))['tests']}
        baseline = {test['id']: observed.get(test['id'], dict(
            test_id=test['id'], execution_status='unmeasured', match=None,
            observation_categories=['not_executed'])) for test in tests}
        available = {test['id'] for test in tests if baseline[test['id']]['match'] is not True}
        if test_ids is None:
            selected = available
        elif (not isinstance(test_ids, list) or not test_ids or any(not isinstance(x, str) for x in test_ids)
              or len(set(test_ids)) != len(test_ids) or not set(test_ids) <= available):
            raise ValueError('불일치·미측정 입력을 중복 없이 선택하세요.')
        else:
            selected = set(test_ids)
        if not selected:
            raise ValueError('저장할 불일치·미측정 입력이 없습니다.')
        if len(list(self.directory.glob('suite_*/suite.json'))) >= MAX_SUITES:
            raise ValueError('회귀 테스트 보관함은 1,000개까지 저장할 수 있습니다.')
        suite_id = new_id('suite')
        folder = self._folder(suite_id)
        saved_tests, cases, files = [], [], []
        total = 0
        # Capture and verify every auxiliary byte before creating a manifest.
        for index, test in enumerate(test for test in tests if test['id'] in selected):
            original_aux = list(test['aux_files'])
            test['aux_files'] = []
            auxiliary = []
            for source in original_aux:
                filename, digest, data = self._historical_auxiliary(run, by_id.get(test['id'], {}), source)
                total += len(data)
                if total > MAX_AUXILIARY_TOTAL_BYTES:
                    raise ValueError('보조 파일의 전체 크기가 32 MiB 한도를 넘습니다.')
                relative = f'aux/{index:03d}/{filename}'
                test['aux_files'].append(relative)
                auxiliary.append(dict(name=filename, path=relative, sha256=digest, size=len(data)))
                files.append((relative, data))
            saved_tests.append(test)
            cases.append(dict(test_id=test['id'], baseline=copy.deepcopy(baseline[test['id']]), auxiliary=auxiliary))
        suite = dict(schema_version=1, suite_id=suite_id, name=name.strip() or '발견한 문제 다시 검사',
                     created=now(), source_run_id=run_id,
                     source_files={side: {key: info[key] for key in FILE_FIELDS if key in info}
                                   for side, info in (run.get('files') or {}).items()
                                   if side in ('original', 'reimplementation') and isinstance(info, dict)},
                     comparison_policy=policy, tests=saved_tests, cases=cases)
        suite['payload_sha256'] = _digest(suite)
        if len(json.dumps(suite, ensure_ascii=False).encode('utf-8')) > MAX_SUITE_BYTES:
            raise ValueError('저장할 회귀 테스트가 32 MiB 한도를 넘습니다.')
        folder.mkdir()
        created = []
        try:
            for relative, data in files:
                path = folder / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open('xb') as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                created.append(path)
            _new_json(folder / 'suite.json', suite)
        except BaseException:
            # Remove only files created by this uncommitted new suite.
            for path in reversed(created):
                path.unlink(missing_ok=True)
            for directory in sorted((p for p in folder.rglob('*') if p.is_dir()), reverse=True):
                directory.rmdir()
            folder.rmdir()
            raise
        return suite

    def prepare_tests(self, suite_id):
        suite = self.load_suite(suite_id)
        folder = self._folder(suite_id).resolve()
        tests = copy.deepcopy(suite['tests'])
        for test, case in zip(tests, suite['cases']):
            prepared = []
            for relative, item in zip(test['aux_files'], case['auxiliary']):
                path = Path(relative)
                if (path.is_absolute() or '..' in path.parts or relative != item.get('path')
                        or Path(item.get('name', '')).name != item.get('name')):
                    raise ValueError('저장된 보조 파일 경로가 올바르지 않습니다.')
                path = folder / path
                if (not path.resolve().is_relative_to(folder)
                        or any(_link(parent) for parent in path.parents if parent != folder and parent.is_relative_to(folder))):
                    raise ValueError('저장된 보조 파일 경로가 저장 범위를 벗어났습니다.')
                try:
                    data = _read_auxiliary(path, item['sha256'])
                    if len(data) != item['size'] or path.name != item['name']:
                        raise ValueError('Auxiliary metadata mismatch')
                except (OSError, ValueError, KeyError, TypeError):
                    raise ValueError('저장된 보조 파일이 없거나 내용이 변경되었습니다.') from None
                prepared.append(str(path))
            test['aux_files'] = prepared
        return tests

    @staticmethod
    def require_policy(suite, settings):
        if suite['comparison_policy'] != {key: getattr(settings, key) for key in POLICY_FIELDS}:
            raise ValueError('출력 정규화 정책이 저장 당시와 다릅니다. 저장한 정책으로 맞춘 뒤 다시 검사하세요.')

    @staticmethod
    def recheck_result(suite, run):
        rows = run.get('dynamic') or []
        expected_tests = _test_inputs(suite.get('tests', []))
        registered = _test_inputs(run.get('tests', []))
        try:
            policy_matches = _recorded_policy(run) == suite['comparison_policy']
        except ValueError:
            policy_matches = False
        results = []
        for saved in suite['cases']:
            test_id = saved['test_id']
            candidates = [row for row in rows if isinstance(row, dict) and row.get('test_id') == test_id]
            if len(candidates) == 1:
                current = compare_behavior(candidates, Settings(**suite['comparison_policy']))['tests'][0]
            else:
                current = dict(test_id=test_id, match=None, execution_status='unmeasured',
                               observation_categories=['invalid_observation' if candidates else 'not_executed'])
            expected_test = expected_tests.get(test_id)
            reproduced = bool(
                policy_matches and expected_test and len(candidates) == 1
                and _same_input(expected_test, registered.get(test_id))
                and _same_input(expected_test, candidates[0].get('input'))
                and all(_actual_input_matches(expected_test, candidates[0].get(side))
                        for side in ('original', 'reimplementation'))
            )
            if saved['auxiliary']:
                expected = {item['name']: item['sha256'] for item in saved['auxiliary']}
                for side in ('original', 'reimplementation'):
                    observation = (candidates[0].get(side) or {}) if len(candidates) == 1 else {}
                    actual_input = observation.get('actual_input') if isinstance(observation, dict) else None
                    actual = actual_input.get('auxiliary_files') if isinstance(actual_input, dict) else None
                    complete = (isinstance(actual, list) and len(actual) == len(expected)
                                and all(isinstance(item, dict) and isinstance(item.get('name'), str) for item in actual))
                    reproduced = reproduced and complete and {item.get('name'): item.get('sha256') for item in actual} == expected
            status = ('unmeasured' if current['match'] is None or not reproduced else
                      'resolved' if current['match'] is True else 'still_different')
            results.append(dict(test_id=test_id, status=status,
                                baseline=copy.deepcopy(saved['baseline']),
                                current=copy.deepcopy(current), saved_input_reproduced=reproduced))
        old = (suite.get('source_files') or {}).get('original', {}).get('sha256')
        new = (run.get('files') or {}).get('original', {}).get('sha256')
        return dict(suite_id=suite['suite_id'], name=suite['name'], source_run_id=suite['source_run_id'],
                    checked_at=now(), policy='pair_equivalence', comparison_policy=suite['comparison_policy'],
                    reference_changed=None if not old or not new else old != new,
                    source_files=suite['source_files'], case_results=results,
                    counts={name: sum(case['status'] == name for case in results)
                            for name in ('resolved', 'still_different', 'unmeasured')},
                    scope='저장한 입력에서 현재 선택한 두 프로그램의 일치 여부입니다. 원본 버전의 모든 동작 보존이나 결함 수정의 증명은 아닙니다.')
