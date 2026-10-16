"""Latest-user-command regressions for the user to run; all model calls are mocked."""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app.agents.runtime import digest
from app.decisions import apply_walking_decision
from app.models import DecisionResolution, Requirements
from app.planner import Planner
from app.rules import check_itinerary


REQ = Requirements(origin="杭州",cities=["杭州"],start_date="2026-11-10",end_date="2026-11-11",
                   people=1,rooms=1,budget=3000).model_dump(mode="json")
QUESTION = {"id":"walk","title":"步行超上限取舍","question":"当天步行超过8000米上限，要删哪个景点？",
            "why_needed":"步行上限与当前路线冲突","options":["删点","保持规划"],"recommendation":"删点",
            "affected_dates":["2026-11-10"]}


class LatestWalkingCommand(unittest.TestCase):
    def test_budget_and_time_conditions_do_not_restore_the_walking_limit(self):
        for answer in ("保持这个规划，预算不要超过5000元", "允许超出步行上限，但预算不要超过5000元",
                       "保持这个规划，但全程预算上限不变", "保持这个规划，车程不要超过2小时",
                       "预算不要超过5000元且允许超出步行上限"):
            with self.subTest(answer=answer):
                req,keep = apply_walking_decision(REQ,QUESTION,answer,action="relax",keep=True)
                self.assertTrue(keep)
                self.assertEqual(req["walking_limit_exempt_dates"],["2026-11-10"])

    def test_keep_plan_waives_only_dates_in_the_current_conflict(self):
        req,keep = apply_walking_decision(REQ,QUESTION,"保持这个规划就行")
        self.assertTrue(keep)
        self.assertEqual(req["max_walk_per_day"],8000)
        self.assertEqual(req["walking_limit_exempt_dates"],["2026-11-10"])
        days = [{"date":day,"city":"杭州","activities":[],"walking_meters":12000} for day in ("2026-11-10","2026-11-11")]
        result = check_itinerary({"days":days,"hotels":[],"transport":[]},req)
        self.assertEqual([issue["target"] for issue in result["issues"] if issue["code"] == "walking_limit"],["2026-11-11"])
        self.assertTrue(result["constraint_notes"])
        self.assertFalse(any("豁免" in note for note in result["information_unknown"]))

    def test_latest_restore_or_strict_limit_replaces_an_earlier_waiver(self):
        waived,_ = apply_walking_decision(REQ,QUESTION,"保持这个规划就行")
        for answer in ("恢复每日步行上限","保持规划，但不要超过8000米","不接受超限"):
            with self.subTest(answer=answer):
                req,keep = apply_walking_decision(waived,QUESTION,answer)
                self.assertFalse(keep)
                self.assertEqual(req["walking_limit_exempt_dates"],[])

    def test_keep_answer_to_an_unrelated_question_does_not_waive_walking(self):
        req,keep = apply_walking_decision(REQ,{"question":"保持公共交通还是改成自驾？"},"保持规划")
        self.assertTrue(keep)
        self.assertEqual(req["walking_limit_exempt_dates"],[])

    def test_keeping_budget_unchanged_does_not_mean_keeping_a_walking_conflict(self):
        req,keep = apply_walking_decision(REQ,QUESTION,"保持预算不变")
        self.assertFalse(keep)
        self.assertEqual(req["walking_limit_exempt_dates"],[])


