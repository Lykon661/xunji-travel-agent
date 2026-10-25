"""Synthetic production-wiring cases for the user to run; never call real providers."""
import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from app.adapters import ToolError
from app.agents.route import RouteAgent
from app.models import OrderChoice, Requirements, RouteToolRequest, RouteTurn
from app.places import canonicalize_stops
from app.planner import Planner
from app.store import Store


DATE, NEXT_DATE, CITY = "2026-11-10", "2026-11-11", "广州"


def source_stop(identity, name, **extra):
    return {"id":identity,"name":name,"city":CITY,"source_id":"guide","source_title":"合成攻略",
            "duration_minutes":90,"evidence":{"quote":name},**extra}


def ready(ids=("guide:a",), **extra):
    return RouteTurn(status="ready",order=OrderChoice(stop_ids=list(ids),explanation="合成安排",**extra))


class DailyToolFlow(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name))
        requirements = Requirements(origin="北京",cities=[CITY],start_date=DATE,end_date=NEXT_DATE,
                                    people=1,rooms=1,budget=5000).model_dump(mode="json")
        trip = self.store.create_trip("合成旅行",requirements)
        run = self.store.start_run(trip["id"],0,"generation",{})
        self.day = {"date":DATE,"city":CITY,"stop_ids":["guide:a"]}
        later = {"date":NEXT_DATE,"city":CITY,"stop_ids":["guide:c"]}
        stops = canonicalize_stops({"guide:a":source_stop("guide:a","东山口"),
            "guide:b":source_stop("guide:b","正佳广场"),"guide:c":source_stop("guide:c","广州塔")})
        self.state = {"trip_id":trip["id"],"run_id":run,"generation":"generation","requirements":requirements,
            "stops":stops,"locks":[],"stop_constraints":{},"selected_route":{"days":[self.day,later]},
            "itinerary":{"days":[{"date":DATE,"city":CITY,"activities":[{"id":"guide:a","name":"东山口"}]},
                                  {"date":NEXT_DATE,"city":CITY,"activities":[{"id":"guide:c","name":"广州塔"}]}],
                         "transport":[],"hotels":[]}}
        self.planner = Planner.__new__(Planner)
        self.planner.store = self.store
        self.planner.emit = Mock()
        self.planner.amap = SimpleNamespace(cache={})
        self.planner.locate_stops = AsyncMock(side_effect=lambda state,pool:canonicalize_stops(
            {identity:{**stop,"map_place":None} for identity,stop in pool.items()}))
        self.planner.movement = AsyncMock(return_value=({"minutes":None,"estimate_minutes":45,"note":"未取得路线"},None))
        self.planner.map_order = AsyncMock(side_effect=lambda state,city,ids,*anchors:(list(ids),"合成顺序"))

    def model(self, turns):
        model = SimpleNamespace(ask=AsyncMock(side_effect=turns))
        self.planner.route_agent = RouteAgent(model,self.store)
        return model

    async def daily(self, payload=None, ids=None):
        return await self.planner.daily_order(self.state,self.day,list(ids or self.day["stop_ids"]),set(),payload or {})

    async def test_real_place_tool_shape_marks_missing_coordinates_unresolved(self):
        request = RouteToolRequest(tool="place",city=CITY,destination_id="guide:a",request="核实明天营业和预约")
        model = self.model([RouteTurn(status="tools",tools=[request]),ready()])
        await self.daily()
        observation = model.ask.call_args.args[2]["tool_observations"][0]
        self.assertEqual(observation["status"],"unresolved")
        self.assertEqual(observation["result"]["query_status"],"unresolved")
        self.assertIsNone(observation["result"]["map_place"])
        self.assertIn("没有查询营业时间",observation["result"]["tool_scope"])

    async def test_real_location_projection_preserves_opening_evidence_for_the_scheduler(self):
        place = {"id":"zoo-poi","name":"长隆野生动物世界","location":"113.315593,23.002660",
                 "evidence_id":"query:synthetic-opening","opentime2":"10月7日-10月31日:9:30-18:00","open_time":""}
        self.planner.amap.place = AsyncMock(return_value=place)
        self.planner.models = SimpleNamespace()
        result = await Planner.locate_stops(self.planner,self.state,{"guide:a":self.state["stops"]["guide:a"]})
        actual = result["guide:a"]["map_place"]
        self.assertEqual(actual["opentime2"],place["opentime2"])
        self.assertEqual(actual["open_time"],"")
        self.assertEqual(actual["evidence_id"],place["evidence_id"])
        self.planner.amap.place.assert_awaited_once()

    async def test_real_route_tool_shape_marks_unknown_minutes_unresolved(self):
        request = RouteToolRequest(tool="route",city=CITY,origin_id="guide:a",destination_id="guide:b")
        model = self.model([RouteTurn(status="tools",tools=[request]),ready(("guide:a","guide:b"))])
        await self.daily(ids=["guide:a","guide:b"])
        observation = model.ask.call_args.args[2]["tool_observations"][0]
        self.assertEqual(observation["status"],"unresolved")
        self.assertEqual(observation["result"]["query_status"],"unresolved")
        self.assertIsNone(observation["result"]["minutes"])
        self.assertEqual(observation["result"]["estimate_minutes"],45)
        self.planner.movement.assert_awaited_once()

    async def test_route_note_cannot_change_the_actual_returned_transport_mode(self):
        self.planner.movement.return_value = ({"minutes":12,"mode":"walking","walking_meters":800},None)
        request = RouteToolRequest(tool="route",city=CITY,origin_id="guide:a",destination_id="guide:b",request="必须只查地铁")
        value = await self.planner.agent_tool(self.state,request)
        self.assertEqual(value["query_status"],"resolved")
        self.assertEqual(value["mode"],"walking")
        self.assertIn("request备注不改变交通方式",value["tool_scope"])

    async def test_explicit_transit_request_checks_walking_then_queries_transit_for_longer_walk(self):
        self.state["requirements"].update(local_transport="public",allow_taxi=False)
        for identity,location in (("guide:a","113.2,23.1"),("guide:b","113.3,23.1")):
            self.state["stops"][identity]["map_place"] = {"id":identity,"name":self.state["stops"][identity]["name"],"location":location}
        self.planner.amap.route = AsyncMock(side_effect=[{"minutes":90,"mode":"walking","walking_meters":7000},
            {"minutes":22,"mode":"transit","evidence_id":"query:123"}])
        request = RouteToolRequest(tool="route",city=CITY,origin_id="guide:a",destination_id="guide:b",mode="transit")
        model = self.model([RouteTurn(status="tools",tools=[request]),ready(("guide:a","guide:b"))])
        await self.daily(ids=["guide:a","guide:b"])
        self.assertEqual([call.args[-1] for call in self.planner.amap.route.await_args_list],["walking","transit"])
        self.assertEqual(self.planner.amap.route.call_args.args[-1],"transit")
        self.planner.movement.assert_not_awaited()
        self.planner.locate_stops.assert_not_awaited()
        observation = model.ask.call_args.args[2]["tool_observations"][0]
        self.assertEqual(observation["status"],"ok")
        self.assertEqual(observation["result"]["mode"],"transit")

    async def test_explicit_driving_cannot_bypass_public_transport_and_no_taxi_constraints(self):
        self.state["requirements"].update(local_transport="public",allow_taxi=False)
        self.planner.amap.route = AsyncMock()
        request = RouteToolRequest(tool="route",city=CITY,origin_id="guide:a",destination_id="guide:b",mode="driving")
        with self.assertRaises(ToolError) as failure:
            await self.planner.agent_tool(self.state,request)
        self.assertEqual(failure.exception.code,"agent_tool_scope")
        self.planner.amap.route.assert_not_awaited()
        self.planner.locate_stops.assert_not_awaited()

    async def test_cached_place_is_merged_back_before_scheduling_the_first_visit(self):
        request = RouteToolRequest(tool="place",city=CITY,destination_id="guide:a")
        self.model([RouteTurn(status="tools",tools=[request]),ready(),RouteTurn(status="tools",tools=[request]),ready()])
        place = {"id":"poi-a","name":"东山口","location":"113.2,23.1","address":"合成地址","evidence_id":"query:123"}
        self.planner.locate_stops.side_effect = lambda state,pool:canonicalize_stops(
            {identity:{**stop,"map_place":copy.deepcopy(place)} for identity,stop in pool.items()})
        warm_pool = copy.deepcopy(self.state["stops"])
        await self.planner.route_agent.plan(self.state,"预先核实位置",{"city":CITY,"available_stops":warm_pool},
            lambda query:self.planner.agent_tool({**self.state,"stops":warm_pool},query),daily=True)
        self.assertIsNone(self.state["stops"]["guide:a"].get("map_place"))
        turn = await self.daily()
        self.planner.locate_stops.assert_awaited_once()
        self.assertEqual(self.state["stops"]["guide:a"]["map_place"],place)
        window = (540,720,[],[],{"name":None,"start_name":None,"label":"结束当天安排"})
        scheduled = await self.planner.schedule_activity_day(self.state,self.day,turn.order,set(),window,{})
        first = next(activity for activity in scheduled["activities"] if activity["kind"] == "visit")
        self.assertEqual(first["id"],"guide:a")
        self.assertEqual(first["map_place"],place)
        self.assertEqual(first["move_before"],{})
        self.assertTrue(first["navigation_url"])

    async def test_daily_tool_boundary_rejects_a_stop_reserved_for_another_day(self):
        request = RouteToolRequest(tool="place",city=CITY,destination_id="guide:c")
        model = self.model([RouteTurn(status="tools",tools=[request]),ready()])
        await self.daily()
        first_payload = model.ask.call_args_list[0].args[2]
        self.assertNotIn("guide:c",first_payload["available_stops"])
        observation = model.ask.call_args.args[2]["tool_observations"][0]
        self.assertEqual(observation["status"],"error")
        self.assertEqual(observation["result"]["code"],"agent_tool_scope")
        self.assertIn("guide:a",observation["result"]["message"])
        self.assertNotIn("guide:c",observation["result"]["message"])
        self.planner.locate_stops.assert_not_awaited()

    async def test_daily_tool_callback_rejects_weather_for_another_trip_city(self):
        self.state["requirements"]["cities"] = [CITY,"深圳"]
        self.planner.amap.weather = AsyncMock()
        request = RouteToolRequest(tool="weather",city="深圳")
        model = self.model([RouteTurn(status="tools",tools=[request]),ready()])
        await self.daily({"city":CITY})
        observation = model.ask.call_args.args[2]["tool_observations"][0]
        self.assertEqual(observation["status"],"error")
        self.assertEqual(observation["result"]["code"],"agent_tool_scope")
        self.planner.amap.weather.assert_not_awaited()

    async def test_daily_order_normalizes_unique_names_in_ids_and_timing_keys(self):
        self.planner.route_agent = SimpleNamespace(plan=AsyncMock(return_value=ready(("东山口",),
            durations={"东山口":120},start_times={"东山口":"10:00"})))
        turn = await self.daily({"time_window":{"start":"09:00","end":"20:00"}})
        self.assertEqual(turn.order.stop_ids,["guide:a"])
        self.assertEqual(turn.order.durations,{"guide:a":120})
        self.assertEqual(turn.order.start_times,{"guide:a":"10:00"})

    async def test_ambiguous_names_get_one_correction_and_are_never_guessed(self):
        self.state["stops"].update(canonicalize_stops({"guide:d":source_stop("guide:d","同名公园"),
                                                    "guide:e":source_stop("guide:e","同名公园")}))
        bad = ready(("同名公园",))
        self.planner.route_agent = SimpleNamespace(plan=AsyncMock(side_effect=[copy.deepcopy(bad),copy.deepcopy(bad)]))
        with self.assertRaises(ToolError) as failure:
            await self.daily()
        self.assertEqual(failure.exception.code,"activity_choice")
        self.assertEqual(self.planner.route_agent.plan.await_count,2)
        correction = self.planner.route_agent.plan.call_args.args[2]
        self.assertIn("同名公园",correction["validation_error"])
        self.assertIn("guide:d",correction["available_stops"])
        self.assertIn("guide:e",correction["available_stops"])
        self.assertEqual(correction["previous_order"]["stop_ids"],["同名公园"])

    async def test_conflicting_alias_duration_keys_require_a_correction(self):
        bad = ready(durations={"东山口":60,"guide:a":120})
        good = ready(durations={"guide:a":120})
        self.planner.route_agent = SimpleNamespace(plan=AsyncMock(side_effect=[bad,good]))
        turn = await self.daily()
        self.assertEqual(turn.order.durations,{"guide:a":120})
        self.assertEqual(self.planner.route_agent.plan.await_count,2)
        self.assertIn("互相冲突",self.planner.route_agent.plan.call_args.args[2]["validation_error"])


    async def test_global_required_and_excluded_ids_are_scoped_without_changing_global_state(self):
        self.state["stops"].update(canonicalize_stops({"guide:d":source_stop("guide:d","次日备选",assigned_date=NEXT_DATE)}))
        constraints = {"required":["guide:a","guide:c"],"excluded":["guide:b","guide:d"]}
        self.state["stop_constraints"] = copy.deepcopy(constraints)
        self.state["requirements"].update(required_stop_ids=constraints["required"].copy(),excluded_stop_ids=constraints["excluded"].copy())
        original_requirements = copy.deepcopy(self.state["requirements"])
        self.planner.route_agent = SimpleNamespace(plan=AsyncMock(return_value=ready()))
        await self.daily({"requirements":self.state["requirements"],"constraints":self.state["stop_constraints"]})
        payload = self.planner.route_agent.plan.call_args.args[2]
        self.assertEqual(set(payload["available_stops"]),{"guide:a"})
        self.assertEqual(payload["constraints"]["required"],["guide:a"])
        self.assertEqual(payload["constraints"]["excluded"],[])
        self.assertEqual(payload["requirements"]["required_stop_ids"],["guide:a"])
        self.assertEqual(payload["requirements"]["excluded_stop_ids"],[])
        self.assertEqual(self.state["stop_constraints"],constraints)
        self.assertEqual(self.state["requirements"],original_requirements)

    def prepare_activity_flow(self, turns):
        self.state["mode"] = "new"
        self.state["requirements"].update(end_date=DATE)
        self.state["selected_route"] = {"days":[self.day]}
        self.state["itinerary"] = {"days":[],"transport":[],"hotels":[]}
        places = {"guide:a":{"id":"poi-a","name":"东山口","location":"113.2,23.1","evidence_id":"query:1"},
                  "guide:b":{"id":"poi-b","name":"正佳广场","location":"113.3,23.1","evidence_id":"query:2"}}
        self.state["stops"]["guide:a"]["map_place"] = copy.deepcopy(places["guide:a"])
        self.planner.flyai = SimpleNamespace(availability=lambda:{"ready":False})
        self.planner.weather_context = AsyncMock(return_value={})
        self.planner.update_day_places = Mock()
        self.planner.refresh_day_routes = AsyncMock()
        self.planner.query_ticket_prices = AsyncMock()
        self.planner.route_agent = SimpleNamespace(plan=AsyncMock(side_effect=turns))
        self.planner.locate_stops.side_effect = lambda state,pool:canonicalize_stops(
            {identity:{**stop,"map_place":copy.deepcopy(places[identity])} for identity,stop in pool.items()})
        async def movement(state,city,origin,destination,origin_place=None,destination_place=None):
            return {"minutes":15,"mode":"transit"},destination_place
        self.planner.movement.side_effect = movement
        return places

    async def test_newly_selected_activity_locates_only_the_missing_selected_subset(self):
        places = self.prepare_activity_flow([ready(("guide:a","guide:b"),short_day_reason="用户只玩半天，保留下午留白")])
        self.state["requirements"]["preferences"] = "只玩半天"
        result = await self.planner.activities(self.state)
        pools = [set(call.args[1]) for call in self.planner.locate_stops.call_args_list]
        self.assertEqual(pools,[{"guide:a"},{"guide:b"}])
        self.assertTrue(all("guide:c" not in pool for pool in pools))
        visits = [activity for activity in result["itinerary"]["days"][0]["activities"] if activity["kind"] == "visit"]
        self.assertEqual([activity["id"] for activity in visits],["guide:a","guide:b"])
        self.assertEqual(visits[1]["map_place"],places["guide:b"])
        self.assertEqual(result["stops"]["guide:b"]["map_place"],places["guide:b"])

    async def test_afternoon_refill_locates_only_its_newly_selected_missing_stop(self):
        places = self.prepare_activity_flow([ready(),ready(("guide:a","guide:b"))])
        self.state["stops"]["guide:b"]["duration_minutes"] = 360
        result = await self.planner.activities(self.state)
        self.assertEqual(self.planner.route_agent.plan.await_count,2)
        self.assertIn("schedule_feedback",self.planner.route_agent.plan.call_args.args[2])
        pools = [set(call.args[1]) for call in self.planner.locate_stops.call_args_list]
        self.assertEqual(pools,[{"guide:a"},{"guide:b"}])
        visits = [activity for activity in result["itinerary"]["days"][0]["activities"] if activity["kind"] == "visit"]
        self.assertEqual([activity["id"] for activity in visits],["guide:a","guide:b"])
        self.assertEqual(visits[1]["map_place"],places["guide:b"])
