"""Mobility recovery with mocked tools and synthetic snapshots."""
import copy
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from app.adapters import Amap, ToolError
from app.models import REVIEW_DIMENSIONS, Constraint, Requirements
from app.planner import Planner
from app.planning_recovery import previous_round, regression_reason
from app.reviewing import bind_report, review_basis


REQ = Requirements(origin="杭州",cities=["杭州"],start_date="2026-11-10",end_date="2026-11-10",
                   people=1,rooms=1,budget=10000,return_to_origin=False).model_dump(mode="json")
POINTS = {name:{"name":name,"location":location} for name,location in
          (("甲","120,30"),("乙","120.04,30"),("丙","120.05,30"),("近处","120.001,30"))}


def plan():
    return {"days":[{"date":"2026-11-10","city":"杭州","activities":[
        {"id":name,"name":name,"kind":"visit","start":start,"end":end,"map_place":POINTS[name],
         "evidence":{"quote":name},"move_before":{} if index == 0 else {"minutes":10,"mode":"transit",
            "origin":"甲" if index == 1 else "乙","destination":name,"evidence_id":f"query:{index}","queried_at":"2026-10-14T01:00:00Z"}}
        for index,(name,start,end) in enumerate((("甲","09:00","10:00"),("乙","10:20","11:00"),("丙","11:20","12:00")))],
        "gaps":[],"return_move":{},"transfers":[],"meal_recommendations":[],"meal_estimate":100}],
        "hotels":[],"transport":[],"issues":[],"budget":{"unknown":[]}}


def candidate(itinerary):
    return {"id":"old-candidate","title":"上一轮","reason":"原攻略依据","gaps":[],"draft":itinerary,
            "days":[{"date":"2026-11-10","city":"杭州","stop_ids":["甲","乙","丙"],"adjustment":""}]}


