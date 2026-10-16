"""User-run regressions; all reviews are synthetic and no external model is called."""
import copy
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.adapters import ToolError
from app.agents.reviewer import ReviewerAgent
from app.agents.runtime import digest
from app.models import REVIEW_DIMENSIONS, Requirements, ReviewReport, ReviewReferenceCorrections
from app.reviewing import canonical_review_path, pointer_exists, review_field_index
from app.store import Store


def report_value(path="/days/0/activities/0"):
    return {"status":"revise","summary":"合成审查结论，不得因定位错误而被删除",
            "checks":[{"dimension":key,"result":"ok","note":"合成检查"} for key in sorted(REVIEW_DIMENSIONS)],
            "findings":[{"code":"duration_unknown","severity":"blocker","owner":"route","action":"repair",
                "target":"stop-a","dates":["2026-11-10"],"field_path":path,"message":"合成景点缺少明确游玩时长",
                "evidence_ids":["requirements"],"suggestion":"补充时长依据"}]}


def reference_patch(path="/days/0/activities/0", index=0):
    return ReviewReferenceCorrections(corrections=[{"finding_index":index,"field_path":path,
        "evidence_ids":["requirements"],"dates":["2026-11-10"]}])


class ReviewPointers(unittest.TestCase):
    def test_standard_json_pointer_escaping_and_existing_parent_objects(self):
        plan = {"days":[{"activities":[{"name":"合成景点"}]}],"a/b":{"~name":1},"":{"value":2}}
        for path in ("","/days","/days/0/activities/0","/a~1b/~0name","//value"):
            with self.subTest(path=path):
                self.assertTrue(pointer_exists(plan,path))
        self.assertFalse(pointer_exists(plan,"/days/0/activities/0/duration_minutes"))

    def test_nonstandard_or_out_of_range_array_indices_are_rejected(self):
        plan = {"days":[{"name":"合成日期"}]}
        for path in ("days/0","/plan/days/0","/days/-1","/days/+0","/days/00","/days/1",
                     "/days/-","/days/０","/days/0/name/0","/days/~2"):
            with self.subTest(path=path):
                self.assertFalse(pointer_exists(plan,path))

    def test_index_points_to_actual_days_activities_meals_and_resources(self):
        plan = {"days":[{"date":"2026-11-10","city":"杭州","activities":[{"id":"stop-a","name":"合成景点"}],
                         "meal_recommendations":[{"id":"food-a","name":"合成餐厅"}]}],
                "transport":[{"id":"train-a","label":"合成交通"}],"hotels":[{"id":"hotel-a","city":"杭州"}],
                "a/b~c":{}}
        index = review_field_index(plan)
        self.assertTrue(all(pointer_exists(plan,item["field_path"]) for item in index))
        by_target = {item["target"]:item["field_path"] for item in index}
        self.assertEqual(by_target["stop-a"],"/days/0/activities/0")
        self.assertEqual(by_target["food-a"],"/days/0/meal_recommendations/0")
        self.assertEqual(by_target["train-a"],"/transport/0")
        self.assertEqual(by_target["hotel-a"],"/hotels/0")

    def test_unambiguous_prefix_and_missing_field_use_the_existing_object(self):
        plan = {"days":[{"activities":[{"id":"stop-a"}]}]}
        self.assertEqual(canonical_review_path(plan,"/plan/days/0/activities/0"),"/days/0/activities/0")
        self.assertEqual(canonical_review_path(plan,"/plan/days/0/activities/0/duration_minutes"),"/days/0/activities/0")
        for path in ("/plan/days/8/activities/0","/days/-1","/days/00","/days/stop-a","/unknown","/days/~2"):
            with self.subTest(path=path):
                self.assertEqual(canonical_review_path(plan,path),path)


