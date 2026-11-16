import asyncio
import json
import threading

import httpx
import pytest

from recheck.ollama import OllamaClient, OllamaError, discover_ollama

META = {
    "details": {"format": "gguf", "family": "gemma4"},
    "model_info": {"gemma4.context_length": 8192},
    "capabilities": ["completion", "tools"],
}
PS = {
    "models": [
        {
            "name": "gemma4:fixture",
            "digest": "abc",
            "size": 12345,
            "size_vram": 1024,
            "context_length": 2048,
        }
    ]
}


class Fragments(httpx.AsyncByteStream):
    def __init__(self, content):
        self.content = content

    async def __aiter__(self):
        for i in range(0, len(self.content), 3):
            yield self.content[i : i + 3]


def transport(chat=None, metadata=None, ps=None):
    async def handler(request):
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.20.0"})
        if request.url.path == "/api/show":
            return httpx.Response(200, json=metadata or META)
        if request.url.path == "/api/ps":
            return httpx.Response(200, json=PS if ps is None else ps)
        if request.url.path == "/api/chat":
            data = (
                chat
                if chat is not None
                else b'{"message":{"content":"\\uac00"},"done":false}\n{"message":{"content":" OK"},"done":true}\n'
            )
            return httpx.Response(200, stream=Fragments(data))
        return httpx.Response(404, json={"error": "not found"})

    return httpx.MockTransport(handler)


def run(coro):
    return asyncio.run(coro)


def test_probe_api_independent_of_cli(monkeypatch):
    monkeypatch.setattr(
        "recheck.ollama.discover_ollama", lambda: {"installed": False, "executable": None}
    )

    async def scenario():
        client = OllamaClient(transport=transport())
        try:
            result = await client.probe()
            assert result["connected"] and not result["installed"]
            assert result["version"] == "0.20.0"
        finally:
            await client.close()

    run(scenario())


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com:11434",
        "http://127.0.0.1:11434@evil.test",
        "http://localhost:11434/prefix",
        "http://localhost:11434?x=1",
    ],
)
def test_only_loopback_url(url):
    with pytest.raises(OllamaError, match="로컬"):
        OllamaClient(url)


def test_fragmented_stream_unicode_and_real_execution():
    async def scenario():
        client = OllamaClient(transport=transport())
        try:
            result = await client.chat("gemma4:fixture", [{"role": "user", "content": "ok"}])
            assert result["message"]["content"] == "가 OK"
            assert result["done"] is True
            assert result["_execution"]["digest"] == "abc"
        finally:
            await client.close()

    run(scenario())


@pytest.mark.parametrize(
    "data,code",
    [
        (b'{"message":{"content":"partial"},"done":false}\n', "incomplete_stream"),
        (b'{"error":"out of memory"}\n', "model_memory"),
        (b'not JSON\n', "stream_json"),
        (b'{"message":"bad","done":true}\n', "stream_json"),
        (b'{"message":{},"done":"true"}\n', "stream_json"),
        (b'{"message":{},"done":false}\n{"error":"context length too long"}\n', "context_exceeded"),
    ],
)
def test_stream_failure_types(data, code):
    async def scenario():
        client = OllamaClient(transport=transport(data))
        try:
            with pytest.raises(OllamaError) as error:
                await client.chat("gemma4:fixture", [])
            assert error.value.code == code
        finally:
            await client.close()

    run(scenario())


def test_cancellation_during_stalled_http():
    cancel = threading.Event()

    async def handler(request):
        await asyncio.sleep(5)
        return httpx.Response(200, json=META)

    async def scenario():
        client = OllamaClient(transport=httpx.MockTransport(handler))
        asyncio.get_running_loop().call_later(0.03, cancel.set)
        try:
            with pytest.raises(OllamaError) as error:
                await client.chat("gemma4:fixture", [], cancel=cancel)
            assert error.value.code == "cancelled"
        finally:
            await client.close()

    run(scenario())


