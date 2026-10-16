"""Synthetic user-run regressions; no model, tool or browser requests. Not run by Codex."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from app.adapters import ToolError
from app.agents.guide import GuideAgent
from app.models import GuideBrief, PlanningDecision


class GuideBriefRecovery(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.agent = GuideAgent(SimpleNamespace(),Mock())
        self.agent.ask = AsyncMock()
        self.state = {"trip_id":"trip","run_id":"run","generation":"generation",
            "requirements":{"cities":["广州","佛山"],"excluded_stop_ids":[]},
            "stops":{"source:a":{"id":"source:a","name":"公园甲","city":"广州"},
                     "source:b":{"id":"source:b","name":"公园乙","city":"佛山"},
                     "source:food":{"id":"source:food","name":"餐厅","city":"广州","category":"dining"}},
            "stop_constraints":{}}

    def brief(self, groups, **fields):
        return GuideBrief(status="ready",summary="合成区域建议",groups=groups,**fields)

    async def test_city_label_and_mixed_city_group_are_corrected_from_existing_sources(self):
        self.agent.ask.return_value = self.brief([{"city":"广州市中心","stop_ids":["source:a","source:b"],"reason":"区域建议"}])
        result = await self.agent.brief(self.state)
        self.assertEqual([(group.city,group.stop_ids) for group in result.groups],[("广州",["source:a"]),("佛山",["source:b"])])
        self.assertEqual(self.agent.ask.await_count,1)
        self.assertFalse(self.agent.ask.call_args.kwargs["cache"])

    async def test_empty_dining_group_does_not_trigger_a_city_error(self):
        self.agent.ask.return_value = self.brief([{"city":"美食区","stop_ids":["source:food"]}])
        result = await self.agent.brief(self.state)
        self.assertEqual(result.groups,[])
        self.assertEqual(self.agent.ask.await_count,1)

    async def test_unknown_ids_are_corrected_once_with_the_actual_catalog(self):
        bad = self.brief([{"city":"广州","stop_ids":["poi-id"]}])
        good = self.brief([{"city":"广州","stop_ids":["source:a"]}])
        self.agent.ask.side_effect = [bad,good]
        result = await self.agent.brief(self.state)
        self.assertEqual(result.groups[0].stop_ids,["source:a"])
        payload = self.agent.ask.call_args.args[3]
        self.assertIn("groups.0.stop_ids",payload["validation_error"])
        self.assertIn("source:a",payload["available_stops"])
        self.assertEqual(payload["allowed_cities"],["广州","佛山"])

    async def test_repeated_bad_suggestions_fall_back_without_inventing_sources_or_losing_constraints(self):
        self.state["stop_constraints"] = {"excluded":["source:b"]}
        self.agent.ask.side_effect = [self.brief([{"city":"未知城市","stop_ids":["invented"]}],required_ids=["source:a"]),
            self.brief([{"city":"广州","stop_ids":["another-invented"]}],required_ids=["source:a"])]
        result = await self.agent.brief(self.state)
        self.assertEqual([(group.city,group.stop_ids) for group in result.groups],[("广州",["source:a"])])
        self.assertEqual(result.required_ids,["source:a"])
        self.assertIn("基础分组",result.summary)
        self.assertEqual(self.agent.ask.await_count,2)

    async def test_invalid_required_constraint_is_not_relaxed_into_a_fallback(self):
        self.agent.ask.side_effect = [self.brief([],required_ids=["invented"]),self.brief([],required_ids=["invented"])]
        with self.assertRaises(ToolError) as caught:
            await self.agent.brief(self.state)
        self.assertEqual(caught.exception.code,"guide_constraints")
        self.assertEqual(self.agent.ask.await_count,2)

    async def test_valid_groups_respect_explicit_exclusions(self):
        self.agent.ask.return_value = self.brief([{"city":"广州","stop_ids":["source:a","source:b"]}],excluded_ids=["source:b"])
        result = await self.agent.brief(self.state)
        self.assertEqual([identity for group in result.groups for identity in group.stop_ids],["source:a"])
        self.assertEqual(result.excluded_ids,["source:b"])

    async def test_pending_user_decision_survives_group_fallback(self):
        question = PlanningDecision(id="tradeoff",title="活动取舍",question="你更希望哪种安排？",options=["公园为主","减少游览"],
            recommendation="公园为主",why_needed="需要用户取舍")
        bad = GuideBrief(status="needs_decision",summary="合成取舍",question=question,
            groups=[{"city":"广州","stop_ids":["invented"]}])
        self.agent.ask.side_effect = [bad.model_copy(deep=True),bad.model_copy(deep=True)]
        result = await self.agent.brief(self.state)
        self.assertEqual(result.status,"needs_decision")
        self.assertEqual(result.question,question)

    async def test_semantically_bad_old_cache_is_not_reused_or_written(self):
        bad = self.brief([{"city":"广州","stop_ids":["invented"]}])
        good = self.brief([{"city":"广州","stop_ids":["source:a"]}])
        models, store = SimpleNamespace(ask=AsyncMock(side_effect=[bad,good])),Mock()
        agent = GuideAgent(models,store)
        agent.assert_current = Mock()
        store.agent_result.return_value = {"result":bad.model_dump(mode="json")}
        result = await agent.brief(self.state)
        self.assertEqual(result.groups[0].stop_ids,["source:a"])
        self.assertEqual(models.ask.await_count,2)
        store.agent_result.assert_not_called()
        store.save_agent_result.assert_not_called()

    def test_duplicate_ids_and_normalized_group_count_stay_bounded(self):
        groups = self.brief([{"city":"广州","stop_ids":["source:a","source:a"]},
                             {"city":"佛山","stop_ids":["source:a","source:b"]}]).groups
        normalized = GuideAgent.normalize_groups(self.state,groups)
        self.assertEqual([identity for group in normalized for identity in group.stop_ids],["source:a","source:b"])
