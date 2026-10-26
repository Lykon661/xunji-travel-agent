import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from app.agents.runtime import digest
from app.models import REVIEW_DIMENSIONS, Requirements, ReviewReport
from app.planner import Planner
from app.reviewing import incremental_review_payload, merge_incremental_review, review_context, review_payload
from app.rules import check_itinerary


DATES = ["2026-10-26","2026-10-27","2026-10-28"]
REQ = Requirements(origin="广州",cities=["广州"],start_date=DATES[0],end_date=DATES[-1],
                   people=1,rooms=1,budget=3000).model_dump(mode="json")


def plan():
    return {"days":[{"date":day,"city":"广州","activities":[{"id":str(index),"name":"公园"+str(index),
        "kind":"visit","start":"09:00","end":"10:00","evidence":{"quote":"攻略"},
        "map_place":{"id":"poi"+str(index),"location":f"{113+index},23"},"move_before":{}}],
        "meal_recommendations":[],"gaps":[],"return_move":{},"transfers":[]}
        for index,day in enumerate(DATES)],"transport":[],"hotels":[],"issues":[],
        "budget":{"status":"within","unknown":[]}}


def finding(index,severity="blocker",dates=None):
    return {"code":"timing" if severity == "blocker" else "ordinary_notice","severity":severity,
        "owner":"route","action":"repair" if severity == "blocker" else "evidence","target":str(index),
        "dates":dates or [DATES[index]],"field_path":f"/days/{index}/activities/0",
        "message":"合成排程冲突" if severity == "blocker" else "普通营业资料待核实","evidence_ids":["requirements"],"suggestion":""}


def report(findings=()):
    blockers = any(item["severity"] == "blocker" for item in findings)
    return ReviewReport.model_validate({"status":"revise" if blockers else "pass_with_warnings" if findings else "pass",
        "summary":"合成审查结论","checks":[{"dimension":dim,"result":"blocker" if blockers and dim == "timing" else "ok"}
            for dim in sorted(REVIEW_DIMENSIONS)],"findings":list(findings),"question":None})


def state():
    return {"trip_id":"trip","run_id":"run","generation":"g","requirements":copy.deepcopy(REQ),
            "stop_constraints":{},"locks":[],"selected_source_ids":[],"decision_history":[],"stops":{},"mode":"new"}


def previous(current,old,value=None):
    return {"plan":copy.deepcopy(old),"report":(value or report()).model_dump(mode="json"),
            "context":review_context(current,[]),"queries":[]}


