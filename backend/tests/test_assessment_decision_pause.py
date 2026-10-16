"""Synthetic assessment pause/resume cases for the user to run; no live calls."""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app.models import DecisionResolution, Requirements
from app.planner import Planner


FIRST, SECOND = "2026-11-10", "2026-11-11"


def activity(identity):
    return {"id":identity,"name":identity,"kind":"visit","start":"09:00","end":"11:00",
            "source_id":"guide","evidence":{"quote":identity},"move_before":{}}


def itinerary():
    # The deterministic check has not run yet, so this draft intentionally has no budget/issues.
    return {"days":[{"date":FIRST,"city":"杭州","activities":[activity("guide:a")],
                     "gaps":[],"available_start":540,"tail_deadline":1200},
                    {"date":SECOND,"city":"杭州","activities":[activity("guide:c")],
                     "gaps":[],"available_start":540,"tail_deadline":1200}],
            "transport":[],"hotels":[]}


class AssessmentDecisionPause(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.calls = []
        self.planner = Planner.__new__(Planner)
        self.planner.emit = Mock()
        self.planner.audit = AsyncMock()
        self.planner.repair = AsyncMock()
        self.requirements = Requirements(origin="杭州",cities=["杭州"],start_date=FIRST,end_date=SECOND,
            people=1,rooms=1,budget=5000,return_to_origin=False).model_dump(mode="json")
        self.stops = {identity:{"id":identity,"name":identity,"city":"杭州","source_id":"guide",
                              "source_title":"合成攻略","evidence":{"quote":identity}}
                      for identity in ("guide:a","guide:b","guide:c")}
        self.candidate = {"id":"candidate","title":"合成两日","reason":"所选攻略", "gaps":[],
            "days":[{"date":FIRST,"city":"杭州","stop_ids":["guide:a"]},
                    {"date":SECOND,"city":"杭州","stop_ids":["guide:b"]}],
            "review":{"status":"pass","summary":"旧的审查不能沿用"},"draft":{}}
        self.decision = {"id":"timing-choice","title":"第一天节奏","question":"是否减少第一天的可选活动？",
            "options":["减少","保留"],"recommendation":"减少","why_needed":"现有时间无法同时满足",
            "affected_dates":[FIRST]}
        self.state = {"trip_id":"trip","run_id":"run","generation":"generation",
            "mode":"new","requirements":self.requirements,"selected_source_ids":["guide"],
            "stops":self.stops,"locks":[],"stop_constraints":{},"candidates":[self.candidate],
            "old_itinerary":{},"decision":{},"draft_revision":0,"warnings":[]}

        async def transport(state):
            self.calls.append("transport")
            return {"itinerary":copy.deepcopy(state["itinerary"])}

        async def hotels(state):
            self.calls.append("hotels")
            return {"itinerary":copy.deepcopy(state["itinerary"])}

        async def activities(state):
            self.calls.append("activities")
            stops = copy.deepcopy(state["stops"])
            stops["guide:c"]["map_place"] = {"name":"guide:c","location":"120,30"}
            return {"itinerary":itinerary(),"decision":copy.deepcopy(self.decision),"stops":stops,
                    "weather":{"杭州":{"forecasts":{}}},"warnings":["保留合成资料缺口"]}

        async def tickets(state):
            self.calls.append("tickets")
            return {}

        async def check(state):
            self.calls.append("check")
            draft = copy.deepcopy(state["itinerary"])
            draft.update(budget={"known_subtotal":0,"known_plus_estimates":200,"unknown":[]},
                         issues=[],information_unknown=[])
            return {"itinerary":draft}

        async def assessed(state,candidate,draft,decision=None):
            self.calls.append("assess_result")
            candidate = {**copy.deepcopy(candidate),"draft":copy.deepcopy(draft)}
            return {"candidates":[candidate],"previews":{candidate["id"]:{"itinerary":copy.deepcopy(draft)}},
                    "decision":copy.deepcopy(decision or {}),"preview_ready":False}

        self.planner.transport = AsyncMock(side_effect=transport)
        self.planner.hotels = AsyncMock(side_effect=hotels)
        self.planner.activities = AsyncMock(side_effect=activities)
        self.planner.tickets = AsyncMock(side_effect=tickets)
        self.planner.check = AsyncMock(side_effect=check)
        self.planner.assess_result = AsyncMock(side_effect=assessed)

    async def test_pending_daily_choice_pauses_before_ticket_node_check_or_review(self):
        original = copy.deepcopy(self.state)
        result = await self.planner.assess(self.state)
        self.assertEqual(self.calls,["transport","hotels","activities"])
        self.planner.tickets.assert_not_awaited()
        self.planner.check.assert_not_awaited()
        self.planner.assess_result.assert_not_awaited()
        self.planner.audit.assert_not_awaited()
        self.planner.repair.assert_not_awaited()
        self.assertEqual(result["decision"],self.decision)
        self.assertEqual(result["decision_resume"],"assess")
        self.assertEqual(result["assessment_base"],"candidate")
        self.assertEqual(result["assessment_dates"],[FIRST])
        self.assertFalse(result["preview_ready"])
        self.assertFalse(result["selected_route"])
        self.assertIsNone(result["candidates"][0]["review"])
        self.assertTrue(result["candidates"][0]["confirmation_blockers"])
        self.assertEqual(self.state,original)

    async def test_pending_preview_keeps_all_days_and_adopted_stop_ids_for_resume(self):
        result = await self.planner.assess(self.state)
        draft = result["candidates"][0]["draft"]
        self.assertEqual([day["date"] for day in draft["days"]],[FIRST,SECOND])
        self.assertEqual(result["previews"]["candidate"]["itinerary"],draft)
        self.assertEqual(result["candidates"][0]["days"][1]["stop_ids"],["guide:c"])
        self.assertEqual(result["stops"]["guide:c"]["map_place"]["location"],"120,30")
        self.assertIn("杭州",result["weather"])
        self.assertIn("保留合成资料缺口",result["warnings"])

    async def test_answered_choice_reuses_preview_and_rebuilds_only_affected_dates(self):
        paused = await self.planner.assess(self.state)
        self.calls.clear()
        self.planner.assess_result.reset_mock()
        before = copy.deepcopy(paused["previews"]["candidate"]["itinerary"])

        async def activities_after_answer(state):
            self.calls.append("activities")
            self.assertEqual(state["mode"],"modify")
            self.assertEqual(state["old_itinerary"],before)
            self.assertEqual(state["change"]["dates"],[FIRST])
            self.assertEqual(state["change"]["scopes"],["activity"])
            draft = copy.deepcopy(state["itinerary"])
            draft["days"][0]["activities"][0]["end"] = "15:30"
            return {"itinerary":draft,"decision":{}}

        self.planner.activities.side_effect = activities_after_answer
        self.planner.models = SimpleNamespace(ask=AsyncMock(return_value=DecisionResolution(
            requirements={"preferences":"第一天少安排一点"},explanation="已记录第一天减少活动的明确要求")))
        pending_state = {**self.state,**paused}
        def answer(prompt):
            return {"round_id":prompt["round_id"],"draft_revision":prompt["draft_revision"],
                    "answers":{self.decision["id"]:"第一天少安排一点"}}
        with patch("app.planner.interrupt",side_effect=answer):
            resolution = await self.planner.decision_gate(pending_state)
        self.assertEqual(resolution["assessment_dates"],[FIRST])
        self.assertEqual(resolution["assessment_base"],"candidate")
        self.assertFalse(resolution["decision"])
        resumed = {**pending_state,**resolution}
        result = await self.planner.assess(resumed)
        self.assertEqual(self.calls,["transport","hotels","activities","tickets","check","assess_result"])
        self.planner.assess_result.assert_awaited_once()
        draft = result["candidates"][0]["draft"]
        self.assertEqual(draft["days"][0]["activities"][0]["end"],"15:30")
        self.assertEqual(draft["days"][1],before["days"][1])
        self.assertEqual(result["candidates"][0]["days"][1]["stop_ids"],["guide:c"])
        self.assertFalse(result["decision"])
        self.assertEqual(result["assessment_dates"],[])
        self.assertEqual(result["assessment_base"],"")
