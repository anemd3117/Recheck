"""사용자가 선택한 AI 연결을 보존하고 재실행에서도 그대로 사용한다."""

import json

import pytest

from recheck.models import Settings, load_settings


@pytest.mark.parametrize(
    "provider,endpoint",
    [
        ("openai", "https://api.openai.com/v1"),
        ("anthropic", "https://api.anthropic.com/v1"),
        ("gemini", "https://generativelanguage.googleapis.com/v1beta"),
        ("compatible", "http://127.0.0.1:1234/v1"),
    ],
)
def test_selected_provider_survives_restart_without_replacing_local_model(
    tmp_path, provider, endpoint
):
    # Missing profile support used to force every selection back to Ollama.
    assert hasattr(Settings(), "ai_profiles"), "여러 AI 연결을 저장하고 선택할 수 있어야 합니다"
    from ai_providers.profiles import ProfileManager, resolve_profile

    initial = {"model": "user-local-model", "num_ctx": 8192, "num_predict": 2048}
    raw = json.dumps(initial).encode("utf-8")
    (tmp_path / "settings.json").write_bytes(raw)
    manager = ProfileManager(settings=load_settings(root=tmp_path), root=tmp_path)
    profile = manager.save_profile(
        {
            "profile_id": provider + "-chosen",
            "name": "내 AI 연결",
            "provider": provider,
            "endpoint": endpoint,
            "auth_mode": "api_key",
            "enabled": True,
            "model": "user-chosen-model",
        }
    )
    manager.select_profile(profile.profile_id)
    reloaded = load_settings(root=tmp_path)
    selected = resolve_profile(reloaded)
    assert (selected.provider, selected.endpoint, selected.model) == (
        provider,
        endpoint,
        "user-chosen-model",
    )
    assert (reloaded.model, reloaded.num_ctx, reloaded.num_predict) == (
        "user-local-model",
        8192,
        2048,
    )
    manager = ProfileManager(settings=reloaded, root=tmp_path)
    manager.select_profile("legacy-local")
    assert resolve_profile(load_settings(root=tmp_path)).provider == "ollama"
    assert load_settings(root=tmp_path).model == "user-local-model"
