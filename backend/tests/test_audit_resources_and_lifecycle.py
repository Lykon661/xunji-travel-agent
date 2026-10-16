"""User-run regressions with synthetic trips and mocked processes; no external services."""
import asyncio
import copy
import json
import queue
import tempfile
import unittest
from concurrent.futures import CancelledError
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.adapters import Models
from app.cache import AdapterCache
from app.llm import ModelClient, ModelWorker
from app.models import OrderChoice, Requirements
from app.planner import Planner
from app.routing import transport_access_transfers
from app.rules import check_itinerary
from app.store import Store


DAY = "2026-11-10"
REQ = Requirements(origin="北京",cities=["杭州"],start_date=DAY,end_date=DAY,
    people=2,rooms=1,budget=5000).model_dump(mode="json")


def move(origin,destination,walking,cost):
    return {"origin":origin,"destination":destination,"minutes":30,"mode":"transit",
            "walking_meters":walking,"cost":cost,"evidence_id":"query:synthetic"}


class AccessAccounting(unittest.TestCase):
    def plan(self):
        return {"hotels":[],"transport":[{"id":"outbound","label":"北京到杭州",
            "context":{"origin":"北京","destination":"杭州","date":DAY},
            "selected":{"departure":DAY+"T06:00:00","arrival":DAY+"T08:00:00",
                "departure_transfer":move("出发地址","北京站",4000,2),
                "arrival_transfer":move("杭州站","景点",5000,3)}}],
            "days":[{"date":DAY,"city":"杭州","walking_meters":5000,"activities":[
                {"id":"visit","kind":"visit","name":"景点","start":"10:00","end":"11:00",
                 "evidence":{"quote":"景点"},"move_before":move("杭州站","景点",5000,3)}],"return_move":{}}]}

    def test_origin_access_is_counted_but_arrival_already_in_daily_route_is_not(self):
        plan = self.plan()
        result = check_itinerary(plan,REQ)
        self.assertEqual(plan["days"][0]["walking_meters"],9000)
        self.assertEqual(result["budget"]["estimated_local_transport"],10)
        self.assertTrue(any(issue["code"] == "walking_limit" for issue in result["issues"]))
        self.assertEqual(len(plan["days"][0]["transport_transfers"]),1)
        check_itinerary(plan,REQ)
        self.assertEqual(plan["days"][0]["walking_meters"],9000)

    def test_actual_station_leg_supersedes_the_options_old_hotel_comparison(self):
        plan = self.plan()
        plan["transport"][0]["selected"]["arrival_transfer"] = move("杭州站","旧推荐酒店",7000,8)
        extras = transport_access_transfers(plan,plan["days"][0])
        self.assertEqual([item["label"] for item in extras],["北京出发接驳"])

    def test_missing_origin_address_does_not_invent_a_home_to_station_leg(self):
        plan = self.plan()
        plan["transport"][0]["selected"]["departure_transfer"]["origin"] = None
        self.assertEqual(transport_access_transfers(plan,plan["days"][0]),[])

    def test_return_home_access_and_cross_day_arrival_use_the_actual_date(self):
        plan = self.plan()
        entry = plan["transport"][0]
        entry["context"].update(origin="杭州",destination="北京")
        entry["selected"].update(departure=DAY+"T21:00:00",arrival="2026-11-11T00:30:00",
            departure_transfer=move("景点","杭州站",1000,2),arrival_transfer=move("北京站","家",2000,4))
        day = plan["days"][0]
        day["return_move"] = move("景点","杭州站",1000,2)
        self.assertEqual(transport_access_transfers(plan,day),[])
        next_day = {"date":"2026-11-11","city":"杭州","activities":[],"return_move":{}}
        self.assertEqual([item["label"] for item in transport_access_transfers(plan,next_day)],["北京到达接驳"])


