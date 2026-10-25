"""Synthetic planner/resource regressions; no providers or real models are called."""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from app.models import OrderChoice, Requirements
from app.planner import Planner
from app.rules import check_itinerary


REQ = Requirements(origin="北京",cities=["杭州"],start_date="2026-11-11",end_date="2026-11-12",
                   budget=10000,people=1,rooms=1,transport="train",return_to_origin=False).model_dump(mode="json")
HOTEL_CONTEXT = {"city":"杭州","check_in":"2026-11-11","check_out":"2026-11-12"}
TRAIN_CONTEXT = {"origin":"北京","destination":"杭州","date":"2026-11-11"}
HOTEL_PLACE = {"id":"hotel-poi","name":"合成酒店","location":"120,30"}


def hotel(**changes):
    return {"id":"hotel-option","supplier_id":"supplier-hotel","kind":"hotel","name":"合成酒店",
            "address":"合成路1号","bed_type":"双床房","currency":"CNY","price":200,
            "price_basis":"room_night","context":copy.deepcopy(HOTEL_CONTEXT),
            "queried_at":"2026-10-15T09:00:00+00:00","map_place":copy.deepcopy(HOTEL_PLACE),**changes}


def train(identity="train-option",**changes):
    return {"id":identity,"kind":"train","name":"G1","currency":"CNY","price":100,
            "price_basis":"person_segment","context":copy.deepcopy(TRAIN_CONTEXT),
            "departure":"2026-11-11 09:00","arrival":"2026-11-11 11:00",
            "departure_place":"北京南","arrival_place":"杭州东","constraint_issues":[],**changes}


