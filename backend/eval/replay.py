"""外部接口录制与回放：让不同消融组看到同一份高德 / 飞猪返回，差异只来自代码与模型。

- 高德：在 Amap.call 层按 (tool, 参数) 录制原始 payload；
- 飞猪：在 adapters.process 层按 CLI 子命令参数录制 stdout（不含 API Key，Key 只在环境变量里）。

模式：
- record：总是真实请求，并覆盖录制；
- auto：命中录制就回放，未命中才真实请求并补录（默认）；
- replay：只回放，未命中按查询失败处理，不访问网络。

只录制成功返回；限流、网络错误等瞬时失败不进入录制，避免把偶发故障固定成“事实”。
回放命中时照常写入 queries 表并登记 evidence_id，审查与证据引用逻辑不受影响。
"""
import copy
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from app import adapters
from app.adapters import ToolError
from app.store import now

MODES = ("record", "auto", "replay")
FLYAI_COMMANDS = {"search-flight", "search-train", "search-hotel", "search-poi"}


class Cassette:
    def __init__(self, file: Path, mode: str):
        if mode not in MODES:
            raise ValueError("录制模式只能是 " + "/".join(MODES))
        self.file, self.mode = file, mode
        self.entries = {}
        if file.is_file() and mode != "record":
            self.entries = json.loads(file.read_text(encoding="utf-8")).get("entries", {})
        self.stats = {"hits": 0, "misses": 0, "recorded": 0, "live": 0}

    @staticmethod
    def key(provider: str, request) -> str:
        return provider + ":" + json.dumps(request, ensure_ascii=False, sort_keys=True, default=str)

    def lookup(self, key):
        if self.mode != "record" and key in self.entries:
            self.stats["hits"] += 1
            return copy.deepcopy(self.entries[key]["response"])
        self.stats["misses"] += 1
        if self.mode == "replay":
            return None
        self.stats["live"] += 1
        return None

    def save(self, key, provider, request, response):
        self.entries[key] = {"provider": provider, "request": request, "response": response, "recorded_at": now()}
        self.stats["recorded"] += 1

    def flush(self):
        self.file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.file.with_suffix(".tmp")
        temporary.write_text(json.dumps({"version": 1, "entries": self.entries}, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(temporary, self.file)


def business_ok(out: str) -> bool:
    """FlyAI can exit 0 with status!=0 (e.g. a failed low-fare search); that is an outage, not data."""
    try:
        payload = json.loads(out.strip())
    except ValueError:
        return False
    return not (isinstance(payload, dict) and (payload.get("success") is False or payload.get("error")
                                               or type(payload.get("status")) is int and payload["status"] != 0))


def install(planner, cassette: Cassette):
    """Patch one Planner's adapters; returns an undo function."""
    amap, flyai, store = planner.amap, planner.flyai, planner.store
    original_call, original_batch = amap.call, amap.batch
    original_process = adapters.process
    original_command, original_key = flyai.command, flyai.api_key

    async def call(trip_id, tool, args):
        request = {"tool": tool, "args": args}
        key = cassette.key("amap", request)
        payload = cassette.lookup(key)
        if payload is None:
            if cassette.mode == "replay":
                store.query(trip_id, "amap", {"tool": tool, **args}, {"status": "error", "code": "replay_miss", "queried_at": now()})
                raise ToolError("replay_miss", "回放录制中没有这次高德查询，相关信息待确认")
            payload = await original_call(trip_id, tool, args)
            cassette.save(key, "amap", request, payload)
            return payload
        record_id = store.query(trip_id, "amap", {"tool": tool, **args},
                                {"raw": payload, "queried_at": now(), "request_attempted": True, "replayed": True})
        amap.cache[("evidence", trip_id, tool, json.dumps(args, sort_keys=True, default=str))] = record_id
        return payload

    @asynccontextmanager
    async def offline_batch():
        if amap.batch_state.get() is not None:
            yield
            return
        token = amap.batch_state.set({"session": None, "tools": {}, "calls": 0, "failed": False, "error": None})
        try:
            yield
        finally:
            amap.batch_state.reset(token)

    async def process(args, cwd=None, timeout=60, env=None):
        index = next((i for i, value in enumerate(args) if value in FLYAI_COMMANDS), None)
        if index is None:
            return await original_process(args, cwd=cwd, timeout=timeout, env=env)
        request = list(args[index:])
        key = cassette.key("flyai", request)
        response = cassette.lookup(key)
        if response is not None:
            return response["code"], response["out"], response.get("stderr", "")
        if cassette.mode == "replay":
            return 1, "", "replay_miss: 回放录制中没有这次飞猪查询"
        code, out, stderr = await original_process(args, cwd=cwd, timeout=timeout, env=env)
        if code == 0 and business_ok(out):
            cassette.save(key, "flyai", request, {"code": code, "out": out,
                                                  "stderr": adapters.redact_diagnostic(stderr or "", flyai.api_key)})
        return code, out, stderr

    amap.call = call
    adapters.process = process
    if cassette.mode == "replay":
        amap.batch = offline_batch
        amap.api_key = amap.api_key or "replay"
        flyai.command = lambda: ["flyai"]
        flyai.api_key = flyai.api_key or "replay"

    def undo():
        adapters.process = original_process
        amap.call, amap.batch = original_call, original_batch
        flyai.command, flyai.api_key = original_command, original_key
    return undo