def test_cloud_metadata_never_gets_prompts():
    calls = []

    async def handler(request):
        calls.append(request.url.path)
        return httpx.Response(
            200, json={"remote_host": "https://ollama.com", "remote_model": "gemma4"}
        )

    async def scenario():
        client = OllamaClient(transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(OllamaError) as error:
                await client.chat("a-cloud-alias", [{"role": "user", "content": "sensitive"}])
            assert error.value.code == "cloud_model_blocked"
            assert calls == ["/api/show"]
        finally:
            await client.close()

    run(scenario())


def test_ps_missing_is_not_claimed_local_execution():
    async def scenario():
        client = OllamaClient(transport=transport(ps={"models": []}))
        try:
            with pytest.raises(OllamaError) as error:
                await client.chat("gemma4:fixture", [])
            assert error.value.code == "local_execution_unverified"
            assert "response" in error.value.details
        finally:
            await client.close()

    run(scenario())


def test_models_preserve_tags_and_prioritize_gemma4():
    async def handler(request):
        return httpx.Response(
            200,
            json={
                "models": [
                    {"name": "z:local", "digest": "z"},
                    {
                        "name": "gemma4:exact-quant",
                        "digest": "g",
                        "details": {"quantization_level": "Q4_K_M"},
                    },
                    {"name": "big:cloud", "remote_model": "big"},
                ]
            },
        )

    async def scenario():
        client = OllamaClient(transport=httpx.MockTransport(handler))
        try:
            models = await client.models()
            assert models[0]["name"] == "gemma4:exact-quant"
            assert models[0]["details"]["quantization_level"] == "Q4_K_M"
            assert next(m for m in models if m["name"] == "big:cloud")["execution_kind"] == "cloud"
        finally:
            await client.close()

    run(scenario())


def test_capabilities_actual_schema_and_harmless_tool_roundtrip():
    called = []

    async def handler(request):
        if request.url.path == "/api/show":
            return httpx.Response(200, json=META)
        if request.url.path == "/api/ps":
            return httpx.Response(200, json=PS)
        body = json.loads(request.content)
        called.append(body)
        if any(message.get('role') == 'tool' for message in body.get('messages', [])):
            message = {"content": '{"ready":true}'}
        elif body.get("tools"):
            message = {"tool_calls": [{"function": {"name": "get_status", "arguments": {}}}]}
        elif body.get("format"):
            message = {"content": '{"ok":true}'}
        else:
            message = {"content": "OK"}
        return httpx.Response(200, content=json.dumps({"message": message, "done": True}) + "\n")

    async def scenario():
        client = OllamaClient(transport=httpx.MockTransport(handler))
        try:
            result = await client.capabilities("gemma4:fixture")
            assert result["basic"] and result["json_schema"] and result["tools"]
            assert result["available_modes"] == ["automatic", "agent"]
            assert len(called) == 4
            assert called[-1]["messages"][-2]["role"] == "tool"
        finally:
            await client.close()

    run(scenario())


def test_pull_progress_requires_success():
    events = []

    async def handler(request):
        return httpx.Response(
            200,
            content='{"status":"downloading","total":100,"completed":50}\n{"status":"success"}\n',
        )

    async def scenario():
        client = OllamaClient(transport=httpx.MockTransport(handler))
        try:
            assert (await client.pull("gemma4:exact", on_event=events.append))[
                "status"
            ] == "success"
            assert events[0]["completed"] == 50
        finally:
            await client.close()

    run(scenario())


def test_http_failure_and_incompatible_server():
    async def handler(request):
        return httpx.Response(200, text="not ollama")

    async def scenario():
        client = OllamaClient(transport=httpx.MockTransport(handler))
        try:
            assert (await client.probe())["code"] == "incompatible_server"
        finally:
            await client.close()

    run(scenario())


def test_context_metadata_limit():
    async def scenario():
        client = OllamaClient(transport=transport())
        try:
            with pytest.raises(OllamaError) as error:
                await client.chat("gemma4:fixture", [], options={"num_ctx": 16384})
            assert error.value.code == "context_exceeded"
        finally:
            await client.close()

    run(scenario())


@pytest.mark.parametrize(
    'follow',
    [
        {'content': ''},
        {'content': '', 'tool_calls': [{'function': {'name': 'get_status', 'arguments': {}}}]},
        {'content': '{"ready":false}'},
        {'content': 'OK'},
    ],
)
def test_tool_capability_rejects_incomplete_or_unsupported_roundtrip(monkeypatch, follow):
    async def scenario():
        client = OllamaClient(transport=transport())

        async def metadata(*args, **kwargs):
            return META

        async def chat(model, messages, **kwargs):
            if kwargs.get('tools'):
                message = {
                    'content': '',
                    'tool_calls': [{'function': {'name': 'get_status', 'arguments': {}}}],
                }
            elif kwargs.get('format'):
                message = {'content': '{"ok":true}'}
            elif any(message.get('role') == 'tool' for message in messages):
                message = follow
            else:
                message = {'content': 'OK'}
            return {'message': message, 'done': True}

        monkeypatch.setattr(client, 'local_metadata', metadata)
        monkeypatch.setattr(client, 'chat', chat)
        try:
            result = await client.capabilities('gemma4:fixture')
            assert result['basic'] and result['json_schema']
            assert result['tools'] is False
            assert result['available_modes'] == ['automatic']
            assert 'tools' in result['errors']
        finally:
            await client.close()

    run(scenario())


@pytest.mark.parametrize(
    'trailing',
    [
        '{"message":{"content":"unfinished"},"done":false}',
        '{"message":{"content":"duplicate"},"done":true}',
    ],
)
def test_completion_marker_must_be_terminal(trailing):
    async def scenario():
        data = ('{"message":{"content":"complete"},"done":true}\n' + trailing + '\n').encode()
        client = OllamaClient(transport=transport(chat=data))
        try:
            with pytest.raises(OllamaError) as error:
                await client.chat('gemma4:fixture', [])
            assert error.value.code == 'stream_after_completion'
            assert error.value.details['partial_content'] == 'complete'
        finally:
            await client.close()

    run(scenario())


def test_cancellation_during_capability_metadata_propagates():
    async def scenario():
        event = threading.Event()
        event.set()
        client = OllamaClient(transport=transport())
        try:
            with pytest.raises(OllamaError) as error:
                await client.capabilities('gemma4:fixture', cancel=event)
            assert error.value.code == 'cancelled'
        finally:
            await client.close()

    run(scenario())
