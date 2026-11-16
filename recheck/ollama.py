"""Native, cancellable Ollama HTTP adapter. No OpenAI-compatible endpoint is used."""

from __future__ import annotations

import asyncio
import inspect
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

DEFAULT_URL = "http://127.0.0.1:11434"
_START_LOCK = threading.Lock()


class OllamaError(RuntimeError):
    def __init__(self, code: str, message: str, details: Any = None):
        self.code, self.details = code, details
        super().__init__(message)


def cancelled(cancel=None):
    return cancel is not None and cancel.is_set()


async def _wait(awaitable, cancel=None, timeout=120):
    task = asyncio.ensure_future(awaitable)
    start = time.monotonic()
    try:
        while not task.done():
            if cancelled(cancel):
                raise OllamaError("cancelled", "사용자가 Ollama 요청을 취소했습니다.")
            if time.monotonic() - start > timeout:
                raise OllamaError("response_timeout", "Ollama 응답 제한 시간을 초과했습니다.")
            await asyncio.wait({task}, timeout=0.1)
        return await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def _event(callback, value):
    if callback:
        result = callback(value)
        if inspect.isawaitable(result):
            await result


def discover_ollama() -> dict:
    """PATH, standard Windows installers, and uninstall registry; never executes registry text."""
    candidates = []
    found = shutil.which("ollama")
    if found:
        candidates.append(found)
    for root in (os.getenv("LOCALAPPDATA", ""), os.getenv("ProgramFiles", "")):
        for suffix in ("Programs/Ollama/ollama.exe", "Ollama/ollama.exe"):
            candidate = Path(root) / suffix
            if candidate.is_file():
                candidates.append(str(candidate))
    if os.name == "nt":
        import winreg

        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for keyname in (
                r"Software\Microsoft\Windows\CurrentVersion\Uninstall",
                r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
            ):
                try:
                    with winreg.OpenKey(hive, keyname) as key:
                        for i in range(winreg.QueryInfoKey(key)[0]):
                            try:
                                with winreg.OpenKey(key, winreg.EnumKey(key, i)) as child:
                                    name = winreg.QueryValueEx(child, "DisplayName")[0]
                                    if "ollama" in name.lower():
                                        location = winreg.QueryValueEx(child, "InstallLocation")[0]
                                        candidate = Path(location) / "ollama.exe"
                                        if candidate.is_file():
                                            candidates.append(str(candidate))
                            except OSError:
                                pass
                except OSError:
                    pass
    paths = list(dict.fromkeys(candidates))
    return {
        "installed": bool(paths),
        "executable": paths[0] if paths else None,
        "candidates": paths,
        "download_url": "https://ollama.com/download/windows",
    }


def _cloud(metadata: dict) -> bool:
    for key in ("remote_host", "remote_model", "remote", "cloud"):
        if metadata.get(key):
            return True
    name = str(metadata.get("name", metadata.get("model", "")))
    return name.endswith(":cloud") or "-cloud:" in name


def _classify(message: str) -> str:
    lower = message.lower()
    for needles, code in [
        (
            (
                "out of memory",
                "not enough memory",
                "insufficient memory",
                "requires more system memory",
                "cuda error",
            ),
            "model_memory",
        ),
        (("not found", "does not exist"), "model_missing"),
        (("context length", "context window", "too long"), "context_exceeded"),
        (
            ("does not support tools", "does not support tool", "does not support thinking"),
            "feature_unsupported",
        ),
        (("failed to load", "unable to load"), "model_load"),
    ]:
        if any(n in lower for n in needles):
            return code
    return "server_error"


