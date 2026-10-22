"""Four-agent review with temporary data and mocked models."""
import copy
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from pydantic import ValidationError

from app.adapters import ToolError
from app.agents.resource import ResourceAgent
from app.agents.reviewer import ReviewerAgent
from app.dining import refresh_meals
from app.models import REVIEW_DIMENSIONS, Requirements, ReviewReport, ResourceChoice
from app.places import canonicalize_stops, duplicate_issues
from app.planner import Planner
from app.reviewing import bind_report, evidence_ids, review_basis, review_payload, require_review
from app.decisions import confirmation_blockers
from app.rules import check_itinerary
from app.store import Store, Conflict


def report_value():
    return {"status":"pass","summary":"合成模型审查结果",
            "checks":[{"dimension":dimension,"result":"ok","note":"已检查"} for dimension in sorted(REVIEW_DIMENSIONS)],
            "findings":[]}


def day(date="2026-11-10"):
    return {"date":date,"city":"杭州","activities":[{"id":"a","name":"合成公园","kind":"visit",
        "start":"11:00","end":"19:00","source_id":"guide","map_place":{"id":"park","name":"合成公园","location":"120.1,30.2"},
        "move_before":{"minutes":0}}],"gaps":[],"return_move":{"minutes":0}}


class GlobalIdentities(unittest.TestCase):
    def test_aliases_share_one_place_but_branches_and_co_located_venues_stay_distinct(self):
        stops = {
            "a":{"id":"a","city":"杭州","name":"公园简称","map_place":{"id":"park","location":"120.1,30.2"}},
            "b":{"id":"b","city":"杭州","name":"公园全称","map_place":{"id":"park","location":"120.1,30.2"}},
            "c":{"id":"c","city":"杭州","name":"餐厅一店","map_place":{"id":"branch-one","name":"品牌一店","address":"街道1号一层","location":"120.1,30.2"}},
            "d":{"id":"d","city":"杭州","name":"餐厅二店","map_place":{"id":"branch-two","name":"品牌二店","address":"街道1号二层","location":"120.1,30.2"}}}
        result = canonicalize_stops(stops)
        self.assertEqual(result["a"]["canonical_place_id"],result["b"]["canonical_place_id"])
        self.assertEqual({ref["stop_id"] for ref in result["a"]["source_references"]},{"a","b"})
        self.assertNotEqual(result["c"]["canonical_place_id"],result["d"]["canonical_place_id"])

    def test_distinct_poi_ids_at_same_name_address_and_coordinates_are_not_merged(self):
        base = {"city":"杭州","name":"品牌餐厅","map_place":{"name":"品牌餐厅","address":"合成商场1号","location":"120.1,30.2"}}
        first = {**base,"id":"a","map_place":{**base["map_place"],"id":"one"}}
        second = {**base,"id":"b","map_place":{**base["map_place"],"id":"two"}}
        result = canonicalize_stops({"a":first,"b":second})
        self.assertNotEqual(result["a"]["canonical_place_id"],result["b"]["canonical_place_id"])
        self.assertIn(result["b"]["canonical_place_id"],result["a"]["possible_duplicate_ids"])

    def test_same_poi_on_different_days_is_blocked_without_an_explicit_exception(self):
        plan = {"days":[day(),day("2026-11-11")]}
        plan["days"][1]["activities"][0]["id"] = "other-source"
        self.assertEqual(duplicate_issues(plan)[0]["code"],"duplicate_place")
        req = {"repeat_exceptions":[{"place_id":"other-source","role":"visit","dates":["2026-11-11"],"authorization":"第二天再去一次"}]}
        self.assertEqual(duplicate_issues(plan,req),[])

    def test_restaurant_is_not_repeated_between_meals_or_days(self):
        plan = {"days":[day(),day("2026-11-11")]}
        food = {"id":"food","name":"餐厅一店","city":"杭州","category":"dining","source_id":"guide",
                "map_place":{"id":"food-poi","name":"餐厅一店","location":"120.101,30.2"}}
        stops = canonicalize_stops({"food":food,"alias":{**food,"id":"alias"}})
        refresh_meals(plan,stops)
        recommendations = [item for current in plan["days"] for item in current["meal_recommendations"]]
        self.assertEqual(len(recommendations),1)
        self.assertEqual(recommendations[0]["meal"],"lunch")

    def test_partial_refresh_reserves_unedited_day_and_replaces_old_recommendations(self):
        plan = {"days":[day(),day("2026-11-11")]}
        food = {"id":"food","name":"推荐餐厅","city":"杭州","category":"dining",
                "map_place":{"id":"food-poi","location":"120.101,30.2"}}
        plan["days"][0]["meal_recommendations"] = [{**food,"meal":"lunch"}]
        plan["days"][1]["meal_recommendations"] = [{**food,"meal":"dinner"}]
        first = copy.deepcopy(plan["days"][0]["meal_recommendations"])
        refresh_meals(plan,{"food":food},dates={"2026-11-11"})
        self.assertEqual(plan["days"][0]["meal_recommendations"],first)
        self.assertEqual(plan["days"][1]["meal_recommendations"],[])


