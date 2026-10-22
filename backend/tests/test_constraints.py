"""Synthetic constraint extraction and lookup; the model is mocked and nothing external is called."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pydantic import ValidationError

from app.constraints import active, check, drop_source, merge, preferences_basis, refresh_form_constraints
from app.models import ConstraintDraft, ConstraintExtraction, Requirements


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
