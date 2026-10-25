import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from pydantic import ValidationError

from app.assumptions import ASSUMPTION_NOTE, active, agent_context, record, replace
from app.authority import user_state
from app.agents.runtime import AgentRuntime, agent_payload
from app.models import Requirements, RequirementUpdate, SystemAssumption
from app.planner import Planner
from app.store import Store, dump


DAY = "2026-11-10"
REQ = Requirements(origin="杭州", cities=["杭州"], start_date=DAY, end_date=DAY, people=1, rooms=1,
                   budget=1000, return_to_origin=False).model_dump(mode="json")


def assumption(**changes):
    return {"id":"duration:a", "title":"公园游玩时长", "dates":[DAY], "stop_ids":["a"],
            "current_action":{"type":"default_duration", "date":DAY, "stop_id":"a", "minutes":90},
            "reason":"采用系统参考游玩时长", "alternatives":[{"id":"short", "label":"缩短参考时长", "description":"改为60分钟",
                "actions":[{"type":"default_duration", "date":DAY, "stop_id":"a", "minutes":60}]}], **changes}


class AssumptionLedger(unittest.TestCase):
    def setUp(self):
        self.state = {"requirements":copy.deepcopy(REQ), "decision_history":[{"answers":{"q":"保留公园"}}]}

    def test_record_and_update_increment_revision_without_mutating_state(self):
        before = copy.deepcopy(self.state)
        updates = record(self.state, assumption(revision=999))
        self.assertEqual(self.state, before)
        self.assertEqual(updates["system_assumptions"][0]["revision"], 1)
        self.state.update(updates)
        updates = record(self.state, assumption(reason="按地图可用时间调整"))
        self.assertEqual(len(updates["system_assumptions"]), 1)
        self.assertEqual(updates["system_assumptions"][0]["revision"], 2)
        self.assertEqual(self.state["system_assumptions"][0]["revision"], 1)

    def test_replace_returns_actions_and_retires_the_previous_default(self):
        self.state.update(record(self.state, assumption()))
        before = copy.deepcopy(self.state)
        actions, updates = replace(self.state, "duration:a", "short")
        self.assertEqual(actions[0]["minutes"], 60)
        self.assertEqual(updates["system_assumptions"][0]["status"], "replaced")
        self.assertEqual(updates["system_assumptions"][0]["revision"], 2)
        self.assertEqual(self.state, before)
        self.assertEqual(active({**self.state, **updates}), [])
        with self.assertRaisesRegex(ValueError, "active"):
            replace({**self.state, **updates}, "duration:a", "short")
        with self.assertRaisesRegex(ValueError, "alternative"):
            replace(self.state, "duration:a", "invented")
        with self.assertRaisesRegex(ValueError, "active"):
            replace(self.state, "missing", "short")

    def test_withdrawn_entries_are_not_active_and_active_results_are_detached(self):
        self.state.update(record(self.state, assumption()))
        returned = active(self.state)
        returned[0]["title"] = "changed"
        self.assertEqual(active(self.state)[0]["title"], "公园游玩时长")
        self.state.update(record(self.state, assumption(status="withdrawn")))
        self.assertEqual(active(self.state), [])
        self.assertEqual(self.state["system_assumptions"][0]["revision"], 2)

    def test_assumptions_are_separate_from_user_state_and_decisions(self):
        before = user_state(self.state)
        self.state.update(record(self.state, assumption()))
        self.assertEqual(user_state(self.state), before)
        self.assertEqual(len(self.state["decision_history"]), 1)
        context = agent_context(self.state)
        self.assertEqual(context["user_decisions"], self.state["decision_history"])
        self.assertEqual(context["system_assumptions"], active(self.state))
        self.assertNotIn("system_assumptions", context["user_requirements"])

    def test_agent_context_separates_user_sources_system_history_and_assumptions(self):
        user = {"source":"decision", "answers":{"q":"保留公园"}}
        system = {"source":"system", "auto_settled":"duration", "instruction":"采用参考时长"}
        self.state.update(record(self.state, assumption()))
        self.state["decision_history"] = [user, system, {"source":"agent", "note":"模型推测"}]
        self.state["requirements"]["constraints"] = [{"source":"form", "kind":"fewer_walking", "meters":4000},
                                                      {"source":"system", "kind":"fewer_walking", "meters":6000}]
        context = agent_context(self.state)
        self.assertEqual(context["user_decisions"], [user])
        self.assertEqual(len(context["user_requirements"]["constraints"]), 1)
        self.assertEqual(context["system_adjustments"][0]["instruction"], "采用参考时长")
        payload = {"requirements":self.state["requirements"], "history":[user, system],
                   "available_stops":{"a":{"name":"公园"}}, "custom_evidence":["query:one"]}
        result = agent_payload(self.state, payload)
        for key in payload:
            self.assertIn(key, result)
        self.assertEqual(result["requirements"], payload["requirements"])
        self.assertEqual(result["history"], [user])
        self.assertEqual(result["available_stops"], payload["available_stops"])
        self.assertEqual(result["custom_evidence"], ["query:one"])
        self.assertEqual(result["system_assumptions"], active(self.state))

    def test_new_ledger_and_nested_models_forbid_extra_fields(self):
        for value in (assumption(extra=True), assumption(alternatives=[{"id":"a", "label":"选项", "actions":[], "extra":1}])):
            with self.assertRaises(ValidationError):
                SystemAssumption.model_validate(value)
        self.assertEqual(active({}), [])


