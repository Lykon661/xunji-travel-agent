"""Synthetic report/correction cases for the user to run; no live providers."""
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from pydantic import ValidationError

from app.adapters import Models, ToolError
from app.models import REVIEW_DIMENSIONS, ReviewReport
from app.store import Conflict


PRIVATE = "PRIVATE_GUIDE_CONTENT_NOT_FOR_LOGS"


def report():
    return {"status":"pass","summary":PRIVATE,"findings":[],
        "checks":[{"dimension":dimension,"result":"ok","note":"合成检查"} for dimension in sorted(REVIEW_DIMENSIONS)]}


def blocker():
    return {"code":"timing","severity":"blocker","owner":"route","action":"repair","target":"visit",
        "dates":["2026-11-10"],"field_path":"/days/0/activities/0","message":PRIVATE,
        "evidence_ids":["requirements"],"suggestion":"调整时间"}


class ReviewFormatDiagnostics(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.models = Models.__new__(Models)
        self.models.config_store = SimpleNamespace(snapshot=Mock(return_value=SimpleNamespace(configured=True)))
        self.messages = []

    def client(self,values):
        responses = iter(values)
        def complete(messages,**kwargs):
            self.messages.append(copy.deepcopy(messages))
            return json.dumps(next(responses),ensure_ascii=False)
        self.models.client = SimpleNamespace(complete=Mock(side_effect=complete))

    def test_each_root_constraint_has_a_specific_safe_diagnostic(self):
        dimensions = report()
        dimensions["checks"][-1]["dimension"] = dimensions["checks"][0]["dimension"]
        blocked_pass = {**report(),"findings":[blocker()]}
        warning_pass = {**report(),"findings":[{**blocker(),"severity":"warning"}]}
        missing_blocker = {**report(),"status":"revise"}
        for value,code in ((dimensions,"review_dimensions"),(blocked_pass,"review_blocker_status"),
                (warning_pass,"review_pass_warnings"),(missing_blocker,"review_missing_blocker")):
            with self.subTest(code=code):
                with self.assertRaises(ValidationError) as raised:
                    ReviewReport.model_validate(value)
                self.assertEqual(raised.exception.errors()[0]["type"],code)
                detail = Models.format_issue(raised.exception)
                self.assertIn("审查报告",detail)
                self.assertNotIn("不符合字段约束",detail)
                self.assertNotIn(PRIVATE,detail)
