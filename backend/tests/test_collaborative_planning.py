"""Synthetic planning regressions; no external tools, model, browser or build. Not run by Codex."""
import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from pydantic import ValidationError

from app.agents.route import RouteAgent
from app.agents.runtime import digest
from app.decisions import confirmation_blockers, input_hash, route_structure
from app.models import Candidate, GuideBrief, OrderChoice, Requirements, RouteToolRequest, RouteTurn
from app.planner import Planner
from app.store import Conflict, Store
from app.models import REVIEW_DIMENSIONS
from app.reviewing import bind_report, review_basis


REQ = Requirements(origin="广州",cities=["广州"],start_date="2026-11-10",end_date="2026-11-10",
                   people=1,rooms=1,budget=3000).model_dump(mode="json")


def complete_draft():
    return {"days":[{"date":"2026-11-10","city":"广州","activities":[
        {"id":"source:0","kind":"visit","name":"合成景点","start":"09:00","end":"10:30",
         "map_place":{"id":"poi-a","location":"113.2,23.1"},"move_before":{"minutes":0,"mode":"walking"},
         "ticket_quote":{"price":None,"status":"unknown"}}],"gaps":[],"return_move":{"minutes":0},
         "weather":{"available":False,"note":"未覆盖此日期"}}],"transport":[],"hotels":[],"issues":[],
         "information_unknown":["天气未覆盖"],"budget":{"unknown":["飞猪门票缺价"],"known_subtotal":0}}


class PlanningInvariants(unittest.TestCase):
    def test_price_refresh_is_not_a_route_change_but_transport_change_is(self):
        original = complete_draft()
        refreshed = copy.deepcopy(original)
        refreshed["days"][0]["activities"][0]["ticket_quote"]["price"] = 80
        self.assertEqual(route_structure(original),route_structure(refreshed))
        refreshed["days"][0]["activities"][0]["move_before"]["mode"] = "transit"
        self.assertNotEqual(route_structure(original),route_structure(refreshed))

    def test_missing_price_and_future_weather_do_not_block_confirmed_structure(self):
        self.assertEqual(confirmation_blockers(complete_draft()),[])

    def test_unknown_actual_route_blocks_confirmation_even_with_an_estimate(self):
        plan = complete_draft()
        plan["days"][0]["activities"][0]["move_before"] = {"minutes":None,"estimate_minutes":45}
        self.assertTrue(confirmation_blockers(plan))

    def test_missing_required_stop_blocks_confirmation(self):
        self.assertTrue(confirmation_blockers(complete_draft(),{"required":["source:missing"]}))

    def test_unresolved_summary_cannot_silently_finish(self):
        with self.assertRaises(ValidationError):
            GuideBrief(status="ready",summary="当前尚未明确的是是否安排长隆",question=None)
        with self.assertRaises(ValidationError):
            GuideBrief(status="needs_decision",summary="需要选择",question=None)

    def test_same_poi_from_two_guides_is_not_two_visits(self):
        plan = complete_draft()
        second = copy.deepcopy(plan["days"][0]["activities"][0])
        second["id"] = "other-source:0"
        plan["days"][0]["activities"].append(second)
        self.assertTrue(confirmation_blockers(plan))

    def test_map_refresh_does_not_change_source_fingerprint_but_source_edit_does(self):
        stops = {"a":{"name":"景点","city":"广州","evidence":{"quote":"景点"}}}
        fingerprint = input_hash(REQ,["source"],stops,[{"id":"source","text":"原文"}])
        enriched = {"a":{**stops["a"],"map_place":{"location":"113,23"}}}
        self.assertEqual(fingerprint,input_hash(REQ,["source"],enriched,[{"id":"source","text":"原文"}]))
        self.assertNotEqual(fingerprint,input_hash(REQ,["source"],stops,[{"id":"source","text":"改后的原文"}]))


