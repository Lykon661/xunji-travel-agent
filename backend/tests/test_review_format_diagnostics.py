"""Synthetic report/correction cases for the user to run; no live providers."""
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from pydantic import ValidationError

from app.adapters import Models, ToolError
from app.models import REVIEW_DIMENSIONS, ReviewReport
from app.reviewing import require_review, review_blockers
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

    async def test_correction_gets_exact_rule_and_retains_the_blocker(self):
        invalid = {**report(),"findings":[blocker()]}
        corrected = {**copy.deepcopy(invalid),"status":"revise"}
        self.client([invalid,corrected])
        with self.assertLogs("app.adapters",level="WARNING") as captured:
            result = await self.models.ask(ReviewReport,"独立审查",{})
        self.assertEqual(result.status,"revise")
        self.assertEqual(result.findings[0].severity,"blocker")
        self.assertIn("status不能为pass",self.messages[1][-1]["content"])
        logs = "\n".join(captured.output)
        self.assertIn("review_blocker_status",logs)
        self.assertNotIn(PRIVATE,logs)

    async def test_structure_retry_cannot_remove_blocker_and_claim_pass(self):
        self.client([{**report(),"findings":[blocker()]},report()])
        with self.assertRaises(ToolError) as raised:
            await self.models.ask(ReviewReport,"独立审查",{})
        self.assertEqual(raised.exception.code,"model_format")
        self.assertIn("不能删除或改写原有阻断",str(raised.exception))
        self.assertEqual(len(self.messages),2)

    async def test_warning_only_report_corrects_status_without_inventing_a_route_blocker(self):
        invalid = {**report(),"status":"revise","findings":[{**blocker(),"severity":"warning"}]}
        invalid["checks"][0].update(result="warning",note="参考价格待确认")
        corrected = {**copy.deepcopy(invalid),"status":"pass_with_warnings"}
        self.client([invalid,corrected])
        result = await self.models.ask(ReviewReport,"独立审查",{})
        self.assertEqual(result.status,"pass_with_warnings")
        self.assertFalse(any(item.severity == "blocker" for item in result.findings))
        correction = self.messages[1][-1]["content"]
        self.assertIn("当前没有原有阻断发现可保留",correction)
        self.assertIn("不能新增要求路线重排的假问题",correction)

    def test_unavailable_review_does_not_claim_binding_changes_but_still_blocks_confirmation(self):
        unavailable = {"status":"review_unavailable","summary":"独立检验未完成：报告格式未通过",
            "checks":[],"policy_version":-1,"plan_hash":"different","review_basis_hash":"different",
            "findings":[blocker()]}
        blockers = review_blockers(unavailable,{}, {},[],[])
        self.assertEqual(blockers,[unavailable["summary"]])
        with self.assertRaises(Conflict):
            require_review(unavailable,{}, {},[],[])