class IncrementalPayload(unittest.TestCase):
    def setUp(self):
        self.state = state()
        self.old = plan()
        self.new = copy.deepcopy(self.old)
        self.new["days"][1]["activities"][0]["end"] = "09:45"
        self.previous = previous(self.state,self.old,report([finding(0,"warning"),finding(1)]))

    def test_payload_contains_changed_day_and_cross_day_summary_and_distinct_digest(self):
        payload = incremental_review_payload(self.state,self.new,[],[],self.previous)
        self.assertEqual(payload["review_mode"],"changed_dates")
        self.assertEqual([day["date"] for day in payload["plan"]["days"]],[DATES[1]])
        self.assertEqual([day["date"] for day in payload["unchanged_days"]],[DATES[0],DATES[2]])
        self.assertEqual(len(payload["occurrences"]),3)
        self.assertNotIn("evidence",payload["unchanged_days"][0]["activities"][0])
        self.assertNotEqual(digest(payload),digest(review_payload(self.state,self.new,[],[])))

    def test_merge_reuses_unchanged_findings_and_rebinds_partial_paths(self):
        payload = incremental_review_payload(self.state,self.new,[],[],self.previous)
        current = report([finding(0,"warning",dates=[DATES[1]])]).model_dump(mode="json")
        merged = merge_incremental_review(self.previous,current,payload,self.new)
        self.assertEqual(merged["status"],"pass_with_warnings")
        self.assertEqual([item["field_path"] for item in merged["findings"]],
                         ["/days/0/activities/0","/days/1/activities/0"])
        self.assertEqual(len(merged["checks"]),10)


    def test_global_or_uncertain_changes_require_full_review(self):
        variants = []
        changed = copy.deepcopy(self.state)
        changed["requirements"]["budget"] += 1
        variants.append((changed,self.new,self.previous))
        changed = copy.deepcopy(self.state)
        changed["stop_constraints"] = {"excluded":["0"]}
        variants.append((changed,self.new,self.previous))
        changed = copy.deepcopy(self.new)
        changed["hotels"] = [{"selected_by_user":True,"selected":{"name":"新酒店"}}]
        variants.append((self.state,changed,self.previous))
        changed = copy.deepcopy(self.new)
        changed["days"].pop()
        variants.append((self.state,changed,self.previous))
        variants.append((self.state,self.old,self.previous))
        variants.append((self.state,self.new,None))
        for current,draft,prior in variants:
            with self.subTest(current=current,draft=draft):
                self.assertIsNone(incremental_review_payload(current,draft,[],[],prior))

    def test_evidence_updates_on_untouched_days_require_full_review(self):
        self.old["days"][0]["activities"][0]["map_place"]["evidence_id"] = "query:old"
        self.new["days"][0] = copy.deepcopy(self.old["days"][0])
        self.previous = previous(self.state,self.old)
        self.previous["queries"] = [{"evidence_id":"query:old","result":{"status":"ok"}}]
        queries = [{"evidence_id":"query:old","result":{"status":"error"}}]
        self.assertIsNone(incremental_review_payload(self.state,self.new,[],queries,self.previous))


class IncrementalAudit(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.planner = Planner.__new__(Planner)
        self.planner.emit = Mock()
        self.planner.store = Mock()
        self.planner.store.review_queries.return_value = []
        self.planner.audit_state = lambda value:value
        self.planner.selected_sources = Mock(return_value=[])
        self.planner.models = SimpleNamespace()
        self.planner.reviewer_agent = SimpleNamespace(review=AsyncMock())

    async def test_repair_runs_code_checks_then_reviews_only_changed_date(self):
        value = plan()
        route = {"id":"candidate","days":[{"date":day["date"],"city":day["city"],
            "stop_ids":[item["id"] for item in day["activities"]]} for day in value["days"]]}
        self.planner.amap = SimpleNamespace(batch_state=SimpleNamespace(get=lambda:None))
        self.planner.refresh_expired_resources = AsyncMock(return_value={})
        self.planner.reviewer_agent.review.side_effect = [report([finding(1)]),report()]
        async def checked(current):
            draft = copy.deepcopy(current["itinerary"])
            draft.update(check_itinerary(draft,current["requirements"]))
            return {"itinerary":draft}
        self.planner.check = AsyncMock(side_effect=checked)
        initial = await self.planner.assess_result(state(),route,value)
        current = {**state(),**initial,"coordinator_previous_plan":copy.deepcopy(initial["itinerary"])}
        async def activities(current):
            draft = copy.deepcopy(current["itinerary"])
            draft["days"][1]["activities"][0]["end"] = "09:45"
            return {"itinerary":draft}
        self.planner.activities = AsyncMock(side_effect=activities)
        self.planner.tickets = AsyncMock(return_value={})
        itinerary,items = await self.planner.reassess(current,[DATES[1]])
        self.assertEqual(self.planner.check.await_count,2)
        self.assertEqual(self.planner.reviewer_agent.review.await_count,2)
        first,second = self.planner.reviewer_agent.review.await_args_list
        self.assertNotIn("review_mode",first.args[1])
        self.assertEqual(second.args[1]["review_dates"],[DATES[1]])
        self.assertEqual(len(second.args[1]["plan"]["days"]),1)
        self.assertFalse([item for item in items if item.blocking])