class OllamaClient:
    def __init__(self, base_url=DEFAULT_URL, timeout=120, transport=None):
        if not isinstance(base_url, str):
            base_url = getattr(base_url, "ollama_url", DEFAULT_URL)
        parsed = urlsplit(base_url)
        try:
            loopback = (
                parsed.hostname == "localhost"
                or ipaddress.ip_address(parsed.hostname or "").is_loopback
            )
        except ValueError:
            loopback = False
        if (
            not loopback
            or parsed.scheme not in ("http", "https")
            or parsed.username
            or parsed.password
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise OllamaError(
                "nonlocal_endpoint", "분석 추론에는 로컬 루프백 Ollama 주소만 사용할 수 있습니다."
            )
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.http = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout, connect=3),
            transport=transport,
            trust_env=False,
            follow_redirects=False,
        )
        self.managed_process = None
        self.managed_log = None
        self.last_execution = None

    async def close(self):
        await self.http.aclose()

    async def _json(self, method, path, body=None, cancel=None, timeout=None):
        try:
            response = await _wait(
                self.http.request(method, path, json=body), cancel, timeout or self.timeout
            )
            if response.status_code >= 400:
                message = response.text[:2000]
                raise OllamaError(
                    _classify(message) if response.status_code != 404 else "model_missing",
                    f"HTTP {response.status_code}: {message}",
                )
            value = response.json()
            if not isinstance(value, dict):
                raise OllamaError("incompatible_server", "Ollama 응답이 JSON 객체가 아닙니다.")
            if value.get("error"):
                raise OllamaError(_classify(str(value["error"])), str(value["error"]))
            return value
        except httpx.ConnectError as exc:
            raise OllamaError(
                "connection_failed", "Ollama API에 연결할 수 없습니다.", str(exc)
            ) from exc
        except httpx.TimeoutException as exc:
            raise OllamaError(
                "response_timeout", "Ollama HTTP 요청 시간이 초과되었습니다.", str(exc)
            ) from exc
        except (ValueError, httpx.RemoteProtocolError) as exc:
            raise OllamaError(
                "incompatible_server", "Ollama API JSON 응답을 확인할 수 없습니다.", str(exc)
            ) from exc

    async def probe(self, cancel=None):
        managed = self.managed_process is not None and self.managed_process.poll() is None
        result = {
            "url": self.base_url,
            **discover_ollama(),
            "connected": False,
            "managed": managed,
            "local_only_setting": (
                "OLLAMA_NO_CLOUD=1"
                if managed
                else "기존 서버의 실행 환경 미확인; 로컬 모델 메타데이터와 /api/ps를 별도 검증"
            ),
            "network": "모델 다운로드는 외부 통신, 분석 추론은 루프백 HTTP",
        }
        try:
            data = await self._json("GET", "/api/version", cancel=cancel, timeout=5)
            if not isinstance(data.get("version"), str) or not re.match(
                r"\d+\.\d+", data["version"]
            ):
                raise OllamaError(
                    "incompatible_server",
                    "이 주소의 서버가 호환 Ollama 버전 정보를 반환하지 않았습니다.",
                )
            result.update(connected=True, version=data["version"], code="connected")
        except OllamaError as exc:
            if exc.code == "cancelled":
                raise
            code = exc.code
            if code == "model_missing":
                code = "incompatible_server"
            if code == "connection_failed":
                parsed = urlsplit(self.base_url)
                try:
                    with socket.create_connection(
                        (parsed.hostname, parsed.port or 11434), timeout=0.2
                    ):
                        code = "port_conflict"
                except OSError:
                    pass
            result.update(
                code=code,
                error=str(exc),
                cli_status="found" if result["installed"] else "cli_not_found",
            )
        return result

    async def models(self, cancel=None):
        result = await self._json("GET", "/api/tags", cancel=cancel)
        if not isinstance(result.get("models"), list):
            raise OllamaError("incompatible_server", "/api/tags models 필드가 없습니다.")
        models = []
        for item in result["models"]:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                raise OllamaError("incompatible_server", "모델 목록 스키마가 잘못되었습니다.")
            models.append(
                {
                    **item,
                    "execution_kind": "cloud" if _cloud(item) else "local_candidate",
                    "local_verified": False,
                }
            )
        return sorted(
            models,
            key=lambda x: (
                "gemma4"
                not in (x["name"] + str(x.get("details", {}).get("family", "")))
                .lower()
                .replace(" ", ""),
                _cloud(x),
                x["name"],
            ),
        )

    async def show(self, model, cancel=None):
        return await self._json("POST", "/api/show", {"model": model}, cancel)

    async def local_metadata(self, model, cancel=None):
        data = await self.show(model, cancel)
        if _cloud({**data, "name": model}):
            raise OllamaError(
                "cloud_model_blocked", "클라우드 모델에는 분석 자료를 전달하지 않습니다."
            )
        details = data.get("details", {})
        if details.get("format") != "gguf" and not data.get("model_info"):
            raise OllamaError(
                "local_unverified", "선택 모델의 로컬 가중치 메타데이터를 확인할 수 없습니다."
            )
        return data

    async def _stream(self, path, body, cancel=None, on_event=None, pull=False):
        """One NDJSON object may span arbitrary network chunks; HTTP and stream errors matter."""

        async def collect():
            done, total, final, parts, calls = False, 0, {}, [], []
            async with self.http.stream("POST", path, json=body) as response:
                if response.status_code >= 400:
                    message = (await response.aread()).decode("utf-8", "replace")[:2000]
                    raise OllamaError(_classify(message), f"HTTP {response.status_code}: {message}")
                async for line in response.aiter_lines():
                    if cancelled(cancel):
                        raise OllamaError("cancelled", "Ollama 스트림을 취소했습니다.")
                    if not line.strip():
                        continue
                    if done:
                        raise OllamaError(
                            "stream_after_completion",
                            "Ollama 완료 표식 이후 추가 응답을 수신했습니다.",
                            {"partial_content": "".join(parts)},
                        )
                    total += len(line.encode("utf-8"))
                    if total > 8_000_000 or len(line) > 2_000_000:
                        raise OllamaError("output_limit", "Ollama 스트림 출력 제한을 초과했습니다.")
                    try:
                        value = json.loads(line)
                    except ValueError as exc:
                        raise OllamaError(
                            "stream_json", "Ollama 스트림의 JSON 조각이 잘못되었습니다."
                        ) from exc
                    if not isinstance(value, dict):
                        raise OllamaError("stream_json", "Ollama 스트림 객체가 잘못되었습니다.")
                    if value.get("error"):
                        raise OllamaError(
                            _classify(str(value["error"])),
                            str(value["error"]),
                            {"partial_content": "".join(parts)},
                        )
                    msg = value.get("message", {})
                    if (
                        not isinstance(msg, dict)
                        or not isinstance(msg.get("content", ""), str)
                        or not isinstance(msg.get("tool_calls", []), list)
                    ):
                        raise OllamaError(
                            "stream_json",
                            "Ollama 메시지의 content/tool_calls 형식이 잘못되었습니다.",
                        )
                    if "done" in value and not isinstance(value["done"], bool):
                        raise OllamaError("stream_json", "Ollama 완료 상태가 불리언이 아닙니다.")
                    parts.append(msg.get("content", ""))
                    calls.extend(msg.get("tool_calls", []))
                    final = value
                    await _event(on_event, value)
                    if value.get("done") is True or (pull and value.get("status") == "success"):
                        done = True
                if not done:
                    raise OllamaError(
                        "incomplete_stream",
                        "완료 표식 없이 Ollama 스트림이 종료되었습니다.",
                        {"partial_content": "".join(parts)},
                    )
                if not pull:
                    final = {
                        **final,
                        "message": {
                            "role": "assistant",
                            "content": "".join(parts),
                            **({"tool_calls": calls} if calls else {}),
                        },
                    }
                return final

        try:
            return await _wait(
                collect(), cancel, self.timeout if not pull else max(self.timeout, 3600)
            )
        except httpx.ConnectError as exc:
            raise OllamaError(
                "connection_failed", "Ollama API에 연결할 수 없습니다.", str(exc)
            ) from exc
        except httpx.TimeoutException as exc:
            raise OllamaError(
                "response_timeout", "Ollama 스트림 응답 시간이 초과되었습니다.", str(exc)
            ) from exc
        except httpx.HTTPError as exc:
            raise OllamaError(
                "stream_transport", "Ollama 스트림 전송이 중단되었습니다.", str(exc)
            ) from exc

    async def pull(self, model, cancel=None, on_event=None):
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._/:-]{0,199}", model):
            raise OllamaError("invalid_model", "모델 태그 형식이 잘못되었습니다.")
        if _cloud({"name": model}):
            raise OllamaError("cloud_model_blocked", "로컬 가중치를 제공하는 모델을 선택하세요.")
        return await self._stream(
            "/api/pull", {"model": model, "stream": True}, cancel, on_event, pull=True
        )

    async def chat(
        self, model, messages, format=None, tools=None, cancel=None, options=None, on_event=None
    ):
        metadata = await self.local_metadata(model, cancel)
        options = {"num_ctx": 4096, "num_predict": 1024, "temperature": 0.1, **(options or {})}
        if not 256 <= options["num_ctx"] <= 131072 or not 1 <= options["num_predict"] <= 16384:
            raise OllamaError(
                "invalid_options", "컨텍스트 또는 생성 길이 설정이 허용 범위를 벗어났습니다."
            )
        maxima = [
            v
            for k, v in metadata.get("model_info", {}).items()
            if k.endswith(".context_length") and isinstance(v, int)
        ]
        if maxima and options["num_ctx"] > max(maxima):
            raise OllamaError(
                "context_exceeded", "설정 컨텍스트가 모델 메타데이터의 지원 길이를 초과합니다."
            )
        body = {
            "model": model,
            "messages": messages,
            "stream": True,
            "options": options,
            "keep_alive": "2m",
        }
        if format is not None:
            body["format"] = format
        if tools:
            body["tools"] = tools
        if "thinking" in metadata.get("capabilities", []):
            body["think"] = False
        result = await self._stream("/api/chat", body, cancel, on_event)
        running = await self._json("GET", "/api/ps", cancel=cancel)
        match = next(
            (
                m
                for m in running.get("models", [])
                if m.get("name") == model or m.get("model") == model
            ),
            None,
        )
        if match is None or _cloud(match) or not match.get("size", 0):
            raise OllamaError(
                "local_execution_unverified",
                "응답은 수신했지만 /api/ps에서 로컬 모델 실행을 확인하지 못했습니다.",
                {"response": result, "running": running},
            )
        self.last_execution = {
            "endpoint": self.base_url,
            "kind": "local",
            "model": model,
            "digest": match.get("digest"),
            "size": match.get("size"),
            "size_vram": match.get("size_vram"),
            "context_length": match.get("context_length"),
            "managed_server": bool(self.managed_process),
            "metadata": metadata.get("details", {}),
        }
        result["_execution"] = self.last_execution
        return result

    async def capabilities(self, model, cancel=None):
        data = {
            "model": model,
            "basic": False,
            "json_schema": False,
            "tools": False,
            "errors": {},
            "tested_at": time.time(),
        }
        try:
            data["metadata"] = await self.local_metadata(model, cancel)
        except OllamaError as exc:
            if exc.code == "cancelled":
                raise
            data["errors"]["metadata"] = {"code": exc.code, "message": str(exc)}
            return data
        schema = {
            "type": "object",
            "properties": {"ok": {"type": "boolean", "const": True}},
            "required": ["ok"],
            "additionalProperties": False,
        }
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "get_status",
                    "description": "Harmless capability test; returns ready=true.",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                },
            }
        ]
        for feature in ("basic", "json_schema", "tools"):
            try:
                prompt = {
                    "basic": "Reply with only OK.",
                    "json_schema": 'Return JSON {"ok":true}.',
                    "tools": "Call the get_status tool now with an empty argument object.",
                }[feature]
                response = await self.chat(
                    model,
                    [{"role": "user", "content": prompt}],
                    format=schema if feature == "json_schema" else None,
                    tools=tools if feature == "tools" else None,
                    cancel=cancel,
                    options={"num_predict": 128, "num_ctx": 2048},
                )
                msg = response["message"]
                if feature == "basic":
                    passed = bool(msg["content"].strip())
                elif feature == "json_schema":
                    decoded = json.loads(msg["content"])
                    passed = (
                        isinstance(decoded, dict)
                        and set(decoded) == {"ok"}
                        and decoded["ok"] is True
                    )
                else:
                    calls = msg.get("tool_calls", [])
                    function = (
                        calls[0].get("function", {})
                        if len(calls) == 1 and isinstance(calls[0], dict)
                        else {}
                    )
                    passed = (
                        isinstance(function, dict)
                        and function.get("name") == "get_status"
                        and function.get("arguments") == {}
                    )
                    if passed:
                        # Complete the harmless round trip, not merely a metadata assertion.
                        follow = await self.chat(
                            model,
                            [
                                {"role": "user", "content": prompt},
                                msg,
                                {
                                    "role": "tool",
                                    "tool_name": "get_status",
                                    "content": '{"ready":true}',
                                },
                                {
                                    "role": "user",
                                    "content": 'Report the tool result as exactly JSON {"ready":true}. Do not request another tool.',
                                },
                            ],
                            cancel=cancel,
                            options={"num_predict": 128, "num_ctx": 2048},
                        )
                        data["tool_roundtrip"] = follow["message"]["content"]
                        data["tool_roundtrip_response"] = follow["message"]
                        passed = not follow["message"].get("tool_calls") and bool(
                            follow["message"]["content"].strip()
                        )
                        if passed:
                            decoded = json.loads(follow["message"]["content"])
                            passed = (
                                isinstance(decoded, dict)
                                and set(decoded) == {"ready"}
                                and decoded["ready"] is True
                            )
                data[feature] = passed
                data[feature + "_response"] = msg
                if not passed:
                    data["errors"][feature] = {
                        "code": "feature_mismatch",
                        "message": "실제 기능 테스트의 기대 결과를 충족하지 않았습니다.",
                    }
            except (OllamaError, ValueError) as exc:
                if getattr(exc, "code", None) == "cancelled":
                    raise
                data["errors"][feature] = {
                    "code": getattr(exc, "code", "json_format"),
                    "message": str(exc),
                }
        data["execution"] = self.last_execution
        data["available_modes"] = ["automatic"] + (
            ["agent"] if data["basic"] and data["tools"] else []
        )
        data["context_limits"] = {
            k: v
            for k, v in data["metadata"].get("model_info", {}).items()
            if k.endswith(".context_length")
        }
        return data

    async def ensure_server(self, cancel=None, timeout=30):
        status = await self.probe(cancel)
        if status["connected"]:
            return status
        if status["code"] in ("incompatible_server", "port_conflict"):
            return status
        if not status["installed"]:
            return {**status, "code": "not_installed"}
        if not _START_LOCK.acquire(blocking=False):
            # Another Recheck request is starting it; only wait and re-probe.
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if cancelled(cancel):
                    raise OllamaError("cancelled", "서버 준비 대기가 취소되었습니다.")
                status = await self.probe(cancel)
                if status["connected"]:
                    return status
                await asyncio.sleep(0.2)
            return {**status, "code": "server_start_timeout"}
        try:
            status = await self.probe(cancel)
            if status["connected"]:
                return status
            env = {
                **os.environ,
                "OLLAMA_HOST": self.base_url,
                "OLLAMA_NO_CLOUD": "1",
                "OLLAMA_NUM_PARALLEL": "1",
                "OLLAMA_MAX_LOADED_MODELS": "1",
            }
            self.managed_log = str(
                Path(tempfile.gettempdir()) / f"recheck-ollama-{os.getpid()}-{time.time_ns()}.log"
            )
            with open(self.managed_log, "ab") as log:
                self.managed_process = subprocess.Popen(
                    [status["executable"], "serve"],
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    env=env,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if cancelled(cancel):
                    if self.managed_process.poll() is None:
                        self.managed_process.terminate()
                    raise OllamaError("cancelled", "서버 준비 대기가 취소되었습니다.")
                status = await self.probe(cancel)
                if status["connected"]:
                    return {**status, "log": self.managed_log}
                if self.managed_process.poll() is not None:
                    # A racing existing server may have taken the port; probe above wins.
                    return {
                        **status,
                        "code": "server_start_failed",
                        "log": self.managed_log,
                        "exit_code": self.managed_process.returncode,
                    }
                await asyncio.sleep(0.2)
            if self.managed_process.poll() is None:
                self.managed_process.terminate()
            return {
                **status,
                "managed": False,
                "code": "server_start_timeout",
                "log": self.managed_log,
            }
        finally:
            _START_LOCK.release()
