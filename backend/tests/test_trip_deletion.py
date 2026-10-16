"""Temporary databases and mocked checkpoints only; no real trips or services."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.planner import Planner
from app.store import Conflict, Store


class TripDeletion(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name))
        self.trip = self.store.create_trip("待删除旅行",{})
        self.other = self.store.create_trip("保留旅行",{})
        self.checkpointer = SimpleNamespace(adelete_thread=AsyncMock())
        self.planner = Planner.__new__(Planner)
        self.planner.store = self.store
        self.planner.gates = {}
        self.planner.tasks = {}
        self.planner.graph = SimpleNamespace(checkpointer=self.checkpointer)
        self.planner.amap = SimpleNamespace(cache={})
        self.planner.flyai = SimpleNamespace(cache={})

    def add_records(self,trip):
        identity = trip["id"]
        self.store.add_source(identity,{"id":identity+":source","title":"合成攻略"})
        self.store.query(identity,"synthetic",{}, {})
        self.store.commit(identity,0,trip["document"])
        for thread in (identity+":old",identity+":current"):
            run = self.store.start_run(identity,1,thread,{})
            self.store.event(run,"waiting","合成等待记录")
            self.store.finish_run(run,"waiting",{})

    async def test_deletes_related_records_and_all_threads_without_touching_other_trip(self):
        self.add_records(self.trip)
        self.add_records(self.other)
        self.store.cache("共享关键词",[{"title":"共享缓存"}])
        await self.planner.delete_trip(self.trip["id"],1)
        self.assertEqual([call.args[0] for call in self.checkpointer.adelete_thread.await_args_list],
                         [self.trip["id"]+":old",self.trip["id"]+":current"])
        with self.assertRaises(KeyError):
            self.store.trip(self.trip["id"])
        self.assertEqual(self.store.trip(self.other["id"])["title"],"保留旅行")
        with self.store.connect() as db:
            for table in ("sources","queries","versions","runs"):
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table} WHERE trip_id=?",(self.trip["id"],)).fetchone()[0],0)
                self.assertGreater(db.execute(f"SELECT COUNT(*) FROM {table} WHERE trip_id=?",(self.other["id"],)).fetchone()[0],0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events").fetchone()[0],2)
        self.assertEqual(self.store.cached("共享关键词",1),[{"title":"共享缓存"}])

    async def test_running_or_stale_trip_cannot_be_deleted(self):
        run = self.store.start_run(self.trip["id"],0,"running-thread",{})
        with self.assertRaises(Conflict):
            await self.planner.delete_trip(self.trip["id"],0)
        with self.assertRaises(Conflict):
            self.store.delete_trip(self.trip["id"],0)
        self.store.finish_run(run,"waiting",{})
        self.store.commit(self.trip["id"],0,self.trip["document"])
        with self.assertRaises(Conflict):
            await self.planner.delete_trip(self.trip["id"],0)
        self.checkpointer.adelete_thread.assert_not_awaited()
        self.assertEqual(self.store.trip(self.trip["id"])["version"],1)

    async def test_checkpoint_failure_keeps_trip_and_related_data(self):
        self.add_records(self.trip)
        self.checkpointer.adelete_thread.side_effect = OSError("synthetic checkpoint failure")
        with self.assertRaises(OSError):
            await self.planner.delete_trip(self.trip["id"],1)
        self.assertEqual(len(self.store.sources(self.trip["id"])),1)
        self.assertEqual(len(self.store.versions(self.trip["id"])),1)

    async def test_draft_can_be_deleted_without_a_checkpoint(self):
        target,other = self.trip["id"],self.other["id"]
        self.planner.amap.cache = {("weather",target,"杭州"):{},("weather",other,"杭州"):{}}
        from app.store import dump
        self.planner.flyai.cache = {dump([target,"hotel",{},{}]):{},dump([other,"hotel",{},{}]):{}}
        await self.planner.delete_trip(self.trip["id"],0)
        self.checkpointer.adelete_thread.assert_not_awaited()
        self.assertEqual([trip["id"] for trip in self.store.trips()],[self.other["id"]])
        self.assertEqual(list(self.planner.amap.cache),[("weather",other,"杭州")])
        self.assertEqual(list(self.planner.flyai.cache),[dump([other,"hotel",{},{}])])
