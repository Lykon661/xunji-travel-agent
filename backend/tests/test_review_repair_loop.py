"""Review and repair loops with synthetic reports, queries and repairs."""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from app.models import PlanningDecision, PlanningReview, Requirements, REVIEW_DIMENSIONS, ReviewRepairResolution
from app.planner import Planner
from app.planning_recovery import previous_round
from app.reviewing import bind_report, review_basis


REQ = Requirements(origin="杭州",cities=["杭州"],start_date="2026-11-10",end_date="2026-11-10",
                   people=1,rooms=1,budget=5000,return_to_origin=False).model_dump(mode="json")


def plan():
    return {"days":[{"city":"杭州","date":"2026-11-10","activities":[
        {"id":"park","kind":"visit","name":"合成园区","start":"09:00","end":"17:00",
         "evidence":{"quote":"攻略中的园区"},"map_place":{"location":"120,30"},
         "meal_break":{"start":"12:00","end":"13:00"},"move_before":{}}],
        "gaps":[],"meal_estimate":100,"meal_recommendations":[],"return_move":{},"transfers":[]}],
        "issues":[],"budget":{"unknown":[]},"transport":[],"hotels":[]}


def candidate(itinerary):
    return {"id":"candidate","title":"候选","reason":"合成攻略","gaps":[],"draft":itinerary,
            "days":[{"city":"杭州","date":"2026-11-10","stop_ids":["park"]}]}


def finding(index=0, action="repair"):
    return {"code":"conflict_"+str(index),"target":"park","severity":"blocker","owner":"route","action":action,
            "field_path":"/days/0/activities/0","message":"需要修复的合成问题","dates":["2026-11-10"],
            "evidence_ids":["requirements"]}



IDENTITY_FIELDS = {"alias_names","canonical_place_id","possible_duplicate_ids","source_references"}


def without_identity(day):
    # Whole-trip duplicate scanning annotates place identity; the protected schedule itself must not change.
    return {**day,"activities":[{key:value for key,value in activity.items() if key not in IDENTITY_FIELDS}
                                for activity in day["activities"]]}

