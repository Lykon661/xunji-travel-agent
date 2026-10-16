"""Synthetic fixtures only; these tests do not call a model, crawler or travel API."""
import tempfile
import unittest
from types import SimpleNamespace
from datetime import date
from pathlib import Path

from app.models import Candidate, CandidateDay, Requirements
from app.rules import check_itinerary, enforce_locks, resource_cost, validate_candidate
from app.store import Conflict, Store


REQ = {"origin":"北京","cities":["杭州","苏州"],"start_date":"2026-11-10","end_date":"2026-11-11",
       "people":2,"rooms":1,"budget":8000,"preferences":"少走路","transport":"any","bed_type":"双床房","return_to_origin":True}


class Prices(unittest.TestCase):
    def option(self,basis="room_night",kind="hotel",**extra):
        return {"kind":kind,"price":200,"price_basis":basis,"currency":"CNY",
                "context":{"check_in":"2026-11-10","check_out":"2026-11-12"},**extra}

    def test_multi_night_rate_is_not_total(self):
        self.assertIsNone(resource_cost(self.option(),REQ))

    def test_single_night_rate_counts_rooms(self):
        result = resource_cost(self.option(context={"check_in":"2026-11-10","check_out":"2026-11-11"}),{**REQ,"rooms":2})
        self.assertEqual(result,400)

    def test_starting_unknown_and_other_currency_are_not_known_costs(self):
        for basis in ("starting","unknown"):
            self.assertIsNone(resource_cost(self.option(basis),REQ))
        self.assertIsNone(resource_cost(self.option("person_segment","train",currency="USD"),REQ))

    def test_transport_total_requires_passenger_count(self):
        self.assertIsNone(resource_cost(self.option("segment_total","train"),REQ))
        self.assertEqual(resource_cost(self.option("segment_total","train",quote_people=2),REQ),200)
        self.assertEqual(resource_cost(self.option("person_segment","train"),REQ),400)

    def test_hotel_total_requires_dates_and_room_scope(self):
        self.assertIsNone(resource_cost(self.option("segment_total"),REQ))
        self.assertEqual(resource_cost(self.option("segment_total",quote_rooms=1,check_in="2026-11-10",check_out="2026-11-12"),REQ),200)


class Provenance(unittest.TestCase):
    def test_three_day_trip_rejects_extra_missing_duplicate_or_shifted_dates(self):
        req = Requirements.model_validate({**REQ,"cities":["杭州"],"end_date":"2026-11-12"})
        def route(dates):
            return Candidate(title="路线",reason="日期约束测试",days=[
                CandidateDay(date=date(2026,11,day),city="杭州",stop_ids=["source:1"] if index == 0 else [])
                for index,day in enumerate(dates)])
        stops = {"source:1":{"city":"杭州"}}
        validate_candidate(route([10,11,12]),req,stops)
        for dates in ([10,11,12,13,14], [10,11], [10,11,11], [11,12,13], [10,12,11]):
            with self.subTest(dates=dates), self.assertRaises(ValueError):
                validate_candidate(route(dates),req,stops)

    def test_candidate_cannot_reorder_cities_or_reference_unknown_stop(self):
        req = Requirements.model_validate(REQ)
        valid = Candidate(title="测试路线",reason="人工测试数据",days=[
            CandidateDay(date=date(2026,11,10),city="杭州",stop_ids=["source:1"]),
            CandidateDay(date=date(2026,11,11),city="苏州",stop_ids=[])])
        validate_candidate(valid,req,{"source:1":{"city":"杭州"}})
        unknown = valid.model_copy(deep=True); unknown.days[0].stop_ids = ["invented"]
        with self.assertRaises(ValueError):
            validate_candidate(unknown,req,{"source:1":{"city":"杭州"}})
        reversed_route = valid.model_copy(deep=True)
        reversed_route.days[0].city = "苏州"; reversed_route.days[1].city = "杭州"
        with self.assertRaises(ValueError):
            validate_candidate(reversed_route,req,{"source:1":{"city":"杭州"}})


class Schedule(unittest.TestCase):
    def base(self):
        return {"transport":[],"hotels":[],"days":[{"date":"2026-11-10","city":"杭州","activities":[],"meal_estimate":200,"return_move":{"minutes":None}}]}

    def test_conflicting_locked_activity_is_preserved_then_reported(self):
        old = self.base()
        activity = {"id":"stop:1","kind":"visit","name":"测试地点","start":"09:00","end":"10:00",
                    "evidence":{"kind":"text","quote":"测试地点"},"move_before":{"minutes":30}}
        old["days"][0]["activities"] = [activity]
        new = self.base()
        new["days"][0]["activities"] = [{**activity,"start":"11:00","end":"12:00"}]
        new["transport"] = [{"id":"transport:测试","label":"北京到杭州","buffer_minutes":60,
            "context":{"origin":"北京","destination":"杭州","date":"2026-11-10"},
            "selected":{"kind":"train","departure":"09:00","arrival":"11:00","price":100,"currency":"CNY","price_basis":"person_segment"}}]
        preserved = enforce_locks(old,new,["stop:1"])
        self.assertEqual(preserved["days"][0]["activities"][0]["start"],"09:00")
        self.assertTrue(any(i["code"] == "arrival_conflict" for i in check_itinerary(preserved,REQ)["issues"]))

    def test_insufficient_movement_and_missing_evidence_are_reported(self):
        itinerary = self.base()
        itinerary["days"][0]["activities"] = [
            {"id":"a","kind":"visit","name":"A","start":"09:00","end":"10:00","evidence":{"quote":"A"}},
            {"id":"b","kind":"visit","name":"B","start":"10:10","end":"11:00","move_before":{"minutes":30}}]
        codes = {i["code"] for i in check_itinerary(itinerary,REQ)["issues"]}
        self.assertIn("move_gap",codes); self.assertIn("evidence_missing",codes)

    def test_unknown_fees_never_mean_within_budget(self):
        itinerary = self.base()
        itinerary["hotels"] = [{"id":"hotel:杭州","city":"杭州","label":"杭州住宿","context":{"check_in":"2026-11-10","check_out":"2026-11-11"},"selected":None}]
        budget = check_itinerary(itinerary,REQ)["budget"]
        self.assertEqual(budget["status"],"incomplete")
        self.assertEqual(budget["known_subtotal"],0)

