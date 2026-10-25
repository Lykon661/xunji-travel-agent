import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from langgraph.types import Command

from app import actions
from app.models import CandidateRevision, ChangeRequest, ReviewReport
from app.planner import Planner
from app.planning_repairs import change_dependencies
from app.reviewing import incremental_review_payload, merge_incremental_review, review_context, review_basis, bind_report
from tests import test_coordinator_graph as graph_cases
from tests.test_actions import DAY, OTHER, state
from tests.test_coordinator import agent, question, unmet
from tests.test_incremental_review import report, finding


class ChangeDependencies(unittest.TestCase):
    def apply(self,proposed):
        before = state()
        updates,_,_ = actions.execute(before,proposed,"user")
        return change_dependencies(before,{**before,**updates},proposed)

    def test_budget_and_scoped_stop_actions_have_distinct_schedule_dependencies(self):
        result = self.apply([{"type":"set_budget","amount":4000}])
        self.assertEqual(result["replan_dates"],[])
        self.assertEqual(result["review_dates"],[])
        self.assertEqual(result["review_dimensions"],["budget"])
        result = self.apply([{"type":"shorten","stop_id":"a","date":DAY,"minutes":60}])
        self.assertEqual(result["replan_dates"],[DAY])
        self.assertEqual(result["review_dates"],[DAY])
        result = self.apply([{"type":"move","stop_id":"a","date":DAY,"to":OTHER}])
        self.assertEqual(result["replan_dates"],[DAY,OTHER])

    def test_global_walk_limit_only_rebuilds_days_exceeding_the_new_limit(self):
        before = state()
        for day,meters in zip(before["itinerary"]["days"],(900,4000)):
            day["walking_meters"] = meters
        after = copy.deepcopy(before)
        after["requirements"]["max_walk_per_day"] = 2000
        dependency = change_dependencies(before,after)
        self.assertEqual(dependency["replan_dates"],[OTHER])
        self.assertEqual(dependency["review_dates"],[DAY,OTHER])

    def test_time_window_only_rebuilds_violating_days_and_no_hotel_return_is_ignored(self):
        before = state()
        before["itinerary"]["days"][1]["activities"][0]["start"] = "11:00"
        after = copy.deepcopy(before)
        after["requirements"]["earliest_departure"] = "10:00"
        self.assertEqual(change_dependencies(before,after)["replan_dates"],[DAY])
        after = copy.deepcopy(before)
        after["requirements"]["latest_return"] = "10:00"
        self.assertEqual(change_dependencies(before,after)["replan_dates"],[])

    def test_hotel_area_is_scoped_to_its_city(self):
        before = state()
        before["itinerary"]["days"][1]["city"] = "苏州"
        after = copy.deepcopy(before)
        after["requirements"]["hotel_areas"] = {"杭州":"西湖"}
        dependency = change_dependencies(before,after)
        self.assertEqual(dependency["replan_dates"],[DAY])
        self.assertEqual(dependency["resource_scopes"],["hotel"])

    def test_latest_arrival_uses_transport_arrival_instead_of_activity_end(self):
        before = state()
        before["requirements"]["origin"] = "北京"
        before["itinerary"]["transport"] = [{"id":"train","context":{"date":DAY,"origin":"北京","destination":"杭州"},
            "selected":{"arrival":DAY+" 09:00"}}]
        after = copy.deepcopy(before)
        after["requirements"]["latest_arrival"] = "10:00"
        self.assertEqual(change_dependencies(before,after)["replan_dates"],[])
        after["requirements"]["latest_arrival"] = "08:00"
        self.assertEqual(change_dependencies(before,after)["replan_dates"],[DAY])

    def test_latest_return_includes_the_selected_hotel_transfer(self):
        before = state()
        before["itinerary"]["hotels"] = [{"id":"hotel","city":"杭州","selected_by_user":True,"selected":{"name":"已选酒店"}}]
        before["itinerary"]["days"][0].update(return_end="13:20",return_move={"minutes":50})
        before["itinerary"]["days"][1].update(return_end="11:00",return_move={"minutes":30})
        after = copy.deepcopy(before)
        after["requirements"]["latest_return"] = "13:00"
        self.assertEqual(change_dependencies(before,after)["replan_dates"],[DAY])


