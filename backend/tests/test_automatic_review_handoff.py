"""Synthetic handoff cases for the user to run; no services or provider calls."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from pydantic import ValidationError

from app.adapters import ToolError
from app.models import PlanningDecision, PlanningReview, ReviewRepairResolution
from app.planner import Planner


class AutomaticReviewHandoff(unittest.IsolatedAsyncioTestCase):
    def state(self,status="revise",blockers=None,decision=None):
        return {"candidates":[{"draft":{"days":[{"date":"2026-11-10","activities":[]}]},
                "review":{"status":status},"confirmation_blockers":blockers or []}],"decision":decision or {}}

    async def test_unresolved_planning_enters_the_interview_instead_of_raising_an_error(self):
        planner = Planner.__new__(Planner)
        planner.emit = Mock()
        planner.route_agent = SimpleNamespace(review=AsyncMock(side_effect=ToolError("model_network","合成问题生成失败")))
        for state in (self.state(),self.state("review_unavailable"),self.state("pass",["关键依据仍无效"])):
            with self.subTest(status=state["candidates"][0]["review"]["status"]):
                self.assertEqual(Planner.after_review_gate(state),"planning_failed")
                result = await planner.planning_failed(state)
                self.assertEqual(Planner.after_review_gate({**state,**result}),"decision_gate")
                question = PlanningDecision(**result["decision"])
                self.assertEqual(question.affected_dates[0].isoformat(),"2026-11-10")
                self.assertNotIn("是否允许",question.question)
                self.assertEqual(result["decision_resume"],"assess")
                self.assertEqual(result["selected_route"],{})

    async def test_clarification_focuses_on_one_blocker_and_retains_the_draft(self):
        planner = Planner.__new__(Planner)
        planner.emit = Mock()
        question = PlanningDecision(id="fixed-time",title="固定活动时刻的取舍",question="这两项活动你优先保留哪一项？",
            options=["保留博物馆","保留夜游"],recommendation="先保留已预约的活动",why_needed="两项固定时刻无法同时满足",
            affected_dates=["2026-11-10","2026-11-11"])
        planner.route_agent = SimpleNamespace(review=AsyncMock(return_value=PlanningReview(question=question)))
        state = self.state()
        first = {"severity":"blocker","message":"两项固定时刻无法衔接","dates":["2026-11-10"],"field_path":"/days/0"}
        second = {**first,"message":"另一天的问题","dates":["2026-11-11"]}
        state["candidates"][0]["review"]["findings"] = [first,second]
        result = await planner.planning_failed(state)
        self.assertEqual(planner.route_agent.review.call_args.args[1],[first])
        self.assertEqual(result["decision"]["affected_dates"],["2026-11-10"])
        self.assertNotIn("candidates",result)
        self.assertTrue(state["candidates"][0]["draft"])

    def test_passed_route_and_real_question_take_distinct_paths(self):
        self.assertEqual(Planner.after_review_gate(self.state("pass")),"selection_gate")
        self.assertEqual(Planner.after_review_gate(self.state("pass_with_warnings")),"selection_gate")
        self.assertEqual(Planner.after_review_gate(self.state(decision={"id":"budget-choice"})),"decision_gate")
        self.assertEqual(Planner.after_review_gate({"candidates":[]}),"selection_gate")

    def test_technical_failure_cannot_be_encoded_as_a_user_question(self):
        question = {"id":"retry","title":"继续查证","question":"是否允许重试？","options":["允许","停止"],
            "recommendation":"允许","why_needed":"接口失败"}
        for status in ("repair","blocked"):
            with self.subTest(status=status),self.assertRaises(ValidationError):
                ReviewRepairResolution(status=status,explanation="技术失败",question=question)
        with self.assertRaises(ValidationError):
            ReviewRepairResolution(status="needs_decision",explanation="缺少具体取舍问题")
