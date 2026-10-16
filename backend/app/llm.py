import json
import queue
import re
import shutil
import subprocess
from concurrent.futures import CancelledError
from dataclasses import asdict
from functools import lru_cache
from threading import Lock, Thread
from time import monotonic
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPRedirectHandler
from uuid import uuid4

from .config import PROJECT_ROOT as ROOT, Settings
from .model_registry import ModelConfig, ModelConfigStore
from .model_registry import ENV_KEYS


class ModelNotConfigured(ValueError):
    pass


class StructuredOutputError(ValueError):
    """The provider returned unusable content; never store it as completed evidence."""


class ModelImageNotSupported(ValueError):
    pass


class ModelRuntimeError(RuntimeError):
    def __init__(self, message, status_code=None, timed_out=False, auth_mode=None):
        super().__init__(message)
        self.status_code = status_code
        self.timed_out = timed_out
        self.auth_mode = auth_mode


def model_failure_message(error: Exception) -> str:
    current, seen = error, set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ModelImageNotSupported):
            return str(current)
        status = getattr(current, "status_code", None)
        if status == 402:
            return "模型服务余额或额度不足（HTTP 402）。请检查当前 API Key 所属账户，恢复余额或更换可用模型配置后重试。"
        if status == 401:
            if getattr(current, "auth_mode", None) == "oauth":
                return "订阅认证失效（HTTP 401），请在模型配置中重新登录订阅账号。"
            return "模型服务认证失败（HTTP 401）。请在页面“模型配置”中更换 API Key。"
        if status == 403:
            if getattr(current, "auth_mode", None) == "oauth":
                return "订阅账号无权使用此模型（HTTP 403），请确认订阅权限或改选账号可用的模型。"
            return "模型服务拒绝访问（HTTP 403）。请检查当前 API Key 的模型使用权限。"
        if status == 404:
            return "模型或接口不存在（HTTP 404）。请在“模型配置”中检查模型名称、协议和接口根地址。"
        if status == 429:
            return "模型服务限流或额度受限（HTTP 429）。请检查服务商额度，稍后重试。"
        if getattr(current, "timed_out", False) or type(current).__name__ == "APITimeoutError":
            return "模型请求超时。请稍后重试，或在“模型配置”中增加请求超时。"
        if isinstance(current, ModelRuntimeError):
            return str(current)
        if type(current).__name__ == "APIConnectionError":
            return "无法连接模型服务。请检查页面中的接口地址和网络后重试。"
        current = current.__cause__ or current.__context__
    return "模型调用失败，请检查模型配置或后端日志。"