class ReviewRepairLoop(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.planner = Planner.__new__(Planner)
        self.planner.emit = Mock()
        self.planner.store = Mock()
        self.planner.store.review_queries.return_value = []
        self.planner.selected_sources = Mock(return_value=[])
        self.planner.audit_state = lambda state:state
        self.planner.check = AsyncMock(side_effect=lambda state:{"itinerary":copy.deepcopy(state["itinerary"])})
        self.planner.tickets = AsyncMock(return_value={})
        self.planner.amap = SimpleNamespace(batch_state=SimpleNamespace(get=lambda:None))
        self.planner.repair = AsyncMock()
        self.planner.refresh_expired_resources = AsyncMock(return_value={})
        self.planner.route_agent = SimpleNamespace(
            resolve_review=AsyncMock(return_value=ReviewRepairResolution(status="blocked",explanation="合成技术失败，不能让用户代查或批准修复")),
            review=AsyncMock())
        self.original = plan()
        self.state = {"trip_id":"trip","run_id":"run","generation":"gen","requirements":REQ,"stops":{},"locks":[],
            "mode":"modify","selected_source_ids":[],"stop_constraints":{},"draft_revision":1,
            "round_backup":previous_round({"requirements":REQ,"candidates":[candidate(self.original)]})}

    def reports(self, values):
        pending = iter(values)
        async def audit(state,itinerary):
            status,findings,question = next(pending)
            queries = self.planner.store.review_queries.return_value
            return bind_report({"status":status,"summary":"合成审查","findings":findings,"question":question,
                "checks":[{"dimension":dimension,"result":"ok","note":"合成"} for dimension in sorted(REVIEW_DIMENSIONS)]},
                state,itinerary,review_basis(state,itinerary,[],queries),[])
        self.planner.audit = AsyncMock(side_effect=audit)

    def changing_repair(self):
        async def repair(state):
            itinerary = copy.deepcopy(state["itinerary"])
            count = state.get("repair_round",0)+1
            itinerary["days"][0]["activities"][0]["end"] = "16:30" if count == 1 else "16:00"
            itinerary["issues"] = []
            return {"itinerary":itinerary,"repair_round":count}
        self.planner.repair = AsyncMock(side_effect=repair)

    async def test_selected_revision_does_not_refresh_unrelated_quotes_or_repair_new_dates(self):
        self.state.update(repair_only=True,assessment_dates=[],revision_selected_ids=["park"])
        self.reports([("revise",[finding()],None)])
        result = await self.planner.assess_result(self.state,candidate(self.original),self.original)
        self.planner.refresh_expired_resources.assert_not_awaited()
        self.planner.repair.assert_not_awaited()
        self.planner.tickets.assert_not_awaited()
        self.assertEqual(result["candidates"][0]["draft"]["days"],self.original["days"])
        self.assertTrue(result["candidates"][0]["confirmation_blockers"])

    async def test_continue_can_locally_repair_a_new_problem_outside_the_previous_failure_date(self):
        draft = plan()
        later = copy.deepcopy(draft["days"][0])
        later["date"] = "2026-11-11"
        later["activities"][0].update(id="later",name="另一座园区",end="19:30",map_place={"location":"121,31"})
        draft["days"].append(later)
        route = candidate(draft)
        route["days"].append({"date":"2026-11-11","city":"杭州","stop_ids":["later"]})
        conflict = {**finding(),"target":"later","dates":["2026-11-11"],"field_path":"/days/1/activities/0"}
        self.reports([("revise",[conflict],None),("pass",[],None)])
        async def activities(state):
            self.assertEqual(state["change"]["dates"],["2026-11-11"])
            self.assertIn("park",state["locks"])
            current = copy.deepcopy(state["itinerary"])
            current["days"][1]["activities"][0]["end"] = "17:00"
            return {"itinerary":current,"stops":{},"weather":{}}
        self.planner.activities = AsyncMock(side_effect=activities)
        async def repair(state):
            return await Planner.repair(self.planner,state)
        self.planner.repair.side_effect = repair
        state = {**self.state,"repair_only":True,"assessment_dates":["2026-11-10"],
                 "requirements":{**REQ,"end_date":"2026-11-11"}}
        result = await self.planner.assess_result(state,route,draft)
        self.planner.activities.assert_awaited_once()
        self.assertEqual(without_identity(result["candidates"][0]["draft"]["days"][0]),without_identity(draft["days"][0]))
        self.assertFalse(result["candidates"][0]["confirmation_blockers"])
        self.planner.tickets.assert_not_awaited()

    async def test_three_new_conflicts_are_repaired_before_any_rollback(self):
        conflicts = [finding(index) for index in range(3)]
        self.reports([("revise",conflicts,None),("pass",[],None)])
        self.changing_repair()
        current = copy.deepcopy(self.original)
        current["issues"] = [{"code":"new_"+str(i),"target":"park","message":"排程冲突","blocking":True} for i in range(3)]
        result = await self.planner.assess_result(self.state,candidate(current),current)
        self.planner.repair.assert_awaited_once()
        self.assertEqual(self.planner.repair.call_args.args[0]["review_feedback"],conflicts)
        self.assertFalse(result["rollback"])
        self.assertEqual(result["candidates"][0]["draft"]["days"][0]["activities"][0]["end"],"16:30")
        self.assertEqual(result["candidates"][0]["review"]["status"],"pass")

    async def test_persistent_regression_gets_two_attempts_then_a_reviewed_rollback(self):
        conflicts = [finding(index) for index in range(3)]
        self.reports([("revise",conflicts,None)]*4)
        self.changing_repair()
        result = await self.planner.assess_result(self.state,candidate(self.original),copy.deepcopy(self.original))
        self.assertEqual(self.planner.repair.await_count,2)
        self.assertEqual(self.planner.audit.await_count,4)
        self.assertTrue(result["rollback"])
        self.assertIn("自动修复后",result["rollback"]["reason"])
        self.assertEqual(result["candidates"][0]["draft"]["days"][0]["activities"][0]["end"],"17:00")
        self.assertTrue(result["candidates"][0]["confirmation_blockers"])

    async def test_user_decision_or_keep_instruction_stops_without_implicit_rollback(self):
        conflicts = [finding(index) for index in range(3)]
        for flags,question in (({"keep_current_plan":True},None),({}, {"title":"选择如何调整","affected_dates":["2026-11-10"]})):
            with self.subTest(flags=flags,question=question):
                self.reports([("revise",conflicts,question)])
                result = await self.planner.assess_result({**self.state,**flags},candidate(self.original),copy.deepcopy(self.original))
                self.assertFalse(result["rollback"])
                self.assertTrue(result["candidates"][0]["confirmation_blockers"])
                self.planner.repair.assert_not_awaited()

    async def test_failed_evidence_does_not_prevent_an_independent_timing_repair(self):
        self.planner.store.review_queries.return_value = [{"evidence_id":"query:failed","result":{"status":"error"}}]
        repair,missing = finding(0),finding(1,action="evidence")
        missing["evidence_ids"] = ["query:failed"]
        self.reports([("needs_evidence",[repair,missing],None),("needs_evidence",[missing],None)])
        self.changing_repair()
        result = await self.planner.assess_result(self.state,candidate(self.original),copy.deepcopy(self.original))
        self.planner.repair.assert_awaited_once()
        self.assertEqual(self.planner.repair.call_args.args[0]["review_feedback"],[repair])
        self.assertFalse(result["rollback"])
        self.assertEqual(result["candidates"][0]["review"]["status"],"needs_evidence")

    async def test_two_repairs_get_a_concrete_final_attempt_without_asking_the_user_to_click(self):
        self.reports([("revise",[finding()],None)]*3+[("pass",[],None)])
        self.planner.route_agent.resolve_review.return_value = ReviewRepairResolution(
            status="repair",explanation="将末项结束时刻提前，保留已有午餐和真实交通")
        async def repair(state):
            draft = copy.deepcopy(state["itinerary"])
            count = state.get("repair_round",0)+1
            draft["days"][0]["activities"][0]["end"] = ("16:30","16:00","15:30")[count-1]
            return {"itinerary":draft,"repair_round":count}
        self.planner.repair.side_effect = repair
        result = await self.planner.assess_result(self.state,candidate(self.original),copy.deepcopy(self.original))
        self.assertEqual(self.planner.repair.await_count,3)
        self.planner.route_agent.resolve_review.assert_awaited_once()
        self.planner.route_agent.review.assert_not_awaited()
        self.assertFalse(result["decision"])
        self.assertFalse(result["candidates"][0]["confirmation_blockers"])
        self.assertEqual(result["candidates"][0]["review"]["status"],"pass")
        self.assertIn("本轮修复指令",self.planner.repair.call_args.args[0]["review_feedback"][0]["suggestion"])

    async def test_repeated_no_progress_enters_the_clarification_handoff(self):
        self.reports([("revise",[finding()],None)]*2)
        self.planner.repair.return_value = {"itinerary":copy.deepcopy(self.original),"repair_round":1}
        result = await self.planner.assess_result(self.state,candidate(self.original),copy.deepcopy(self.original))
        self.planner.repair.assert_awaited_once()
        self.planner.route_agent.resolve_review.assert_awaited_once()
        self.planner.route_agent.review.assert_not_awaited()
        self.assertFalse(result["decision"])
        self.assertEqual(self.planner.after_review_gate(result),"planning_failed")
        self.planner.emit = Mock()
        self.planner.route_agent.review.return_value = PlanningReview(question=PlanningDecision(
            id="priority",title="明确调整优先级",question="当前活动和固定时刻优先保留哪项？",
            options=["保留活动","保留固定时刻"],recommendation="优先保留已确定的时刻",why_needed="自动局部调整仍无法同时满足",
            affected_dates=["2026-11-10"]))
        handoff = await self.planner.planning_failed({**self.state,**result})
        self.assertEqual(self.planner.after_review_gate({**result,**handoff}),"decision_gate")

    async def test_genuine_required_item_tradeoff_is_asked_after_diagnosis(self):
        self.reports([("revise",[finding()],None)]*2)
        self.planner.repair.return_value = {"itinerary":copy.deepcopy(self.original),"repair_round":1}
        question = PlanningDecision(id="required-time",title="固定时刻与必去冲突",question="保留哪个条件？",
            why_needed="两个用户条件无法同时满足",options=["改预约时刻","调整必去安排"],recommendation="改预约时刻",
            affected_dates=["2026-11-10"])
        self.planner.route_agent.resolve_review.return_value = ReviewRepairResolution(
            status="needs_decision",explanation="必须修改用户固定时刻，不能自动接受",question=question)
        result = await self.planner.assess_result(self.state,candidate(self.original),copy.deepcopy(self.original))
        self.assertEqual(result["decision"],question.model_dump(mode="json"))
        self.assertEqual(self.planner.after_review_gate(result),"decision_gate")

    async def test_review_only_decision_is_not_lost_when_code_has_no_issues(self):
        only_decision = finding(action="decision")
        self.reports([("revise",[only_decision],None)])
        question = PlanningDecision(id="choice",title="必去取舍",question="如何调整固定安排？",why_needed="不能自行改变必去",
            options=["调整日期","调整时刻"],recommendation="调整时刻",affected_dates=["2026-11-10"])
        self.planner.route_agent.review.return_value = PlanningReview(question=question)
        result = await self.planner.assess_result(self.state,candidate(self.original),copy.deepcopy(self.original))
        self.assertEqual(result["decision"],question.model_dump(mode="json"))
        self.planner.repair.assert_not_awaited()

    async def test_unrelated_failed_query_does_not_suppress_automatic_evidence_work(self):
        self.planner.store.review_queries.return_value = [{"evidence_id":"query:other","result":{"status":"error"}}]
        self.reports([("needs_evidence",[finding(action="evidence")],None),("pass",[],None)])
        self.changing_repair()
        result = await self.planner.assess_result(self.state,candidate(self.original),copy.deepcopy(self.original))
        self.planner.repair.assert_awaited_once()
        self.assertFalse(result["decision"])
        self.assertEqual(result["candidates"][0]["review"]["status"],"pass")

    async def test_independent_day_is_repaired_before_asking_another_days_tradeoff(self):
        draft = plan()
        later = copy.deepcopy(draft["days"][0])
        later["date"] = "2026-11-11"
        later["activities"][0].update(id="later",end="21:30")
        draft["days"].append(later)
        route = candidate(draft)
        route["days"].append({"date":"2026-11-11","city":"杭州","stop_ids":["later"]})
        question = {"title":"第一天固定时刻取舍","affected_dates":["2026-11-10"]}
        choice = finding(action="decision")
        fix = {**finding(),"target":"later","dates":["2026-11-11"],"field_path":"/days/1"}
        self.reports([("needs_decision",[choice,fix],question),("needs_decision",[choice],question)])
        async def repair(state):
            self.assertEqual(state["review_feedback"],[fix])
            current = copy.deepcopy(state["itinerary"])
            current["days"][1]["activities"][0]["end"] = "17:00"
            return {"itinerary":current,"repair_round":1}
        self.planner.repair.side_effect = repair
        result = await self.planner.assess_result(self.state,route,draft)
        self.planner.repair.assert_awaited_once()
        self.assertEqual(result["decision"],question)
        self.assertEqual(result["candidates"][0]["draft"]["days"][1]["activities"][0]["end"],"17:00")

    async def test_keep_answer_for_one_day_does_not_disable_repairs_on_another_day(self):
        itinerary = plan()
        later = copy.deepcopy(itinerary["days"][0])
        later["date"] = "2026-11-11"
        later["activities"][0].update(id="later",name="另一天景点",end="21:30")
        itinerary["days"].append(later)
        route = candidate(itinerary)
        route["days"].append({"city":"杭州","date":"2026-11-11","stop_ids":["later"]})
        local = {**finding(),"target":"later","field_path":"/days/1/activities/0","dates":["2026-11-11"]}
        self.reports([("revise",[finding(),local],None),("revise",[finding()],None)])
        async def repair(state):
            self.assertFalse(state["keep_current_plan"])
            self.assertEqual(state["change"]["dates"],["2026-11-11"])
            self.assertEqual(state["review_feedback"],[local])
            draft = copy.deepcopy(state["itinerary"])
            draft["days"][1]["activities"][0]["end"] = "17:00"
            return {"itinerary":draft,"repair_round":state.get("repair_round",0)+1}
        self.planner.repair.side_effect = repair
        state = {**self.state,"keep_current_plan":True,"assessment_dates":["2026-11-10"],
            "requirements":{**REQ,"end_date":"2026-11-11"}}
        result = await self.planner.assess_result(state,route,copy.deepcopy(itinerary))
        self.planner.repair.assert_awaited_once()
        draft = result["candidates"][0]["draft"]
        self.assertEqual(draft["days"][0],itinerary["days"][0])
        self.assertEqual(draft["days"][1]["activities"][0]["end"],"17:00")
        self.assertTrue(result["candidates"][0]["confirmation_blockers"])

    async def test_quote_only_refresh_preserves_all_days_even_with_local_query_dates(self):
        itinerary = plan()
        later = copy.deepcopy(itinerary["days"][0])
        later["date"] = "2026-11-11"
        itinerary["days"].append(later)
        local = {**finding(),"field_path":"/days/1","dates":["2026-11-11"]}
        self.reports([("revise",[local],None)])
        state = {**self.state,"keep_current_plan":True,"assessment_refresh":True,"assessment_dates":["2026-11-10"]}
        result = await self.planner.assess_result(state,candidate(itinerary),itinerary)
        self.planner.repair.assert_not_awaited()
        self.assertTrue(result["candidates"][0]["confirmation_blockers"])

    async def test_new_query_evidence_counts_as_progress_without_changing_visit_times(self):
        missing = finding(action="evidence")
        self.reports([("needs_evidence",[missing],None),("needs_evidence",[missing],None),("pass",[],None)])
        async def repair(state):
            itinerary = copy.deepcopy(state["itinerary"])
            count = state.get("repair_round",0)+1
            itinerary["days"][0]["activities"][0]["map_place"]["evidence_id"] = "query:"+str(count)
            return {"itinerary":itinerary,"repair_round":count}
        self.planner.repair = AsyncMock(side_effect=repair)
        result = await self.planner.assess_result(self.state,candidate(self.original),copy.deepcopy(self.original))
        self.assertEqual(self.planner.repair.await_count,2)
        self.assertFalse(result["rollback"])
        self.assertEqual(result["candidates"][0]["review"]["status"],"pass")

    async def test_mixed_timing_and_evidence_feedback_still_calls_daily_planning(self):
        itinerary = plan()
        self.planner.activities = AsyncMock(return_value={"itinerary":itinerary,"stops":{},"weather":{}})
        # Call the real dispatch method, keeping every provider and scheduling operation mocked.
        state = {**self.state,"itinerary":itinerary,"change":{},"review_feedback":[finding(),finding(1,action="evidence")]}
        result = await Planner.repair(self.planner,state)
        self.planner.activities.assert_awaited_once()
        self.assertFalse(self.planner.activities.call_args.args[0]["change"].get("refresh_resources"))
        self.assertEqual(result["repair_round"],1)

    async def test_automatic_repair_locks_unrelated_visits_even_without_continue_mode(self):
        itinerary = plan()
        original = itinerary["days"][0]["activities"][0]
        original.update(id="before",start="09:00",end="10:00")
        target = {**copy.deepcopy(original),"id":"museum","name":"合成博物馆","start":"15:20","end":"17:20",
                  "appointment_time":"14:09","map_place":{"location":"121,31"}}
        after = {**copy.deepcopy(original),"id":"after","name":"合成街区","start":"18:00","end":"19:00",
                 "map_place":{"location":"122,32"}}
        itinerary["days"][0]["activities"] = [original,target,after]
        later = copy.deepcopy(itinerary["days"][0])
        later.update(date="2026-11-11",activities=[{**copy.deepcopy(original),"id":"other-day"}])
        itinerary["days"].append(later)
        self.planner.activities = AsyncMock(return_value={"itinerary":itinerary,"stops":{},"weather":{}})
        conflict = {**finding(),"target":"museum","field_path":"/days/0/activities/1","message":"预约时刻与活动开始时间冲突"}
        state = {**self.state,"itinerary":itinerary,"change":{},"review_feedback":[conflict]}
        await Planner.repair(self.planner,state)
        scoped = self.planner.activities.call_args.args[0]
        self.assertTrue(scoped["repair_only"])
        self.assertEqual(scoped["change"]["dates"],["2026-11-10"])
        self.assertEqual(set(scoped["locks"]),{"before","after","other-day"})
        self.planner.tickets.assert_not_awaited()

        day_path = {**conflict,"field_path":"/days"}
        await Planner.repair(self.planner,{**state,"review_feedback":[day_path]})
        self.assertEqual(set(self.planner.activities.call_args.args[0]["locks"]),{"before","after","other-day"})

        await Planner.repair(self.planner,{**state,"repair_round":1,"locks":["before"]})
        scoped = self.planner.activities.call_args.args[0]
        self.assertEqual(set(scoped["locks"]),{"before","other-day"})
        self.assertEqual(scoped["change"]["dates"],["2026-11-10"])

    async def test_correcting_an_appointment_field_counts_as_repair_progress(self):
        current = copy.deepcopy(self.original)
        current["days"][0]["activities"][0]["appointment_time"] = "08:00"
        self.reports([("revise",[finding()],None),("revise",[finding()],None),("pass",[],None)])
        async def repair(state):
            draft = copy.deepcopy(state["itinerary"])
            draft["days"][0]["activities"][0]["appointment_time"] = None
            return {"itinerary":draft,"repair_round":state.get("repair_round",0)+1}
        self.planner.repair.side_effect = repair
        result = await self.planner.assess_result(self.state,candidate(current),current)
        self.assertEqual(self.planner.repair.await_count,2)
        self.assertEqual(result["candidates"][0]["review"]["status"],"pass")
        self.planner.tickets.assert_not_awaited()

    async def test_missing_review_dates_rebuilds_only_the_day_named_by_its_path(self):
        itinerary = plan()
        later = copy.deepcopy(itinerary["days"][0])
        later.update(date="2026-11-11")
        later["activities"][0].update(id="other",name="另一座合成公园",map_place={"location":"121,31"})
        itinerary["days"].append(later)
        self.planner.activities = AsyncMock(return_value={"itinerary":itinerary,"stops":{},"weather":{}})
        feedback = {**finding(),"target":"other","dates":[],"field_path":"/days/1/activities/0"}
        state = {**self.state,"itinerary":itinerary,"change":{},"review_feedback":[feedback]}
        await Planner.repair(self.planner,state)
        self.planner.activities.assert_awaited_once()
        change = self.planner.activities.call_args.args[0]["change"]
        self.assertEqual(change["dates"],["2026-11-11"])
        self.assertEqual(change["scopes"],["activity"])

    async def test_full_review_can_repair_a_later_day_outside_the_previous_answers_edit_dates(self):
        draft = plan()
        later = copy.deepcopy(draft["days"][0])
        later["date"] = "2026-11-11"
        later["activities"][0].update(id="later",name="另一座合成园区",end="19:30",map_place={"location":"121,31"})
        draft["days"].append(later)
        route = candidate(draft)
        route["days"].append({"date":"2026-11-11","city":"杭州","stop_ids":["later"]})
        conflict = {**finding(),"target":"later","dates":["2026-11-11"],"field_path":"/days/1/activities/0"}
        self.reports([("revise",[conflict],None),("pass",[],None)])
        async def activities(state):
            self.assertEqual(state["change"]["dates"],["2026-11-11"])
            current = copy.deepcopy(state["itinerary"])
            current["days"][1]["activities"][0]["end"] = "17:00"
            return {"itinerary":current,"stops":{},"weather":{}}
        self.planner.activities = AsyncMock(side_effect=activities)
        async def repair(state):
            return await Planner.repair(self.planner,state)
        self.planner.repair.side_effect = repair
        state = {**self.state,"mode":"modify","change":{"scopes":["activity"],"dates":["2026-11-10"]},
                 "requirements":{**REQ,"end_date":"2026-11-11"}}
        result = await self.planner.assess_result(state,route,draft)
        self.planner.activities.assert_awaited_once()
        self.assertEqual(without_identity(result["candidates"][0]["draft"]["days"][0]),without_identity(draft["days"][0]))
        self.assertEqual(result["candidates"][0]["draft"]["days"][1]["activities"][0]["end"],"17:00")
        self.assertFalse(result["decision"])
