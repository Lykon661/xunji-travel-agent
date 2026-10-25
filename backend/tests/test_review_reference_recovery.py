"""Review reference recovery with synthetic reports."""
import copy
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.adapters import ToolError
from app.agents.reviewer import ReviewerAgent
from app.agents.runtime import agent_payload, digest
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
