"""Workflow rollback with temporary storage."""
import tempfile
import unittest
from pathlib import Path

from app.models import Requirements
from app.store import Conflict, Store


class WorkflowBack(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name))
        self.req = Requirements(origin="北京",cities=["杭州"],start_date="2026-11-10",end_date="2026-11-10",
                                people=1,rooms=1,budget=3000).model_dump(mode="json")
        self.trip = self.store.create_trip("回退测试",self.req)
        self.other = self.store.create_trip("另一趟旅行",self.req)
        self.source = {"id":"guide","title":"攻略","text":"公园","extraction":{"stops":[{"id":"guide:park"}]}}
        self.store.add_source(self.trip["id"],self.source)
        self.itinerary = {"days":[{"date":"2026-11-10","city":"杭州","activities":[]}]}
        self.interview = {"summary":"喜欢公园","history":[{"answers":{"pace":"慢一点"}}],"confirmed":True}
        self.candidates = [{"id":"candidate","draft":self.itinerary}]
        doc = self.trip["document"]
        doc.update(selected_source_ids=["guide"],interview=self.interview,candidates=self.candidates,
                   route_chat=[{"role":"user","content":"调整公园"}],selected_route=self.candidates[0],
                   itinerary=self.itinerary,itinerary_saved_version=1,confirmed_plan={"plan_hash":"saved"},
                   products=[{"id":"product"}],product_search={"query":"公园"},locks=["guide:park"],stage="completed")
        self.store.commit(self.trip["id"],0,doc,generation="saved")
        self.run = self.store.start_run(self.trip["id"],1,"old-thread",{})
        self.store.event(self.run,"completed","旧记录")
        self.store.finish_run(self.run,"waiting",{})
        self.store.query(self.trip["id"],"amap",{"tool":"maps_text_search"},{"raw":{"location":"120,30"}})
        self.trip = self.store.trip(self.trip["id"])

    def back(self, step):
        return self.store.back_to_step(self.trip["id"],self.trip["version"],self.trip["updated_at"],step)

    def test_back_to_requirements_removes_all_later_records_and_cannot_resume_old_run(self):
        result = self.back("requirements")
        self.assertEqual(result["document"]["requirements"],self.req)
        self.assertEqual(result["sources"],[])
        self.assertEqual(result["document"]["selected_source_ids"],[])
        self.assertIsNone(result["document"]["interview"])
        self.assertEqual(result["document"]["candidates"],[])
        self.assertEqual(result["document"]["route_chat"],[])
        self.assertIsNone(result["document"]["itinerary"])
        self.assertEqual(self.store.versions(result["id"]),[])
        self.assertIsNone(result["active_thread"])
        self.assertIsNone(result["active_run"])
        with self.assertRaises(KeyError):
            self.store.run(self.run)
        self.assertEqual(self.store.events(self.run,0),[])
        self.assertEqual(self.store.trip(self.other["id"]),self.other)

    def test_back_to_guides_keeps_sources_but_clears_questions_and_plans(self):
        result = self.back("guides")
        self.assertEqual(result["sources"],[self.source])
        self.assertEqual(result["document"]["selected_source_ids"],["guide"])
        self.assertIsNone(result["document"]["interview"])
        self.assertIsNone(result["document"]["itinerary"])

    def test_back_to_interview_keeps_answers_but_clears_candidates_chat_and_saved_versions(self):
        doc = self.back("interview")["document"]
        self.assertEqual(doc["interview"]["history"],self.interview["history"])
        self.assertFalse(doc["interview"]["confirmed"])
        self.assertEqual(doc["candidates"],[])
        self.assertEqual(doc["route_chat"],[])
        self.assertIsNone(doc["itinerary"])
        self.assertEqual(self.store.versions(self.trip["id"]),[])

    def test_back_to_routes_keeps_candidate_and_chat_but_removes_itinerary_confirmation(self):
        result = self.back("routes")
        doc = result["document"]
        self.assertEqual(doc["candidates"],self.candidates)
        self.assertEqual(doc["route_chat"],[{"role":"user","content":"调整公园"}])
        self.assertIsNone(doc["itinerary"])
        self.assertIsNone(doc["confirmed_plan"])
        self.assertEqual(doc["locks"],[])
        self.assertEqual(self.store.versions(result["id"]),[])
        self.assertEqual(result["version"],1)
        self.assertEqual(self.store.commit(result["id"],1,doc,generation="next"),2)

    def test_back_to_itinerary_keeps_saved_plan_and_removes_later_resource_search(self):
        doc = self.back("itinerary")["document"]
        self.assertEqual(doc["itinerary"],self.itinerary)
        self.assertEqual(doc["stage"],"completed")
        self.assertEqual(len(self.store.versions(self.trip["id"])),1)
        self.assertEqual(doc["products"],[])
        self.assertIsNone(doc["product_search"])

    def test_stale_page_or_running_task_cannot_clear_records(self):
        with self.assertRaises(Conflict):
            self.store.back_to_step(self.trip["id"],1,"stale","requirements")
        self.assertEqual(self.store.sources(self.trip["id"]),[self.source])
        self.store.start_run(self.trip["id"],1,"running-thread",{})
        with self.assertRaises(Conflict):
            self.back("requirements")
        self.assertEqual(len(self.store.versions(self.trip["id"])),1)
