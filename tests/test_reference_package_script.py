"""GUI가 실행 예산·모델을 바꾸면 같은 설정의 패키지 검증으로 인정하지 않는다."""

import importlib.util
import os
from pathlib import Path
import subprocess
import sys

from recheck.models import Settings

SPEC = importlib.util.spec_from_file_location(
    'reference_package', Path(__file__).resolve().parents[1] / 'scripts/verify_reference_package.py'
)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def test_help_with_explicit_samples_root_does_not_discover_a_default(tmp_path):
    root = Path(__file__).resolve().parents[1]
    data = tmp_path / 'isolated-data'
    env = {
        **os.environ,
        'PYTHONPATH': str(root),
        'PYTHONDONTWRITEBYTECODE': '1',
        'RECHECK_DATA_DIR': str(data),
        'PYTHONUTF8': '1',
    }
    result = subprocess.run(
        [
            sys.executable,
            '-B',
            str(root / 'scripts/verify_reference_package.py'),
            '--samples-root',
            str(tmp_path / 'explicit-samples'),
            '--help',
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace',
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert '--samples-root' in result.stdout
    assert not data.exists()


def run_record(settings):
    return {
        'settings': settings.model_dump(),
        'llm': {
            'model': settings.model,
            'settings': {'num_ctx': settings.num_ctx, 'num_predict': settings.num_predict},
        },
    }


def test_gui_execution_with_the_requested_model_and_budget_is_accepted():
    settings = Settings(model='selected', num_ctx=16384, num_predict=4096)
    assert module.check_execution_settings(run_record(settings), settings)['passed'] is True


def test_changed_gui_output_budget_is_rejected_even_if_analysis_succeeded():
    settings = Settings(model='selected', num_ctx=16384, num_predict=4096)
    run = run_record(settings)
    run['settings']['num_predict'] = 2048
    run['llm']['settings']['num_predict'] = 2048
    assert module.check_execution_settings(run, settings)['passed'] is False


def test_model_used_by_ai_must_match_the_requested_model():
    settings = Settings(model='selected')
    run = run_record(settings)
    run['llm']['model'] = 'unrequested-model'
    assert module.check_execution_settings(run, settings)['passed'] is False
