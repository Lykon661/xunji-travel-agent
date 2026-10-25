"""Synthetic guides and mocked models; these tests never call external services."""
import copy
from contextlib import nullcontext
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from pydantic import ValidationError

from app.models import (Candidate, CandidatePack, CandidateRevision, InterviewQuestion,
                        InterviewRound, RequirementUpdate)
from app.models import GuideBrief, RouteTurn
from app.planner import Planner
from app.store import Conflict, Store


REQ = {"origin":"北京","cities":["杭州"],"start_date":"2026-11-10","end_date":"2026-11-12",
       "people":2,"rooms":1,"budget":8000,"preferences":"少走路","transport":"any","bed_type":"双床房","return_to_origin":True}
ROUTE = {"id":"original","title":"杭州三天","reason":"按偏好安排","gaps":[],"days":[
    {"date":"2026-11-10","city":"杭州","stop_ids":["chosen:0"],"adjustment":""},
    {"date":"2026-11-11","city":"杭州","stop_ids":["chosen:1"],"adjustment":""},
    {"date":"2026-11-12","city":"杭州","stop_ids":["chosen:2"],"adjustment":""}]}


class RouteInterview(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name))
        self.trip = self.store.create_trip("需求追问",REQ)
        self.stops = {f"chosen:{index}":{"id":f"chosen:{index}","city":"杭州","day":index+1,
                     "name":f"合成地点{index}","source_id":"chosen"} for index in range(4)}
        self.store.add_source(self.trip["id"],{"id":"chosen","title":"合成攻略","kind":"manual",
            "text":"合成资料","images":[],"extraction_cities":["杭州"],"extraction_version":4,"extraction":{"stops":list(self.stops.values())}})
        self.model = SimpleNamespace(ask=AsyncMock())
        async def assessed(planner, state):
            # Interview/revision fixtures stop at selection; resource assessment has separate tests.
            return {"previews":{},"preview_ready":False,"coordinator_resume":"review_gate"}
        with patch("app.planner.Models",return_value=self.model), patch("app.planner.Crawler"), patch("app.planner.FlyAI"), patch("app.planner.Amap"), patch.object(Planner,"assess",new=assessed):
            self.planner = Planner(self.store,InMemorySaver())
            # The graph is built lazily; bind the stubbed assessment node while the patch is active.
            self.planner.graph
        self.planner.emit = Mock()
        async def route_plan(state,instruction,payload,tools):
            pack = await self.model.ask(CandidatePack,instruction,payload)
            return RouteTurn(status="ready",candidate=pack.candidates[0])
        async def route_ask(state,schema,instruction,payload,**options):
            return await self.model.ask(schema,instruction,payload)
        self.planner.route_agent = SimpleNamespace(plan=route_plan,ask=route_ask)
        self.planner.amap = SimpleNamespace(cache={},place=AsyncMock(return_value=None),weather=AsyncMock(return_value={"forecasts":{}}))
        self.config = {"configurable":{"thread_id":"synthetic-interview"}}
        self.state = {"trip_id":self.trip["id"],"requirements":REQ,"selected_source_ids":["chosen"],
                      "mode":"new","message":"","warnings":[],"interview":{},"route_edit":{},"stops":{}}

    def revision(self,stop_id="chosen:3"):
        return CandidateRevision(days=[{"date":"2026-11-11","city":"杭州","stop_ids":[stop_id],
                                       "adjustment":"替换为用户指定的活动"}],explanation="只调整第二天")

    def completed_document(self):
        doc = self.store.trip(self.trip["id"])["document"]
        doc.update(selected_source_ids=["chosen"],candidates=[copy.deepcopy(ROUTE)],selected_route=copy.deepcopy(ROUTE),
                   stage="ready",itinerary={"days":[],"hotels":[],"transport":[]},
                   interview={"summary":"保留西湖，少走路","questions":[],"round":2,"round_id":"old","history":[],"confirmed":True})
        self.store.update(self.trip["id"],doc)
        self.planner.amap.batch = nullcontext
        return self.store.trip(self.trip["id"])

    async def test_back_to_routes_preserves_candidates_and_clears_saved_itineraries(self):
        trip = self.completed_document()
        self.store.commit(trip["id"],0,trip["document"],generation="saved-before-back")
        trip = self.store.trip(trip["id"])
        run = await self.planner.back_to_step(trip["id"],trip["version"],trip["updated_at"],"routes")
        await self.planner.tasks[run]
        current = self.store.trip(trip["id"])
        self.assertEqual(current["document"]["pending"]["kind"],"selection")
        self.assertFalse(current["document"]["itinerary_outdated"])
        self.assertIsNone(current["document"]["itinerary"])
        self.assertEqual(current["document"]["candidates"],trip["document"]["candidates"])
        self.assertEqual(current["version"],trip["version"])
        self.assertEqual(self.store.versions(trip["id"]),[])
        snapshot = await self.planner.graph.aget_state({"configurable":{"thread_id":current["active_thread"]}})
        self.assertEqual(snapshot.next,("selection_gate",))
        self.assertEqual(snapshot.values["old_itinerary"],{})
        self.model.ask.assert_not_awaited()


    async def test_back_rejects_a_stale_page_without_starting_a_run(self):
        trip = self.completed_document()
        with self.assertRaises(Conflict):
            await self.planner.back_to_step(trip["id"],trip["version"],"stale","routes")
        self.assertEqual(self.store.trip(trip["id"])["active_run"],trip["active_run"])

    def test_patch_preserves_other_days_and_original_object(self):
        original = copy.deepcopy(ROUTE)
        revised = self.planner.apply_candidate_revision(original,self.revision(),["2026-11-11"],REQ,self.stops)
        self.assertEqual(revised["days"][0],ROUTE["days"][0])
        self.assertEqual(revised["days"][2],ROUTE["days"][2])
        self.assertEqual(revised["days"][1]["stop_ids"],["chosen:3"])
        self.assertEqual(original,ROUTE)

    def test_patch_rejects_foreign_stop_or_reusing_fixed_activity(self):
        for stop_id in ("other:0","chosen:0"):
            with self.subTest(stop_id=stop_id), self.assertRaises(ValueError):
                self.planner.apply_candidate_revision(ROUTE,self.revision(stop_id),["2026-11-11"],REQ,self.stops)

    def test_patch_rejects_changed_city_extra_dates_and_duplicate_dates(self):
        changed_city = self.revision()
        changed_city.days[0].city = "苏州"
        extra = self.revision()
        extra.days.append(extra.days[0].model_copy(update={"date":Candidate.model_validate(ROUTE).days[0].date}))
        duplicate = self.revision()
        duplicate.days.append(duplicate.days[0].model_copy())
        for revision in (changed_city,extra,duplicate):
            with self.assertRaises(ValueError):
                self.planner.apply_candidate_revision(ROUTE,revision,["2026-11-11"],REQ,self.stops)



    def test_generated_round_rejects_multiple_questions(self):
        question = {"id":"first","title":"偏好","question":"喜欢哪种节奏？","options":[],"recommendation":"按偏好选择"}
        with self.assertRaises(ValidationError):
            InterviewRound(summary="当前需求",questions=[question,{**question,"id":"second"}])

    async def test_invalid_revision_keeps_candidate_available_for_another_edit(self):
        self.model.ask.return_value = self.revision("other:0")
        result = await self.planner.revise_candidate({**self.state,"candidates":[ROUTE],"stops":self.stops,
            "route_edit":{"candidate_id":"original","dates":["2026-11-11"],"content":"修改第二天"}})
        self.assertEqual(result["route_edit"],{})
        self.assertNotIn("candidates",result)
        self.assertIn("原候选路线已保留",result["warnings"][0])
        self.assertEqual(self.model.ask.await_count,2)