class ModelWorker:
    """One long-lived Node bridge shared by all model calls, so the SDK is imported once.

    Requests are multiplexed by id; a cancelled or timed-out request is aborted inside the
    worker instead of killing it. A worker that exits is restarted by the next request.
    """

    def __init__(self):
        self.lock = Lock()
        self.process = None
        self.pending = {}

    def _start(self):
        if self.process is not None and self.process.poll() is None:
            return self.process
        node = shutil.which("node")
        if not node:
            raise ModelRuntimeError("未找到 Node.js。请安装 Node.js 22.19 或更高版本后重启后端。")
        process = subprocess.Popen(
            [node, str(ROOT / "model-runtime" / "bridge.mjs"), "--worker"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", cwd=ROOT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.process = process
        Thread(target=self._read, args=(process,), daemon=True).start()
        return process

    def _read(self, process):
        try:
            for line in process.stdout:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                with self.lock:
                    entry = self.pending.get(event.get("id"))
                if entry is not None:
                    entry[1].put(event)
        finally:
            with self.lock:
                if self.process is process:
                    self.process = None
                orphaned = [events for owner, events in self.pending.values() if owner is process]
            for events in orphaned:
                events.put(None)

    def _write(self, process, message: dict):
        process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        process.stdin.flush()

    def submit(self, request: dict):
        identity, events = uuid4().hex, queue.Queue()
        with self.lock:
            process = self._start()
            self.pending[identity] = (process, events)
            try:
                self._write(process, {"id": identity, "request": request})
            except (OSError, ValueError) as exc:
                self.pending.pop(identity, None)
                raise ModelRuntimeError("模型运行进程无法接收请求，请重试；仍失败时请重启后端。") from exc
        return identity, events

    def finish(self, identity: str, cancel: bool):
        with self.lock:
            entry = self.pending.pop(identity, None)
            if cancel and entry is not None and entry[0].poll() is None:
                try:
                    self._write(entry[0], {"id": identity, "cancel": True})
                except (OSError, ValueError):
                    pass

    def close(self):
        with self.lock:
            process, self.process = self.process, None
        if process is None or process.poll() is not None:
            return
        try:
            process.stdin.close()
            process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
            process.wait()


worker = ModelWorker()


class ModelClient:
    """Python adapter to pi-ai; each invocation uses one immutable settings snapshot."""

    def __init__(self, settings: Settings, config_store: ModelConfigStore | None = None):
        self.settings = settings
        self.config_store = config_store or ModelConfigStore(settings)

    @property
    def configured(self):
        return self.config_store.snapshot().configured

    def _events(self, request: dict, timeout: float, cancel_event=None):
        if cancel_event is not None and cancel_event.is_set():
            raise CancelledError()
        identity, events = worker.submit(request)
        deadline = monotonic() + timeout + 10  # Include the first request's SDK import overhead.
        terminal = finished = False
        try:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise CancelledError()
                remaining = deadline-monotonic()
                if remaining <= 0:
                    raise ModelRuntimeError("模型请求超时。",timed_out=True)
                try:
                    event = events.get(timeout=min(0.1,remaining) if cancel_event is not None else remaining)
                except queue.Empty as exc:
                    if cancel_event is not None and monotonic() < deadline:
                        continue
                    raise ModelRuntimeError("模型请求超时。", timed_out=True) from exc
                if event is None:
                    break
                if event.get("type") == "end":
                    finished = True
                    break
                if event.get("type") == "error":
                    if event.get("code") == "image_not_supported":
                        raise ModelImageNotSupported("当前模型不支持图片输入。请在“模型配置”中选择支持图片的视觉模型；自定义接口需确认模型支持图片并启用该选项。")
                    message = str(event.get("message", "模型请求失败。"))
                    key = request.get("config", {}).get("api_key")
                    if key:
                        message = message.replace(key, "[密钥已隐藏]")
                    for value in request.get("config", {}).get("headers", {}).values():
                        if value:
                            message = message.replace(value, "[请求头已隐藏]")
                    # Providers sometimes include request bodies or headers in error messages.
                    status = event.get("status")
                    if status is None:
                        match = re.search(r"(?:HTTP\s*|status(?:_code)?[\s:=]+|^)([45]\d\d)\b", message, re.I)
                        status = int(match.group(1)) if match else None
                    if ("模型运行依赖未安装" in message or "当前页面不支持" in message
                            or event.get("code") == "invalid_model_options"):
                        safe_message = message
                    else:
                        safe_message = ("订阅模型调用失败，请尝试重新登录，并确认账号的模型权限与额度。"
                            if request.get("config", {}).get("credential_mode") == "oauth"
                            else "模型请求失败。请检查模型配置、网络和服务商额度。")
                    raise ModelRuntimeError(safe_message, status, event.get("timeout", False), request.get("config", {}).get("credential_mode"))
                terminal = event.get("type") in ("done", "catalog") or terminal
                yield event
            if not terminal:
                raise ModelRuntimeError("模型运行进程提前结束。请确认 Node.js 版本和 model-runtime 依赖已安装。")
        finally:
            # Abort an unfinished request inside the shared worker; other requests keep running.
            worker.finish(identity, cancel=not finished)

    @lru_cache(maxsize=1)
    def catalog(self):
        for event in self._events({"action": "catalog"}, 30):
            if event["type"] == "catalog":
                return [{**provider, "env_key": ENV_KEYS.get(provider["id"])} for provider in event["providers"]]
        raise ModelRuntimeError("未能读取模型目录。")

    def discover(self, config: ModelConfig):
        if config.api not in ("openai-completions", "openai-responses"):
            raise ValueError("获取模型列表适用于 OpenAI 兼容接口；其他协议可从内置目录添加或手动填写模型 ID")

        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, *_args, **_kwargs):
                return None

        headers = {**config.headers, "Accept": "application/json"}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"
        request = Request(config.base_url.rstrip("/") + "/models", headers=headers)
        try:
            # Never forward a credential to another endpoint via a redirect.
            with build_opener(NoRedirect).open(request, timeout=min(config.timeout_seconds, 30)) as response:
                data = response.read(4 * 1024 * 1024 + 1)
            if len(data) > 4 * 1024 * 1024:
                raise ModelRuntimeError("服务返回的模型列表过大，请手动填写模型 ID。")
            body = json.loads(data)
        except HTTPError as exc:
            if exc.code in (404, 405):
                raise ModelRuntimeError("服务没有提供 /models 列表接口，请从内置目录添加或手动填写模型 ID。") from exc
            raise ModelRuntimeError("无法获取模型列表。", status_code=exc.code) from exc
        except (URLError, TimeoutError) as exc:
            raise ModelRuntimeError("无法连接模型列表接口，请检查地址和网络。") from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ModelRuntimeError("模型列表接口未返回有效 JSON，请手动填写模型 ID。") from exc
        items = body.get("data") if isinstance(body, dict) else None
        if not isinstance(items, list):
            raise ModelRuntimeError("服务返回的模型列表格式不受支持，请手动填写模型 ID。")
        identities = sorted({item["id"].strip() for item in items if isinstance(item, dict)
            and isinstance(item.get("id"), str) and 0 < len(item["id"].strip()) <= 200})
        return {"models": [{"id": identity, "name": identity} for identity in identities[:2000]],
            "message": "已获取模型 ID；列表未声明的视觉能力、推理能力和 token 上限需要自行确认。"}

    def _request(self, messages, action, json_mode=False, config: ModelConfig | None = None, max_tokens=None, cancel_event=None):
        config = config or self.config_store.snapshot()
        if not config.configured:
            raise ModelNotConfigured("请点击页面右上角“模型配置”，添加服务商与模型，填写 API Key 或完成订阅登录后保存。")
        request = {"action": action, "config": asdict(config), "messages": messages, "json_mode": json_mode}
        if max_tokens is not None:
            request["max_tokens"] = max_tokens
        return self._events(request, config.timeout_seconds, cancel_event=cancel_event)

    @staticmethod
    def _text(event):
        if event.get("stop_reason") == "length":
            raise StructuredOutputError("模型输出被长度限制截断，未保存。请拆分问题或章节，或在模型配置中增加最大输出 token 数。")
        if event.get("stop_reason") != "stop":
            raise StructuredOutputError("模型未正常完成输出，未保存，请稍后重试。")
        text = event.get("text", "")
        if not isinstance(text, str) or not text.strip():
            raise StructuredOutputError("模型返回了空内容，未保存结果。")
        return text

    def complete(self, messages: list[dict], json_mode: bool = False, config: ModelConfig | None = None, cancel_event=None) -> str:
        events = self._request(messages, "complete", json_mode, config=config,cancel_event=cancel_event)
        try:
            for event in events:
                if event["type"] == "done":
                    return self._text(event)
        finally:
            events.close()
        raise StructuredOutputError("模型未返回完整结果。")

    def check(self, config: ModelConfig):
        budget = min(config.max_tokens, 4096 if config.reasoning and config.thinking_level != "off" else 256)
        for event in self._request([{"role": "user", "content": "请只回复 OK。"}], "complete", config=config, max_tokens=budget):
            if event["type"] == "done":
                # A short probe may consume its budget in reasoning; receiving text still proves transport.
                if not event.get("text", "").strip():
                    if event.get("stop_reason") == "length":
                        return {"message": "服务已连接，但检查请求在输出正文前达到 token 上限。可降低思考强度或增加最大输出后再检查。", "model": config.model}
                    raise StructuredOutputError("服务已响应但没有返回正文，请调整模型或关闭推理后重试。")
                return {"message": "模型已连接并返回正文。", "model": config.model}
        raise StructuredOutputError("模型未返回连接结果。")

def parse_json(text: str) -> dict:
    text = text.strip()
    # Reasoning-capable gateways sometimes put a private thought section or
    # Markdown fence around the JSON despite the requested response format.
    text = re.sub(r"^<think>.*?</think>\s*", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    decoder = json.JSONDecoder()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        # Accept prose around one complete root object, but never skip a
        # broken root and mistake one of its nested objects for the result.
        start = text.find("{")
        if start < 0 or text.startswith("["):
            raise StructuredOutputError("模型返回的 JSON 无法解析：未返回完整 JSON 对象。")
        try:
            value, _ = decoder.raw_decode(text[start:])
        except json.JSONDecodeError as exc:
            raise StructuredOutputError(
                f"模型返回的 JSON 无法解析：内容不完整或格式错误（第 {exc.lineno} 行，第 {exc.colno} 列）。"
            ) from exc
    if not isinstance(value, dict):
        raise StructuredOutputError("模型应返回 JSON 对象。")
    return value
