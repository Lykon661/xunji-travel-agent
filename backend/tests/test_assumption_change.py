import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from app import assumptions, authority
from app.main import app
from app.planner import Planner
from app.store import Conflict
from tests import test_coordinator_graph as graph_cases
from tests.test_actions import DAY
from tests.test_incremental_review import report
from tests.test_system_assumptions import assumption


class AssumptionChange(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = graph_cases.CoordinatorGraph()
        self.fixture.addCleanup = self.addCleanup
        self.fixture.setUp()
        self.fixture.initial["candidates"][0]["title"] = "完整候选"
        self.fixture.initial["candidates"][0]["reason"] = "合成攻略安排"
        for stop in self.fixture.initial["stops"].values():
            stop["source_title"] = "合成攻略"
        self.fixture.initial.update(assumptions.record(self.fixture.initial,assumption()))
        self.planner,self.store = self.fixture.planner,self.fixture.store
        self.planner.after_coordinate = Planner.after_coordinate.__get__(self.planner)

    async def ready(self):
        initial,_,run_id = self.fixture.context()
        await self.planner.execute(run_id,initial)
        self.assertEqual(self.store.run(run_id)["status"],"waiting")
        self.planner.audit = Planner.audit.__get__(self.planner)
        self.planner.reviewer_agent = SimpleNamespace(review=AsyncMock(return_value=report()))
        return self.store.trip(self.fixture.trip["id"])

    def body(self,trip):
        return {"alternative_id":"short","assumption_revision":1,"draft_revision":trip["document"]["draft_revision"],
                "base_version":trip["version"]}

    async def request(self,trip,body,identity="duration:a"):
        with patch.object(app.state,"planner",self.planner,create=True),patch.object(app.state,"store",self.store,create=True):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://testserver") as client:
                response = await client.post(f"/api/trips/{trip['id']}/assumptions/{identity}/change",json=body)
                if response.status_code == 202:
                    await self.planner.tasks[response.json()["run_id"]]
                return response

    async def test_changes_assumption_without_model_and_preserves_other_day_and_user_history(self):
        trip = await self.ready()
        original = copy.deepcopy(trip["document"])
        protected = authority.user_state(original)
        response = await self.request(trip,self.body(trip))
        self.assertEqual(response.status_code,202,response.text)
        self.assertEqual(self.store.run(response.json()["run_id"])["status"],"waiting")
        doc = self.store.trip(trip["id"])["document"]
        self.assertEqual(doc["system_durations"][DAY]["a"],60)
        self.assertEqual(doc["system_assumptions"][0]["revision"],2)
        self.assertEqual(doc["system_assumptions"][0]["status"],"replaced")
        self.assertEqual(doc["decision_history"],original["decision_history"])
        self.assertEqual(authority.user_state(doc),protected)
        self.assertEqual(assumptions.agent_context(doc)["user_decisions"],[])
        self.assertEqual(self.planner.activities.await_args.args[0]["change"]["dates"],[DAY])
        self.assertEqual(self.planner.reviewer_agent.review.await_args.args[1]["review_dates"],[DAY])
        self.assertEqual(doc["candidates"][0]["draft"]["days"][1],original["candidates"][0]["draft"]["days"][1])
        self.assertGreater(doc["draft_revision"],original["draft_revision"])
        self.assertIn("已切换系统假设",doc["coordinator_messages"][-1]["text"])
        self.assertNotIn("你要求",doc["coordinator_messages"][-1]["text"])
        self.assertIsNone(doc["coordinator_messages"][-1].get("decision_index"))
        self.planner.coordinator_agent.interpret.assert_not_awaited()
        self.planner.coordinator_agent.resolve.assert_not_awaited()
        self.planner.models.ask.assert_not_awaited()

    async def test_all_three_stale_versions_and_unknown_alternative_return_409(self):
        trip = await self.ready()
        for fields in ({"base_version":trip["version"]+1},{"draft_revision":trip["document"]["draft_revision"]+1},
                       {"assumption_revision":2},{"alternative_id":"missing"}):
            with self.subTest(fields=fields):
                response = await self.request(trip,{**self.body(trip),**fields})
                self.assertEqual(response.status_code,409,response.text)
        response = await self.request(trip,self.body(trip),"missing")
        self.assertEqual(response.status_code,409)
        self.assertEqual(self.store.trip(trip["id"])["document"]["system_assumptions"][0]["revision"],1)

    async def test_confirmation_saves_current_dialogue_fields_in_version_snapshot(self):
        trip = await self.ready()
        response = await self.request(trip,self.body(trip))
        self.assertEqual(response.status_code,202,response.text)
        trip = self.store.trip(trip["id"])
        doc = trip["document"]
        candidate = doc["candidates"][0]
        marker = {"id":"before-save","text":"保存前的主理人记录","time":"2026-10-24T00:00:00+00:00"}
        messages = [*doc["coordinator_messages"],marker]
        await self.planner.graph.aupdate_state({"configurable":{"thread_id":trip["active_thread"]}},
            {"coordinator_messages":messages})
        run_id = await self.planner.submit(trip["id"],trip["version"],candidate_id=candidate["id"],
            candidate_revision=candidate["draft_revision"],candidate_hash=candidate["plan_hash"])
        await self.planner.tasks[run_id]
        self.assertEqual(self.store.run(run_id)["status"],"completed")
        saved = self.store.trip(trip["id"])
        version = self.store.version(trip["id"],saved["version"])["document"]
        for field in ("coordinator_messages","system_assumptions","decision_history","draft_revision"):
            self.assertEqual(saved["document"][field],version[field])
        self.assertEqual(version["coordinator_messages"][-1],marker)
        self.assertEqual(version["system_assumptions"][0]["revision"],2)

    async def test_refreshed_document_does_not_expose_alternative_actions(self):
        trip = await self.ready()
        with patch.object(app.state,"planner",self.planner,create=True),patch.object(app.state,"store",self.store,create=True):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://testserver") as client:
                response = await client.get(f"/api/trips/{trip['id']}")
        self.assertEqual(response.status_code,200,response.text)
        [item] = response.json()["document"]["system_assumptions"]
        self.assertTrue({"id","title","dates","stop_ids","stop_names","current_action","reason","revision","alternatives"} <= item.keys())
        self.assertEqual(set(item["alternatives"][0]),{"id","label","description"})
        self.assertEqual(item["stop_names"],[self.fixture.initial["stops"]["a"]["name"]])

    async def test_invalid_alternative_stop_is_rejected_before_starting_run(self):
        trip = await self.ready()
        doc = trip["document"]
        doc["system_assumptions"][0]["alternatives"][0]["actions"][0]["stop_id"] = "missing"
        self.store.update(trip["id"],doc)
        response = await self.request(trip,self.body(trip))
        self.assertEqual(response.status_code,409,response.text)
        self.assertEqual(self.store.trip(trip["id"])["active_run"],trip["active_run"])

    async def test_unauthorized_replan_rolls_back_ledger_and_user_changes(self):
        trip = await self.ready()
        original = copy.deepcopy(trip["document"])
        async def unsafe(current,dates):
            current["requirements"]["budget"] = 1
            return current["itinerary"],[]
        self.planner.reassess = unsafe
        response = await self.request(trip,self.body(trip))
        self.assertEqual(response.status_code,202,response.text)
        self.assertEqual(self.store.run(response.json()["run_id"])["status"],"waiting")
        doc = self.store.trip(trip["id"])["document"]
        self.assertEqual(doc["system_assumptions"],original["system_assumptions"])
        self.assertEqual(doc["requirements"],original["requirements"])
        self.assertEqual(doc["decision_history"],original["decision_history"])
        self.assertIn("已撤回",doc["coordinator_messages"][-1]["text"])