class HotelAdoptionFlow(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.planner = Planner.__new__(Planner)
        self.planner.emit = Mock()
        self.planner.hotel_areas = Mock(return_value=["合成区域"])
        self.planner.resource_agent = SimpleNamespace(search=AsyncMock(),choose=AsyncMock())
        self.planner.enrich_hotels = AsyncMock(side_effect=lambda state,city,options:options)
        selected = hotel()
        entry = {"id":"hotel:杭州","city":"杭州","context":copy.deepcopy(HOTEL_CONTEXT),
                 "selected_by_user":True,"selected":selected,"options":[selected]}
        days = [{"date":day,"city":"杭州","activities":[],"meal_estimate":0}
                for day in ("2026-11-11","2026-11-12")]
        itinerary = {"hotels":[entry],"transport":[],"days":days}
        self.state = {"trip_id":"trip","mode":"modify","requirements":copy.deepcopy(REQ),"locks":[],
                      "stops":{},"change":{"scopes":["full"]},"itinerary":itinerary,
                      "old_itinerary":copy.deepcopy(itinerary),"selected_route":{"days":[
                          {"date":day["date"],"city":"杭州","stop_ids":[]} for day in days]}}

    def response(self,option):
        self.planner.resource_agent.search.return_value = {"status":"ok","options":[option],"evidence_id":"query:new"}

    async def test_preserving_the_adopted_hotel_does_not_ask_a_model_to_reselect_it(self):
        fresh = hotel(id="new-query-option",price=220)
        self.response(fresh)
        original = copy.deepcopy(self.state)
        result = await self.planner.hotels(self.state)
        entry = result["itinerary"]["hotels"][0]
        self.assertEqual(entry["selected"]["id"],"new-query-option")
        self.assertEqual(entry["selected"]["price"],220)
        self.assertTrue(entry["selected_by_user"])
        self.planner.resource_agent.choose.assert_not_awaited()
        self.assertEqual(self.state,original)

    async def test_rejected_adopted_candidate_keeps_the_original_quote_and_remains_blocking(self):
        fresh = hotel(id="new-query-option",price=220,constraint_issues=["住宿接驳步行已超过每日上限"])
        self.response(fresh)
        result = await self.planner.hotels(self.state)
        entry = result["itinerary"]["hotels"][0]
        self.assertEqual(entry["selected"]["id"],"hotel-option")
        self.assertEqual(entry["selected"]["price"],200)
        self.assertTrue(entry["selected_by_user"])
        self.assertEqual(entry["selected"]["constraint_issues"],fresh["constraint_issues"])
        self.assertIn("query:new",entry["evidence_ids"])
        self.assertTrue(any("未重新确认" in message for message in entry["errors"]))
        issues = check_itinerary(result["itinerary"],self.state["requirements"])["issues"]
        self.assertTrue(any(issue["code"] == "resource_constraint" and issue["blocking"] for issue in issues))
        self.planner.resource_agent.choose.assert_not_awaited()
        self.assertNotIn("constraint_issues",self.state["old_itinerary"]["hotels"][0]["selected"])

    async def test_changed_dates_do_not_restore_user_adoption_from_matching_supplier_or_option_id(self):
        old = self.state["old_itinerary"]["hotels"][0]
        old_context = {**HOTEL_CONTEXT,"check_in":"2026-11-10","check_out":"2026-11-11"}
        old["context"] = copy.deepcopy(old_context)
        old["selected"]["context"] = copy.deepcopy(old_context)
        self.response(hotel())
        result = await self.planner.hotels(self.state)
        entry = result["itinerary"]["hotels"][0]
        self.assertEqual(entry["context"],HOTEL_CONTEXT)
        self.assertIsNone(entry["selected"])
        self.assertFalse(entry["selected_by_user"])
        self.planner.resource_agent.choose.assert_not_awaited()

    async def test_an_explicit_hotel_replacement_with_another_room_type_requires_new_adoption(self):
        replacement = hotel(bed_type="大床房",price=150)
        self.response(replacement)
        self.state["change"]["cheaper_hotel"] = True
        self.planner.resource_agent.choose.return_value = (replacement,"更便宜的不同房型，需重新采用")
        result = await self.planner.hotels(self.state)
        entry = result["itinerary"]["hotels"][0]
        self.assertEqual(entry["selected"]["bed_type"],"大床房")
        self.assertFalse(entry["selected_by_user"])
        self.planner.resource_agent.choose.assert_awaited_once()

    async def test_a_normal_replan_does_not_silently_replace_the_adopted_room_type(self):
        self.response(hotel(bed_type="大床房",price=150))
        result = await self.planner.hotels(self.state)
        entry = result["itinerary"]["hotels"][0]
        self.assertEqual(entry["selected"]["bed_type"],"双床房")
        self.assertEqual(entry["selected"]["price"],200)
        self.assertTrue(any("未取得身份、房型和日期一致" in message for message in entry["errors"]))
        self.planner.resource_agent.choose.assert_not_awaited()

    async def test_explicit_bed_requirement_change_keeps_the_old_quote_but_adds_a_hard_blocker(self):
        self.state["old_requirements"] = {**copy.deepcopy(REQ),"bed_type":"双床房"}
        self.state["requirements"]["bed_type"] = "大床房"
        self.response(hotel(id="new-king-option",bed_type="大床房",price=150,
                            queried_at="2026-10-15T10:00:00+00:00"))
        result = await self.planner.hotels(self.state)
        entry = result["itinerary"]["hotels"][0]
        self.assertEqual(entry["selected"]["id"],"hotel-option")
        self.assertEqual(entry["selected"]["bed_type"],"双床房")
        self.assertEqual(entry["selected"]["queried_at"],"2026-10-15T09:00:00+00:00")
        self.assertTrue(any("本次修改后的床型" in message for message in entry["selected"]["constraint_issues"]))
        issues = check_itinerary(result["itinerary"],self.state["requirements"])["issues"]
        self.assertTrue(any(issue["code"] == "resource_constraint" and issue["blocking"] for issue in issues))
        self.assertNotEqual(entry["selected"]["id"],"new-king-option")
        self.planner.resource_agent.choose.assert_not_awaited()


class HotelTransferDirections(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.planner = Planner.__new__(Planner)
        self.planner.amap = SimpleNamespace(match_resource=AsyncMock(return_value=copy.deepcopy(HOTEL_PLACE)))
        self.planner.movement = AsyncMock(return_value=({"minutes":10,"walking_meters":100},None))
        self.state = {"trip_id":"trip","requirements":copy.deepcopy(REQ),"stops":{
            "first":{"name":"首个景点","map_place":{"id":"first-poi","location":"120.01,30"}},
            "last":{"name":"末个景点","map_place":{"id":"last-poi","location":"120.02,30"}}},
            "selected_route":{"days":[{"date":"2026-11-11","city":"杭州","stop_ids":["first","last"]}]},
            "itinerary":{"transport":[],"hotels":[]}}

    def movements(self):
        return [call.args[2:4] for call in self.planner.movement.await_args_list]

    def arrival(self):
        return {"context":copy.deepcopy(TRAIN_CONTEXT),"selected":{"arrival_place":"杭州东"}}

    def departure(self):
        return {"context":{"origin":"杭州","destination":"苏州","date":"2026-11-11"},
                "selected":{"departure_place":"杭州西"}}

    async def test_arrival_station_moves_towards_hotel_and_sightseeing_returns_towards_hotel(self):
        self.state["itinerary"]["transport"] = [self.arrival()]
        result = await self.planner.enrich_hotels(self.state,"杭州",[hotel()])
        self.assertEqual(self.movements(),[("杭州东","合成酒店"),("合成酒店","首个景点"),("末个景点","合成酒店")])
        self.assertEqual(result[0]["commute_minutes"],30)
        self.assertEqual(result[0]["walking_meters"],300)

    async def test_departure_after_visits_does_not_add_a_fictitious_return_to_hotel_or_hotel_station_leg(self):
        self.state["itinerary"]["transport"] = [self.departure()]
        result = await self.planner.enrich_hotels(self.state,"杭州",[hotel()])
        self.assertEqual(self.movements(),[("合成酒店","首个景点")])
        self.assertEqual(result[0]["commute_minutes"],10)

    async def test_arrival_and_departure_with_visits_only_include_actual_hotel_legs(self):
        self.state["itinerary"]["transport"] = [self.arrival(),self.departure()]
        await self.planner.enrich_hotels(self.state,"杭州",[hotel()])
        self.assertEqual(self.movements(),[("杭州东","合成酒店"),("合成酒店","首个景点")])

    async def test_departure_without_visits_moves_from_hotel_to_station(self):
        self.state["selected_route"]["days"][0]["stop_ids"] = []
        self.state["itinerary"]["transport"] = [self.departure()]
        await self.planner.enrich_hotels(self.state,"杭州",[hotel()])
        self.assertEqual(self.movements(),[("合成酒店","杭州西")])


class TransportSortReuse(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.planner = Planner.__new__(Planner)
        self.planner.emit = Mock()
        self.planner.resource_agent = SimpleNamespace(search=AsyncMock(),choose=AsyncMock(return_value=(None,"留给用户选择")))
        self.planner.enrich_transport = AsyncMock(side_effect=lambda state,context,options:options)
        self.state = {"trip_id":"trip","mode":"new","requirements":copy.deepcopy(REQ),"locks":[],"change":{},
                      "old_itinerary":{},"itinerary":{"transport":[],"hotels":[],"days":[]},
                      "selected_route":{"days":[{"date":"2026-11-11","city":"杭州","stop_ids":[]},
                                                  {"date":"2026-11-12","city":"杭州","stop_ids":[]}]}}

    async def test_repeated_sort_results_stop_after_two_queries_and_deduplicate_the_options(self):
        async def search(*args,**kwargs):
            return {"options":[train()],"evidence_id":"query:"+str(args[3]["sort-type"])}
        self.planner.resource_agent.search.side_effect = search
        result = await self.planner.transport(self.state)
        calls = self.planner.resource_agent.search.await_args_list
        self.assertEqual([call.args[3]["sort-type"] for call in calls],[2,3])
        entry = result["itinerary"]["transport"][0]
        self.assertEqual([option["id"] for option in entry["options"]],["train-option"])
        self.assertEqual(entry["evidence_ids"],["query:2","query:3"])

    async def test_new_options_keep_later_sort_queries_available(self):
        async def search(*args,**kwargs):
            sort = args[3]["sort-type"]
            return {"options":[train("train-"+str(sort))],"evidence_id":"query:"+str(sort)}
        self.planner.resource_agent.search.side_effect = search
        result = await self.planner.transport(self.state)
        self.assertEqual([call.args[3]["sort-type"] for call in self.planner.resource_agent.search.await_args_list],[2,3,4])
        self.assertEqual(len(result["itinerary"]["transport"][0]["options"]),3)