class IndependentReview(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name))
        self.req = Requirements(origin="杭州",cities=["杭州"],start_date="2026-11-10",end_date="2026-11-10",
                                people=1,rooms=1,budget=3000).model_dump(mode="json")
        self.trip = self.store.create_trip("合成审查",self.req)
        run = self.store.start_run(self.trip["id"],0,"generation",{})
        self.state = {"trip_id":self.trip["id"],"run_id":run,"generation":"generation","requirements":self.req,
                      "selected_source_ids":[],"locks":[],"decision_history":[],"stop_constraints":{},"draft_revision":1}
        self.plan = {"days":[day()],"transport":[],"hotels":[],"issues":[],"budget":{"unknown":[]}}

    def bound_report(self, state=None, plan=None, queries=None):
        state, plan, queries = state or self.state, plan or self.plan, queries or []
        return bind_report(report_value(),state,plan,review_basis(state,plan,[],queries),[])

    def test_unselected_hotel_is_not_a_cost_blocker_or_expiring_evidence(self):
        plan = copy.deepcopy(self.plan)
        plan["hotels"] = [{"id":"hotel:杭州","city":"杭州","context":{"check_in":"2026-11-10","check_out":"2026-11-11"},
            "evidence_ids":["query:hotel"],"selected":{"id":"hotel-a","name":"推荐酒店","kind":"hotel","price":99999,
                "currency":"CNY","price_basis":"room_night","context":{"check_in":"2026-11-10","check_out":"2026-11-11"},
                "queried_at":(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()}}]
        result = check_itinerary(plan,self.req)
        self.assertEqual(result["budget"]["known_subtotal"],0)
        self.assertFalse(any(issue["code"] in {"over_budget","hotel_gap","hotel_dates"} for issue in result["issues"]))
        self.assertEqual(confirmation_blockers(plan,requirements=self.req),[])
        self.assertNotIn("query:hotel",evidence_ids(plan))
        report = self.bound_report(plan=plan)
        self.assertGreater(datetime.fromisoformat(report["expires_at"]),datetime.now(timezone.utc))
        plan["hotels"][0]["selected_by_user"] = True
        self.assertIn("query:hotel",evidence_ids(plan))
        self.assertTrue(any(issue["code"] == "over_budget" for issue in check_itinerary(plan,self.req)["issues"]))

    def test_report_is_invalidated_by_lock_requirement_meal_and_query_changes(self):
        report = self.bound_report()
        require_review(report,self.state,self.plan,[],[])
        changed_states = [{**self.state,"locks":["a"]},
                          {**self.state,"requirements":{**self.req,"budget":3500}}]
        for state in changed_states:
            with self.assertRaises(Conflict):
                require_review(report,state,self.plan,[],[])
        changed = copy.deepcopy(self.plan)
        changed["days"][0]["meal_recommendations"] = [{"id":"food","meal":"lunch"}]
        with self.assertRaises(Conflict):
            require_review(report,self.state,changed,[],[])
        with self.assertRaises(Conflict):
            require_review(report,self.state,self.plan,[],[{"evidence_id":"query:9","created_at":"new"}])

    def test_map_export_metadata_does_not_invalidate_review_but_route_changes_do(self):
        report = self.bound_report()
        changed = copy.deepcopy(self.plan)
        changed["personal_map"] = {"links":["https://amap.com/synthetic"],"queried_at":"export-time"}
        require_review(report,self.state,changed,[],[])
        changed["days"][0]["activities"][0]["start"] = "10:15"
        with self.assertRaises(Conflict):
            require_review(report,self.state,changed,[],[])

    def test_old_and_expired_reports_cannot_confirm(self):
        with self.assertRaises(Conflict):
            require_review(None,self.state,self.plan,[],[])
        report = self.bound_report()
        report["expires_at"] = (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
        with self.assertRaises(Conflict):
            require_review(report,self.state,self.plan,[],[])

    async def test_reviewer_cannot_approve_away_deterministic_duplicate(self):
        planner = Planner.__new__(Planner)
        planner.store = self.store
        planner.models = SimpleNamespace()
        planner.emit = Mock()
        planner.reviewer_agent = SimpleNamespace(review=AsyncMock(return_value=ReviewReport(**report_value())))
        plan = copy.deepcopy(self.plan)
        duplicate = copy.deepcopy(plan["days"][0]["activities"][0])
        duplicate["id"] = "b"
        plan["days"][0]["activities"].append(duplicate)
        report = await planner.audit(self.state,plan)
        self.assertEqual(report["status"],"revise")
        with self.assertRaises(Conflict):
            planner.assert_review(self.state,report,plan)

    async def test_reviewer_failure_is_not_a_pass(self):
        planner = Planner.__new__(Planner)
        planner.store = self.store
        planner.models = SimpleNamespace()
        planner.emit = Mock()
        planner.reviewer_agent = SimpleNamespace(review=AsyncMock(side_effect=ToolError("model_network","合成连接错误")))
        report = await planner.audit(self.state,self.plan)
        self.assertEqual(report["status"],"review_unavailable")
        with self.assertRaises(Conflict):
            require_review(report,self.state,self.plan,[],[])

    async def test_unknown_evidence_in_review_is_rejected(self):
        value = report_value()
        value.update(status="revise",findings=[{"code":"claim","severity":"blocker","owner":"route","action":"repair",
            "target":"a","dates":["2026-11-10"],"field_path":"/days/0/activities/0",
            "message":"合成发现","evidence_ids":["query:foreign"],"suggestion":"核对"}])
        agent = ReviewerAgent(SimpleNamespace(ask=AsyncMock(return_value=ReviewReport(**value))),self.store)
        with self.assertRaises(ToolError):
            await agent.review(self.state,review_payload(self.state,self.plan,[],[]))

    def test_incomplete_checklist_is_not_accepted(self):
        value = report_value()
        value["checks"].pop()
        with self.assertRaises(ValidationError):
            ReviewReport(**value)

    def test_new_query_for_same_request_invalidates_the_old_evidence_binding(self):
        first = self.store.query(self.trip["id"],"flyai",{"kind":"hotel","city":"杭州"},{"price":100})
        ids = [f"query:{first}"]
        previous = self.store.review_queries(self.trip["id"],ids)
        self.store.query(self.trip["id"],"flyai",{"kind":"hotel","city":"杭州"},{"price":120})
        self.assertNotEqual(previous,self.store.review_queries(self.trip["id"],ids))

    async def test_resource_agent_only_selects_real_candidates_and_records_its_handoff(self):
        model = SimpleNamespace(ask=AsyncMock(return_value=ResourceChoice(selected_id="hotel-a",reason="符合条件")))
        flyai = SimpleNamespace(search=AsyncMock())
        agent = ResourceAgent(model,self.store,flyai)
        selected,_ = await agent.choose(self.state,[{"id":"hotel-a","name":"合成酒店"}],"选择住宿")
        self.assertEqual(selected["id"],"hotel-a")
        self.assertTrue(any(event.get("agent") == "资源 Agent" for event in self.store.events(self.state["run_id"],0)))
        with self.assertRaises(ToolError):
            await agent.search(self.state,"hotel",{"city":"未知城市"},{})
        flyai.search.assert_not_awaited()
