"""단독 CLI의 AI 선택·설정 경계와 비밀값 노출 방지."""

import json
import os
from pathlib import Path
import subprocess
import sys


def command(settings, *args):
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8", RECHECK_DATA_DIR=str(settings))
    return subprocess.run(
        [sys.executable, "-m", "recheck", "--settings-dir", str(settings), *args],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )


def test_cli_lists_profiles_and_keeps_the_settings_root_separate_from_results(tmp_path):
    settings, results = tmp_path / "settings", tmp_path / "results"
    result = command(settings, "--data-dir", str(results), "ai", "profiles")
    assert result.returncode == 0, result.stderr
    profiles = json.loads(result.stdout)
    assert {item['profile_id'] for item in profiles} == {'legacy-local', 'gemini_free_default'}
    fresh = next(item for item in profiles if item['profile_id'] == 'gemini_free_default')
    assert fresh['provider'] == 'gemini' and not fresh['credential_ref']
    assert not fresh['enabled'] and not fresh['external_consent']
    assert not results.exists()


def test_cli_selects_imported_remote_profile_and_can_switch_back_to_ollama(tmp_path):
    settings = tmp_path / "settings"
    settings.mkdir()
    (settings / "settings.json").write_text('{"model":"saved-local-model"}', encoding="utf-8")
    profile = tmp_path / "profile.json"
    profile.write_text(
        json.dumps(
            {
                "profile_id": "chosen-api",
                "name": "내 AI",
                "provider": "openai",
                "auth_mode": "api_key",
                "model": "user-model",
                "enabled": True,
                "structured_mode": "json_prompt",
            }
        ),
        encoding="utf-8",
    )
    imported = command(settings, "ai", "import", str(profile))
    assert imported.returncode == 0, imported.stderr
    selected = command(settings, "ai", "select", "chosen-api")
    assert selected.returncode == 0, selected.stderr
    assert (
        json.loads((settings / "settings.json").read_text(encoding="utf-8"))["active_ai_profile"]
        == "chosen-api"
    )
    assert command(settings, "ai", "select", "legacy-local").returncode == 0
    assert (
        json.loads((settings / "settings.json").read_text(encoding="utf-8"))["active_ai_profile"]
        == "legacy-local"
    )
    assert (
        json.loads((settings / "settings.json").read_text(encoding="utf-8"))["model"]
        == "saved-local-model"
    )


def test_cli_invalid_secret_argument_returns_a_code_without_echoing_the_value(tmp_path):
    result = command(tmp_path, "ai", "profiles", "--api-key", "DO-NOT-ECHO-THIS-VALUE")
    assert result.returncode == 2
    assert "DO-NOT-ECHO-THIS-VALUE" not in result.stdout + result.stderr
    assert json.loads(result.stdout)["code"] == "invalid_arguments"


def test_cli_json_file_is_utf8_and_never_overwrites_existing_content(tmp_path):
    output = tmp_path / "result.json"
    result = command(tmp_path / "settings", "ai", "profiles", "--json-output", str(output))
    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text(encoding="utf-8"))[0]["profile_id"] == "legacy-local"
    before = output.read_bytes()
    repeated = command(tmp_path / "settings", "ai", "profiles", "--json-output", str(output))
    assert repeated.returncode == 2
    assert output.read_bytes() == before