class ReviewReferenceRecovery(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name))
        req = Requirements(origin="杭州",cities=["杭州"],start_date="2026-11-10",end_date="2026-11-10",
                           people=1,rooms=1,budget=3000).model_dump(mode="json")
        trip = self.store.create_trip("合成审查定位",req)
        run = self.store.start_run(trip["id"],0,"generation",{})
        self.state = {"trip_id":trip["id"],"run_id":run,"generation":"generation"}
        self.payload = {"plan":{"days":[{"date":"2026-11-10","city":"杭州","activities":[{"id":"stop-a","name":"合成景点"}]}],
                                "transport":[],"hotels":[],"budget":{}},"evidence_index":{"requirements":req}}
        self.models = SimpleNamespace(ask=AsyncMock())
        self.agent = ReviewerAgent(self.models,self.store)

    def invalid_key(self, attempt):
        return "review_invalid:"+digest([self.payload,self.state["generation"],self.state["run_id"],attempt])

    async def test_valid_review_is_accepted_without_an_extra_model_call(self):
        value = report_value()
        self.models.ask.return_value = ReviewReport(**value)
        result = await self.agent.review(self.state,self.payload)
        self.assertEqual(result.model_dump(mode="json"),ReviewReport(**value).model_dump(mode="json"))
        self.models.ask.assert_awaited_once()
        self.assertIsNone(self.store.agent_result(self.state["trip_id"],self.invalid_key(0)))

    async def test_invalid_path_uses_a_small_patch_and_preserves_the_entire_report(self):
        invalid = report_value("/days/9/activities/0/duration_minutes")
        corrected = report_value()
        self.models.ask.side_effect = [ReviewReport(**invalid),reference_patch()]
        original_plan = copy.deepcopy(self.payload["plan"])
        with self.assertLogs("app.agents.reviewer",level="WARNING") as logged:
            result = await self.agent.review(self.state,self.payload)
        self.assertEqual(self.models.ask.await_count,2)
        self.assertEqual(result.findings[0].field_path,"/days/0/activities/0")
        self.assertEqual(result.status,"revise")
        self.assertEqual(result.findings[0].severity,"blocker")
        request = self.models.ask.await_args_list[1].args[2]
        self.assertNotIn("plan",request)
        self.assertNotIn("previous_review",request)
        self.assertEqual(self.payload["plan"],original_plan)
        self.assertEqual(request["invalid_findings"][0]["field_path"],invalid["findings"][0]["field_path"])
        self.assertEqual(result.model_dump(mode="json"),ReviewReport(**corrected).model_dump(mode="json"))
        self.assertEqual(request["evidence_catalog"],[{"evidence_id":"requirements"}])
        self.assertEqual(self.models.ask.await_args_list[1].args[0],ReviewReferenceCorrections)
        self.assertEqual(request["validation_errors"][0]["field"],"field_path")
        self.assertTrue(any(item["field_path"] == "/days/0/activities/0" for item in request["field_path_index"]))
        diagnostic = self.store.agent_result(self.state["trip_id"],self.invalid_key(0))
        self.assertEqual(diagnostic["errors"][0]["value"],invalid["findings"][0]["field_path"])
        self.assertIn(invalid["findings"][0]["field_path"],"\n".join(logged.output))
        self.assertNotIn(invalid["summary"],"\n".join(logged.output))

    async def test_prefix_and_missing_field_do_not_need_an_extra_model_request(self):
        value = report_value("/plan/days/0/activities/0/duration_minutes")
        original = ReviewReport(**value)
        self.models.ask.return_value = original
        result = await self.agent.review(self.state,self.payload)
        self.models.ask.assert_awaited_once()
        self.assertEqual(original.findings[0].field_path,value["findings"][0]["field_path"])
        expected = copy.deepcopy(value)
        expected["findings"][0]["field_path"] = "/days/0/activities/0"
        self.assertEqual(result.model_dump(mode="json"),ReviewReport(**expected).model_dump(mode="json"))

    async def test_evidence_and_date_errors_are_reported_together_and_revalidated(self):
        invalid = report_value()
        invalid["findings"][0].update(evidence_ids=["query:foreign"],dates=["2026-11-20"])
        self.models.ask.side_effect = [ReviewReport(**invalid),reference_patch()]
        result = await self.agent.review(self.state,self.payload)
        feedback = self.models.ask.await_args_list[1].args[2]["validation_errors"]
        self.assertEqual({item["field"] for item in feedback},{"evidence_ids","dates"})
        self.assertEqual(result.findings[0].evidence_ids,["requirements"])
        self.assertEqual(result.findings[0].dates[0].isoformat(),"2026-11-10")

    async def test_cross_day_correction_includes_the_bound_day_and_keeps_the_other_day(self):
        self.payload["plan"]["days"].append({"date":"2026-11-11","activities":[]})
        invalid = report_value()
        invalid["findings"][0]["dates"] = ["2026-11-11"]
        self.models.ask.side_effect = [ReviewReport(**invalid),reference_patch()]
        result = await self.agent.review(self.state,self.payload)
        self.assertEqual({day.isoformat() for day in result.findings[0].dates},{"2026-11-10","2026-11-11"})
        self.assertEqual(self.models.ask.await_args_list[1].args[2]["validation_errors"][0]["field"],"dates")

    async def test_two_invalid_results_remain_unavailable_and_keep_both_diagnostics(self):
        value = report_value("/days/9/activities/0")
        self.models.ask.side_effect = [ReviewReport(**value),reference_patch("/days/9/activities/0")]
        with self.assertRaises(ToolError) as raised:
            await self.agent.review(self.state,self.payload)
        self.assertEqual(self.models.ask.await_count,2)
        self.assertEqual(raised.exception.code,"review_evidence")
        self.assertIn("/days/9/activities/0",str(raised.exception))
        for attempt in (0,1):
            self.assertIsNotNone(self.store.agent_result(self.state["trip_id"],self.invalid_key(attempt)))
        accepted_key = "review:"+digest([self.payload,self.state["generation"]])
        self.assertIsNone(self.store.agent_result(self.state["trip_id"],accepted_key))

    async def test_correction_cannot_drop_a_blocker_and_claim_a_pass(self):
        invalid = report_value("/days/9")
        bypass = report_value()
        bypass.update(status="pass",findings=[])
        self.models.ask.side_effect = [ReviewReport(**invalid),ReviewReport(**bypass)]
        with self.assertRaises(ToolError) as raised:
            await self.agent.review(self.state,self.payload)
        self.assertEqual(raised.exception.code,"review_correction")
        self.assertEqual(self.models.ask.await_count,2)

    async def test_correction_cannot_lower_the_existing_finding_severity(self):
        invalid = report_value("/days/9")
        bypass = report_value()
        bypass.update(status="pass_with_warnings")
        bypass["findings"][0]["severity"] = "warning"
        self.models.ask.side_effect = [ReviewReport(**invalid),ReviewReport(**bypass)]
        with self.assertRaises(ToolError) as raised:
            await self.agent.review(self.state,self.payload)
        self.assertEqual(raised.exception.code,"review_correction")

    async def test_network_failure_does_not_trigger_reference_correction(self):
        self.models.ask.side_effect = ToolError("model_network","合成网络失败")
        with self.assertRaises(ToolError) as raised:
            await self.agent.review(self.state,self.payload)
        self.assertEqual(raised.exception.code,"model_network")
        self.models.ask.assert_awaited_once()

    async def test_patch_cannot_edit_a_different_finding_or_omit_a_required_fix(self):
        self.models.ask.side_effect = [ReviewReport(**report_value("/days/9")),reference_patch(index=1)]
        with self.assertRaises(ToolError) as raised:
            await self.agent.review(self.state,self.payload)
        self.assertEqual(raised.exception.code,"review_correction")

    async def test_patch_only_updates_invalid_fields_and_keeps_other_findings(self):
        invalid = report_value("/days/9")
        invalid["findings"].append({**report_value()["findings"][0],"code":"another_blocker"})
        patch = reference_patch()
        patch.corrections[0].dates = []
        self.models.ask.side_effect = [ReviewReport(**invalid),patch]
        result = await self.agent.review(self.state,self.payload)
        expected = copy.deepcopy(invalid)
        expected["findings"][0]["field_path"] = "/days/0/activities/0"
        self.assertEqual(result.model_dump(mode="json"),ReviewReport(**expected).model_dump(mode="json"))
        request = self.models.ask.await_args_list[1].args[2]
        self.assertEqual(len(request["invalid_findings"]),1)

    async def test_patch_retains_valid_members_of_partially_invalid_lists(self):
        self.payload["evidence_index"]["query:local"] = {"request":{"date":"2026-11-10"}}
        self.payload["plan"]["days"].append({"date":"2026-11-11","activities":[]})
        invalid = report_value()
        invalid["findings"][0].update(evidence_ids=["query:local","query:foreign"],dates=["2026-11-10","2026-11-20"])
        patch = reference_patch()
        patch.corrections[0].dates = [date(2026,11,11)]
        self.models.ask.side_effect = [ReviewReport(**invalid),patch]
        result = await self.agent.review(self.state,self.payload)
        self.assertEqual(result.findings[0].evidence_ids,["query:local","requirements"])
        self.assertEqual([day.isoformat() for day in result.findings[0].dates],["2026-11-10","2026-11-11"])
