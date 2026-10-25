import copy
import unittest
from datetime import date, timedelta
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock

from app import authority
from app.models import OrderChoice, Requirements, wish_stop_id
from app.nodes.coordinating import identity_unmet, public_document
from app.planner import Planner
from app.planning_recovery import regression_reason
from app.unmet import collect
from tests import test_coordinator_graph as graph_fixture
from tests.test_coordinator import agent, question, unmet


class DraftVisibility(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = graph_fixture.CoordinatorGraph()
        self.fixture.addCleanup = self.addCleanup
        self.fixture.setUp()
        self.planner = self.fixture.planner
        self.initial = self.fixture.initial
        self.store = self.fixture.store
        self.trip_id = self.fixture.trip["id"]

    def five_day_fixture(self):
        req = Requirements.model_validate({**self.initial["requirements"],"end_date":"2026-11-14",
            "wish_places":[{"city":"杭州","name":"广东科学中心"}]}).model_dump(mode="json")
        wish = wish_stop_id("杭州","广东科学中心")
        self.initial["requirements"] = req
        self.initial["stop_constraints"]["required"] = [wish]
        template = copy.deepcopy(self.initial["itinerary"]["days"][0])
        self.initial["stops"][wish] = {**copy.deepcopy(self.initial["stops"]["a"]),
            "id":wish,"name":"广东科学中心","source_kind":"user",
            "map_place":{"id":"science","name":"广东科学中心","location":"120.5,30"}}
        days, routes = [], []
        for index,identity in enumerate(["a","b","c","d",wish]):
            day = copy.deepcopy(template)
            day["date"] = (date(2026,11,10)+timedelta(days=index)).isoformat()
            activity = {**copy.deepcopy(day["activities"][0]),**self.initial["stops"][identity]}
            day["activities"] = [activity]
            days.append(day)
            routes.append({"date":day["date"],"city":"杭州","stop_ids":[identity],"adjustment":""})
        self.initial["itinerary"]["days"] = days
        self.initial["candidates"][0].update(title="五天合成路线",reason="等价现场结构",days=routes)
        for identity,name in (("unused-chimelong","长隆"),("unused-community","晓港社区")):
            self.initial["stops"][identity] = {"id":identity,"name":name,"city":"杭州","source_id":"guide",
                "source_title":"合成攻略","duration_minutes":90,"evidence":{"quote":name},
                "map_candidates":[{"id":identity+str(index),"name":name+str(index),"location":"120,30"}
                                  for index in range(20)]}
        self.initial["selected_route"] = {}
        return wish

    async def test_five_day_generation_ignores_unused_guide_identities_and_keeps_visible_draft(self):
        self.five_day_fixture()
        self.initial["stops"]["a"].update(map_place=None,map_candidates=[
            {"id":"east","name":"公园东区","location":"120,30"},
            {"id":"west","name":"公园西区","location":"121,30"}])
        self.planner.guide_analysis.return_value = {"unmet":[unmet("place_identity")],"coordinator_resume":"discover"}
        async def discover(value):
            result = await self.fixture.discover(value)
            return {**result,"agent_unmet":copy.deepcopy(value.get("coordinator_deferred_unmet",[]))}
        self.planner.discover.side_effect = discover
        self.planner.assess = AsyncMock(side_effect=MethodType(Planner.assess,self.planner))
        self.planner.activities = AsyncMock(side_effect=MethodType(Planner.activities,self.planner))
        self.planner.transport = AsyncMock(return_value={})
        self.planner.hotels = AsyncMock(return_value={})
        self.planner.locate_stops = AsyncMock(side_effect=lambda state,stops:copy.deepcopy(stops))
        self.planner.weather_context = AsyncMock(return_value={})
        self.planner.resolve_hubs = AsyncMock(return_value=None)
        self.planner.refresh_day_routes = AsyncMock()
        self.planner.query_ticket_prices = AsyncMock()
        self.planner.daily_order = AsyncMock(side_effect=lambda state,day,ids,*args:
            SimpleNamespace(order=OrderChoice(stop_ids=ids,explanation="采用现有地点",short_day_reason="攻略池有限"),unmet=[]))
        async def schedule(state,route,order,*args):
            day = copy.deepcopy(next(item for item in self.initial["itinerary"]["days"] if item["date"] == route["date"]))
            day["activities"] = [item for item in day["activities"] if item["id"] in order.stop_ids]
            return day
        self.planner.schedule_activity_day = AsyncMock(side_effect=schedule)
        initial,_,run_id = self.fixture.context()
        await self.planner.execute(run_id,initial)
        doc = public_document(self.store.trip(self.trip_id)["document"])
        self.planner.assess.assert_awaited_once()
        self.assertEqual(len(doc["candidates"][0]["draft"]["days"]),5)
        self.assertFalse(any(item["kind"] == "place_identity" for item in doc["unmet"]))
        self.assertEqual(doc["system_assumptions"][0]["current_action"]["type"],"skip")
        self.assertNotEqual((doc.get("pending") or {}).get("kind"),"decision")
        self.assertEqual(self.store.run(run_id)["status"],"waiting")

    async def test_wanted_ambiguous_place_still_interrupts_before_scheduling(self):
        self.five_day_fixture()
        wanted = wish_stop_id("杭州","长隆")
        self.initial["requirements"] = Requirements.model_validate({**self.initial["requirements"],
            "wish_places":[{"city":"杭州","name":"长隆"}]}).model_dump(mode="json")
        self.initial["stops"][wanted] = {**copy.deepcopy(self.initial["stops"]["unused-chimelong"]),
            "id":wanted,"source_kind":"user"}
        q = question({"type":"select_poi","stop_id":wanted,"poi_id":"unused-chimelong0"},
            {"type":"select_poi","stop_id":wanted,"poi_id":"unused-chimelong1"},refs=[{"type":"stop","id":wanted}])
        self.planner.coordinator_agent = agent({"needs_user":[{"unmet_indexes":[0],
            "reason_kind":"needs_user_authority","question":q}]})
        _,snapshot,_,_ = await self.fixture.invoke()
        pending = snapshot.interrupts[0].value
        self.assertEqual((pending["kind"],pending["stage_scope"]),("decision","early_identity"))
        self.planner.assess.assert_not_awaited()

    def test_identity_scope_excludes_unused_guides_but_includes_route_required_and_locks(self):
        self.five_day_fixture()
        self.initial["itinerary"] = {}
        self.assertEqual(identity_unmet(self.initial),[])
        for kind in ("route","required","locked"):
            value = copy.deepcopy(self.initial)
            if kind == "route":
                value["candidates"][0]["days"][0]["stop_ids"].append("unused-chimelong")
            elif kind == "required":
                value["requirements"]["required_stop_ids"].append("unused-chimelong")
            else:
                value["locks"] = ["unused-chimelong"]
            self.assertEqual([item.refs[0].id for item in identity_unmet(value)],["unused-chimelong"])

    def test_unused_identity_from_worker_does_not_become_confirmation_blocker(self):
        self.five_day_fixture()
        self.initial["agent_unmet"] = [unmet("place_identity",stop="unused-chimelong")]
        self.assertFalse(any(item.kind == "place_identity" for item in collect(self.initial,self.initial["itinerary"])))

    async def test_pre_draft_identity_reassessment_never_runs_partial_scheduling(self):
        self.five_day_fixture()
        initial,_,_ = self.fixture.context()
        initial.update(candidates=copy.deepcopy(self.initial["candidates"]),itinerary={"days":[],"transport":[],"hotels":[]})
        plan,items = await self.planner.reassess(initial,["2026-11-10"])
        self.assertFalse(plan.get("days"))
        self.assertEqual(items,[])
        self.assertNotIn("draft",initial["candidates"][0])
        self.planner.activities.assert_not_awaited()
        self.planner.audit.assert_not_awaited()

    async def test_empty_reassessment_rolls_back_without_overwriting_existing_draft(self):
        initial,_,_ = self.fixture.context()
        initial.update(itinerary=copy.deepcopy(self.initial["itinerary"]),candidates=copy.deepcopy(self.initial["candidates"]))
        initial["candidates"][0]["draft"] = copy.deepcopy(initial["itinerary"])
        original = copy.deepcopy(initial)
        self.planner.activities = AsyncMock(return_value={"itinerary":{"days":[],"transport":[],"hotels":[]}})
        plan,items = await self.planner.reassess(initial,["2026-11-10"])
        self.assertEqual(plan,original["itinerary"])
        self.assertEqual(initial["candidates"],original["candidates"])
        self.assertTrue(any(item.blocking and item.kind == "other" for item in items))
        self.planner.audit.assert_not_awaited()

    async def test_empty_candidate_pack_preserves_draft_and_prevents_confirmation(self):
        initial,_,_ = self.fixture.context()
        initial["candidates"] = copy.deepcopy(self.initial["candidates"])
        assessed = await self.fixture.assess(initial)
        candidate = assessed["candidates"][0]
        result = self.planner.pack_candidate({**initial,**assessed},candidate,{"days":[]},assessed["review"])
        self.assertEqual(result["candidates"][0]["draft"],candidate["draft"])
        self.assertTrue(result["candidates"][0]["confirmation_blockers"])

    async def test_empty_assessment_result_preserves_draft_and_refresh_keeps_blocker(self):
        initial,_,_ = self.fixture.context()
        initial["candidates"] = copy.deepcopy(self.initial["candidates"])
        assessed = await self.fixture.assess(initial)
        initial.update(assessed)
        result = await self.planner.assess_result(initial,assessed["candidates"][0],
                                                 {"days":[],"transport":[],"hotels":[]})
        def visits(plan):
            return [(day["date"],[(item["id"],item["start"],item["end"],item.get("map_place"),item.get("evidence"))
                    for item in day["activities"] if item.get("kind") == "visit"]) for day in plan["days"]]
        self.assertEqual(visits(result["itinerary"]),visits(assessed["itinerary"]))
        self.assertTrue(result["candidates"][0]["confirmation_blockers"])
        trip = self.store.trip(self.trip_id)
        trip["document"].update(result)
        trip["document"]["stops"] = copy.deepcopy(initial["stops"])
        refreshed = self.planner.refresh_review_status(trip)
        self.assertTrue(refreshed["document"]["candidates"][0]["confirmation_blockers"])

    async def test_review_only_reassessment_uses_candidate_draft_behind_empty_shell(self):
        initial,_,_ = self.fixture.context()
        initial["candidates"] = copy.deepcopy(self.initial["candidates"])
        assessed = await self.fixture.assess(initial)
        initial.update(assessed,itinerary={"days":[],"transport":[],"hotels":[]},
                       coordinator_previous_plan={"days":[]})
        plan,_ = await self.planner.reassess(initial,[])
        self.assertEqual(plan["days"],assessed["itinerary"]["days"])
        self.planner.activities.assert_not_awaited()
        self.planner.audit.assert_awaited()

    def test_empty_itinerary_cannot_be_confirmed_or_saved(self):
        from app.store import Conflict
        with self.assertRaises(Conflict):
            self.planner.assert_review({},None,{"days":[]})

    def test_nonempty_draft_wins_over_empty_itinerary_shell(self):
        plan = self.initial["itinerary"]
        for key,value in (("candidates",[{"draft":plan}]),("selected_route",{"draft":plan}),
                          ("old_itinerary",plan),("review_previous",{"plan":plan})):
            with self.subTest(source=key):
                result = self.planner.coordinator_plan({"itinerary":{"days":[],"transport":[]},key:value})
                self.assertEqual(result,plan)
        self.assertIsNotNone(regression_reason(plan,{"days":[]}))

    async def test_blocked_terminal_publishes_latest_unconfirmed_draft(self):
        self.fixture.problems = [unmet("fact_unverified")]
        initial,_,run_id = self.fixture.context()
        await self.planner.execute(run_id,initial)
        doc = public_document(self.store.trip(self.trip_id)["document"])
        self.assertEqual(doc["stage"],"conflicts")
        self.assertIsNone(doc["pending"])
        self.assertTrue(doc["itinerary"]["days"])
        self.assertEqual(doc["itinerary"],doc["candidates"][0]["draft"])
        self.assertTrue(doc["candidates"][0]["confirmation_blockers"])
        self.assertEqual(self.store.run(run_id)["status"],"completed")

    async def test_blocked_empty_state_keeps_saved_draft_visible(self):
        doc = self.store.trip(self.trip_id)["document"]
        route = copy.deepcopy(self.initial["candidates"][0])
        route["draft"] = copy.deepcopy(self.initial["itinerary"])
        doc.update(candidates=[route],itinerary=copy.deepcopy(route["draft"]))
        self.store.update(self.trip_id,doc)
        initial,_,_ = self.fixture.context()
        await self.planner.blocked_plan(initial)
        saved = self.store.trip(self.trip_id)["document"]
        self.assertEqual(saved["itinerary"],route["draft"])
        self.assertEqual(saved["candidates"][0]["draft"],route["draft"])

    async def test_blocked_terminal_updates_candidate_with_latest_nonempty_plan(self):
        value,_,_ = self.fixture.context()
        older = copy.deepcopy(self.initial["itinerary"])
        latest = copy.deepcopy(older)
        latest["days"][0]["activities"][0]["end"] = "11:00"
        value.update(candidates=copy.deepcopy(self.initial["candidates"]),itinerary=latest,
                     unmet=[unmet("fact_unverified")])
        value["candidates"][0]["draft"] = older
        await self.planner.blocked_plan(value)
        saved = self.store.trip(self.trip_id)["document"]
        self.assertEqual(saved["itinerary"],latest)
        self.assertEqual(saved["candidates"][0]["draft"],latest)

    async def test_ordinary_route_ambiguity_is_skipped_by_system_with_visible_draft(self):
        self.five_day_fixture()
        value,_,_ = self.fixture.context()
        value.update(selected_route=copy.deepcopy(self.initial["candidates"][0]),
                     candidates=copy.deepcopy(self.initial["candidates"]),itinerary=copy.deepcopy(self.initial["itinerary"]),
                     resource_retry_done=True,mode="new",weather={})
        value["stops"]["a"].update(map_place=None,map_candidates=[
            {"id":"east","name":"公园东区","location":"120,30"},
            {"id":"west","name":"公园西区","location":"121,30"}])
        protected = authority.user_state(value)
        self.planner.locate_stops = AsyncMock(side_effect=lambda state,stops:copy.deepcopy(stops))
        self.planner.weather_context = AsyncMock(return_value={})
        self.planner.resolve_hubs = AsyncMock(return_value=None)
        self.planner.refresh_day_routes = AsyncMock()
        self.planner.query_ticket_prices = AsyncMock()
        self.planner.daily_order = AsyncMock(side_effect=lambda state,day,ids,*args:
            SimpleNamespace(order=OrderChoice(stop_ids=ids,explanation="沿用地点",short_day_reason="攻略池有限"),unmet=[]))
        async def schedule(state,route,order,*args):
            old = copy.deepcopy(next(day for day in self.initial["itinerary"]["days"] if day["date"] == route["date"]))
            old["activities"] = [item for item in old["activities"] if item["id"] in order.stop_ids]
            return old
        self.planner.schedule_activity_day = AsyncMock(side_effect=schedule)
        result = await Planner.activities(self.planner,value)
        self.assertEqual(len(result["itinerary"]["days"]),5)
        self.assertNotIn("a",[item["id"] for day in result["itinerary"]["days"] for item in day["activities"]])
        self.assertEqual(result.get("unmet",[]),[])
        self.assertTrue(result["system_assumptions"])
        self.assertEqual(result["system_assumptions"][0]["current_action"]["type"],"skip")
        self.assertEqual(authority.user_state({**value,**result}),protected)