class CacheBounds(unittest.TestCase):
    def test_expired_entries_are_removed_and_fresh_entries_are_retained(self):
        with patch("app.cache.monotonic",return_value=0):
            cache = AdapterCache(ttl=10,max_entries=2)
            cache["old"] = {"payload":"old"}
        with patch("app.cache.monotonic",return_value=5):
            cache["fresh"] = {"payload":"fresh"}
        with patch("app.cache.monotonic",return_value=11):
            self.assertIsNone(cache.get("old"))
            self.assertEqual(list(cache),["fresh"])
            self.assertEqual(cache["fresh"],{"payload":"fresh"})

    def test_size_is_bounded_and_clear_removes_expiry_metadata(self):
        cache = AdapterCache(max_entries=2)
        for index in range(5):
            cache[index] = index
        self.assertEqual(list(cache),[3,4])
        self.assertEqual(set(cache.expires),{3,4})
        cache.clear()
        self.assertEqual(cache.expires,{})


class LazyHistory(unittest.TestCase):
    def test_summaries_do_not_parse_documents_and_detail_is_scoped_to_the_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder))
            trip = store.create_trip("合成旅行",REQ)
            other = store.create_trip("另一趟旅行",REQ)
            document = copy.deepcopy(trip["document"])
            document["messages"] = [{"role":"user","content":"仅在读取正文时返回"}]
            store.commit(trip["id"],0,document)
            with patch("app.store.json.loads",side_effect=AssertionError("摘要不应解析完整正文")):
                summaries = store.versions(trip["id"],include_documents=False)
            self.assertEqual(set(summaries[0]),{"version","created_at"})
            self.assertEqual(store.version(trip["id"],1)["document"],document)
            with self.assertRaises(KeyError):
                store.version(other["id"],1)
            self.assertEqual(store.versions(trip["id"])[0]["document"],document)


class CancelModelProcess(unittest.TestCase):
    def worker_process(self):
        process = Mock()
        process.poll.return_value = None
        return process

    def test_cancelled_wait_aborts_the_request_and_keeps_the_shared_worker(self):
        cancelled = Event()
        process = self.worker_process()
        events = Mock()
        def waiting(**kwargs):
            cancelled.set()
            raise queue.Empty()
        events.get.side_effect = waiting
        client, worker = ModelClient.__new__(ModelClient), ModelWorker()
        with patch("app.llm.shutil.which",return_value="node"),patch("app.llm.subprocess.Popen",return_value=process), \
             patch("app.llm.queue.Queue",return_value=events),patch("app.llm.Thread"),patch("app.llm.worker",worker):
            with self.assertRaises(CancelledError):
                list(client._events({},60,cancel_event=cancelled))
        written = [json.loads(call.args[0]) for call in process.stdin.write.call_args_list]
        self.assertEqual(written[-1],{"id":written[0]["id"],"cancel":True})
        process.kill.assert_not_called()
        self.assertEqual(worker.pending,{})

    def test_requests_share_one_process_and_an_exited_worker_is_replaced(self):
        first,second = self.worker_process(),self.worker_process()
        worker = ModelWorker()
        with patch("app.llm.shutil.which",return_value="node"),patch("app.llm.subprocess.Popen",side_effect=[first,second]) as popen, \
             patch("app.llm.Thread"):
            worker.submit({"action":"catalog"})
            worker.submit({"action":"catalog"})
            self.assertEqual(popen.call_count,1)
            first.poll.return_value = 0
            worker.submit({"action":"catalog"})
        self.assertEqual(popen.call_count,2)
        self.assertIn("--worker",popen.call_args.args[0])


class CancelPlannerRequest(unittest.IsolatedAsyncioTestCase):
    async def test_planner_close_signals_and_waits_for_model_worker_cleanup(self):
        loop,started,finished = asyncio.get_running_loop(),asyncio.Event(),Event()
        def complete(messages,*,cancel_event,**kwargs):
            loop.call_soon_threadsafe(started.set)
            try:
                if not cancel_event.wait(2):
                    raise AssertionError("Model worker did not receive cancellation")
                raise CancelledError()
            finally:
                finished.set()
        models = Models.__new__(Models)
        models.config_store = SimpleNamespace(snapshot=lambda **kwargs:SimpleNamespace(configured=True))
        models.client = SimpleNamespace(complete=complete)
        task = asyncio.create_task(models.ask(OrderChoice,"合成请求",{}))
        planner = Planner.__new__(Planner)
        planner.tasks = {"synthetic":task}
        try:
            await asyncio.wait_for(started.wait(),1)
            await planner.close()
            self.assertTrue(task.cancelled())
            self.assertTrue(finished.is_set())
        finally:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
