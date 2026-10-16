"""Temporary data only; left for the user to run, with no external requests."""
import copy
import tempfile
import unittest
from pathlib import Path
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

from app.main import app, delete_itinerary, export_map, lock, save_itinerary
from app.models import ItinerarySaveInput, LockInput, Requirements, REVIEW_DIMENSIONS
from app.reviewing import bind_report, review_basis
from app.agents.runtime import digest
from app.planner import Planner
from app.store import Conflict, Store


class ItinerarySaving(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name))
        self.req = Requirements(origin="北京",cities=["杭州"],start_date="2026-11-10",
                                end_date="2026-11-10",people=2,rooms=1,budget=5000).model_dump(mode="json")
        self.trip = self.store.create_trip("保存测试",self.req)
        self.id = self.trip["id"]
        self.store.add_source(self.id,{"id":"guide","title":"合成攻略"})
        self.planner = Planner.__new__(Planner)
        self.planner.store = self.store
        self.planner.gates = {}
        self.enterContext(patch.object(app.state,"store",self.store,create=True))
        self.enterContext(patch.object(app.state,"planner",self.planner,create=True))
        day = {"date":"2026-11-10","city":"杭州","activities":[{"id":"visit","kind":"visit","name":"合成景点",
               "start":"09:00","end":"10:00","map_place":{"id":"poi","location":"120.1,30.2"},
               "move_before":{"minutes":0}}],"gaps":[],"return_move":{"minutes":0}}
        self.state = {"trip_id":self.id,"generation":"generation-one","base_version":0,
                      "run_id":self.store.start_run(self.id,0,"thread",{}),
                      "requirements":self.req,"selected_source_ids":["guide"],
                      "selected_route":{"id":"route","days":[{"date":day["date"],"stop_ids":["visit"]}]},
                      "itinerary":{"days":[day],"transport":[],"hotels":[],"issues":[],"budget":{"unknown":[]}}}
        doc = self.store.trip(self.id)["document"]
        doc["selected_source_ids"] = ["guide"]
        self.store.update(self.id,doc)

    def approve_fixture(self, document=None):
        state = self.state if document is None else {**document,"trip_id":self.id}
        itinerary = state["itinerary"]
        report = bind_report({"status":"pass","summary":"合成审查结果，仅用于保存门禁用例",
            "checks":[{"dimension":key,"result":"ok","note":"合成"} for key in sorted(REVIEW_DIMENSIONS)],"findings":[]},
            state,itinerary,review_basis(state,itinerary,self.store.sources(self.id),[]),[])
        if document is None:
            state["selected_route"].update(review=report,plan_hash=digest(itinerary),draft_revision=0)
        else:
            document["review"] = report
            document["selected_route"]["review"] = report
            self.store.update(self.id,document)

    async def generate(self):
        current = self.store.trip(self.id)
        if not current["active_run"] or self.store.run(current["active_run"])["status"] != "running":
            self.state["run_id"] = self.store.start_run(self.id,self.state["base_version"],"fixture-thread",{})
        self.approve_fixture()
        await self.planner.save(copy.deepcopy(self.state))
        self.store.finish_run(self.state["run_id"],"completed",{})

    def save_input(self):
        trip = self.store.trip(self.id)
        return ItinerarySaveInput(base_version=trip["version"],updated_at=trip["updated_at"])

    async def test_generation_and_checkpoint_replay_only_keep_one_draft(self):
        await self.generate()
        await self.planner.save(copy.deepcopy(self.state))
        trip = self.store.trip(self.id)
        self.assertEqual(trip["version"],0)
        self.assertEqual(self.store.versions(self.id),[])
        self.assertIsNone(trip["document"]["itinerary_saved_version"])
        self.assertEqual(trip["document"]["stage"],"ready")
        self.assertEqual(len(trip["document"]["messages"]),1)

    async def test_successful_map_export_keeps_the_existing_review_valid(self):
        await self.generate()
        await save_itinerary(self.id,self.save_input())
        self.planner.amap = SimpleNamespace(batch=nullcontext,export_map=AsyncMock(return_value=["https://amap.com/synthetic"]))
        await export_map(self.id,self.save_input())
        trip = self.store.trip(self.id)
        self.assertTrue(trip["document"]["itinerary"]["personal_map"]["links"])
        self.planner.assert_document_review(trip)
        self.assertEqual(trip["document"]["itinerary_saved_version"],1)

    async def test_explicit_save_and_lock_require_explicit_second_save(self):
        await self.generate()
        await save_itinerary(self.id,self.save_input())
        await save_itinerary(self.id,self.save_input())
        await lock(self.id,LockInput(item_id="visit",locked=True,base_version=1))
        self.assertEqual(len(self.store.versions(self.id)),1)
        self.assertIsNone(self.store.trip(self.id)["document"]["itinerary_saved_version"])
        with self.assertRaises(Conflict):
            await save_itinerary(self.id,self.save_input())
        self.approve_fixture(self.store.trip(self.id)["document"])
        await save_itinerary(self.id,self.save_input())
        versions = self.store.versions(self.id)
        self.assertEqual([v["version"] for v in versions],[2,1])
        self.assertEqual(versions[1]["document"]["locks"],[])
        self.assertEqual(versions[0]["document"]["locks"],["visit"])

    async def test_stale_or_running_save_and_delete_are_rejected(self):
        await self.generate()
        old = self.save_input()
        await lock(self.id,LockInput(item_id="visit",locked=True,base_version=0))
        with self.assertRaises(Conflict):
            await save_itinerary(self.id,old)
        with self.assertRaises(Conflict):
            self.store.delete_itinerary(self.id,0,old.updated_at)
        self.store.start_run(self.id,0,"new-thread",{})
        with self.assertRaises(Conflict):
            await save_itinerary(self.id,self.save_input())
        with self.assertRaises(Conflict):
            self.store.delete_itinerary(self.id,0,self.save_input().updated_at)

    async def test_deleting_draft_preserves_saved_history_and_sources(self):
        await self.generate()
        await save_itinerary(self.id,self.save_input())
        await lock(self.id,LockInput(item_id="visit",locked=True,base_version=1))
        trip = self.store.trip(self.id)
        await delete_itinerary(self.id,1,trip["updated_at"],None)
        trip = self.store.trip(self.id)
        self.assertIsNone(trip["document"]["itinerary"])
        self.assertIsNone(trip["active_thread"])
        self.assertEqual(trip["document"]["requirements"],self.req)
        self.assertEqual(len(trip["sources"]),1)
        self.assertEqual(len(self.store.versions(self.id)),1)

    async def test_deleting_history_preserves_other_versions_and_never_reuses_numbers(self):
        await self.generate()
        await save_itinerary(self.id,self.save_input())
        await lock(self.id,LockInput(item_id="visit",locked=True,base_version=1))
        self.approve_fixture(self.store.trip(self.id)["document"])
        await save_itinerary(self.id,self.save_input())
        await delete_itinerary(self.id,2,self.save_input().updated_at,1)
        self.assertIsNotNone(self.store.trip(self.id)["document"]["itinerary"])
        await delete_itinerary(self.id,2,self.save_input().updated_at,2)
        self.assertEqual(self.store.versions(self.id),[])
        self.assertEqual(self.store.trip(self.id)["version"],2)
        self.state.update(generation="generation-three",base_version=2)
        await self.generate()
        await save_itinerary(self.id,self.save_input())
        self.assertEqual([v["version"] for v in self.store.versions(self.id)],[3])

    async def test_outdated_or_failed_itinerary_cannot_be_saved(self):
        await self.generate()
        trip = self.store.trip(self.id)
        doc = trip["document"]
        doc["itinerary_outdated"] = True
        self.store.update(self.id,doc)
        with self.assertRaises(Conflict):
            await save_itinerary(self.id,self.save_input())
        doc.update(itinerary_outdated=False,stage="failed")
        self.store.update(self.id,doc)
        with self.assertRaises(Conflict):
            await save_itinerary(self.id,self.save_input())
