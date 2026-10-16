"""Synthetic provider responses only; intended for the user to run."""
import copy
import json
import unittest
from contextvars import ContextVar
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app.adapters import Amap, FlyAI, ToolError


class PlaceEvidenceReuse(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.amap = Amap.__new__(Amap)
        self.amap.cache = {}
        self.places = [
            {"id":"east","name":"合成公园东门","location":"120,30","address":"东路"},
            {"id":"west","name":"合成公园西门","location":"120.01,30","address":"西路"},
        ]
        async def call(trip, tool, args):
            if tool == "maps_text_search":
                return {"pois":copy.deepcopy(self.places)}
            return copy.deepcopy(next(row for row in self.places if row["id"] == args["id"]))
        self.amap.call = AsyncMock(side_effect=call)

    async def test_new_selector_evidence_can_replace_a_previously_selected_branch(self):
        east = AsyncMock(return_value=("east","攻略写东路"))
        west = AsyncMock(return_value=("west","用户补充为西路"))
        first = await self.amap.place("trip","杭州","合成公园",selector=east)
        second = await self.amap.place("trip","杭州","合成公园",selector=west)
        await self.amap.place("trip","杭州","合成公园",selector=west)
        self.assertEqual(first["id"],"east")
        self.assertEqual(second["id"],"west")
        self.assertEqual(self.amap.call.await_count,3)
        self.assertEqual(west.await_count,2)

    async def test_a_new_ambiguous_decision_does_not_reuse_the_old_positive_result(self):
        await self.amap.place("trip","杭州","合成公园",selector=AsyncMock(return_value=("east","原地址")))
        result = await self.amap.place("trip","杭州","合成公园",
                                      selector=AsyncMock(return_value=(None,"新增资料出现两个分店")))
        self.assertIsNone(result)
        self.assertIsNone(await self.amap.place("trip","杭州","合成公园"))

    async def test_single_exact_name_does_not_override_branch_evidence_among_multiple_pois(self):
        self.places[0]["name"] = "合成公园"
        selector = AsyncMock(return_value=("west","攻略明确西门"))
        result = await self.amap.place("trip","杭州","合成公园",selector=selector)
        self.assertEqual(result["id"],"west")
        selector.assert_awaited_once()

    async def test_valid_search_coordinates_survive_detail_failure_and_keep_search_provenance(self):
        args = {"keywords":"合成公园东门","city":"杭州","citylimit":True}
        self.amap.cache[("evidence","trip","maps_text_search",json.dumps(args,sort_keys=True,default=str))] = 71
        self.amap.call.side_effect = [{"pois":[copy.deepcopy(self.places[0])]},ToolError("amap_network","合成网络故障")]
        result = await self.amap.place("trip","杭州","合成公园东门")
        self.assertEqual(result["location"],"120,30")
        self.assertEqual(result["evidence_id"],"query:71")
        self.assertNotIn(("place_detail","trip","east"),self.amap.cache)

    async def test_detail_failure_without_coordinates_remains_unresolved_and_can_retry(self):
        row = {key:value for key,value in self.places[0].items() if key != "location"}
        self.amap.call.side_effect = [{"pois":[row]},ToolError("amap_network","合成网络故障"),self.places[0]]
        with self.assertRaises(ToolError):
            await self.amap.place("trip","杭州","合成公园东门")
        result = await self.amap.place("trip","杭州","合成公园东门")
        self.assertEqual(result["location"],"120,30")
        self.assertEqual(self.amap.call.await_count,3)

    async def test_callers_cannot_mutate_cached_place_records(self):
        result = await self.amap.place("trip","杭州","合成公园东门")
        result["location"] = "0,0"
        again = await self.amap.place("trip","杭州","合成公园东门")
        self.assertEqual(again["location"],"120,30")


class RouteEvidenceReuse(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.amap = Amap.__new__(Amap)
        self.amap.cache = {}
        self.amap.batch_state = ContextVar("provider_reuse_test_batch",default=None)
        self.amap.call = AsyncMock(return_value={"transits":[]})
        self.origin = {"id":"a","name":"起点","location":"120,30"}
        self.destination = {"id":"b","name":"终点","location":"120.01,30"}

    async def route(self, mode="transit"):
        return await self.amap.route("trip","杭州",self.origin,self.destination,mode)

    async def test_empty_response_is_reused_only_in_the_current_planning_batch(self):
        token = self.amap.batch_state.set({})
        try:
            first = await self.route()
            second = await self.route()
            self.assertIsNone(first["minutes"])
            self.assertIsNone(second["minutes"])
            self.assertFalse(self.amap.cache)
            self.amap.call.assert_awaited_once()
        finally:
            self.amap.batch_state.reset(token)
        token = self.amap.batch_state.set({})
        try:
            await self.route()
            self.assertEqual(self.amap.call.await_count,2)
        finally:
            self.amap.batch_state.reset(token)

    async def test_failed_request_is_not_mistaken_for_a_confirmed_empty_response(self):
        self.amap.call.side_effect = [ToolError("amap_network","合成网络故障"),{"transits":[]}]
        token = self.amap.batch_state.set({})
        try:
            with self.assertRaises(ToolError):
                await self.route()
            self.assertIsNone((await self.route())["minutes"])
            self.assertEqual(self.amap.call.await_count,2)
        finally:
            self.amap.batch_state.reset(token)

    async def test_positive_cache_returns_current_endpoints_and_isolated_nested_data(self):
        self.amap.call.return_value = {"paths":[{"duration":"600","distance":"800"}]}
        first = await self.route("walking")
        first["origin_place"]["name"] = "调用者误改"
        second = await self.amap.route("trip","杭州",{**self.origin,"name":"最新起点"},self.destination,"walking")
        self.assertEqual(second["origin"],"最新起点")
        self.assertEqual(second["origin_place"]["name"],"最新起点")
        self.assertIn("%E6%9C%80%E6%96%B0%E8%B5%B7%E7%82%B9",second["navigation_url"])
        self.amap.call.assert_awaited_once()

    async def test_bus_placeholders_and_non_object_fields_stay_unknown_without_crashing(self):
        for bus in ([{"buslines":[{"name":"错误结构"}]}],{"buslines":[{}]},
                    {"buslines":"地铁1号线"},{"buslines":[{"name":None,"id":""}]}):
            with self.subTest(bus=bus):
                self.amap.call.return_value = {"transits":[{"duration":"600","segments":[{"bus":bus}]}]}
                self.assertIsNone((await self.route())["minutes"])

    async def test_known_line_id_with_malformed_name_does_not_crash_summary(self):
        self.amap.call.return_value = {"transits":[{"duration":"600","segments":[
            {"bus":{"buslines":[{"id":"known-line","name":["错误结构"]}]}}]}]}
        result = await self.route()
        self.assertEqual(result["minutes"],10)
        self.assertEqual(result["summary"],"")

    async def test_malformed_optional_segment_does_not_hide_a_valid_subway_route(self):
        self.amap.call.return_value = {"transits":[{"duration":"600","segments":[
            {"bus":[{"buslines":"无效占位"}]},{"bus":{"buslines":[{"name":"地铁1号线"}]}}]}]}
        result = await self.route()
        self.assertEqual(result["minutes"],10)
        self.assertEqual(result["summary"],"地铁1号线")


class ResourceResponseReuse(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.flyai = FlyAI.__new__(FlyAI)
        self.flyai.cache = {}
        self.flyai.api_key = "synthetic-key"
        self.flyai.store = Mock()
        self.flyai.store.query.side_effect = range(1,30)
        self.flyai.command = Mock(return_value=["synthetic-flyai"])
        self.context = {"city":"杭州","check_in":"2026-11-10","check_out":"2026-11-11"}
        self.flags = {"dest-name":"杭州"}
        setting_patch = patch("app.adapters.settings",SimpleNamespace(flyai_enabled=True))
        setting_patch.start()
        self.addCleanup(setting_patch.stop)

    async def test_explicitly_empty_resource_response_is_reused_but_manual_refresh_requeries(self):
        process = AsyncMock(return_value=(0,json.dumps({"status":0,"data":{"itemList":[]}}),""))
        with patch("app.adapters.process",process):
            first = await self.flyai.search("trip","hotel",self.context,self.flags)
            second = await self.flyai.search("trip","hotel",self.context,self.flags)
            self.assertEqual(first["status"],"empty")
            self.assertEqual(second["status"],"empty")
            self.assertEqual(second["options"],[])
            self.assertTrue(second["cached"])
            self.assertEqual(second["queried_at"],first["queried_at"])
            self.assertNotEqual(first["evidence_id"],second["evidence_id"])
            process.assert_awaited_once()
            await self.flyai.search("trip","hotel",self.context,self.flags,fresh=True)
            self.assertEqual(process.await_count,2)

    async def test_changed_resource_context_does_not_reuse_the_empty_response(self):
        process = AsyncMock(return_value=(0,"[]",""))
        with patch("app.adapters.process",process):
            await self.flyai.search("trip","hotel",self.context,self.flags)
            await self.flyai.search("trip","hotel",{**self.context,"check_out":"2026-11-12"},self.flags)
            self.assertEqual(process.await_count,2)

    async def test_unrecognized_provider_shape_is_not_cached_as_a_valid_empty_search(self):
        process = AsyncMock(return_value=(0,json.dumps({"new_wrapper":{"unexpected":"shape"}}),""))
        with patch("app.adapters.process",process):
            for kind in ("hotel","poi"):
                first = await self.flyai.search("trip",kind,self.context,self.flags)
                second = await self.flyai.search("trip",kind,self.context,self.flags)
                self.assertEqual(first["status"],"unrecognized")
                self.assertEqual(second["status"],"unrecognized")
                self.assertNotIn("cached",second)
            self.assertEqual(process.await_count,4)
