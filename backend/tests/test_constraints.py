"""Synthetic constraint extraction and lookup; the model is mocked and nothing external is called."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pydantic import ValidationError

from app.constraints import (FEWER_WALKING_METERS, active, check, drop_source, merge, preferences_basis, public_only,
                             refresh_form_constraints, station_buffer, walking_target)
from app.models import Constraint, ConstraintDraft, ConstraintExtraction, Requirements
from app.rules import constraint_issues, walking_limit


REQ = Requirements(origin="上海",cities=["杭州"],start_date="2026-11-10",end_date="2026-11-12",
                   people=2,rooms=1,budget=6000,preferences="少走路，第二天先去西湖再去灵隐寺").model_dump(mode="json")
STOPS = {"guide:xh":{"id":"guide:xh","name":"西湖","city":"杭州"},"guide:ly":{"id":"guide:ly","name":"灵隐寺","city":"杭州"}}


def draft(**value):
    return ConstraintDraft(**value).model_dump(mode="json")


class ConstraintShape(unittest.TestCase):
    def test_kind_specific_parameters_are_required(self):
        with self.assertRaises(ValidationError):
            ConstraintDraft(kind="visit_duration",scope={"stop_ids":["guide:xh"]},quote="西湖玩3小时")
        with self.assertRaises(ValidationError):
            ConstraintDraft(kind="visit_duration",minutes=180,quote="西湖玩3小时")
        with self.assertRaises(ValidationError):
            ConstraintDraft(kind="fixed_order",before="guide:xh",after="guide:xh",quote="先去西湖")
        order = ConstraintDraft(kind="fixed_order",before="guide:xh",after="guide:ly",quote="先去西湖再去灵隐寺")
        self.assertEqual(order.scope.stop_ids,["guide:xh","guide:ly"])

    def test_identity_follows_the_condition_not_the_stored_id(self):
        stored = Constraint(id="stale",source="form",kind="fewer_walking",quote="少走路")
        self.assertEqual(stored.id,merge([],[draft(kind="fewer_walking",quote="少走路")],"form")[0]["id"])
        softer = Constraint(**{**stored.model_dump(),"strength":"soft"})
        self.assertNotEqual(softer.id,stored.id)

    def test_daily_minimum_needs_a_count_and_flags_short_days(self):
        with self.assertRaises(ValidationError):
            ConstraintDraft(kind="daily_min_stops",quote="每天至少三个景点")
        req = {**REQ,"constraints":merge([],[draft(kind="daily_min_stops",count=2,quote="每天至少两个景点")],"form")}
        plan = {"days":[{"date":"2026-11-10","city":"杭州","activities":[visit("guide:xh","09:00","12:00")]},
                        {"date":"2026-11-11","city":"杭州","activities":[visit("guide:xh","09:00","10:00"),visit("guide:ly","11:00","12:00")]}]}
        [issue] = constraint_issues(plan,req)[0]
        self.assertEqual((issue["target"],issue["blocking"]),("2026-11-10",True))

    def test_old_documents_without_constraints_still_load(self):
        legacy = {key:value for key,value in REQ.items() if key not in {"constraints","constraints_basis"}}
        self.assertEqual(Requirements.model_validate(legacy).constraints,[])

    def test_no_taxi_text_alone_no_longer_rewrites_the_flag(self):
        self.assertTrue(Requirements.model_validate({**REQ,"preferences":"不打车","allow_taxi":True}).allow_taxi)


class ConstraintChecks(unittest.TestCase):
    def test_quote_stop_date_and_city_must_come_from_this_trip(self):
        drafts = [draft(kind="fewer_walking",quote="少走路"),
                  draft(kind="fewer_walking",quote="用户说过不想走路"),
                  draft(kind="booking_prerequisite",scope={"stop_ids":["guide:none"]},quote="少走路"),
                  draft(kind="fewer_walking",scope={"dates":["2026-12-01"]},quote="少走路"),
                  draft(kind="fewer_walking",scope={"cities":["苏州"]},quote="少走路")]
        valid,issues = check(drafts,REQ["preferences"],REQ,STOPS)
        self.assertEqual(len(valid),1)
        self.assertEqual([item["index"] for item in issues],[1,2,3,4])

    def test_quote_ignores_whitespace_differences(self):
        valid,issues = check([draft(kind="fewer_walking",quote="少 走路")],"少走\n路",REQ,STOPS)
        self.assertEqual((len(valid),issues),(1,[]))


class ConstraintMerging(unittest.TestCase):
    def test_equal_constraint_is_kept_once_and_overlapping_one_is_superseded(self):
        first = merge([],[draft(kind="visit_duration",scope={"stop_ids":["guide:xh"]},minutes=120,quote="西湖两小时")],"form")
        again = merge(first,[draft(kind="visit_duration",scope={"stop_ids":["guide:xh"]},minutes=120,quote="西湖玩两小时")],"decision")
        self.assertEqual(again,first)
        changed = merge(first,[draft(kind="visit_duration",scope={"stop_ids":["guide:xh"]},minutes=180,quote="西湖三小时")],"decision","q1")
        self.assertEqual(changed[0]["superseded_by"],changed[1]["id"])
        self.assertEqual([item["minutes"] for item in active({"constraints":changed},"visit_duration",stop_id="guide:xh")],[180])
        self.assertEqual(changed[1]["round_id"],"q1")

    def test_different_targets_or_disjoint_dates_do_not_supersede(self):
        items = merge([],[draft(kind="visit_duration",scope={"stop_ids":["guide:xh"]},minutes=120,quote="西湖两小时"),
                          draft(kind="visit_duration",scope={"stop_ids":["guide:ly"]},minutes=60,quote="灵隐一小时"),
                          draft(kind="fewer_walking",scope={"dates":["2026-11-10"]},quote="第一天少走路"),
                          draft(kind="fewer_walking",scope={"dates":["2026-11-11"]},quote="第二天少走路")],"form")
        self.assertFalse(any(item["superseded_by"] for item in items))

    def test_revoke_supersedes_and_is_itself_inactive(self):
        items = merge([],[draft(kind="fewer_walking",quote="少走路")],"form")
        items = merge(items,[draft(kind="fewer_walking",revoke=True,quote="不用少走路了")],"decision")
        self.assertEqual(active({"constraints":items},"fewer_walking"),[])

    def test_dropping_a_source_reinstates_what_it_superseded(self):
        items = merge([],[draft(kind="fewer_walking",quote="少走路")],"decision")
        items = merge(items,[draft(kind="fewer_walking",strength="soft",quote="尽量少走路")],"form")
        self.assertTrue(items[0]["superseded_by"])
        restored = drop_source(items,"form")
        self.assertEqual([(item["source"],item["superseded_by"]) for item in restored],[("decision","")])


class ConstraintLookup(unittest.TestCase):
    def test_scope_limits_dates_and_stops(self):
        req = {"constraints":merge([],[draft(kind="fixed_order",before="guide:xh",after="guide:ly",
                                             scope={"dates":["2026-11-11"]},quote="第二天先去西湖再去灵隐寺")],"form")}
        self.assertTrue(active(req,"fixed_order",date="2026-11-11",stop_id="guide:ly"))
        self.assertFalse(active(req,"fixed_order",date="2026-11-10",stop_id="guide:ly"))
        self.assertFalse(active(req,"fixed_order",date="2026-11-11",stop_id="guide:other"))
        self.assertTrue(active(req,"fixed_order"))


def visit(identity, start, end, **extra):
    return {"id":identity,"kind":"visit","name":STOPS[identity]["name"],"start":start,"end":end,**extra}


class PlanChecks(unittest.TestCase):
    def plan(self, first, second):
        return {"days":[{"date":"2026-11-10","city":"杭州","activities":first},{"date":"2026-11-11","city":"杭州","activities":second}]}

    def test_order_appointment_and_duration_are_checked_against_the_plan(self):
        req = {"constraints":merge([],[
            draft(kind="fixed_order",before="guide:xh",after="guide:ly",quote="先去西湖再去灵隐寺"),
            draft(kind="time_anchor",anchor="appointment",at="14:00",scope={"stop_ids":["guide:xh"]},quote="西湖下午2点"),
            draft(kind="visit_duration",minutes=180,scope={"stop_ids":["guide:ly"]},quote="灵隐寺玩3小时")],"form")}
        issues,_ = constraint_issues(self.plan([visit("guide:ly","09:00","11:00")],[visit("guide:xh","13:00","15:00")]),req)
        self.assertEqual(len(issues),3)
        self.assertTrue(all(issue["code"] == "user_constraint" and issue["blocking"] for issue in issues))
        issues,_ = constraint_issues(self.plan([visit("guide:xh","14:00","15:00")],[visit("guide:ly","09:00","12:00")]),req)
        self.assertEqual(issues,[])

    def test_meal_break_is_not_counted_as_visit_time(self):
        req = {"constraints":merge([],[draft(kind="visit_duration",minutes=180,scope={"stop_ids":["guide:ly"]},quote="灵隐寺玩3小时")],"form")}
        lunch = {"start":"11:30","end":"12:30"}
        issues,_ = constraint_issues(self.plan([visit("guide:ly","09:00","12:30",meal_break=lunch)],[]),req)
        self.assertEqual(len(issues),1)

    def test_soft_out_of_scope_and_booking_constraints(self):
        req = {"constraints":merge([],[
            draft(kind="fixed_order",strength="soft",before="guide:xh",after="guide:ly",quote="最好先去西湖"),
            draft(kind="visit_duration",minutes=300,scope={"stop_ids":["guide:xh"],"dates":["2026-11-12"]},quote="最后一天西湖玩5小时"),
            draft(kind="booking_prerequisite",scope={"stop_ids":["guide:ly"]},quote="灵隐寺约好了再去")],"form")}
        issues,notes = constraint_issues(self.plan([visit("guide:ly","09:00","10:00")],[visit("guide:xh","09:00","10:00")]),req)
        self.assertEqual([issue["blocking"] for issue in issues],[False])
        self.assertEqual(len(notes),1)

    def test_user_transfer_buffer_only_raises_the_default(self):
        req = {"constraints":merge([],[draft(kind="transfer_buffer",mode="train",minutes=90,quote="火车提前一个半小时")],"form")}
        self.assertEqual((station_buffer(req,"train"),station_buffer(req,"flight"),station_buffer({},"train")),(90,120,60))
        req = {"constraints":merge([],[draft(kind="transfer_buffer",mode="train",minutes=30,quote="火车提前半小时")],"form")}
        self.assertEqual(station_buffer(req,"train"),60)


class WalkingAndTransit(unittest.TestCase):
    def day(self, walked, *activities, date="2026-11-10"):
        return {"days":[{"date":date,"city":"杭州","walking_meters":walked,"activities":list(activities)}]}

    def test_a_walking_distance_tightens_the_daily_cap(self):
        req = {**REQ,"constraints":merge([],[draft(kind="fewer_walking",meters=4000,quote="每天走路别超过4公里")],"form")}
        self.assertEqual((walking_limit(req,"2026-11-10"),walking_target(req,"2026-11-10")),(4000,None))
        self.assertIsNone(walking_limit({**req,"walking_limit_exempt_dates":["2026-11-10"]},"2026-11-10"))

    def test_less_walking_without_a_number_is_a_reported_target(self):
        req = {**REQ,"constraints":merge([],[draft(kind="fewer_walking",quote="少走路")],"form")}
        self.assertEqual((walking_limit(req,"2026-11-10"),walking_target(req,"2026-11-10")),(8000,FEWER_WALKING_METERS))
        issues,_ = constraint_issues(self.day(6200),req)
        self.assertEqual([(issue["code"],issue["blocking"]) for issue in issues],[("user_constraint",False)])
        self.assertEqual(constraint_issues(self.day(4800),req)[0],[])
        self.assertEqual(constraint_issues(self.day(6200),{**req,"walking_limit_exempt_dates":["2026-11-10"]})[0],[])

    def test_public_only_flags_driving_and_disables_taxi_routes_trip_wide(self):
        rule = draft(kind="transit_rule",rule="public_only",quote="只坐公交地铁")
        req = {**REQ,"constraints":merge([],[rule],"form")}
        self.assertTrue(public_only(req))
        driven = visit("guide:ly","10:00","11:00",move_before={"mode":"driving","minutes":12})
        self.assertEqual(len(constraint_issues(self.day(0,driven),req)[0]),1)
        dated = {**REQ,"constraints":merge([],[{**rule,"scope":{**rule["scope"],"dates":["2026-11-11"]}}],"form")}
        self.assertFalse(public_only(dated))
        self.assertEqual(constraint_issues(self.day(0,driven),dated)[0],[])

    def test_stop_without_a_transit_route_is_marked_for_removal(self):
        req = {**REQ,"constraints":merge([],[draft(kind="transit_rule",rule="auto_by_transit",scope={"stop_ids":["guide:ly"]},
                                                     quote="公交到不了就不去灵隐寺")],"form")}
        missing = visit("guide:ly","10:00","11:00",move_before={"mode":"transit","minutes":None})
        [issue] = constraint_issues(self.day(0,missing),req)[0]
        self.assertEqual((issue["target"],issue["repair"],issue["blocking"]),("guide:ly","drop",True))
        reached = visit("guide:ly","10:00","11:00",move_before={"mode":"transit","minutes":25})
        self.assertEqual(constraint_issues(self.day(0,reached),req)[0],[])


class FormExtraction(unittest.IsolatedAsyncioTestCase):
    async def test_extracts_once_per_preferences_text(self):
        models = SimpleNamespace(ask=AsyncMock(return_value=ConstraintExtraction(
            constraints=[ConstraintDraft(kind="fewer_walking",quote="少走路"),
                         ConstraintDraft(kind="fixed_order",before="guide:xh",after="guide:ly",scope={"dates":["2026-11-11"]},
                                         quote="第二天先去西湖再去灵隐寺")],allow_taxi=False)))
        req = await refresh_form_constraints(models,{**REQ,"allow_taxi":True},STOPS)
        self.assertEqual([item["kind"] for item in req["constraints"]],["fewer_walking","fixed_order"])
        self.assertEqual(req["constraints_basis"],preferences_basis(REQ["preferences"]))
        self.assertFalse(req["allow_taxi"])
        self.assertEqual(await refresh_form_constraints(models,req,STOPS),req)
        models.ask.assert_awaited_once()

    async def test_invalid_extraction_is_corrected_once_then_dropped(self):
        bad = ConstraintExtraction(constraints=[ConstraintDraft(kind="fewer_walking",quote="原文没有这句")])
        models = SimpleNamespace(ask=AsyncMock(side_effect=[bad,bad]))
        with self.assertLogs("app.constraints",level="WARNING"):
            req = await refresh_form_constraints(models,REQ,STOPS)
        self.assertEqual(models.ask.await_count,2)
        self.assertIn("validation_errors",models.ask.await_args_list[1].args[2])
        self.assertEqual(req["constraints"],[])

    async def test_edited_text_replaces_only_form_constraints(self):
        kept = merge([],[draft(kind="booking_prerequisite",scope={"stop_ids":["guide:ly"]},quote="灵隐寺约好了")],"decision")
        old_form = merge(kept,[draft(kind="fewer_walking",quote="少走路")],"form")
        models = SimpleNamespace(ask=AsyncMock(return_value=ConstraintExtraction()))
        req = await refresh_form_constraints(models,{**REQ,"preferences":"喜欢园林","constraints":old_form,"constraints_basis":"old"},STOPS)
        self.assertEqual([item["source"] for item in req["constraints"]],["decision"])

    async def test_empty_text_clears_form_constraints_without_a_model_call(self):
        models = SimpleNamespace(ask=AsyncMock())
        old = merge([],[draft(kind="fewer_walking",quote="少走路")],"form")
        req = await refresh_form_constraints(models,{**REQ,"preferences":"","constraints":old,"constraints_basis":"old"},STOPS)
        self.assertEqual((req["constraints"],req["constraints_basis"]),([],""))
        models.ask.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
