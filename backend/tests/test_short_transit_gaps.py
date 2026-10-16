"""User-run synthetic regressions based on recorded response shapes; no real APIs."""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.adapters import Amap, ToolError
from app.models import Requirements, RouteToolRequest
from app.planner import Planner


CASES = [
    ("K11","广东省博物馆","113.326132,23.117789","113.326369,23.114763",642,514,9),
    ("广东省博物馆","广州大剧院","113.326369,23.114763","113.322547,23.115025",479,383,7),
    ("宏城公园","二沙岛艺术公园","113.317076,23.110475","113.319436,23.110215",299,239,4),
]


class ShortTransitGaps(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.amap = Amap.__new__(Amap)
        self.amap.cache = {}
        self.amap.call = AsyncMock()
        self.planner = Planner.__new__(Planner)
        self.planner.amap = self.amap
        self.requirements = Requirements(origin="广州",cities=["广州"],start_date="2026-10-14",end_date="2026-10-16",
            people=1,rooms=1,budget=5000,local_transport="public",
            preferences="10月16日东山口和正佳广场的公交可行则保留，否则删除").model_dump(mode="json")
        self.request = RouteToolRequest(tool="route",city="广州",origin_id="source:a",destination_id="source:b",mode="transit")

    def state(self,case):
        origin,destination,start,end,*_ = case
        return {"trip_id":"trip","requirements":copy.deepcopy(self.requirements),"stops":{
            "source:a":{"id":"source:a","city":"广州","name":origin,"map_place":{"name":origin,"location":start}},
            "source:b":{"id":"source:b","city":"广州","name":destination,"map_place":{"name":destination,"location":end}}}}

    async def test_verified_short_walk_skips_transit_even_when_agent_requests_it(self):
        for case in CASES:
            with self.subTest(pair=case[:2]):
                self.amap.cache.clear()
                _,_,_,_,meters,seconds,minutes = case
                self.amap.call.reset_mock()
                self.amap.call.side_effect = [{"route":{"paths":[{"distance":str(meters),"duration":str(seconds)}]}}]
                state = self.state(case)
                original = copy.deepcopy(state)
                result = await self.planner.agent_tool(state,self.request)
                self.assertEqual(result["minutes"],minutes)
                self.assertEqual(result["mode"],"walking")
                self.assertEqual(result["query_status"],"resolved")
                self.assertEqual(result["walking_meters"],meters)
                self.assertFalse(result["transit_queried"])
                self.amap.call.assert_awaited_once()
                self.assertEqual(state,original)

    async def test_fifteen_minutes_is_inclusive_but_sixteen_queries_transit(self):
        for minutes in (15,16):
            with self.subTest(minutes=minutes):
                route = AsyncMock(side_effect=[{"minutes":minutes,"mode":"walking","walking_meters":1000},
                    {"minutes":None,"mode":"transit","route_status":"empty"}])
                self.planner.amap = SimpleNamespace(route=route)
                result = await self.planner.agent_tool(self.state(CASES[0]),self.request)
                self.assertEqual([call.args[-1] for call in route.await_args_list],
                    ["walking"] if minutes == 15 else ["walking","transit"])
                if minutes == 15:
                    self.assertEqual(result["mode"],"walking")
                else:
                    self.assertIsNone(result["minutes"])
                    self.assertEqual(result["route_status"],"empty")
                    self.assertEqual(result["walking_alternative"]["minutes"],16)

    async def test_unrecognized_response_is_distinct_from_explicitly_empty_transit(self):
        self.amap.call.return_value = {"new_protocol":{"unexpected":[]}}
        self.amap.call.side_effect = None
        points = self.state(CASES[0])["stops"]
        result = await self.amap.route("trip","广州",points["source:a"]["map_place"],points["source:b"]["map_place"],"transit")
        self.assertEqual(result["route_status"],"unrecognized")
        self.assertIsNone(result["minutes"])

    async def test_filtered_candidate_is_distinct_from_provider_returning_no_candidates(self):
        self.amap.call.return_value = {"transits":[{"duration":"600","segments":[{"taxi":{"distance":"1000"}}]}]}
        points = self.state(CASES[0])["stops"]
        result = await self.amap.route("trip","广州",points["source:a"]["map_place"],points["source:b"]["map_place"],"transit")
        self.assertEqual(result["route_status"],"unusable")
        self.assertIsNone(result["minutes"])

    async def test_long_or_unknown_distance_walk_is_not_offered_as_a_verified_short_connection(self):
        for walking in ({"minutes":30,"walking_meters":1800},{"minutes":16,"walking_meters":None}):
            with self.subTest(walking=walking):
                self.planner.amap = SimpleNamespace(route=AsyncMock(side_effect=[walking,{"minutes":None,"mode":"transit"}]))
                result = await self.planner.agent_tool(self.state(CASES[0]),self.request)
                self.assertNotIn("walking_alternative",result)
                self.assertIsNone(result["minutes"])

    async def test_walking_query_failure_does_not_hide_the_empty_transit_result(self):
        self.planner.amap = SimpleNamespace(route=AsyncMock(side_effect=[ToolError("no_route","没有步行方案"),
            {"minutes":None,"mode":"transit","route_status":"empty"}]))
        result = await self.planner.agent_tool(self.state(CASES[0]),self.request)
        self.assertEqual(result["route_status"],"empty")
        self.assertIsNone(result["minutes"])
        self.assertNotIn("walking_alternative",result)

    async def test_provider_limit_during_walking_does_not_trigger_more_queries(self):
        self.planner.amap = SimpleNamespace(route=AsyncMock(side_effect=ToolError("amap_rate_limit","合成限流")))
        with self.assertRaises(ToolError):
            await self.planner.agent_tool(self.state(CASES[0]),self.request)
        self.planner.amap.route.assert_awaited_once()
