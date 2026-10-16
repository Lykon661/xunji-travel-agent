"""Synthetic sources and mocked models only; no crawler, model or external API is called."""
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from app.models import Candidate, CandidateDay, CandidatePack, RouteTurn
from app.adapters import ToolError
from app.planner import Planner
from app.store import Conflict, Store


REQ = {"origin":"北京","cities":["杭州"],"start_date":"2026-11-10","end_date":"2026-11-12",
       "people":2,"rooms":1,"budget":8000,"preferences":"少走路","transport":"any","bed_type":"双床房","return_to_origin":True}


class GuideSelection(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = Store(Path(self.folder.name))
        self.trip = self.store.create_trip("攻略选择",REQ)
        for identity in ("chosen", "other"):
            self.store.add_source(self.trip["id"],{
                "id":identity,"title":identity,"kind":"manual","text":"合成资料","images":[],
                "extraction_cities":["杭州"],"extraction_version":4,"extraction":{"days":5,"stops":[{
                    "id":identity+":0","city":"杭州","day":5,"name":"合成地点"}]}})
        self.planner = Planner.__new__(Planner)
        self.planner.store = self.store
        self.planner.emit = Mock()
        self.planner.parse_source = AsyncMock()
        self.planner.models = SimpleNamespace(ask=AsyncMock())
        self.planner.amap = SimpleNamespace(cache={},place=AsyncMock(return_value=None),weather=AsyncMock(return_value={"forecasts":{}}))
        async def route_plan(state,instruction,payload,tools):
            pack = await self.planner.models.ask(CandidatePack,instruction,payload)
            return RouteTurn(status="ready",candidate=pack.candidates[0])
        self.planner.route_agent = SimpleNamespace(plan=route_plan)
        self.state = {"trip_id":self.trip["id"],"requirements":REQ,"selected_source_ids":["chosen"]}

    def pack(self, count):
        return CandidatePack(candidates=[Candidate(title="合成路线",reason="合成测试",days=[
            CandidateDay(date=date(2026,11,10+index),city="杭州",stop_ids=["chosen:0"] if index == 0 else [])
            for index in range(count)])])

    async def test_unselected_source_never_enters_candidate_model_context(self):
        self.planner.models.ask.return_value = self.pack(3)
        result = await self.planner.discover(self.state)
        payload = self.planner.models.ask.call_args.args[2]
        self.assertEqual([guide["id"] for guide in payload["guides"]],["chosen"])
        self.assertEqual(set(payload["available_stops"]),{"chosen:0"})
        self.assertEqual(payload["required_dates"],["2026-11-10","2026-11-11","2026-11-12"])
        self.assertEqual(len(result["candidates"][0]["days"]),3)
        self.planner.parse_source.assert_not_awaited()

    async def test_five_day_model_output_is_corrected_once_and_never_returned(self):
        self.planner.models.ask.side_effect = [self.pack(5),self.pack(3)]
        result = await self.planner.discover(self.state)
        self.assertEqual(self.planner.models.ask.await_count,2)
        self.assertEqual(len(result["candidates"][0]["days"]),3)
        self.assertTrue(self.planner.models.ask.call_args.args[2]["validation_error"])

    async def test_repeated_invalid_output_is_rejected_without_a_candidate(self):
        self.planner.models.ask.side_effect = [self.pack(5),self.pack(5)]
        with self.assertRaises(ToolError):
            await self.planner.discover(self.state)
        self.assertEqual(self.planner.models.ask.await_count,2)

    async def test_foreign_trip_source_cannot_be_selected(self):
        other_trip = self.store.create_trip("另一个旅行",REQ)
        self.store.add_source(other_trip["id"],{"id":"foreign","title":"另一旅行的攻略"})
        with self.assertRaises(Conflict):
            self.planner.selected_sources(self.trip["id"],["foreign"])

    async def test_resume_refreshes_stale_city_extraction_but_keeps_valid_place_evidence(self):
        source = next(item for item in self.store.sources(self.trip["id"]) if item["id"] == "chosen")
        source["extraction_version"] = 3
        source["extraction"]["stops"].append({"id":"chosen:outside","city":"杭州","day":1,"name":"原文其他城市的地点"})
        self.store.add_source(self.trip["id"],source)
        self.state["stops"] = self.planner.source_stops(self.trip["id"],["chosen"])
        place = {"id":"verified","location":"120,30","evidence_id":"query:1"}
        self.state["stops"]["chosen:0"]["map_place"] = place
        async def refreshed(source,cities):
            source["extraction"]["stops"] = source["extraction"]["stops"][:1]
            source["extraction"]["gaps"] = ["原文其他城市的地点：超出本次目的地范围"]
            source["extraction_version"] = 4
            return source
        self.planner.parse_source.side_effect = refreshed
        self.planner.models.ask.return_value = self.pack(3)
        result = await self.planner.discover(self.state)
        self.planner.parse_source.assert_awaited_once()
        self.assertNotIn("chosen:outside",result["stops"])
        self.assertEqual(result["stops"]["chosen:0"]["map_place"],place)
        self.planner.amap.place.assert_not_awaited()
        payload = self.planner.models.ask.call_args.args[2]
        self.assertIn("超出本次目的地范围",payload["guides"][0]["guide"]["gaps"][0])