class DecisionFlow(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.planner = Planner.__new__(Planner)
        self.planner.emit = Mock()
        self.planner.models = SimpleNamespace(ask=AsyncMock(return_value=DecisionResolution(
            requirements={"max_walk_per_day":20000},explanation="没有明确授权")))
        self.state = {"generation":"generation","draft_revision":0,"requirements":copy.deepcopy(REQ),
                      "decision":copy.deepcopy(QUESTION),"decision_resume":"assess","decision_history":[],"stops":{},
                      "candidates":[{"id":"draft","days":[{"date":"2026-11-10","city":"杭州","stop_ids":[]}]}]}

    async def test_keep_answer_changes_rule_without_inventing_a_numeric_limit(self):
        token = digest({"question":QUESTION,"history":[],"generation":"generation","revision":0})[:40]
        reply = {"round_id":token,"draft_revision":0,"answers":{"walk":"保持这个规划就行"},"note":""}
        with patch("app.planner.interrupt",return_value=reply):
            result = await self.planner.decision_gate(self.state)
        self.assertEqual(result["requirements"]["max_walk_per_day"],8000)
        self.assertEqual(result["requirements"]["walking_limit_exempt_dates"],["2026-11-10"])
        self.assertTrue(result["keep_current_plan"])
        self.assertTrue(result["decision_history"][-1]["keep_current_plan"])

    async def test_saved_keep_answer_resolves_a_reworded_duplicate_without_interrupt(self):
        self.state["decision_history"] = [{"questions":[QUESTION],"answers":{"walk":"保持这个规划就行"},"note":""}]
        self.state["decision"]["id"] = "walk_again"
        with patch("app.planner.interrupt",side_effect=AssertionError("不应再次追问")):
            result = await self.planner.decision_gate(self.state)
        self.assertEqual(result["decision"],{})
        self.assertTrue(result["keep_current_plan"])
        self.planner.models.ask.assert_not_awaited()

    async def test_explicit_reopen_does_not_reuse_the_previous_keep_answer(self):
        self.state["decision_history"] = [{"questions":[QUESTION],"answers":{"walk":"保持这个规划就行"},"note":""}]
        self.state["requirements"],_ = apply_walking_decision(REQ,QUESTION,"保持这个规划就行")
        self.state["decision_reopened"] = True
        self.planner.models.ask.return_value = DecisionResolution(walking_limit_action="restore",explanation="恢复步行上限")
        token = digest({"question":QUESTION,"history":self.state["decision_history"],"generation":"generation","revision":0})[:40]
        reply = {"round_id":token,"draft_revision":0,"answers":{"walk":"恢复每日步行上限"},"note":""}
        with patch("app.planner.interrupt",return_value=reply) as interrupted:
            result = await self.planner.decision_gate(self.state)
        interrupted.assert_called_once()
        self.planner.models.ask.assert_awaited_once()
        self.assertEqual(result["requirements"]["walking_limit_exempt_dates"],[])
        self.assertFalse(result["keep_current_plan"])
        self.assertFalse(result["decision_reopened"])
        self.assertEqual(result["decision_history"][-1]["answers"],reply["answers"])

    async def test_keep_plan_assessment_preserves_stops_times_and_skips_replanning(self):
        req,_ = apply_walking_decision(REQ,QUESTION,"保持这个规划就行")
        req["end_date"] = "2026-11-10"
        itinerary = {"hotels":[],"transport":[],"days":[{"date":"2026-11-10","city":"杭州","available_start":540,
            "tail_deadline":1200,"gaps":[],"activities":[
                {"id":"a","name":"景点A","kind":"visit","start":"09:00","end":"10:00","evidence":{"quote":"景点A"},"move_before":{}},
                {"id":"b","name":"景点B","kind":"visit","start":"11:00","end":"12:00","evidence":{"quote":"景点B"},
                 "move_before":{"minutes":40,"mode":"walking","walking_meters":12000}}],"return_move":{}}]}
        candidate = {"id":"draft","days":[{"date":"2026-11-10","city":"杭州","stop_ids":["a","b"]}],"gaps":[],"draft":itinerary}
        state = {"requirements":req,"candidates":[candidate],"previews":{"draft":{"itinerary":itinerary}},
                 "assessment_base":"draft","assessment_dates":["2026-11-10"],"keep_current_plan":True,"stops":{},"locks":[]}
        for name in ("transport","hotels","activities","tickets"):
            setattr(self.planner,name,AsyncMock(side_effect=AssertionError("不应重新规划")))
        async def checked(probe):
            plan = probe["itinerary"]
            plan.update(check_itinerary(plan,probe["requirements"]))
            return {"itinerary":plan}
        self.planner.check = AsyncMock(side_effect=checked)
        self.planner.assess_result = AsyncMock(return_value={})
        await self.planner.assess(state)
        preserved = self.planner.assess_result.call_args.args[2]
        self.assertEqual([(activity["id"],activity["start"],activity["end"]) for activity in preserved["days"][0]["activities"]],
                         [("a","09:00","10:00"),("b","11:00","12:00")])
        self.assertFalse(any(issue["code"] == "walking_limit" for issue in preserved["issues"]))
        self.planner.activities.assert_not_awaited()
