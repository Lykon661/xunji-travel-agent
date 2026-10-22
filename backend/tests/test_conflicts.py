"""Synthetic day-capacity and stop-pool questions built by code; no model, map or supplier calls."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app.agents.runtime import digest
from app.adapters import ToolError
from app.agents.route import RouteAgent
from app.conflicts import accepted_shortage, apply_option, capacity_question, code_decision, pool_question, trip_pool_question
from app.constraints import merge
from app.models import ConstraintDraft
from app.constraints import active
from app.models import Requirements
from app.rules import constraint_issues
from app.planner import Planner


A, B = "2026-11-10", "2026-11-11"
REQ = Requirements(origin="上海",cities=["杭州"],start_date=A,end_date=B,people=2,rooms=1,budget=6000).model_dump(mode="json")
STOPS = {identity:{"id":identity,"name":name,"city":"杭州","duration_minutes":minutes,"source_id":"s1"}
         for identity,name,minutes in (("xh","西湖",240),("ly","灵隐寺",180),("ld","雷峰塔",60))}


def visit(identity, start, end):
    return {"id":identity,"kind":"visit","name":STOPS[identity]["name"],"start":start,"end":end}


def day(date, *activities, start=540, deadline=1020):
    return {"date":date,"city":"杭州","available_start":start,"tail_deadline":deadline,"activities":list(activities),"gaps":[]}


def state(required=("xh","ly")):
    return {"requirements":{**REQ,"required_stop_ids":list(required)},"stops":dict(STOPS),"locks":[],"change":{},
            "stop_constraints":{"required":list(required),"excluded":[]},"selected_source_ids":["s1"],
            "selected_route":{"days":[{"date":A,"city":"杭州","stop_ids":["xh"]},{"date":B,"city":"杭州","stop_ids":["ld"]}]},
            "candidates":[{"id":"draft","days":[{"date":A,"city":"杭州","stop_ids":["xh"]},{"date":B,"city":"杭州","stop_ids":["ld"]}]}]}


def itinerary():
    return {"days":[day(A,visit("xh","09:00","13:00")),day(B,visit("ld","09:00","10:00"))]}


class Capacity(unittest.TestCase):
    def test_overloaded_day_lists_the_conflict_and_options_that_fit(self):
        question,options = capacity_question(state(),itinerary(),A)
        self.assertIn("西湖",question["question"])
        self.assertIn("灵隐寺",question["question"])
        self.assertEqual([options[label]["type"] for label in question["options"]],["move","move","shorten","shorten"])
        self.assertIn(B,question["affected_dates"])
        self.assertTrue(question["recommendation"].startswith(question["options"][0]))

    def test_day_that_fits_needs_no_question(self):
        self.assertIsNone(capacity_question(state(required=("xh",)),itinerary(),A))
        self.assertIsNone(code_decision(state(required=("xh",)),itinerary(),[A,B],lambda:[]))

    def test_locked_items_are_never_offered_for_change(self):
        locked = {**state(),"locks":["xh","ly"]}
        self.assertIsNone(capacity_question(locked,itinerary(),A))

    def test_move_and_shorten_options_change_route_and_constraints(self):
        current = state()
        question,options = capacity_question(current,itinerary(),A)
        move = next(label for label in question["options"] if options[label]["type"] == "move" and options[label]["stop_id"] == "ly")
        updates,dates,_ = apply_option(current,options[move],move)
        self.assertEqual(dates,[A,B])
        self.assertIn("ly",updates["candidates"][0]["days"][1]["stop_ids"])
        shorten = next(label for label in question["options"] if options[label]["type"] == "shorten" and options[label]["stop_id"] == "xh")
        updates,_,_ = apply_option(current,options[shorten],shorten)
        [limit] = active(updates["requirements"],"visit_duration",date=A,stop_id="xh")
        self.assertEqual((limit["minutes"],limit["quote"],limit["source"]),(180,shorten,"decision"))

    def test_exclude_moves_a_required_stop_to_excluded(self):
        updates,_,_ = apply_option(state(),{"type":"exclude","stop_id":"ly","date":A},"这次不去灵隐寺")
        self.assertEqual(updates["stop_constraints"],{"required":["xh"],"excluded":["ly"]})
        self.assertEqual(updates["requirements"]["excluded_stop_ids"],["ly"])


class StopPool(unittest.TestCase):
    def short_day(self):
        current = {**state(required=()),"stops":{"xh":STOPS["xh"]}}
        plan = {"days":[day(A,visit("xh","09:00","10:00"),deadline=1200)]}
        return current,plan

    def test_unselected_imported_guides_are_offered_before_free_time(self):
        current,plan = self.short_day()
        other = {"id":"s2","title":"杭州小众路线","url":"","extraction":{"stops":[
            {"id":"s2:ly","name":"灵隐寺","city":"杭州","category":"sight"},
            {"id":"s2:xh","name":"西湖","city":"杭州","category":"sight"},
            {"id":"s2:food","name":"知味观","city":"杭州","category":"dining"}]}}
        question,options = pool_question(current,plan,A,lambda:[{"id":"s1","title":"已选"},other])
        self.assertEqual(question["options"],["加入《杭州小众路线》里的灵隐寺","这天剩余时间自由安排"])
        updates,dates,_ = apply_option(current,options[question["options"][0]],question["options"][0])
        self.assertEqual((updates["selected_source_ids"],dates),(["s1","s2"],[A]))
        self.assertIn("s2:ly",updates["stops"])
        self.assertIn("s2:ly",updates["candidates"][0]["days"][0]["stop_ids"])

    def test_without_other_guides_only_free_time_is_offered(self):
        current,plan = self.short_day()
        question,options = pool_question(current,plan,A,lambda:[])
        self.assertEqual(question["options"],["这天剩余时间自由安排"])
        updates,_,_ = apply_option(current,options["这天剩余时间自由安排"],"这天剩余时间自由安排")
        self.assertTrue(updates["keep_current_plan"])

    def test_stops_that_cannot_be_placed_do_not_count_as_unused(self):
        current,plan = self.short_day()
        current["stops"] = {**current["stops"],
            "ly":{**STOPS["ly"],"map_issue":"高德没有唯一匹配","map_place":None},
            "ld":{**STOPS["ld"],"assigned_date":B}}
        question,_ = pool_question(current,plan,A,lambda:[])
        self.assertEqual(question["id"],"pool-"+A)

    def test_unused_selected_stops_mean_no_pool_question_and_no_store_read(self):
        current,plan = self.short_day()
        current["stops"] = dict(STOPS)
        sources = Mock(side_effect=AssertionError("store should not be read"))
        self.assertIsNone(pool_question(current,plan,A,sources))


def minimum(count, **scope):
    return merge([],[ConstraintDraft(kind="daily_min_stops",count=count,scope=scope,quote="每天至少三个景点").model_dump(mode="json")],"form")


class DailyMinimum(unittest.TestCase):
    def test_a_full_day_below_the_minimum_is_a_pool_question(self):
        current = {**state(required=()),"stops":{"xh":STOPS["xh"]}}
        current["requirements"] = {**current["requirements"],"constraints":minimum(3)}
        plan = {"days":[day(A,visit("xh","09:00","19:30"),deadline=1200)]}
        question,options = pool_question(current,plan,A,lambda:[])
        self.assertEqual(question["options"],["这天只安排1个景点"])
        self.assertIn("少于你要求的每天至少3个",question["question"])
        updates,_,_ = apply_option(current,options["这天只安排1个景点"],"这天只安排1个景点")
        self.assertEqual(active(updates["requirements"],"daily_min_stops",date=A),[])
        [rest] = active(updates["requirements"],"daily_min_stops",date=B)
        self.assertEqual((rest["count"],rest["scope"]["dates"]),(3,[B]))

    def test_candidate_route_is_checked_before_any_schedule(self):
        current = {**state(required=()),"stops":{"xh":STOPS["xh"]}}
        current["requirements"] = {**current["requirements"],"constraints":minimum(2)}
        current["selected_route"] = {"days":[{"date":A,"city":"杭州","stop_ids":["xh"]}]}
        question,_ = pool_question(current,{"days":[]},A,lambda:[])
        self.assertEqual(question["id"],"pool-"+A)

    def test_trip_pool_counts_usable_stops_against_the_daily_minimum(self):
        current = {**state(required=()),"stops":dict(STOPS)}
        current["requirements"] = {**current["requirements"],"constraints":minimum(2)}
        other = {"id":"s2","title":"杭州补充","extraction":{"stops":[{"id":"s2:hz","name":"河坊街","city":"杭州","category":"sight"}]}}
        question,options = trip_pool_question(current,lambda:[other])
        self.assertIn("共有3个可安排的地点",question["question"])
        self.assertEqual(question["options"][0],"加入《杭州补充》里的1个地点（河坊街）")
        added,dates,_ = apply_option(current,options[question["options"][0]],question["options"][0])
        self.assertEqual((added["selected_source_ids"],dates),(["s1","s2"],[]))
        labels = {options[label]["type"]:label for label in question["options"]}
        self.assertEqual(labels["lower_count"],"改为每天至少1个景点")
        lowered,_,_ = apply_option(current,options[labels["lower_count"]],labels["lower_count"])
        self.assertEqual([(item["count"],item["strength"]) for item in active(lowered["requirements"],"daily_min_stops",date=A)],[(1,"soft")])
        self.assertEqual(labels["accept_short"],"按现有攻略安排，景点不够的日子少于2个也可以")
        accepted,_,_ = apply_option(current,options[labels["accept_short"]],labels["accept_short"])
        self.assertEqual([(item["count"],item["strength"]) for item in active(accepted["requirements"],"daily_min_stops",date=B)],[(2,"soft")])
        current["requirements"]["constraints"] = minimum(1)
        self.assertIsNone(trip_pool_question(current,lambda:[other]))


class DailyMinimumRoom(unittest.TestCase):
    def test_full_day_is_asked_even_with_unused_stops_but_a_day_with_room_is_left_to_scheduling(self):
        current = {**state(required=()),"stops":dict(STOPS)}
        current["requirements"] = {**current["requirements"],"constraints":minimum(3)}
        full = {"days":[day(A,visit("xh","09:00","19:30"),deadline=1200)]}
        question,options = pool_question(current,full,A,lambda:[])
        self.assertEqual(question["options"],["这天只安排1个景点"])
        self.assertIn("时间已经排满",question["question"])
        roomy = {"days":[day(A,visit("xh","09:00","10:00"),deadline=1200)]}
        self.assertIsNone(pool_question(current,roomy,A,lambda:[]))

    def test_aliases_count_once_and_a_whole_day_place_fills_its_day(self):
        current = {**state(required=()),"stops":{
            "xh":{**STOPS["xh"],"duration_minutes":None,"evidence":{"kind":"text","quote":"Day1：西湖一整天。Day2：灵隐寺"}},
            "ly":STOPS["ly"],"ld":STOPS["ld"],"ld2":{**STOPS["ld"],"id":"ld2","name":"杭州雷峰塔","source_id":"s2"}}}
        current["requirements"] = {**current["requirements"],"constraints":minimum(1)}
        self.assertIsNone(trip_pool_question(current,lambda:[]))
        current["requirements"]["constraints"] = minimum(2)
        question,options = trip_pool_question(current,lambda:[])
        self.assertIn("共有3个可安排的地点，其中西湖需要一整天",question["question"])
        self.assertEqual([options[label]["type"] for label in question["options"]],["accept_short"])

    def test_after_accepting_short_days_nothing_asks_or_blocks_again(self):
        current = {**state(required=()),"stops":{"xh":STOPS["xh"]}}
        current["requirements"] = {**current["requirements"],"constraints":minimum(3)}
        question,options = trip_pool_question(current,lambda:[])
        label = question["options"][-1]
        updates,_,_ = apply_option(current,options[label],label)
        current.update(requirements=updates["requirements"])
        self.assertIsNone(trip_pool_question(current,lambda:[]))
        plan = {"days":[day(A,visit("xh","09:00","19:30"),deadline=1200)]}
        self.assertIsNone(pool_question(current,plan,A,lambda:[]))
        [issue] = constraint_issues(plan,current["requirements"])[0]
        self.assertFalse(issue["blocking"])


def accepted(count):
    return merge([],[ConstraintDraft(kind="daily_min_stops",strength="soft",count=count,quote="每天至少三个景点").model_dump(mode="json")],"decision")


def uneven():
    """Day A holds all three stops and day B none, under an accepted (soft) minimum of two."""
    current = {**state(required=()),"stops":dict(STOPS)}
    current["requirements"] = {**current["requirements"],"constraints":accepted(2)}
    current["candidates"] = [{"id":"draft","days":[{"date":A,"city":"杭州","stop_ids":["xh","ly","ld"]},{"date":B,"city":"杭州","stop_ids":[]}]}]
    current["selected_route"] = {"days":current["candidates"][0]["days"]}
    plan = {"days":[day(A,visit("xh","09:00","11:00"),visit("ly","11:30","13:00"),visit("ld","13:30","14:00")),day(B)]}
    return current,plan


class AcceptedShortage(unittest.TestCase):
    def test_an_empty_day_still_blocks_and_offers_a_stop_from_a_fuller_day(self):
        current,plan = uneven()
        [issue] = constraint_issues(plan,current["requirements"])[0]
        self.assertEqual((issue["target"],issue["blocking"]),(B,True))
        question,options = pool_question(current,plan,B,lambda:[])
        self.assertIn("一个景点都没有安排",question["question"])
        self.assertEqual(question["options"],["把雷峰塔从11月10日改到11月11日","这天不安排景点，自由活动"])
        moved,dates,_ = apply_option(current,options[question["options"][0]],question["options"][0])
        self.assertEqual([entry["stop_ids"] for entry in moved["candidates"][0]["days"]],[["xh","ly"],["ld"]])
        self.assertEqual(dates,[A,B])

    def test_a_count_question_after_acceptance_is_settled_only_when_the_guides_are_used_up(self):
        current,plan = uneven()
        plan["days"][1]["activities"] = [plan["days"][0]["activities"].pop()]
        current["candidates"][0]["draft"] = plan
        question = {"title":"11月11日景点不足","question":"只有雷峰塔，无法满足每天至少2个景点","affected_dates":[B]}
        self.assertTrue(accepted_shortage(current,question))
        self.assertFalse(accepted_shortage(current,{**question,"title":"步行超限","question":"11月11日步行超过上限"}))
        extra = {**current,"stops":{**STOPS,"hf":{"id":"hf","name":"河坊街","city":"杭州","source_id":"s1"}}}
        self.assertFalse(accepted_shortage(extra,question))
        alias = {**current,"stops":{**STOPS,"ld2":{**STOPS["ld"],"id":"ld2","name":"杭州雷峰塔"}}}
        self.assertTrue(accepted_shortage(alias,question))
        strict = {**current,"requirements":{**current["requirements"],"constraints":minimum(2)}}
        self.assertFalse(accepted_shortage(strict,question))
        plan["days"][1]["activities"] = []
        self.assertFalse(accepted_shortage(current,question))


class RouteFormat(unittest.IsolatedAsyncioTestCase):
    async def test_missing_daily_order_is_corrected_once(self):
        agent = RouteAgent(SimpleNamespace(),Mock())
        ready = SimpleNamespace(status="ready",order=SimpleNamespace(stop_ids=["xh"]),candidate=None)
        agent.ask = AsyncMock(side_effect=[SimpleNamespace(status="ready",order=None,candidate=object()),ready])
        self.assertIs(await agent.plan_turn({},"合成指令",{},None,True),ready)
        retry = agent.ask.await_args_list[1]
        self.assertIn("没有order",retry.args[2])
        self.assertFalse(retry.kwargs["cache"])

    async def test_second_incomplete_result_still_fails(self):
        agent = RouteAgent(SimpleNamespace(),Mock())
        agent.ask = AsyncMock(return_value=SimpleNamespace(status="ready",order=None,candidate=None))
        with self.assertRaises(ToolError):
            await agent.plan_turn({},"合成指令",{},None,False)
        self.assertEqual(agent.ask.await_count,2)


class DecisionGate(unittest.IsolatedAsyncioTestCase):
    async def test_chosen_option_is_executed_without_a_model_call(self):
        planner = Planner.__new__(Planner)
        planner.emit = Mock()
        planner.models = SimpleNamespace(ask=AsyncMock())
        current = state()
        question,options = capacity_question(current,itinerary(),A)
        current.update(trip_id="t",run_id="r",generation="g",draft_revision=0,decision_history=[],decision_resume="assess",
                       decision=question,decision_options={"question_id":question["id"],"options":options})
        label = question["options"][0]
        token = digest({"question":question,"history":[],"generation":"g","revision":0})[:40]
        with patch("app.planner.interrupt",return_value={"round_id":token,"draft_revision":0,"answers":{question["id"]:label},"note":""}):
            result = await planner.decision_gate(current)
        planner.models.ask.assert_not_awaited()
        self.assertEqual(result["decision_options"],{})
        self.assertEqual(result["decision_history"][-1]["code_option"],"move")
        self.assertEqual(result["assessment_dates"],[A,B])

    async def test_count_question_after_acceptance_is_settled_without_asking(self):
        planner = Planner.__new__(Planner)
        planner.emit = Mock()
        current,plan = uneven()
        plan["days"][1]["activities"] = [plan["days"][0]["activities"].pop()]
        current["candidates"][0]["draft"] = plan
        question = {"id":"few","title":"11月11日景点不足","question":"无法满足每天至少2个景点","affected_dates":[B],
                    "options":["接受"],"recommendation":"接受","why_needed":"地点不够"}
        current.update(trip_id="t",run_id="r",generation="g",draft_revision=0,decision_history=[],decision_resume="review_result",decision=question)
        with patch("app.planner.interrupt") as asked:
            result = await planner.decision_gate(current)
        asked.assert_not_called()
        self.assertEqual(result["decision_history"][-1]["auto_settled"],"accepted_shortage")
        self.assertEqual((result["decision_resume"],result["keep_current_plan"],result["assessment_dates"]),("assess",True,[B]))


if __name__ == "__main__":
    unittest.main()
