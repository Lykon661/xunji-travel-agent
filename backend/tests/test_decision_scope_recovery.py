"""User-run synthetic decision regressions. No external model or provider calls."""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app.adapters import ToolError
from app.agents.runtime import digest
from app.models import DECISION_REQUIREMENT_FIELDS, ConstraintDraft, DecisionResolution, Requirements
from app.planner import Planner


class DecisionScopeRecovery(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.planner = Planner.__new__(Planner)
        self.planner.emit = Mock()
        self.planner.models = SimpleNamespace(ask=AsyncMock())
        requirements = Requirements(origin="广州",cities=["广州"],start_date="2026-10-16",end_date="2026-10-17",
            people=1,rooms=1,budget=3000,preferences="公共交通，不打车").model_dump(mode="json")
        self.state = {"trip_id":"synthetic","run_id":"run","generation":"generation","draft_revision":0,
            "requirements":requirements,"decision_history":[],"decision_resume":"guide_analysis","locks":[],
            "decision":{"id":"transit","question":"是否允许根据真实公交查询结果决定保留或删除景点？",
                "why_needed":"确认取舍授权","affected_dates":["2026-10-16"]},
            "stops":{"guide:dsk":{"id":"guide:dsk","name":"东山口","city":"广州","category":"sight"},
                     "guide:zj":{"id":"guide:zj","name":"正佳广场","city":"广州","category":"sight"},
                     "guide:food":{"id":"guide:food","name":"合成餐厅","city":"广州","category":"dining"}},
            "candidates":[{"id":"draft","days":[{"date":"2026-10-16","city":"广州","stop_ids":["guide:dsk","guide:zj"]}]}]}
        self.answer = "允许按实时公共交通结果自动判断并取舍"

    async def resolve(self):
        state = self.state
        token = digest({"question":state["decision"],"history":state["decision_history"],
                        "generation":state["generation"],"revision":state["draft_revision"]})[:40]
        reply = {"round_id":token,"draft_revision":state["draft_revision"],
                 "answers":{state["decision"]["id"]:self.answer},"note":""}
        with patch("app.planner.interrupt",return_value=reply):
            return await self.planner.decision_gate(state)

    async def test_allowed_scope_is_explicit_and_valid_resolution_does_not_retry(self):
        self.planner.models.ask.return_value = DecisionResolution(explanation="继续按原约束规划")
        result = await self.resolve()
        self.planner.models.ask.assert_awaited_once()
        payload = self.planner.models.ask.await_args.args[2]
        self.assertEqual(set(payload["allowed_requirement_fields"]),DECISION_REQUIREMENT_FIELDS)
        self.assertEqual(set(payload["allowed_stop_ids"]),{"guide:dsk","guide:zj"})
        self.assertEqual(set(payload["available_stops"]),set(payload["allowed_stop_ids"]))
        self.assertEqual(result["requirements"],self.state["requirements"])
        schema = DecisionResolution.model_json_schema()["properties"]["requirements"]
        self.assertEqual(set(schema["propertyNames"]["enum"]),DECISION_REQUIREMENT_FIELDS)

    async def test_custom_policy_field_is_corrected_to_a_preference_without_excluding_stops(self):
        private = "PRIVATE_MODEL_FIELD_VALUE_NOT_FOR_LOGS"
        preferences = "公共交通，不打车；10月16日按真实公交查询结果自动判断东山口和正佳广场的取舍，查询前不预先删除。"
        self.planner.models.ask.side_effect = [
            DecisionResolution(requirements={"transit_decision_policy":private}),
            DecisionResolution(requirements={"preferences":preferences},explanation="已记录条件式取舍")]
        original = copy.deepcopy(self.state)
        with self.assertLogs("app.planner",level="WARNING") as captured:
            result = await self.resolve()
        self.assertEqual(self.planner.models.ask.await_count,2)
        self.assertEqual(result["requirements"]["preferences"],preferences)
        for field in ("budget","cities","start_date","end_date","people","max_walk_per_day"):
            self.assertEqual(result["requirements"][field],original["requirements"][field])
        self.assertEqual(result["stop_constraints"],{"required":[],"excluded":[]})
        self.assertEqual(result["candidates"],original["candidates"])
        self.assertEqual(self.state,original)
        correction = self.planner.models.ask.await_args_list[1].args[2]
        self.assertEqual(correction["validation_errors"],[{"field":"requirements","unknown_fields":["transit_decision_policy"]}])
        self.assertEqual(correction["answer"],{"transit":self.answer})
        self.assertIn("transit_decision_policy","\n".join(captured.output))
        self.assertNotIn(private,"\n".join(captured.output))
        self.assertNotIn(self.answer,"\n".join(captured.output))
        self.assertEqual(result["decision_history"][-1]["answers"],{"transit":self.answer})

    async def test_conditional_transit_choice_becomes_a_dated_constraint(self):
        rule = ConstraintDraft(kind="transit_rule",rule="auto_by_transit",quote="按实时公共交通结果自动判断",
                               scope={"dates":["2026-10-16"],"stop_ids":["guide:dsk","guide:zj"]})
        self.planner.models.ask.return_value = DecisionResolution(constraints=[rule],explanation="已记录条件式取舍")
        result = await self.resolve()
        self.planner.models.ask.assert_awaited_once()
        [stored] = result["requirements"]["constraints"]
        self.assertEqual((stored["kind"],stored["rule"],stored["source"]),("transit_rule","auto_by_transit","decision"))
        self.assertEqual(stored["scope"]["dates"],["2026-10-16"])
        self.assertEqual(result["requirements"]["preferences"],self.state["requirements"]["preferences"])

    async def test_constraint_quote_outside_the_answer_is_corrected_once(self):
        invented = ConstraintDraft(kind="fewer_walking",quote="用户说过少走路")
        self.planner.models.ask.side_effect = [DecisionResolution(constraints=[invented]),
                                               DecisionResolution(explanation="按回答继续")]
        result = await self.resolve()
        self.assertEqual(self.planner.models.ask.await_count,2)
        issues = self.planner.models.ask.await_args_list[1].args[2]["validation_errors"]
        self.assertEqual([item["field"] for item in issues],["constraints"])
        self.assertEqual(result["requirements"]["constraints"],[])

    async def test_invalid_requirement_value_is_corrected_instead_of_crashing_the_run(self):
        self.answer = "返程最晚下午3点到家"
        self.planner.models.ask.side_effect = [
            DecisionResolution(requirements={"latest_return":"2026-10-17 15:00"}),
            DecisionResolution(requirements={"latest_return":"15:00"},explanation="最晚15:00返回")]
        result = await self.resolve()
        self.assertEqual(self.planner.models.ask.await_count,2)
        self.assertEqual(result["requirements"]["latest_return"],"15:00")
        errors = self.planner.models.ask.await_args_list[1].args[2]["validation_errors"]
        self.assertEqual(errors[0]["field"],"requirements")
        self.assertIn("latest_return",errors[0]["invalid_values"][0])

    async def test_unknown_ids_and_conflicting_marks_are_corrected_together(self):
        self.answer = "东山口必去，正佳广场不去"
        self.planner.models.ask.side_effect = [
            DecisionResolution(required_ids=["不存在的景点","guide:zj"],excluded_ids=["guide:zj"]),
            DecisionResolution(required_ids=["guide:dsk"],excluded_ids=["guide:zj"])]
        result = await self.resolve()
        issues = self.planner.models.ask.await_args_list[1].args[2]["validation_errors"]
        self.assertEqual({item["field"] for item in issues},{"required_ids","required_ids/excluded_ids"})
        self.assertEqual(result["stop_constraints"],{"required":["guide:dsk"],"excluded":["guide:zj"]})

    async def test_second_invalid_result_stops_and_reports_scope_without_mutating_the_draft(self):
        self.planner.models.ask.return_value = DecisionResolution(requirements={"cities":["北京"]},
            required_ids=["unknown-stop","guide:dsk"],excluded_ids=["guide:dsk"])
        original = copy.deepcopy(self.state)
        with self.assertRaises(ToolError) as caught:
            await self.resolve()
        self.assertEqual(caught.exception.code,"decision_resolution")
        self.assertIn("自动纠正一次",str(caught.exception))
        for detail in ("cities","unknown-stop","required_ids/excluded_ids"):
            self.assertIn(detail,str(caught.exception))
        self.assertEqual(self.planner.models.ask.await_count,2)
        self.assertEqual(self.state,original)

    async def test_network_failure_does_not_trigger_a_scope_retry(self):
        self.planner.models.ask.side_effect = ToolError("model_network","合成网络失败")
        with self.assertRaises(ToolError) as caught:
            await self.resolve()
        self.assertEqual(caught.exception.code,"model_network")
        self.planner.models.ask.assert_awaited_once()

    async def test_corrected_scope_does_not_authorize_an_invented_budget_or_walk_limit(self):
        self.planner.models.ask.side_effect = [DecisionResolution(requirements={"custom_policy":"自动判断"}),
            DecisionResolution(requirements={"budget":9000,"max_walk_per_day":20000})]
        result = await self.resolve()
        self.assertEqual(result["requirements"]["budget"],self.state["requirements"]["budget"])
        self.assertEqual(result["requirements"]["max_walk_per_day"],self.state["requirements"]["max_walk_per_day"])
