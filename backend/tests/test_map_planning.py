"""Synthetic coordinates and mocked tools only. No external service is contacted."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from app.adapters import Amap, ToolError, has_route_leg
from app.models import OrderChoice, Requirements, RouteTurn
from app.planner import Planner
from app.routing import geographic_order
from app.rules import check_itinerary


REQ = {"origin":"北京","cities":["杭州"],"start_date":"2026-11-10","end_date":"2026-11-10",
       "people":2,"rooms":1,"budget":8000,"preferences":"不打车","transport":"train"}
PLACES = {name:{"name":name,"location":f"{120+offset},30"} for name,offset in
          (("酒店",0),("近处",0.01),("中间",0.02),("远处",0.03),("车站",0.04))}


class GeographicPlanning(unittest.TestCase):
    def test_outward_route_avoids_far_then_near_backtrack(self):
        places = {name:PLACES[name] for name in ("近处","中间","远处")}
        self.assertEqual(geographic_order(["远处","近处","中间"],places,PLACES["酒店"],PLACES["车站"]),["近处","中间","远处"])

    def test_missing_location_keeps_order(self):
        self.assertEqual(geographic_order(["近处","远处"],{"近处":PLACES["近处"],"远处":None}),["近处","远处"])

    def test_no_taxi_disables_taxi_but_keeps_self_driving_or_walking(self):
        self.assertFalse(Requirements(**REQ,local_transport="driving",allow_taxi=True).allow_taxi)
        self.assertEqual(Requirements(**REQ,local_transport="driving").local_transport,"driving")
        self.assertEqual(Requirements(**REQ,local_transport="walking").local_transport,"walking")

    def test_forecast_must_match_trip_date(self):
        context = {"杭州":{"forecasts":{"2026-10-13":{"date":"2026-10-13","dayweather":"雨","nightweather":"阴"}}}}
        self.assertFalse(Planner.daily_weather(context,"杭州","2026-11-10")["available"])
        self.assertTrue(Planner.daily_weather(context,"杭州","2026-10-13")["available"])
        self.assertIn("室内",Planner.daily_weather(context,"杭州","2026-10-13")["note"])


class MapTools(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.amap = Amap.__new__(Amap)
        self.amap.cache = {}
        self.amap.call = AsyncMock()

    async def test_transit_seconds_round_up_and_taxi_alternative_is_rejected(self):
        self.amap.call.return_value = {"route":{"taxi_cost":"10","transits":[
            {"duration":"60","segments":[{"taxi":{"distance":"500"}}]},
            {"duration":"601","walking_distance":"300","segments":[{"bus":{"buslines":[{"name":"地铁1号线"}]}}]}]}}
        route = await self.amap.route("trip","杭州",PLACES["酒店"],PLACES["近处"],"transit")
        self.assertEqual(route["minutes"],11)
        self.assertEqual(route["summary"],"地铁1号线")
        self.assertEqual(route["transport_label"],"地铁 + 步行接驳")
        self.assertEqual(route["walking_meters"],300)
        await self.amap.route("trip","杭州",PLACES["酒店"],PLACES["近处"],"transit")
        self.assertEqual(self.amap.call.await_count,1)

    async def test_mcp_empty_railway_placeholder_keeps_real_subway_routes(self):
        self.amap.call.return_value = {"origin":"120,30","destination":"120.01,30","transits":[
            {"duration":"1511","walking_distance":"300","segments":[{
                "walking":{},"bus":{"buslines":[{"name":"地铁1号线"}]},
                "entrance":{},"exit":{},"railway":{"name":"","trip":""}}]}]}
        route = await self.amap.route("trip","杭州",PLACES["酒店"],PLACES["近处"],"transit")
        self.assertEqual(route["minutes"],26)
        self.assertEqual(route["summary"],"地铁1号线")
        self.assertEqual(route["transport_label"],"地铁 + 步行接驳")
        self.amap.call.assert_awaited_once_with("trip","maps_direction_transit_integrated",
            {"origin":"120,30","destination":"120.01,30","city":"杭州","cityd":"杭州"})

    async def test_empty_optional_taxi_and_railway_objects_do_not_discard_bus(self):
        self.amap.call.return_value = {"route":{"transits":[
            {"duration":"600","segments":[{"bus":{"buslines":[{"name":"1路"}]},
                "taxi":{"distance":"0","duration":[],"origin":"","destination":""},
                "railway":{"name":" ","trip":None,"departure_stop":{"name":"","location":[]}}}]}]}}
        route = await self.amap.route("trip","杭州",PLACES["酒店"],PLACES["近处"],"transit")
        self.assertEqual(route["minutes"],10)
        self.assertEqual(route["summary"],"1路")

    async def test_actual_railway_or_taxi_leg_still_excludes_mixed_transit_route(self):
        for extra in ({"railway":{"name":"城际列车","trip":"C123"}},
                      {"railway":{"departure_stop":{"name":"火车站"},"distance":"1000"}},
                      {"taxi":{"distance":"500"}},
                      {"taxi":{"origin":"120,30","destination":"120.01,30"}}):
            with self.subTest(extra=extra):
                self.amap.cache.clear()
                self.amap.call.return_value = {"transits":[{"duration":"60","segments":[
                    {"bus":{"buslines":[{"name":"不能采用的混合方案"}]},**extra}]},
                    {"duration":"601","segments":[{"bus":{"buslines":[{"name":"地铁2号线"}]},
                        "railway":{"name":"","trip":""}}]}]}
                route = await self.amap.route("trip","杭州",PLACES["酒店"],PLACES["近处"],"transit")
                self.assertEqual(route["minutes"],11)
                self.assertEqual(route["summary"],"地铁2号线")

    async def test_placeholders_without_any_bus_line_do_not_become_a_transit_route(self):
        self.amap.call.return_value = {"transits":[{"duration":"600","segments":[
            {"walking":{"duration":"600"},"bus":{"buslines":[]},"railway":{"name":"","trip":""}}]}]}
        route = await self.amap.route("trip","杭州",PLACES["酒店"],PLACES["近处"],"transit")
        self.assertIsNone(route["minutes"])
        self.assertFalse(self.amap.cache)

    def test_optional_leg_presence_depends_on_route_content(self):
        for empty in (None,{},[],{"name":"","trip":""},{"duration":"0","distance":[],"name":" "}):
            with self.subTest(empty=empty):
                self.assertFalse(has_route_leg(empty))
        self.assertTrue(has_route_leg({"trip":"C123"}))
        self.assertTrue(has_route_leg({"duration":"60"}))
        self.assertTrue(has_route_leg({"departure_stop":{"location":"120,30"}}))

    async def test_weather_keeps_forecasts_not_current_observation(self):
        self.amap.call.return_value = {"lives":[{"weather":"晴","temperature":"30"}],
            "forecasts":[{"reporttime":"2026-10-13 08:00:00","casts":[{"date":"2026-10-14","dayweather":"小雨","daytemp":"22"}]}]}
        weather = await self.amap.weather("trip","杭州")
        self.assertEqual(list(weather["forecasts"]),["2026-10-14"])
        self.assertEqual(weather["reporttime"],"2026-10-13 08:00:00")


class DailyPlanning(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.planner = Planner.__new__(Planner)
        self.planner.emit = lambda *args:None
        self.planner.store = Mock()
        self.planner.flyai = SimpleNamespace(availability=lambda:{"ready":False})
        self.planner.amap = SimpleNamespace(cache={},place=AsyncMock(side_effect=lambda trip,city,name:PLACES.get(name)),route=AsyncMock())
        async def route_plan(state,instruction,payload,tools,daily=False):
            order = await self.planner.models.ask(OrderChoice,instruction,payload)
            return RouteTurn(status="ready",order=order)
        self.planner.route_agent = SimpleNamespace(plan=route_plan)
        self.state = {"trip_id":"trip","requirements":REQ,"mode":"new","locks":[],"change":{}}

    async def test_no_taxi_queries_only_walking_and_transit_and_uses_shorter_duration(self):
        async def route(trip,city,origin,destination,mode):
            return {"minutes":35 if mode == "walking" else 12,"mode":mode,"walking_meters":100}
        self.planner.amap.route.side_effect = route
        move,_ = await self.planner.movement(self.state,"杭州","酒店","近处")
        self.assertEqual(move["mode"],"transit")
        self.assertEqual({call.args[-1] for call in self.planner.amap.route.await_args_list},{"walking","transit"})

    async def test_failed_public_routes_stay_unconfirmed_without_driving_fallback(self):
        self.planner.amap.route.side_effect = ToolError("no_route","没有公交路线")
        move,_ = await self.planner.movement(self.state,"杭州","酒店","近处")
        self.assertIsNone(move["minutes"])
        self.assertEqual(move["allowed_modes"],["walking","transit"])
        self.assertEqual(self.planner.amap.route.await_count,2)

    async def test_walking_only_does_not_query_public_transport(self):
        self.planner.amap.route.return_value = {"minutes":25,"mode":"walking"}
        state = {**self.state,"requirements":{**REQ,"local_transport":"walking"}}
        await self.planner.movement(state,"杭州","酒店","近处")
        self.assertEqual([call.args[-1] for call in self.planner.amap.route.await_args_list],["walking"])

    async def test_city_change_still_replans_fully_when_local_mobility_also_changes(self):
        state = {**self.state,"mode":"modify","requirements":{**REQ,"cities":["苏州"],"local_transport":"walking"},
            "old_requirements":{**REQ,"local_transport":"public"},"old_itinerary":{}}
        result = await self.planner.coordinate(state)
        self.assertEqual(result["change"]["scopes"],["full"])

    async def test_actual_route_comparison_rejects_geographic_shortcut_over_barrier(self):
        ids = ["远处","近处","中间"]
        state = {**self.state,"stops":{name:{"name":name,"map_place":PLACES[name]} for name in ids}}
        async def movement(state,city,origin,destination,*places):
            return {"minutes":100 if (origin,destination) == ("酒店","近处") else 1},PLACES.get(destination)
        self.planner.movement = AsyncMock(side_effect=movement)
        result,_ = await self.planner.map_order(state,"杭州",ids,"酒店","车站")
        self.assertEqual(result,ids)

    async def test_return_travel_is_reserved_and_excess_activity_is_omitted(self):
        stops = {name:{"id":name,"name":name,"city":"杭州","duration_minutes":60,
            "source_id":"guide","source_title":"合成攻略","evidence":{"kind":"text","quote":name}} for name in ("近处","远处")}
        self.planner.models = SimpleNamespace(ask=AsyncMock(return_value=OrderChoice(stop_ids=list(stops))))
        self.planner.locate_stops = AsyncMock(return_value=stops)
        self.planner.weather_context = AsyncMock(return_value={})
        self.planner.map_order = AsyncMock(return_value=(list(stops),"合成地图顺序"))
        self.planner.window = AsyncMock(return_value=(540,730,[],[],{"name":"酒店","label":"返回住宿"}))
        self.planner.movement = AsyncMock(return_value=({"minutes":40,"mode":"transit"},None))
        state = {**self.state,"stops":stops,"selected_route":{"days":[{"date":"2026-11-10","city":"杭州","stop_ids":list(stops)}]},
            "itinerary":{"days":[],"transport":[],"hotels":[{"city":"杭州","selected_by_user":True,"selected":{"name":"酒店"}}]}}
        result = await self.planner.activities(state)
        day = result["itinerary"]["days"][0]
        self.assertEqual([a["id"] for a in day["activities"] if a["kind"] == "visit"],["近处"])
        self.assertLessEqual(day["tail_arrival"],day["tail_deadline"])
        self.assertEqual(day["travel_minutes"],0)

    async def test_departure_tail_uses_station_and_keeps_its_transfer_time(self):
        self.planner.movement = AsyncMock(return_value=({"minutes":30,"mode":"transit"},None))
        state = {**self.state,"itinerary":{"hotels":[{"city":"杭州","selected_by_user":True,"selected":{"name":"酒店"}}],"transport":[
            {"context":{"date":"2026-11-10","origin":"杭州","destination":"北京"},"label":"杭州到北京","buffer_minutes":60,
             "selected":{"departure":"18:00","departure_place":"车站"}}]}}
        _,end,_,transfers,tail = await self.planner.window(state,{"date":"2026-11-10","city":"杭州"})
        self.assertEqual(end,17*60)
        self.assertEqual(tail["name"],"车站")
        self.assertEqual(transfers,[])

    async def test_lock_or_repair_changes_predecessor_and_routes_are_refreshed(self):
        day = {"date":"2026-11-10","city":"杭州","available_start":540,"tail_deadline":1200,"end_place":"车站",
            "activities":[{"id":"near","kind":"visit","name":"近处","end":"10:00"},{"id":"far","kind":"visit","name":"远处","end":"11:00"}],"transfers":[]}
        itinerary = {"days":[day],"hotels":[{"city":"杭州","selected_by_user":True,"selected":{"name":"酒店"}}],
                     "transport":[{"context":{"date":"2026-11-10","origin":"杭州","destination":"北京"},"selected":{"departure_place":"车站"}}]}
        self.planner.movement = AsyncMock(return_value=({"minutes":20,"mode":"transit"},None))
        await self.planner.refresh_day_routes(self.state,itinerary)
        self.assertEqual([call.args[2:4] for call in self.planner.movement.await_args_list],[("酒店","近处"),("近处","远处"),("远处","车站")])
        self.assertEqual(day["return_start"],"11:20")
        self.assertEqual(day["return_end"],"11:40")

    def test_tail_and_local_mode_conflicts_are_blocking(self):
        itinerary = {"days":[{"date":"2026-11-10","city":"杭州","activities":[],"meal_estimate":0,
            "tail_arrival":1050,"tail_deadline":1020,"return_move":{"minutes":30,"mode":"driving"}}],
            "hotels":[{"id":"hotel:杭州","city":"杭州","selected_by_user":True,"selected":{"name":"已采用酒店"},
                       "context":{"check_in":"2026-11-10","check_out":"2026-11-11"}}]}
        issues = check_itinerary(itinerary,REQ)["issues"]
        self.assertEqual({issue["code"] for issue in issues},{"tail_conflict","local_transport_conflict"})
        self.assertTrue(all(issue["blocking"] for issue in issues))
