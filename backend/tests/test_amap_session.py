"""Simulate MCP task-group failures locally; no provider or model request is sent."""
import asyncio
import unittest
from contextlib import asynccontextmanager
from contextvars import ContextVar
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import anyio
import httpx

from app.adapters import Amap, ToolError


class AmapSession(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.amap = Amap.__new__(Amap)
        self.amap.api_key = "synthetic-key"
        self.amap.batch_state = ContextVar("test_amap_batch", default=None)
        self.amap.store = Mock()
        self.amap.cache = {}
        self.disconnect = asyncio.Event()
        self.owners, self.exit_owners = [], []
        self.fail_on_cleanup = False
        self.session = SimpleNamespace(initialize=AsyncMock(),
            list_tools=AsyncMock(return_value=SimpleNamespace(tools=[SimpleNamespace(
                name="maps_text_search",inputSchema={"properties":{"keywords":{"type":"string"}},"required":["keywords"]})])),
            call_tool=AsyncMock(return_value=SimpleNamespace(isError=False,structuredContent={"pois":[]},content=[])))

        async def background_request():
            await self.disconnect.wait()
            raise self.connection_failure()

        @asynccontextmanager
        async def transport(*args, **kwargs):
            self.owners.append(asyncio.current_task())
            try:
                async with anyio.create_task_group() as group:
                    group.start_soon(background_request)
                    try:
                        yield None, None, None
                    finally:
                        group.cancel_scope.cancel()
            finally:
                self.exit_owners.append(asyncio.current_task())

        @asynccontextmanager
        async def client(*args, **kwargs):
            try:
                yield self.session
            finally:
                if self.fail_on_cleanup:
                    raise self.connection_failure()

        for name, replacement in (("mcp.ClientSession",client),
                                  ("mcp.client.streamable_http.streamablehttp_client",transport)):
            patched = patch(name,replacement)
            patched.start()
            self.addCleanup(patched.stop)

    @staticmethod
    def connection_failure():
        return httpx.ConnectError("private-provider-response",request=httpx.Request(
            "POST","https://example.invalid/mcp?key=private-test-key"))

    async def test_connection_failure_during_initialization_does_not_cancel_caller(self):
        async def initialize():
            self.disconnect.set()
            await asyncio.Event().wait()
        self.session.initialize.side_effect = initialize
        async def plan():
            async with self.amap.batch():
                with self.assertRaises(ToolError) as caught:
                    await self.amap.call("trip","maps_text_search",{"keywords":"合成景点"})
                self.assertEqual(caught.exception.code,"amap_network")
                self.assertNotIn("private",str(caught.exception))
                return "planner continued"
        self.assertEqual(await asyncio.wait_for(plan(),2),"planner continued")
        self.assertEqual(self.owners,self.exit_owners)
        self.assertIsNone(self.amap.batch_state.get())

    async def test_background_disconnect_after_success_is_isolated_from_planning(self):
        async with self.amap.batch():
            self.assertIsNot(self.owners[0],asyncio.current_task())
            result = await self.amap.call("trip","maps_text_search",{"keywords":"合成景点"})
            self.assertEqual(result,{"pois":[]})
            self.disconnect.set()
            with self.assertLogs("app.adapters",level="WARNING") as logged:
                await asyncio.wait_for(asyncio.shield(self.owners[0]),2)
            with self.assertRaises(ToolError) as caught:
                await self.amap.call("trip","maps_text_search",{"keywords":"另一景点"})
            self.assertEqual(caught.exception.code,"amap_network")
            self.assertNotIn("private","\n".join(logged.output)+str(caught.exception))
            self.assertEqual(self.session.call_tool.await_count,1)
        self.assertEqual(self.owners,self.exit_owners)

    async def test_cleanup_failure_does_not_replace_planner_error(self):
        self.fail_on_cleanup = True
        original = ToolError("model_format","合成模型错误")
        with self.assertRaises(ToolError) as caught:
            async with self.amap.batch():
                raise original
        self.assertIs(caught.exception,original)
        self.assertEqual(self.owners,self.exit_owners)
        self.assertTrue(self.owners[0].done())

    async def test_nested_batch_reuses_session_and_closes_in_owner_task(self):
        async with self.amap.batch():
            state = self.amap.batch_state.get()
            async with self.amap.batch():
                self.assertIs(self.amap.batch_state.get(),state)
                await self.amap.call("trip","maps_text_search",{"keywords":"合成景点"})
            self.assertIs(self.amap.batch_state.get(),state)
        self.assertEqual(len(self.owners),1)
        self.assertEqual(self.owners,self.exit_owners)
        self.assertTrue(self.owners[0].done())

    async def test_quota_in_a_successful_mcp_envelope_stops_remaining_provider_calls(self):
        self.amap.cache = {}
        self.session.call_tool.return_value = SimpleNamespace(isError=False,structuredContent={"status":"0","info":"DAILY_QUERY_OVER_LIMIT"},content=[])
        async with self.amap.batch():
            for name in ("甲","乙"):
                with self.assertRaises(ToolError) as caught:
                    await self.amap.call("trip","maps_text_search",{"keywords":name})
                self.assertEqual(caught.exception.code,"amap_quota_limit")
        self.assertEqual(self.session.call_tool.await_count,1)

    async def test_local_budget_is_exhausted_without_another_provider_call(self):
        async with self.amap.batch():
            self.amap.batch_state.get()["calls"] = 300
            with self.assertRaises(ToolError) as caught:
                await self.amap.call("trip","maps_text_search",{"keywords":"甲"})
            self.assertEqual(caught.exception.code,"amap_limit")
            self.assertTrue(self.amap.batch_state.get()["failed"])
        self.session.call_tool.assert_not_awaited()