class Mobility(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.planner = Planner.__new__(Planner)
        self.planner.emit = Mock()
        self.planner.amap = SimpleNamespace(cache={},place=AsyncMock(side_effect=lambda trip,city,name:POINTS[name]),route=AsyncMock())
        self.state = {"trip_id":"trip","requirements":REQ,"stops":{}}


    async def test_short_walk_does_not_spend_a_transit_query(self):
        self.planner.amap.route.return_value = {"minutes":5,"mode":"walking","walking_meters":150}
        move,_ = await self.planner.movement(self.state,"杭州","甲","近处")
        self.assertEqual(move["mode"],"walking")
        self.assertEqual([call.args[-1] for call in self.planner.amap.route.await_args_list],["walking"])

    async def test_explicit_walking_only_remains_walking_on_a_long_leg(self):
        self.planner.amap.route.return_value = {"minutes":45,"mode":"walking","walking_meters":3500}
        await self.planner.movement({**self.state,"requirements":{**REQ,"local_transport":"walking"}},"杭州","甲","乙")
        self.assertEqual([call.args[-1] for call in self.planner.amap.route.await_args_list],["walking"])




    async def test_quota_error_stops_other_modes_in_the_same_leg(self):
        self.planner.amap.route.side_effect = ToolError("amap_rate_limit","查询超限")
        move,_ = await self.planner.movement(self.state,"杭州","甲","乙")
        self.assertEqual(move["failure_codes"],["amap_rate_limit"])
        self.assertEqual(self.planner.amap.route.await_count,1)

    async def test_recheck_only_queries_missing_leg_and_keeps_times_and_evidence(self):
        itinerary = plan()
        day = itinerary["days"][0]
        known = copy.deepcopy(day["activities"][1]["move_before"])
        day["activities"][2]["move_before"] = {"minutes":None,"estimate_minutes":45}
        times = [(item["start"],item["end"]) for item in day["activities"]]
        self.planner.movement = AsyncMock(return_value=({"minutes":7,"mode":"transit","origin":"乙","destination":"丙"},POINTS["丙"]))
        await self.planner.refresh_day_routes(self.state,itinerary,missing_only=True)
        self.assertEqual(self.planner.movement.await_count,1)
        self.assertEqual(self.planner.movement.call_args.args[2:4],("乙","丙"))
        self.assertEqual(day["activities"][1]["move_before"],known)
        self.assertEqual([(item["start"],item["end"]) for item in day["activities"]],times)

    async def test_recheck_discards_only_failed_place_cache(self):
        itinerary = plan()
        original = candidate(itinerary)
        self.planner.amap.cache = {("trip","杭州","甲"):POINTS["甲"],("trip","杭州","丙"):None,
            ("place_candidates","trip","杭州","丙"):{"pois":[]},("route","trip","杭州","120,30","120.04,30","transit"):(1,{"minutes":10})}
        known = copy.deepcopy(self.planner.amap.cache[("route","trip","杭州","120,30","120.04,30","transit")])
        state = {**self.state,"candidates":[original],"stops":{name:{"name":name,"city":"杭州"} for name in ("甲","乙","丙")},
            "route_edit":{"candidate_id":"old-candidate","dates":["2026-11-10"],"recheck":True}}
        await self.planner.revise_candidate(state)
        self.assertIn(("trip","杭州","甲"),self.planner.amap.cache)
        self.assertNotIn(("trip","杭州","丙"),self.planner.amap.cache)
        self.assertNotIn(("place_candidates","trip","杭州","丙"),self.planner.amap.cache)
        self.assertEqual(self.planner.amap.cache[("route","trip","杭州","120,30","120.04,30","transit")],known)

    async def test_verified_coordinates_reuse_a_legacy_leg_with_official_poi_names(self):
        itinerary = plan()
        move = itinerary["days"][0]["activities"][1]["move_before"]
        move.update(origin="甲公园官方名称",destination="乙公园官方名称",
            origin_place={**POINTS["甲"],"name":"甲公园官方名称"},destination_place={**POINTS["乙"],"name":"乙公园官方名称"})
        self.planner.movement = AsyncMock()
        await self.planner.refresh_day_routes(self.state,itinerary,missing_only=True)
        self.planner.movement.assert_not_awaited()
        self.assertEqual(move["evidence_id"],"query:1")

    async def test_failed_full_refresh_keeps_same_leg_evidence_with_its_original_time(self):
        itinerary = plan()
        known = copy.deepcopy(itinerary["days"][0]["activities"][1]["move_before"])
        self.planner.movement = AsyncMock(return_value=({"minutes":None,"failure_codes":["amap_network"],
            "evidence_id":"query:failed","note":"地图连接失败"},None))
        await self.planner.refresh_day_routes(self.state,itinerary)
        move = itinerary["days"][0]["activities"][1]["move_before"]
        self.assertEqual(move["minutes"],known["minutes"])
        self.assertEqual(move["evidence_id"],known["evidence_id"])
        self.assertEqual(move["queried_at"],known["queried_at"])
        self.assertTrue(move["refresh_failed"])
        self.assertIn("并非本次更新",move["refresh_note"])

    async def test_failed_refresh_does_not_reuse_changed_endpoints_or_disallowed_modes(self):
        for change in ("coordinates","mode","no_evidence"):
            with self.subTest(change=change):
                itinerary = plan()
                move = itinerary["days"][0]["activities"][1]["move_before"]
                move.update(origin_place=POINTS["甲"],destination_place=POINTS["乙"])
                if change == "coordinates":
                    itinerary["days"][0]["activities"][1]["map_place"] = {"name":"乙分馆","location":"120.03,30"}
                elif change == "mode":
                    move["mode"] = "driving"
                else:
                    move.pop("evidence_id")
                self.planner.movement = AsyncMock(return_value=({"minutes":None,"failure_codes":["amap_network"]},None))
                await self.planner.refresh_day_routes(self.state,itinerary)
                self.assertIsNone(itinerary["days"][0]["activities"][1]["move_before"]["minutes"])


    async def test_route_refresh_leaves_dates_outside_scope_untouched(self):
        itinerary = plan()
        before = copy.deepcopy(itinerary["days"])
        self.planner.movement = AsyncMock()
        self.planner.update_day_places = Mock()
        await self.planner.refresh_day_routes(self.state,itinerary,dates={"2026-11-11"},missing_only=True)
        self.assertEqual(itinerary["days"],before)
        self.planner.movement.assert_not_awaited()
        self.planner.update_day_places.assert_not_called()


class Parsing(unittest.IsolatedAsyncioTestCase):
    async def test_nested_walking_paths_do_not_hide_public_transport(self):
        amap = Amap.__new__(Amap)
        amap.cache = {}
        amap.call = AsyncMock(return_value={"walking_preview":{"paths":[{"duration":"2000"}]},
            "route":{"transits":[{"cost":{"duration":"601"},"walking_distance":"300",
                "segments":[{"bus":{"buslines":[{"name":"地铁1号线"}]}}]}]}})
        result = await amap.route("trip","杭州",POINTS["甲"],POINTS["乙"],"transit")
        self.assertEqual(result["minutes"],11)
        self.assertEqual(result["summary"],"地铁1号线")

    async def test_walking_only_transit_result_is_not_presented_as_bus_or_subway(self):
        amap = Amap.__new__(Amap)
        amap.cache = {}
        amap.call = AsyncMock(return_value={"route":{"transits":[{"duration":"1800","segments":[{"walking":{"duration":"1800"}}]}]}})
        result = await amap.route("trip","杭州",POINTS["甲"],POINTS["乙"],"transit")
        self.assertIsNone(result["minutes"])


class Recovery(unittest.IsolatedAsyncioTestCase):

    def test_backup_is_detached_and_widespread_failures_trigger_rollback(self):
        original = plan()
        backup = previous_round({"requirements":REQ,"candidates":[candidate(original)]})
        for item in original["days"][0]["activities"][1:]:
            item["move_before"] = {"minutes":None,"failure_codes":["amap_network"]}
        self.assertTrue(regression_reason(backup["itinerary"],original))
        self.assertEqual(backup["itinerary"]["days"][0]["activities"][1]["move_before"]["minutes"],10)

    def test_one_failed_leg_and_intentional_removal_do_not_count_as_bulk_failure(self):
        original = plan()
        edited = copy.deepcopy(original)
        edited["days"][0]["activities"][1]["move_before"] = {"minutes":None}
        self.assertIsNone(regression_reason(original,edited))
        edited["days"][0]["activities"] = edited["days"][0]["activities"][:1]
        self.assertIsNone(regression_reason(original,edited))

    def test_retained_successful_evidence_is_not_lost_when_refresh_fails(self):
        original = plan()
        edited = copy.deepcopy(original)
        for item in edited["days"][0]["activities"][1:]:
            item["move_before"].update(refresh_failed=True,failure_codes=["amap_network"])
        self.assertIsNone(regression_reason(original,edited))
        for item in edited["days"][0]["activities"][1:]:
            item["move_before"]["failure_codes"] = ["amap_quota_limit"]
        self.assertIsNone(regression_reason(original,edited))

    def test_unrelated_failed_new_leg_does_not_mark_removed_old_legs_as_lost(self):
        original = plan()
        edited = copy.deepcopy(original)
        edited["days"][0]["activities"] = [edited["days"][0]["activities"][0],{
            "id":"new","name":"新景点","kind":"visit","move_before":{"minutes":None,"failure_codes":["amap_network"]}}]
        self.assertIsNone(regression_reason(original,edited))

    def test_bulk_loss_message_does_not_claim_that_database_evidence_was_deleted(self):
        original = plan()
        edited = copy.deepcopy(original)
        for item in edited["days"][0]["activities"][1:]:
            item["move_before"] = {"minutes":None,"failure_codes":["amap_network"]}
        message = regression_reason(original,edited)
        self.assertIn("2 个仍需使用",message)
        self.assertIn("旧查询记录仍保留",message)


    async def test_failed_task_restores_previous_candidate_but_is_not_reported_as_completed(self):
        original = plan()
        backup = previous_round({"requirements":REQ,"candidates":[candidate(original)]})
        doc = {"requirements":REQ,"stage":"queued","round_backup":backup,"candidates":[candidate(plan())]}
        planner = Planner.__new__(Planner)
        planner.store = Mock()
        planner.store.run.return_value = {"trip_id":"trip","thread_id":"thread"}
        planner.store.trip.return_value = {"active_run":"run","document":doc}
        planner.graph = SimpleNamespace(ainvoke=AsyncMock(side_effect=ToolError("amap_limit","本轮查询超限")),
            aget_state=AsyncMock(return_value=SimpleNamespace(next=("assess",),values={"candidates":[]})))
        @asynccontextmanager
        async def batch():
            yield
        planner.amap = SimpleNamespace(batch=batch)
        await planner.execute("run",{})
        self.assertEqual(doc["candidates"][0]["draft"]["days"],original["days"])
        self.assertTrue(doc["rollback"])
        self.assertEqual(doc["stage"],"failed")
        self.assertEqual(planner.store.finish_run.call_args.args[1],"failed")
