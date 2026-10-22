"""Rate-limit handling with a fake clock and MCP responses."""
import asyncio
import unittest
from contextvars import ContextVar
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

from app.adapters import Amap, ToolError


def reply(payload):
    return SimpleNamespace(isError=False,structuredContent=payload,content=[])


class AmapThrottling(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = 0
        self.waits, self.starts = [],[]
        self.inflight, self.max_inflight = 0,0
        real_sleep = asyncio.sleep
        self.amap = Amap.__new__(Amap)
        self.amap.api_key = "synthetic-key"
        self.amap.cache = {}
        self.amap.store = Mock()
        self.amap.batch_state = ContextVar("synthetic_amap_batch",default=None)
        self.amap.request_slots = asyncio.Semaphore(3)
        self.amap.request_interval = 0.0
        self.amap.next_request_at = 0
        self.amap.rate_limited_until = 0
        self.amap.quota_blocked_until = 0
        self.responses = []
        async def send(*args,**kwargs):
            self.starts.append(self.clock)
            self.inflight += 1
            self.max_inflight = max(self.max_inflight,self.inflight)
            await real_sleep(0)
            self.inflight -= 1
            value = self.responses.pop(0) if self.responses else reply({"pois":[]})
            if isinstance(value,Exception):
                raise value
            return value
        self.session = SimpleNamespace(call_tool=AsyncMock(side_effect=send))
        async def sleep(seconds):
            self.waits.append(seconds)
            self.clock += seconds
            await real_sleep(0)
        for target,kwargs in (("app.adapters.monotonic",{"side_effect":lambda:self.clock}),
                              ("app.adapters.asyncio.sleep",{"side_effect":sleep})):
            mocked = patch(target,**kwargs)
            mocked.start()
            self.addCleanup(mocked.stop)

    def state(self):
        return {"failed":False,"error":None,"calls":0,"session":self.session,
            "tools":{"maps_text_search":SimpleNamespace(inputSchema={"properties":{"keywords":{"type":"string"}},"required":["keywords"]})}}

    async def call(self, trip, state):
        token = self.amap.batch_state.set(state)
        try:
            return await self.amap.call(trip,"maps_text_search",{"keywords":"合成地点"})
        finally:
            self.amap.batch_state.reset(token)

    async def test_trips_share_three_slots_without_a_normal_interval(self):
        await asyncio.gather(*(self.call(f"trip-{number}",self.state()) for number in range(6)))
        self.assertEqual(self.max_inflight,3)
        self.assertEqual(self.starts,[0]*6)
        self.assertEqual(self.waits,[])

    async def test_one_batch_can_use_three_slots_and_counts_all_requests(self):
        state = self.state()
        await asyncio.gather(*(self.call("trip",state) for _ in range(6)))
        self.assertEqual(self.max_inflight,3)
        self.assertEqual(state["calls"],6)
        self.assertEqual(self.starts,[0]*6)
        records = [call.args[3] for call in self.amap.store.query.call_args_list]
        self.assertEqual(sorted(record["round_calls"] for record in records),list(range(1,7)))

    async def test_transient_frequency_error_slows_down_and_recovers_without_stopping_the_batch(self):
        self.responses = [reply({"status":"0","infocode":"10021","info":"CUQPS_HAS_EXCEEDED_THE_LIMIT"}),reply({"pois":[]})]
        state = self.state()
        result = await self.call("trip",state)
        self.assertEqual(result,{"pois":[]})
        self.assertEqual(self.starts,[0,5])
        self.assertEqual(self.amap.request_interval,0.5)
        self.assertFalse(state["failed"])
        failures = [call.args[3] for call in self.amap.store.query.call_args_list if call.args[3].get("status") == "error"]
        self.assertEqual(failures[0]["provider_code"],"10021")
        self.assertTrue(failures[0]["request_attempted"])

    async def test_minute_window_uses_longer_backoff(self):
        self.responses = [reply({"status":"0","infocode":"10004"}),reply({"pois":[]})]
        state = self.state()
        await self.call("trip",state)
        self.assertEqual(self.starts,[0,65])
        self.assertFalse(state["failed"])

    async def test_retries_are_bounded_and_local_blocking_does_not_extend_cooldown(self):
        self.responses = [reply({"status":"0","infocode":"10021"}) for _ in range(3)]
        state = self.state()
        with self.assertRaises(ToolError) as caught:
            await self.call("trip",state)
        self.assertEqual(caught.exception.code,"amap_rate_limit")
        self.assertIn("重试两次",str(caught.exception))
        self.assertEqual(self.starts,[0,5,15])
        self.assertTrue(state["failed"])
        cooldown = self.amap.rate_limited_until
        with self.assertRaises(ToolError):
            await self.call("trip",state)
        self.assertEqual(self.amap.rate_limited_until,cooldown)
        self.assertEqual(self.session.call_tool.await_count,3)
        self.assertFalse(self.amap.store.query.call_args.args[3]["request_attempted"])

    async def test_daily_quota_is_not_retried_and_is_recorded_without_private_body(self):
        self.responses = [reply({"status":"0","infocode":"10003","info":"DAILY_QUERY_OVER_LIMIT","private":"secret-key"})]
        state = self.state()
        with self.assertRaises(ToolError) as caught:
            await self.call("trip",state)
        self.assertEqual(caught.exception.code,"amap_quota_limit")
        self.assertEqual(self.session.call_tool.await_count,1)
        self.assertGreater(self.amap.quota_blocked_until,self.clock)
        with self.assertRaises(ToolError):
            await self.call("another-trip",self.state())
        self.assertEqual(self.session.call_tool.await_count,1)
        self.assertNotIn("secret-key",str(caught.exception))
        self.assertNotIn("secret-key",repr(self.amap.store.query.call_args_list))

    async def test_http_429_respects_numeric_retry_after(self):
        request = httpx.Request("POST","https://example.invalid/mcp?key=private-key")
        response = httpx.Response(429,headers={"Retry-After":"30"},request=request)
        self.responses = [httpx.HTTPStatusError("private-response",request=request,response=response),reply({"pois":[]})]
        await self.call("trip",self.state())
        self.assertEqual(self.starts,[0,30])
        self.assertNotIn("private-key",repr(self.amap.store.query.call_args_list))

    def test_official_codes_distinguish_qps_from_daily_quota(self):
        for code in ("10004","10014","10015","10019","10020","10021","10029"):
            self.assertEqual(Amap.response_error({"status":"0","infocode":code}).code,"amap_rate_limit")
        for code in ("10003","10044","10045"):
            self.assertEqual(Amap.response_error({"status":"0","infocode":code}).code,"amap_quota_limit")
        self.assertEqual(Amap.response_error({"status":"0","info":"配额超限"}).code,"amap_usage_limit")
        error = Amap.response_error(None,'请求被拒绝，错误码：10021；private-key',True)
        self.assertEqual(error.code,"amap_rate_limit")
        self.assertEqual(error.diagnostic["provider_code"],"10021")
        self.assertNotIn("private-key",str(error))