class AssumptionPersistence(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name))
        self.trip = self.store.create_trip("合成旅行", copy.deepcopy(REQ))
        self.ledger = record({}, assumption())["system_assumptions"]

    def test_new_and_legacy_documents_default_to_an_empty_ledger(self):
        self.assertEqual(self.trip["document"]["system_assumptions"], [])
        legacy = copy.deepcopy(self.trip["document"])
        legacy.pop("system_assumptions")
        with self.store.connect() as db:
            db.execute("UPDATE trips SET document=? WHERE id=?", (dump(legacy), self.trip["id"]))
        self.assertEqual(self.store.trip(self.trip["id"])["document"]["system_assumptions"], [])

    def test_update_commit_and_version_roundtrip_keep_the_ledger(self):
        doc = self.trip["document"]
        doc["system_assumptions"] = self.ledger
        self.store.update(self.trip["id"], doc)
        self.assertEqual(self.store.trip(self.trip["id"])["document"]["system_assumptions"], self.ledger)
        self.store.commit(self.trip["id"], 0, doc)
        self.assertEqual(self.store.version(self.trip["id"], 1)["document"]["system_assumptions"], self.ledger)
        self.assertEqual(self.store.versions(self.trip["id"])[0]["document"]["system_assumptions"], self.ledger)
        legacy = copy.deepcopy(doc)
        legacy.pop("system_assumptions")
        with self.store.connect() as db:
            db.execute("UPDATE versions SET document=? WHERE trip_id=?", (dump(legacy), self.trip["id"]))
        self.assertEqual(self.store.version(self.trip["id"], 1)["document"]["system_assumptions"], [])
        self.assertEqual(self.store.versions(self.trip["id"])[0]["document"]["system_assumptions"], [])

    async def test_save_and_idempotent_save_synchronize_assumptions(self):
        planner = Planner.__new__(Planner)
        planner.store, planner.emit, planner.assert_review = self.store, Mock(), Mock()
        plan = {"days":[{"date":DAY, "city":"杭州", "activities":[], "gaps":[]}], "issues":[], "budget":{"unknown":[]}}
        state = {"trip_id":self.trip["id"], "generation":"generation", "base_version":0, "requirements":REQ,
                 "itinerary":plan, "selected_route":{"days":[{"date":DAY, "city":"杭州", "stop_ids":[]}]},
                 "system_assumptions":self.ledger}
        await planner.save(state)
        self.assertEqual(self.store.trip(self.trip["id"])["document"]["system_assumptions"], self.ledger)
        withdrawn = record({"system_assumptions":self.ledger}, assumption(status="withdrawn"))["system_assumptions"]
        await planner.save({**state, "system_assumptions":withdrawn})
        self.assertEqual(self.store.trip(self.trip["id"])["document"]["system_assumptions"], withdrawn)

    async def test_new_submission_copies_the_document_ledger(self):
        doc = self.trip["document"]
        doc["system_assumptions"] = self.ledger
        self.store.update(self.trip["id"], doc)
        planner = Planner.__new__(Planner)
        planner.store, planner.gates, planner.tasks = self.store, {}, {}
        planner.amap = SimpleNamespace(cache={})
        planner.execute = AsyncMock()
        run = await planner.submit(self.trip["id"], 0)
        task = planner.tasks.get(run)
        if task:
            await task
        invocation = planner.execute.await_args.args[1]
        self.assertEqual(invocation["system_assumptions"], self.ledger)

    async def test_runtime_includes_context_and_the_attribution_instruction(self):
        user = {"source":"decision", "answers":{"q":"保持预算"}}
        state = {"trip_id":self.trip["id"], "run_id":"run", "generation":"gen", "requirements":REQ,
                 "system_assumptions":self.ledger, "decision_history":[user]}
        models = SimpleNamespace(ask=AsyncMock(return_value=RequirementUpdate()))
        store = Mock()
        store.trip.return_value = {"active_run":"run"}
        store.run.return_value = {"status":"running"}
        store.agent_result.return_value = None
        runtime = AgentRuntime("合成Agent", models, store)
        await runtime.ask(state, RequirementUpdate, "读取旅行需求", {"legacy_key":"retained"})
        instruction, payload = models.ask.await_args.args[1:3]
        self.assertIn(ASSUMPTION_NOTE, instruction)
        self.assertEqual(payload["legacy_key"], "retained")
        self.assertEqual(payload["user_decisions"], [user])
        self.assertEqual(payload["system_assumptions"], self.ledger)