class CollaborativeFlow(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name))
        self.trip = self.store.create_trip("合成旅行",REQ)
        self.run_id = self.store.start_run(self.trip["id"],0,"generation",{})
        self.state = {"trip_id":self.trip["id"],"run_id":self.run_id,"generation":"generation","base_version":0,
                      "requirements":REQ,"selected_source_ids":[],"locks":[],"stop_constraints":{},"warnings":[],"decision_history":[]}

    async def test_tool_loop_records_observation_and_reuses_finished_result(self):
        model = SimpleNamespace(ask=AsyncMock(side_effect=[
            RouteTurn(status="tools",tools=[RouteToolRequest(tool="route",city="广州",origin_id="a",destination_id="b")]),
            RouteTurn(status="ready",order=OrderChoice(stop_ids=["a","b"],explanation="按实际交通排序"))]))
        tool = AsyncMock(return_value={"minutes":12,"provider":"amap"})
        agent = RouteAgent(model,self.store)
        first = await agent.plan(self.state,"安排当天",{"stops":["a","b"]},tool,daily=True)
        second = await agent.plan(self.state,"安排当天",{"stops":["a","b"]},tool,daily=True)
        self.assertEqual(first,second)
        self.assertEqual(tool.await_count,1)
        self.assertEqual(model.ask.await_count,2)
        self.assertTrue(model.ask.call_args.args[2]["tool_observations"][0]["evidence_id"].startswith("query:"))

    async def test_late_agent_result_cannot_write_after_new_run_started(self):
        async def delayed(*args,**kwargs):
            self.store.finish_run(self.run_id,"waiting",{})
            self.store.start_run(self.trip["id"],0,"new-generation",{})
            return RouteTurn(status="ready",order=OrderChoice(stop_ids=["a"]))
        agent = RouteAgent(SimpleNamespace(ask=AsyncMock(side_effect=delayed)),self.store)
        with self.assertRaises(Conflict):
            await agent.plan(self.state,"安排当天",{},AsyncMock(),daily=True)

    async def test_confirm_and_save_preserves_exact_preview_without_model_or_tools(self):
        planner = Planner.__new__(Planner)
        planner.store = self.store
        planner.emit = Mock()
        planner.models = SimpleNamespace(ask=AsyncMock())
        planner.amap = SimpleNamespace(route=AsyncMock())
        draft = complete_draft()
        candidate = Candidate(id="draft",title="一天",reason="合成路线",days=[{"date":"2026-11-10","city":"广州","stop_ids":["source:0"]}],
                              draft=draft,draft_revision=1,plan_hash=digest(draft)).model_dump(mode="json")
        state = {**self.state,"selected_route":candidate,"itinerary":{},"candidates":[candidate],"old_itinerary":{},"draft_revision":1}
        candidate["review"] = bind_report({"status":"pass_with_warnings","summary":"合成审查，保留缺价提示",
            "checks":[{"dimension":key,"result":"ok","note":"合成"} for key in sorted(REVIEW_DIMENSIONS)],"findings":[]},
            state,draft,review_basis(state,draft,[],[]),[])
        state.update(await planner.confirm_route(state))
        await planner.save(state)
        saved = self.store.trip(self.trip["id"])
        self.assertEqual(saved["document"]["itinerary"],draft)
        self.assertEqual(saved["version"],1)
        self.assertEqual(saved["document"]["stage"],"completed")
        await planner.save(state)
        self.assertEqual(self.store.trip(self.trip["id"])["version"],1)
        planner.models.ask.assert_not_awaited()
        planner.amap.route.assert_not_awaited()

    async def test_agent_cannot_request_route_outside_selected_sources(self):
        planner = Planner.__new__(Planner)
        planner.amap = SimpleNamespace(route=AsyncMock())
        from app.adapters import ToolError
        with self.assertRaises(ToolError):
            await planner.agent_tool({**self.state,"stops":{}},RouteToolRequest(tool="route",city="广州",origin_id="foreign",destination_id="foreign"))
        planner.amap.route.assert_not_awaited()
