"""Synthetic decision answers and mocked models; never access production databases."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app.adapters import ToolError
from app.agents.runtime import digest
from app.models import ConstraintDraft, DecisionResolution, Requirements, wish_stop_id, wish_stops
from app.planner import Planner


DAY = "2026-11-10"
MUSEUM = "534771bbbe2040d39319df28163d38e2:3880012b259545df"
EAST, ZOO = "guide:east", "guide:zoo"


class DecisionExclusionGuard(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.planner = Planner.__new__(Planner)
        self.planner.emit = Mock()
        self.planner.models = SimpleNamespace(ask=AsyncMock())
        req = Requirements(origin="北京",cities=["广州"],start_date=DAY,end_date=DAY,people=1,rooms=1,budget=4000,
                           required_stop_ids=[MUSEUM]).model_dump(mode="json")
        stops = {identity:{"id":identity,"name":name,"city":"广州","category":"sight","source_kind":"guide"}
                 for identity,name in ((MUSEUM,"广东省博物馆"),(EAST,"东山口"),(ZOO,"广州动物园"))}
        stops[MUSEUM].update(alias_names=["博物馆"],map_place={"id":"museum-poi","name":"广东省博物馆","location":"113.32,23.11"})
        self.state = {"trip_id":"synthetic","run_id":"run","generation":"generation","draft_revision":1,
                      "requirements":req,"stops":stops,"decision_history":[],"decision_resume":"guide_analysis",
                      "locks":[],"stop_constraints":{"required":[MUSEUM],"excluded":[]},
                      "decision":{"id":"keep-museum","question":"请确认广东省博物馆是否应保留？","why_needed":"确认景点取舍","affected_dates":[DAY]},
                      "candidates":[{"id":"candidate","days":[{"date":DAY,"city":"广州","stop_ids":[MUSEUM,EAST,ZOO]}]}]}

    async def resolve(self, answer, note=""):
        current = self.state
        token = digest({"question":current["decision"],"history":current["decision_history"],
                        "generation":current["generation"],"revision":current["draft_revision"]})[:40]
        reply = {"round_id":token,"draft_revision":current["draft_revision"],"answers":{current["decision"]["id"]:answer},"note":note}
        with patch("app.planner.interrupt",return_value=reply):
            return await self.planner.decision_gate(current)

    def twice(self, **values):
        self.planner.models.ask.side_effect = [DecisionResolution(**values),DecisionResolution(**values)]

    def summary(self, result):
        return result["decision_history"][-1]["summary"]

    async def test_keep_museum_rejects_repeated_model_exclusion_without_failing(self):
        self.state["requirements"]["excluded_stop_ids"] = [EAST]
        self.state["stop_constraints"]["excluded"] = [EAST]
        original = deepcopy(self.state)
        self.twice(excluded_ids=[MUSEUM],explanation="确认保留广东省博物馆，撤销此前对该景点的排除")
        result = await self.resolve("保留广东省博物馆")
        self.assertEqual(self.planner.models.ask.await_count,2)
        self.assertEqual(result["stop_constraints"],{"required":[MUSEUM],"excluded":[EAST]})
        self.assertNotIn(MUSEUM,result["requirements"]["excluded_stop_ids"])
        self.assertIn("回答中没有删除意图，未将广东省博物馆设为排除",self.summary(result))
        self.assertEqual(result["candidates"],original["candidates"])
        self.assertEqual(self.state,original)
        correction = self.planner.models.ask.await_args_list[1].args[2]
        self.assertIn({"field":"excluded_ids","unauthorized_ids":[MUSEUM]},correction["validation_errors"])

    async def test_corrected_second_answer_is_used(self):
        self.state["requirements"]["required_stop_ids"] = []
        self.state["stop_constraints"] = {"required":[],"excluded":[MUSEUM]}
        self.state["requirements"]["excluded_stop_ids"] = [MUSEUM]
        self.planner.models.ask.side_effect = [DecisionResolution(excluded_ids=[MUSEUM]),
                                               DecisionResolution(required_ids=[MUSEUM],explanation="保留广东省博物馆")]
        result = await self.resolve("保留广东省博物馆")
        self.assertEqual(result["stop_constraints"],{"required":[MUSEUM],"excluded":[]})
        self.assertEqual(self.summary(result),"保留广东省博物馆")

    async def test_explicit_removal_and_replacement_are_allowed(self):
        self.planner.models.ask.return_value = DecisionResolution(excluded_ids=[EAST],required_ids=[ZOO])
        result = await self.resolve("删除东山口，改为广州动物园")
        self.planner.models.ask.assert_awaited_once()
        self.assertEqual(result["stop_constraints"]["excluded"],[EAST])
        self.assertIn(ZOO,result["stop_constraints"]["required"])

    async def test_keep_target_is_protected_despite_other_deletion_words(self):
        self.twice(excluded_ids=[EAST,MUSEUM])
        result = await self.resolve("删除东山口，保留博物馆")
        self.assertEqual(result["stop_constraints"]["excluded"],[EAST])
        self.assertIn(MUSEUM,result["stop_constraints"]["required"])
        self.assertIn("未将广东省博物馆设为排除",self.summary(result))

    async def test_answer_and_note_are_both_checked(self):
        self.twice(excluded_ids=[EAST,MUSEUM])
        result = await self.resolve("删除东山口",note="保留广东省博物馆")
        self.assertEqual(result["stop_constraints"]["excluded"],[EAST])

    async def test_negated_deletion_does_not_authorize_exclusion(self):
        for answer in ("不删除东山口","不要删东山口","不删掉东山口","别删除东山口","不能移除东山口","不要排除东山口"):
            with self.subTest(answer=answer):
                self.twice(excluded_ids=[EAST])
                result = await self.resolve(answer)
                self.assertNotIn(EAST,result["stop_constraints"]["excluded"])
                self.assertIn("回答中没有删除意图",self.summary(result))

    async def test_keep_phrases_and_postfix_keep_are_protected(self):
        for answer in ("留下广东省博物馆","继续安排广东省博物馆","广东省博物馆要去","广东省博物馆必去",
                       "删除东山口，不要删广东省博物馆"):
            with self.subTest(answer=answer):
                self.twice(excluded_ids=[MUSEUM])
                result = await self.resolve(answer)
                self.assertNotIn(MUSEUM,result["stop_constraints"]["excluded"])

    async def test_canonical_alias_name_protects_the_original_stop(self):
        self.state["stops"]["guide:museum-alias"] = {**self.state["stops"][MUSEUM],"id":"guide:museum-alias","name":"广东博物馆","alias_names":[]}
        self.twice(excluded_ids=[MUSEUM])
        result = await self.resolve("删除东山口，保留广东博物馆")
        self.assertNotIn(MUSEUM,result["stop_constraints"]["excluded"])

    async def test_explicit_removal_rejects_new_required_mark(self):
        self.twice(required_ids=[EAST])
        result = await self.resolve("移除东山口")
        self.assertEqual(self.planner.models.ask.await_count,2)
        self.assertNotIn(EAST,result["stop_constraints"]["required"])
        self.assertIn("未将其设为必去",self.summary(result))
        correction = self.planner.models.ask.await_args_list[1].args[2]
        self.assertIn({"field":"required_ids","unauthorized_ids":[EAST]},correction["validation_errors"])

    async def test_postfix_removal_also_rejects_new_required_mark(self):
        self.twice(required_ids=[EAST])
        result = await self.resolve("东山口不去")
        self.assertNotIn(EAST,result["stop_constraints"]["required"])

    async def test_replacement_destination_is_not_excluded(self):
        self.twice(excluded_ids=[EAST,ZOO],required_ids=[ZOO])
        result = await self.resolve("东山口改为广州动物园")
        self.assertEqual(result["stop_constraints"]["excluded"],[EAST])
        self.assertIn(ZOO,result["stop_constraints"]["required"])

    async def test_substitution_keeps_first_place_and_removes_second(self):
        for verb in ("代替","替代"):
            with self.subTest(verb=verb):
                self.twice(excluded_ids=[EAST,ZOO],required_ids=[ZOO])
                result = await self.resolve("用广州动物园"+verb+"东山口")
                self.assertEqual(result["stop_constraints"]["excluded"],[EAST])
                self.assertIn(ZOO,result["stop_constraints"]["required"])
                self.assertIn("未将广州动物园设为排除",self.summary(result))

    async def test_substitution_rejects_required_mark_for_replaced_place(self):
        self.twice(required_ids=[EAST,ZOO])
        result = await self.resolve("用广州动物园代替东山口")
        self.assertNotIn(EAST,result["stop_constraints"]["required"])
        self.assertIn(ZOO,result["stop_constraints"]["required"])

    async def test_colloquial_removals_are_allowed(self):
        for answer, identity in (("东山口不想去了",EAST),("不考虑广东省博物馆",MUSEUM),
                                 ("不需要东山口",EAST),("东山口不用了",EAST),("广东省博物馆算了",MUSEUM)):
            with self.subTest(answer=answer):
                self.planner.models.ask.reset_mock()
                self.planner.models.ask.return_value = DecisionResolution(excluded_ids=[identity])
                result = await self.resolve(answer)
                self.planner.models.ask.assert_awaited_once()
                self.assertEqual(result["stop_constraints"]["excluded"],[identity])
                self.assertNotIn(identity,result["stop_constraints"]["required"])

    async def test_negated_substitution_cannot_exclude_target(self):
        for answer in ("不要替代东山口","不要代替东山口","不用替代东山口","不用代替东山口"):
            with self.subTest(answer=answer):
                self.twice(excluded_ids=[EAST])
                result = await self.resolve(answer)
                self.assertNotIn(EAST,result["stop_constraints"]["excluded"])
                self.assertIn("回答中没有删除意图",self.summary(result))

    async def test_give_up_is_limited_to_adjacent_place_in_same_clause(self):
        self.twice(excluded_ids=[EAST,ZOO])
        result = await self.resolve("东山口算了")
        self.assertEqual(result["stop_constraints"]["excluded"],[EAST])
        for answer in ("算了，按原方案继续","东山口，算了","东山口的门票算了"):
            with self.subTest(answer=answer):
                self.twice(excluded_ids=[EAST])
                result = await self.resolve(answer)
                self.assertNotIn(EAST,result["stop_constraints"]["excluded"])

    async def test_delete_verbs_allow_removal(self):
        for verb in ("删除","删掉","去掉","移除","排除","不去","不要","不安排","不再安排","取消","放弃","跳过","换掉","替换"):
            with self.subTest(verb=verb):
                self.planner.models.ask.side_effect = None
                self.planner.models.ask.return_value = DecisionResolution(excluded_ids=[EAST])
                result = await self.resolve(verb+"东山口")
                self.assertEqual(result["stop_constraints"]["excluded"],[EAST])
        for verb in ("改为","改成","换成"):
            with self.subTest(verb=verb):
                self.planner.models.ask.return_value = DecisionResolution(excluded_ids=[EAST],required_ids=[ZOO])
                result = await self.resolve("东山口"+verb+"广州动物园")
                self.assertEqual(result["stop_constraints"]["excluded"],[EAST])
                self.assertIn(ZOO,result["stop_constraints"]["required"])

    async def test_negated_keep_is_a_removal(self):
        self.planner.models.ask.return_value = DecisionResolution(excluded_ids=[EAST])
        result = await self.resolve("不要保留东山口")
        self.planner.models.ask.assert_awaited_once()
        self.assertEqual(result["stop_constraints"]["excluded"],[EAST])

    async def test_neutral_answer_cannot_authorize_new_exclusions(self):
        self.twice(excluded_ids=[EAST])
        result = await self.resolve("按原方案继续")
        self.assertNotIn(EAST,result["stop_constraints"]["excluded"])

    async def test_authorized_wish_removal_still_updates_wishes_and_stops(self):
        wish = {"city":"广州","name":"广州塔"}
        identity = wish_stop_id(wish["city"],wish["name"])
        req = Requirements(**{**self.state["requirements"],"wish_places":[wish]}).model_dump(mode="json")
        self.state["requirements"] = req
        self.state["stops"].update(wish_stops(req))
        self.state["stop_constraints"]["required"].append(identity)
        self.state["candidates"][0]["days"][0]["stop_ids"].append(identity)
        self.planner.models.ask.return_value = DecisionResolution(excluded_ids=[identity])
        result = await self.resolve("移除广州塔")
        self.planner.models.ask.assert_awaited_once()
        self.assertEqual(result["requirements"]["wish_places"],[])
        self.assertNotIn(identity,result["stops"])
        self.assertNotIn(identity,result["stop_constraints"]["excluded"])
        self.assertNotIn(identity,result["stop_constraints"]["required"])
        self.assertNotIn(identity,result["candidates"][0]["days"][0]["stop_ids"])

    async def test_coded_options_bypass_model_intent_guard(self):
        self.state["decision_options"] = {"question_id":"keep-museum","options":{"采用方案一":{"type":"exclude","stop_id":EAST,"date":DAY}}}
        result = await self.resolve("采用方案一")
        self.planner.models.ask.assert_not_awaited()
        self.assertEqual(result["stop_constraints"]["excluded"],[EAST])

    async def test_unauthorized_marks_and_unquoted_constraints_are_dropped_together(self):
        invented = ConstraintDraft(kind="fewer_walking",quote="不存在的用户原话")
        self.twice(excluded_ids=[MUSEUM],constraints=[invented])
        result = await self.resolve("保留广东省博物馆")
        self.assertNotIn(MUSEUM,result["stop_constraints"]["excluded"])
        self.assertEqual(result["requirements"]["constraints"],[])
        self.assertIn("没有删除意图",self.summary(result))
        self.assertIn("无法对应到你回答的原话",self.summary(result))

    async def test_unrelated_scope_errors_still_fail_without_mutating_state(self):
        self.twice(required_ids=["unknown"],excluded_ids=[MUSEUM])
        original = deepcopy(self.state)
        with self.assertRaises(ToolError):
            await self.resolve("保留广东省博物馆")
        self.assertEqual(self.state,original)


if __name__ == "__main__":
    unittest.main()