class Persistence(unittest.TestCase):
    def test_save_replay_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory)); trip = store.create_trip("重放测试",REQ)
            self.assertEqual(store.commit(trip["id"],0,trip["document"],generation="one"),1)
            self.assertEqual(store.commit(trip["id"],1,trip["document"],generation="one"),1)
            self.assertEqual(len(store.versions(trip["id"])),1)

    def test_extraction_is_not_erased_by_source_upsert(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory)); trip = store.create_trip("攻略测试",REQ)
            source = {"id":"s1","note_id":"n1","text":"测试正文","images":[],"extraction":None}
            store.add_source(trip["id"],source)
            source["extraction"] = {"stops":[{"id":"s1:0","name":"测试地点"}]}
            store.add_source(trip["id"],source)
            self.assertEqual(store.sources(trip["id"])[0]["extraction"]["stops"][0]["id"],"s1:0")

    def test_stale_versions_cannot_overwrite_and_only_one_run_is_active(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory)); trip = store.create_trip("测试旅行",REQ)
            version = store.commit(trip["id"],0,trip["document"])
            self.assertEqual(version,1)
            with self.assertRaises(Conflict):
                store.commit(trip["id"],0,trip["document"])
            run = store.start_run(trip["id"],1,"test-thread",{})
            with self.assertRaises(Conflict):
                store.start_run(trip["id"],1,"other-thread",{})
            store.recover()
            self.assertEqual(store.run(run)["status"],"recoverable")
            reopened = Store(Path(directory))
            self.assertEqual(reopened.trip(trip["id"])["version"],1)


class PartialModification(unittest.IsolatedAsyncioTestCase):
    async def test_hotel_change_reroutes_its_city_and_preserves_other_city(self):
        from unittest.mock import AsyncMock, Mock
        from app.models import OrderChoice, RouteTurn
        from app.planner import Planner
        planner = Planner.__new__(Planner)
        planner.emit = lambda *args:None
        planner.store = Mock()
        planner.flyai = SimpleNamespace(availability=lambda:{"ready":False})
        planner.models = type("Models",(),{"ask":AsyncMock(return_value=OrderChoice(stop_ids=["s:1"]))})()
        async def route_plan(state,instruction,payload,tools,daily=False):
            return RouteTurn(status="ready",order=await planner.models.ask(OrderChoice,instruction,payload))
        planner.route_agent = SimpleNamespace(plan=route_plan)
        planner.window = AsyncMock(return_value=(540,1200,[],[],{"name":"新酒店","label":"返回住宿"}))
        planner.locate_stops = AsyncMock(side_effect=lambda state,stops:stops)
        planner.weather_context = AsyncMock(return_value={})
        planner.map_order = AsyncMock(side_effect=lambda state,city,ids,origin,destination:(ids,"合成地图顺序"))
        planner.movement = AsyncMock(return_value=({"minutes":25},None))
        suzhou = {"date":"2026-11-11","city":"苏州","activities":[],"gaps":["原有待确认项"],"meal_estimate":200,
                  "return_move":{"minutes":None},"adjustment":"原安排"}
        state = {"trip_id":"trip","requirements":REQ,"mode":"modify","change":{"scopes":["hotel"],"cities":["杭州"],"preserve_cities":["苏州"]},
            "locks":[],"old_itinerary":{"days":[{"date":"2026-11-10","city":"杭州","activities":[]},suzhou]},
            "itinerary":{"days":[],"transport":[],"hotels":[{"id":"hotel:杭州","city":"杭州","selected":{"name":"新酒店"},"selected_by_user":True}]},
            "selected_route":{"days":[{"date":"2026-11-10","city":"杭州","stop_ids":["s:1"]},
                                        {"date":"2026-11-11","city":"苏州","stop_ids":[]}]},
            "stops":{"s:1":{"name":"真实导入地点","city":"杭州","duration_minutes":60,"source_id":"s",
                      "source_title":"人工测试攻略","evidence":{"kind":"text","quote":"真实导入地点"}}}}
        result = await planner.activities(state)
        self.assertEqual(result["itinerary"]["days"][1],suzhou)
        # The arrival day starts at the station, so the adopted hotel is routed as the day's end.
        self.assertTrue(any("新酒店" in call.args[2:4] for call in planner.movement.await_args_list))


if __name__ == "__main__":
    unittest.main()