class IncrementalChanges(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = graph_cases.CoordinatorGraph()
        self.fixture.addCleanup = self.addCleanup
        self.fixture.setUp()
        self.fixture.initial["candidates"][0]["title"] = "完整候选"
        self.fixture.initial["candidates"][0]["reason"] = "合成攻略安排"
        self.planner = self.fixture.planner

    def real_reviewer(self):
        self.planner.audit = Planner.audit.__get__(self.planner)
        self.planner.reviewer_agent = SimpleNamespace(review=AsyncMock(return_value=report()))


    async def test_candidate_revision_rebuilds_and_reviews_only_one_day(self):
        original,snapshot,config,_ = await self.fixture.invoke()
        candidate = original["candidates"][0]
        later = copy.deepcopy(original["itinerary"]["days"][1])
        self.planner.route_agent = SimpleNamespace(ask=AsyncMock(return_value=CandidateRevision(
            days=[{"date":DAY,"city":"杭州","stop_ids":["b","a"],"adjustment":"顺序调整"}],explanation="调整当天")))
        self.planner.assess = Planner.assess.__get__(self.planner)
        self.planner.transport = AsyncMock(return_value={})
        self.planner.hotels = AsyncMock(return_value={})
        self.real_reviewer()
        del self.planner.graph
        resumed = await self.planner.graph.ainvoke(Command(resume={"route_edit":{"candidate_id":candidate["id"],
            "dates":[DAY],"content":"调整当天顺序","selected_stop_ids":[]}}),config)
        self.assertEqual(self.planner.activities.await_args.args[0]["change"]["dates"],[DAY])
        self.assertEqual(resumed["itinerary"]["days"][1],later)
        payload = self.planner.reviewer_agent.review.await_args.args[1]
        self.assertEqual(payload["review_dates"],[DAY])
        self.assertEqual([day["date"] for day in payload["plan"]["days"]],[DAY])
        self.assertIsNone(resumed.get("review_previous"))

    async def test_requirement_budget_update_reuses_draft_without_daily_scheduling(self):
        initial,_,run_id = self.fixture.context()
        await self.planner.execute(run_id,initial)
        trip = self.fixture.store.trip(self.fixture.trip["id"])
        original = copy.deepcopy(trip["document"]["candidates"][0]["draft"])
        self.planner.coordinate = Planner.coordinate.__get__(self.planner)
        self.planner.after_coordinate = Planner.after_coordinate.__get__(self.planner)
        self.planner.transport = AsyncMock(return_value={})
        self.planner.hotels = AsyncMock(return_value={})
        self.planner.activities = Planner.activities.__get__(self.planner)
        self.planner.tickets = Planner.tickets.__get__(self.planner)
        self.planner.daily_order = AsyncMock(side_effect=AssertionError("Budget changes cannot reschedule days"))
        self.real_reviewer()
        del self.planner.graph
        run_id = await self.planner.submit(trip["id"],trip["version"],requirements={"budget":4000})
        await self.planner.tasks[run_id]
        self.assertEqual(self.fixture.store.run(run_id)["status"],"waiting")
        doc = self.fixture.store.trip(trip["id"])["document"]
        self.assertEqual(doc["requirements"]["budget"],4000)
        self.assertEqual(doc["candidates"][0]["draft"]["days"],original["days"])
        self.planner.daily_order.assert_not_awaited()
        self.planner.models.ask.assert_not_awaited()
        self.assertEqual(self.planner.reviewer_agent.review.await_args.args[1]["review_dimensions"],["budget"])
        snapshot = await self.planner.graph.aget_state({"configurable":{"thread_id":self.fixture.store.run(run_id)["thread_id"]}})
        self.assertIsNone(snapshot.values.get("review_previous"))

    async def test_chat_edit_of_saved_draft_keeps_snapshot_through_review(self):
        initial,_,run_id = self.fixture.context()
        await self.planner.execute(run_id,initial)
        trip = self.fixture.store.trip(self.fixture.trip["id"])
        doc = trip["document"]
        original = copy.deepcopy(doc["candidates"][0]["draft"])
        doc.update(pending=None,itinerary=copy.deepcopy(original),selected_route=copy.deepcopy(doc["candidates"][0]),stage="ready")
        self.fixture.store.update(trip["id"],doc)
        self.planner.coordinate = Planner.coordinate.__get__(self.planner)
        self.planner.after_coordinate = Planner.after_coordinate.__get__(self.planner)
        self.planner.models.ask.return_value = ChangeRequest(scopes=["activity"],dates=[DAY],explanation="调整当天")
        self.planner.models.ask.side_effect = None
        self.planner.transport = AsyncMock(return_value={})
        self.planner.hotels = AsyncMock(return_value={})
        self.real_reviewer()
        del self.planner.graph
        run_id = await self.planner.submit(trip["id"],trip["version"],message="调整第一天的顺序")
        await self.planner.tasks[run_id]
        self.assertEqual(self.fixture.store.run(run_id)["status"],"waiting")
        doc = self.fixture.store.trip(trip["id"])["document"]
        self.assertEqual(self.planner.activities.await_args.args[0]["change"]["dates"],[DAY])
        self.assertEqual(doc["candidates"][0]["draft"]["days"][1],original["days"][1])
        self.assertEqual(self.planner.reviewer_agent.review.await_args.args[1]["review_dates"],[DAY])
