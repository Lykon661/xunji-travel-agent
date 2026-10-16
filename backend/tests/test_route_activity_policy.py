"""Regression cases for the user to run; no live model, map or ticket queries."""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from app.adapters import FlyAI, ToolError
from app.decisions import confirmation_blockers
from app.models import OrderChoice, RouteTurn
from app.planner import Planner
from app.resource_planning import chosen_hotel
from app.rules import check_itinerary, ticket_policy


REQ = {"origin":"北京","cities":["杭州"],"start_date":"2026-11-10","end_date":"2026-11-10",
       "people":1,"rooms":1,"budget":5000,"preferences":"","transport":"train"}


class ActivityPolicy(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.planner = Planner.__new__(Planner)
        self.planner.emit = Mock()
        self.planner.store = Mock()
        self.planner.flyai = SimpleNamespace(availability=lambda:{"ready":True},ticket=AsyncMock())
        self.planner.amap = SimpleNamespace(cache={})

    async def test_no_ticket_claim_free_site_and_dining_do_not_trigger_queries(self):
        activities = [{"id":"street","name":"街区","kind":"visit"},
                      {"id":"park","name":"公园","kind":"visit","ticket_claim":"免费开放"},
                      {"id":"food","name":"攻略推荐餐馆","kind":"visit","category":"dining"}]
        itinerary = {"days":[{"city":"杭州","date":"2026-11-10","activities":activities}]}
        await self.planner.query_ticket_prices({"trip_id":"trip"},itinerary,{"2026-11-10"})
        self.planner.flyai.ticket.assert_not_awaited()
        self.assertTrue(all("ticket_quote" not in activity for activity in activities))
        for activity in activities:
            activity.update(start="09:00",end="10:00",evidence={"quote":activity["name"]})
        result = check_itinerary(itinerary,REQ)
        self.assertFalse(any("门票" in note for note in result["budget"]["unknown"]))

    def test_free_entry_with_paid_optional_exhibit_retains_ticket_information(self):
        self.assertEqual(ticket_policy({"ticket_claim":"公园免费，展馆门票30元"}),"paid")

    async def test_multiple_ticket_types_are_listed_without_choosing_or_summing(self):
        flyai = FlyAI.__new__(FlyAI)
        flyai.search = AsyncMock(return_value={"queried_at":"now","raw":{"items":[
            {"id":"park","name":"合成景区","ticketInfo":[
                {"ticketName":"成人票","price":50,"priceDate":"2026-11-10"},
                {"ticketName":"联票","price":80,"priceDate":"2026-11-10"}]}]}})
        quote = await flyai.ticket("trip","杭州","合成景区","2026-11-10")
        self.assertIsNone(quote["price"])
        self.assertEqual(quote["status"],"options")
        self.assertEqual([option["price"] for option in quote["options"]],[50,80])
        self.assertEqual(len({option["id"] for option in quote["options"]}),2)

    async def test_daily_order_accepts_user_selected_stop_from_unassigned_source_pool(self):
        day = {"date":"2026-11-10","city":"杭州","stop_ids":["guide:a"]}
        state = {"stops":{identity:{"id":identity,"city":"杭州","name":identity} for identity in ("guide:a","guide:b")},
                 "selected_route":{"days":[day]},"stop_constraints":{}}
        self.planner.route_agent = SimpleNamespace(plan=AsyncMock(return_value=RouteTurn(
            status="ready",order=OrderChoice(stop_ids=["guide:b","guide:a"]))))
        turn = await self.planner.daily_order(state,day,["guide:a"],set(),{})
        self.assertEqual(turn.order.stop_ids,["guide:b","guide:a"])

    async def test_invalid_id_gets_one_correction_and_other_dates_remain_protected(self):
        day = {"date":"2026-11-10","city":"杭州","stop_ids":["a"]}
        state = {"stops":{identity:{"id":identity,"city":"杭州"} for identity in ("a","other-day")},
                 "selected_route":{"days":[day,{"date":"2026-11-11","stop_ids":["other-day"]}]}}
        bad = RouteTurn(status="ready",order=OrderChoice(stop_ids=["other-day"]))
        good = RouteTurn(status="ready",order=OrderChoice(stop_ids=["a"]))
        self.planner.route_agent = SimpleNamespace(plan=AsyncMock(side_effect=[bad,good]))
        await self.planner.daily_order(state,day,["a"],set(),{})
        self.assertEqual(self.planner.route_agent.plan.await_count,2)
        self.assertIn("validation_error",self.planner.route_agent.plan.call_args.args[2])
        self.planner.route_agent.plan = AsyncMock(return_value=bad)
        with self.assertRaises(ToolError):
            await self.planner.daily_order(state,day,["a"],set(),{})
        self.assertEqual(self.planner.route_agent.plan.await_count,2)

    async def test_unknown_lodging_does_not_become_a_fictitious_start_or_end(self):
        state = {"requirements":REQ,"itinerary":{"hotels":[],"transport":[]}}
        _,_,_,_,tail = await self.planner.window(state,{"date":"2026-11-10","city":"杭州"})
        self.assertIsNone(tail["start_name"])
        self.assertIsNone(tail["name"])
        day = {"date":"2026-11-10","city":"杭州","activities":[
            {"id":"a","name":"首站","kind":"visit","start":"09:00","end":"10:00","map_place":{"location":"120,30"}},
            {"id":"b","name":"次站","kind":"visit","start":"10:35","end":"11:00","map_place":{"location":"120.01,30"}}]}
        self.planner.movement = AsyncMock(return_value=({"minutes":15,"mode":"walking"},None))
        await self.planner.refresh_day_routes(state,{"days":[day]})
        self.assertEqual(day["activities"][0]["move_before"],{})
        self.assertEqual(day["return_move"],{})
        self.assertEqual(day["travel_minutes"],15)
        self.planner.movement.assert_awaited_once()
        self.assertEqual(confirmation_blockers({"days":[day]}),[])

    async def test_model_recommendation_does_not_count_as_user_chosen_lodging(self):
        hotel = {"id":"hotel:杭州","city":"杭州","selected":{"name":"推荐公寓"}}
        itinerary = {"hotels":[hotel],"transport":[]}
        state = {"requirements":REQ,"itinerary":itinerary}
        self.planner.movement = AsyncMock()
        _,_,_,_,tail = await self.planner.window(state,{"city":"杭州","date":"2026-11-10"})
        self.assertIsNone(tail["start_name"])
        self.assertIsNone(tail["name"])
        self.planner.movement.assert_not_awaited()
        self.assertIsNone(chosen_hotel(itinerary,"杭州"))
        self.assertNotIn("selected_by_user",hotel)
        hotel["selected_by_user"] = True
        self.assertEqual(chosen_hotel(itinerary,"杭州"),hotel["selected"])

    async def test_unselected_hotel_legs_are_removed_from_walking_and_costs(self):
        day = {"city":"杭州","date":"2026-11-10","start_place":"推荐公寓","end_place":"推荐公寓",
               "activities":[
                   {"id":"a","name":"首站","kind":"visit","start":"09:00","end":"10:00","evidence":{"quote":"首站"},
                    "move_before":{"minutes":60,"walking_meters":7000,"mode":"transit","cost":10}},
                   {"id":"b","name":"次站","kind":"visit","start":"10:35","end":"11:00","evidence":{"quote":"次站"},
                    "move_before":{"minutes":15,"walking_meters":500,"mode":"walking"}}],
               "return_move":{"minutes":70,"walking_meters":7700,"mode":"transit","cost":10},
               "transfers":[{"label":"到达后前往住宿放行李","move":{"minutes":20,"walking_meters":1000,"mode":"transit","cost":5}}]}
        itinerary = {"days":[day],"transport":[],"hotels":[{"id":"hotel:杭州","city":"杭州",
                     "context":{"check_in":"2026-11-10","check_out":"2026-11-11"},"selected":{"name":"推荐公寓"}}]}
        await self.planner.refresh_day_routes({"requirements":REQ},itinerary,refresh_routes=False)
        self.assertIsNone(day["start_place"])
        self.assertIsNone(day["end_place"])
        self.assertEqual(day["activities"][0]["move_before"],{})
        self.assertEqual(day["return_move"],{})
        self.assertEqual(day["transfers"],[])
        self.assertEqual(day["walking_meters"],500)
        self.assertEqual(day["travel_minutes"],15)
        result = check_itinerary(itinerary,REQ)
        self.assertEqual(result["budget"]["estimated_local_transport"],0)
        self.assertFalse(any(issue["code"] == "walking_limit" for issue in result["issues"]))

    async def test_daily_schedule_reserves_lunch_without_injecting_a_restaurant(self):
        stop = {"id":"a","city":"杭州","name":"景点","source_id":"guide","source_title":"攻略",
                "map_place":{"location":"120,30"},"evidence":{"quote":"景点"},"duration_minutes":30}
        self.planner.locate_stops = AsyncMock(return_value={"a":stop})
        self.planner.weather_context = AsyncMock(return_value={})
        self.planner.map_order = AsyncMock(return_value=(["a"],"单点"))
        self.planner.route_agent = SimpleNamespace(plan=AsyncMock(return_value=RouteTurn(status="ready",order=OrderChoice(stop_ids=["a"]))))
        self.planner.amap.around = AsyncMock()
        state = {"trip_id":"trip","requirements":{**REQ,"earliest_departure":"12:00"},"stops":{"a":stop},
                 "mode":"new","locks":[],"selected_route":{"days":[{"date":"2026-11-10","city":"杭州","stop_ids":["a"]}]},
                 "itinerary":{"days":[],"hotels":[],"transport":[]}}
        result = await self.planner.activities(copy.deepcopy(state))
        activities = result["itinerary"]["days"][0]["activities"]
        self.assertEqual([activity["kind"] for activity in activities],["visit","rest"])
        self.assertEqual(activities[0]["start"],"12:00")
        self.assertEqual((activities[1]["start"],activities[1]["end"]),("12:30","13:30"))
        self.assertEqual(activities[1]["meal"],"lunch")
        self.assertNotIn("source_id",activities[1])
        self.planner.amap.around.assert_not_awaited()
