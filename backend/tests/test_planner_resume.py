"""Temporary databases and mocked checkpoints; no model or platform queries."""
import tempfile
import unittest
from pathlib import Path
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.adapters import ToolError
from app.models import Requirements
from app.planner import Planner
from app.store import Conflict, Store


class PlannerResume(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = Store(Path(self.folder.name))
        self.requirements = Requirements(origin="北京", cities=["杭州"], start_date="2026-11-10", end_date="2026-11-11",
                                         people=2, rooms=1, budget=5000).model_dump(mode="json")
        self.trip = self.store.create_trip("中断行程", self.requirements)
        self.store.add_source(self.trip["id"], {"id":"guide", "title":"合成攻略"})
        self.old_run = self.store.start_run(self.trip["id"], 0, "existing-thread", {})
        self.store.finish_run(self.old_run, "failed", {})
        doc = self.trip["document"]
        doc.update(stage="failed", progress_stage="activities", selected_source_ids=["guide"],
                   candidates=[{"id":"candidate"}], selected_route={"id":"candidate"})
        self.store.update(self.trip["id"], doc)
        self.snapshot = SimpleNamespace(next=("activities",), values={"requirements":self.requirements,
                                          "selected_source_ids":["guide"], "candidates":[{"id":"candidate"}]})
        self.planner = Planner.__new__(Planner)
        self.planner.store = self.store; self.planner.tasks = {}; self.planner.gates = {}
        self.planner.amap = SimpleNamespace(batch=nullcontext)
        self.planner.graph = SimpleNamespace(aget_state=AsyncMock(return_value=self.snapshot), aupdate_state=AsyncMock())
        self.planner.execute = AsyncMock()

    async def test_continue_keeps_checkpoint_route_and_progress_while_queued(self):
        run = await self.planner.submit(self.trip["id"], 0, retry=True, resume=True)
        await self.planner.tasks[run]
        self.assertEqual(self.store.run(run)["thread_id"], "existing-thread")
        self.planner.execute.assert_awaited_once_with(run, None)
        doc = self.store.trip(self.trip["id"])["document"]
        self.assertEqual(doc["stage"], "queued")
        self.assertEqual(doc["progress_stage"], "activities")
        self.assertEqual(doc["selected_route"], {"id":"candidate"})
        self.assertEqual(doc["candidates"], [{"id":"candidate"}])

    async def test_continue_does_not_silently_restart_after_input_change(self):
        with self.assertRaises(Conflict):
            await self.planner.submit(self.trip["id"], 0, requirements={"budget":6000}, resume=True)
        self.planner.execute.assert_not_awaited()
        self.assertEqual(self.store.trip(self.trip["id"])["active_run"], self.old_run)

    async def test_reopening_a_decision_marks_the_checkpoint_as_an_explicit_new_answer(self):
        doc = self.store.trip(self.trip["id"])["document"]
        question = {"id":"walk","title":"步行上限","question":"是否保持规划？","why_needed":"步行超限",
                    "affected_dates":["2026-11-10"]}
        doc.update(candidates=[{"id":"candidate","draft":{"days":[{"date":"2026-11-10"}]},
                                "days":[{"date":"2026-11-10","city":"杭州","stop_ids":[]}]}],
                   decision_history=[{"questions":[question],"answers":{"walk":"保持规划"}}])
        self.store.update(self.trip["id"],doc)
        trip = self.store.trip(self.trip["id"])
        run = await self.planner.reopen_decision(trip["id"],trip["version"],trip["updated_at"],0)
        await self.planner.tasks[run]
        checkpoint = self.planner.graph.aupdate_state.await_args.args[1]
        self.assertTrue(checkpoint["decision_reopened"])
        self.assertEqual(checkpoint["decision"],question)
        self.assertEqual(checkpoint["decision_history"],doc["decision_history"])

    async def test_quote_refresh_keeps_route_candidates_and_starts_at_resources(self):
        self.store.add_source(self.trip["id"],{"id":"guide","title":"合成攻略","extraction":{"stops":[
            {"id":"guide:0","city":"杭州","name":"合成景点"}]}})
        candidate = {"id":"candidate","title":"现有路线","reason":"合成资料","gaps":[],"days":[
            {"date":"2026-11-10","city":"杭州","stop_ids":["guide:0"],"adjustment":""},
            {"date":"2026-11-11","city":"杭州","stop_ids":[],"adjustment":""}]}
        doc = self.store.trip(self.trip["id"])["document"]
        doc.update(stage="incomplete",selected_route=candidate,candidates=[candidate],
                   itinerary={"days":[],"transport":[],"hotels":[]})
        self.store.update(self.trip["id"],doc)
        run = await self.planner.submit(self.trip["id"],0,refresh_resources=True)
        await self.planner.tasks[run]
        invocation = self.planner.execute.await_args.args[1]
        self.assertTrue(invocation["change"]["refresh_resources"])
        self.assertEqual(invocation["selected_route"],candidate)
        updated = self.store.trip(self.trip["id"])["document"]
        self.assertEqual(updated["candidates"],[candidate])
        self.assertEqual(updated["progress_stage"],"transport")

    async def test_hotel_area_change_keeps_route_and_only_affects_selected_city(self):
        self.store.add_source(self.trip["id"],{"id":"guide","title":"合成攻略","extraction":{"stops":[
            {"id":"guide:0","city":"杭州","name":"合成景点"}]}})
        candidate = {"id":"candidate","title":"现有路线","reason":"合成资料","gaps":[],"days":[
            {"date":"2026-11-10","city":"杭州","stop_ids":["guide:0"],"adjustment":""},
            {"date":"2026-11-11","city":"杭州","stop_ids":[],"adjustment":""}]}
        doc = self.store.trip(self.trip["id"])["document"]
        doc.update(stage="ready",selected_route=candidate,candidates=[candidate],itinerary={"days":[],"transport":[],"hotels":[]})
        self.store.update(self.trip["id"],doc)
        trip = self.store.trip(self.trip["id"])
        run = await self.planner.submit(trip["id"],0,updated_at=trip["updated_at"],hotel_area_choice={"city":"杭州","area":"湖滨路"})
        await self.planner.tasks[run]
        invocation = self.planner.execute.await_args.args[1]
        self.assertEqual(invocation["selected_route"],candidate)
        self.assertEqual(invocation["requirements"]["hotel_areas"],{"杭州":"湖滨路"})
        self.assertEqual(invocation["change"]["cities"],["杭州"])
        self.assertEqual(invocation["change"]["scopes"],["hotel","activity"])
        self.assertEqual(self.store.trip(trip["id"])["document"]["candidates"],[candidate])

    async def test_locked_hotel_prevents_area_change(self):
        doc = self.store.trip(self.trip["id"])["document"]
        doc.update(itinerary={"days":[]},locks=["hotel:杭州"])
        self.store.update(self.trip["id"],doc)
        with self.assertRaises(Conflict):
            await self.planner.submit(self.trip["id"],0,hotel_area_choice={"city":"杭州","area":"湖滨路"})
        self.planner.execute.assert_not_awaited()

    async def test_failure_records_checkpoint_stage(self):
        run = self.store.start_run(self.trip["id"], 0, "existing-thread", {})
        self.planner.graph.ainvoke = AsyncMock(side_effect=ToolError("model_format", "格式校验失败"))
        await Planner.execute(self.planner, run, None)
        self.assertEqual(self.store.trip(self.trip["id"])["document"]["progress_stage"], "activities")
        self.assertEqual(self.store.run(run)["document"]["stage"], "activities")
        self.assertEqual(self.store.events(run, 0)[-1]["stage"], "activities")

    async def test_nested_exception_group_keeps_model_error_and_checkpoint(self):
        run = self.store.start_run(self.trip["id"], 0, "existing-thread", {})
        self.planner.graph.ainvoke = AsyncMock(side_effect=ExceptionGroup("SDK cleanup", [
            ExceptionGroup("session", [ToolError("model_format", "模型返回字段不完整")])]))
        await Planner.execute(self.planner, run, None)
        result = self.store.run(run)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["document"]["code"], "model_format")
        self.assertEqual(result["document"]["message"], "模型返回字段不完整")
        doc = self.store.trip(self.trip["id"])["document"]
        self.assertEqual(doc["progress_stage"], "activities")
        self.assertEqual(doc["selected_route"], {"id":"candidate"})

    async def test_unexpected_group_logs_location_without_private_exception_text(self):
        run = self.store.start_run(self.trip["id"], 0, "existing-thread", {})
        async def fail(*args):
            try:
                raise ValueError("private-provider-response")
            except ValueError as error:
                raise ExceptionGroup("private-group-message", [error])
        self.planner.graph.ainvoke = fail
        with self.assertLogs("app.planner", level="ERROR") as logged:
            await Planner.execute(self.planner, run, None)
        result = self.store.run(run)["document"]
        self.assertEqual(result["code"], "run_failed")
        self.assertIn("server.log", result["message"])
        self.assertIn("test_planner_resume.py:", "\n".join(logged.output))
        self.assertNotIn("private", "\n".join(logged.output) + result["message"])
