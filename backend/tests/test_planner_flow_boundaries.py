"""Synthetic flow regressions for the user to run; no external services."""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from pydantic import ValidationError

from app import ablation
from app.adapters import ToolError
from app.agents.guide import GuideAgent
from app.models import Candidate, GuideBrief, GuideGroup, OrderChoice, Requirements, RouteTurn
from app.planner import Planner


REQ = Requirements(origin="广州",cities=["广州","深圳"],start_date="2026-11-10",end_date="2026-11-11",
                   people=1,rooms=1,budget=5000).model_dump(mode="json")


def question(text="是否保留这个可选活动？", day="2026-11-10"):
    return {"id":"choice","title":"取舍","question":text,"options":["保留","删除"],"recommendation":"保留",
            "why_needed":"需要用户明确选择","affected_dates":[day]}


class DiscoveryQueryScope(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.planner = Planner.__new__(Planner)
        self.planner.emit = Mock()
        self.planner.selected_sources = Mock(return_value=[{"id":"guide","title":"合成攻略",
            "extraction":{"title":"合成","stops":[],"cautions":["仍需核实开放"]}}])
        self.planner.locate_stops = AsyncMock(side_effect=lambda state,stops:stops)
        self.planner.weather_context = AsyncMock(return_value={})
        self.stops = {f"stop:{index}":{"id":f"stop:{index}","city":"广州" if index < 40 else "深圳",
            "name":f"地点{index}","source_id":"guide","source_title":"合成攻略","category":"sight",
            "evidence":{"kind":"text","quote":f"地点{index}"}} for index in range(80)}
        self.state = {"trip_id":"trip","requirements":copy.deepcopy(REQ),"selected_source_ids":["guide"],
            "stops":copy.deepcopy(self.stops),"locks":[],"warnings":[],
            "stop_constraints":{"required":["stop:79"],"excluded":["stop:25"]}}
        self.proposal = Candidate(title="合成两天",reason="按当前攻略和城市顺序",days=[
            {"date":"2026-11-10","city":"广州","stop_ids":["stop:0"]},
            {"date":"2026-11-11","city":"深圳","stop_ids":["stop:40","stop:79"]}])
        self.planner.route_agent = SimpleNamespace(plan=AsyncMock(return_value=RouteTurn(status="ready",candidate=self.proposal)))

    async def test_first_pass_locates_only_anchors_and_required_places_without_shrinking_the_pool(self):
        original = copy.deepcopy(self.state)
        with ablation.use("no_clustering"):
            result = await self.planner.discover(self.state)
        selected = self.planner.locate_stops.await_args.args[1]
        self.assertLessEqual(len(selected),25)
        self.assertIn("stop:79",selected)
        self.assertNotIn("stop:25",selected)
        self.assertEqual({stop["city"] for stop in selected.values()},{"广州","深圳"})
        available = self.planner.route_agent.plan.await_args.args[2]["available_stops"]
        self.assertEqual(len(available),79)
        self.assertNotIn("stop:25",available)
        self.assertEqual(result["candidates"][0]["days"][1]["stop_ids"],["stop:40","stop:79"])
        self.assertEqual(self.state,original)

    async def test_unique_names_in_candidate_days_are_normalized_without_another_model_call(self):
        self.proposal.days[0].stop_ids = ["地点0"]
        self.proposal.days[1].stop_ids = ["地点40","地点79"]
        result = await self.planner.discover(self.state)
        self.planner.route_agent.plan.assert_awaited_once()
        self.assertEqual(result["candidates"][0]["days"][0]["stop_ids"],["stop:0"])

    async def test_all_excluded_places_return_to_source_selection_without_model_or_map_calls(self):
        self.state["stop_constraints"] = {"required":[],"excluded":list(self.stops)}
        result = await self.planner.discover(self.state)
        self.assertEqual(result["candidates"],[])
        self.assertTrue(result["warnings"])
        self.planner.locate_stops.assert_not_awaited()
        self.planner.weather_context.assert_not_awaited()
        self.planner.route_agent.plan.assert_not_awaited()


class GuideQuestionRecovery(unittest.IsolatedAsyncioTestCase):



    def test_guide_group_unique_names_normalize_but_ambiguous_names_remain_rejected(self):
        state = {"requirements":REQ,"stops":{"a":{"id":"a","city":"广州","name":"合成公园"}}}
        groups = [GuideGroup(city="广州",stop_ids=["合成公园"])]
        self.assertEqual(GuideAgent.normalize_groups(state,groups)[0].stop_ids,["a"])
        state["stops"]["b"] = {"id":"b","city":"广州","name":"合成公园"}
        with self.assertRaises(ToolError):
            GuideAgent.normalize_groups(state,groups)


class FactGapsAreNotUserChoices(unittest.TestCase):
    def test_unknown_opening_is_kept_as_a_fact_gap_without_an_extra_question(self):
        brief = GuideBrief(status="ready",summary="尚未确定是否开放，需后续核实")
        turn = RouteTurn(status="ready",order=OrderChoice(stop_ids=[],explanation="尚未确定是否开放，保留缺口"))
        self.assertEqual(brief.unmet,[])
        self.assertEqual(turn.unmet,[])

    def test_unresolved_user_choice_is_structured_without_a_question(self):
        from app.models import UnmetItem
        problem = UnmetItem(kind="capacity",refs=[{"type":"requirement","id":"constraints"}])
        brief = GuideBrief(status="ready",summary="有待解决的条件",unmet=[problem])
        self.assertEqual(brief.unmet,[problem])
        self.assertNotIn("question",GuideBrief.model_json_schema()["properties"])
