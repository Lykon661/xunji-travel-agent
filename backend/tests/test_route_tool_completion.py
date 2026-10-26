"""Route tool-loop completion with mocked external calls."""
import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pydantic import ValidationError

from app.adapters import ToolError
from app.agents.route import RouteAgent, RouteCompletion, resolve_stop_id
from app.models import Candidate, OrderChoice, Requirements, RouteToolRequest, RouteTurn
from app.store import Store


class StopIdentity(unittest.TestCase):
    def test_only_exact_unique_names_inside_the_city_resolve(self):
        stops = {"guide:a":{"name":"东山口","city":"广州"},"guide:b":{"name":"公园","city":"广州"},
                 "guide:c":{"name":"公园","city":"广州"},"other:a":{"name":"东山口","city":"深圳"}}
        self.assertEqual(resolve_stop_id(" 东山口 ",stops,"广州"),"guide:a")
        self.assertEqual(resolve_stop_id(" guide:a ",stops,"广州"),"guide:a")
        self.assertIsNone(resolve_stop_id("公园",stops,"广州"))
        self.assertIsNone(resolve_stop_id("guide:a",stops,"深圳"))
        self.assertIsNone(resolve_stop_id("东山口",stops))
        self.assertIsNone(resolve_stop_id("未知景点",stops,"广州"))


    def test_multi_city_daily_query_can_fill_only_the_authorized_current_city(self):
        agent = RouteAgent(SimpleNamespace(),SimpleNamespace())
        state = {"requirements":{"cities":["广州","深圳"]}}
        payload = {"city":"广州","available_stops":{"guide:a":{"name":"东山口","city":"广州"}}}
        request = RouteToolRequest(tool="place",destination_id="东山口")
        actual = agent.prepare_tool_request(state,payload,request)
        self.assertEqual((actual.city,actual.destination_id),("广州","guide:a"))
        explicit = request.model_copy(update={"city":"深圳"})
        actual = agent.prepare_tool_request(state,payload,explicit)
        self.assertEqual((actual.city,actual.destination_id),("深圳","东山口"))


class RouteToolCompletion(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name))
        req = Requirements(origin="广州",cities=["广州"],start_date="2026-11-10",end_date="2026-11-10",
                           people=1,rooms=1,budget=3000).model_dump(mode="json")
        trip = self.store.create_trip("合成旅行",req)
        run = self.store.start_run(trip["id"],0,"generation",{})
        self.state = {"trip_id":trip["id"],"run_id":run,"generation":"generation","requirements":req}
        self.stops = {"guide:a":{"name":"东山口","city":"广州"},"guide:b":{"name":"正佳广场","city":"广州"},
                      "guide:c":{"name":"广州塔","city":"广州"}}
        self.payload = {"available_stops":self.stops}


    def query(self, origin="guide:a", destination="guide:b", note="", mode="auto"):
        return RouteTurn(status="tools",tools=[RouteToolRequest(tool="route",city="广州",origin_id=origin,
                                                               destination_id=destination,request=note,mode=mode)])


    def test_place_status_keeps_detail_failure_separate_from_candidate_ambiguity(self):
        agent = RouteAgent(SimpleNamespace(),self.store)
        payload = {"available_stops":{"guide:a":{"name":"当前景点","map_issue":"旧的歧义"}}}
        agent.apply_tool_observation(self.state,payload,{"status":"unresolved",
            "request":{"tool":"place","destination_id":"guide:a"},"result":{
                "map_place":None,"map_selected_id":"chosen-poi","map_candidates":[{"id":"chosen-poi"}],
                "query_status":"unresolved","map_issue":"详情请求超时"}})
        stop = payload["available_stops"]["guide:a"]
        self.assertEqual(stop["map_selected_id"],"chosen-poi")
        self.assertEqual(stop["map_issue"],"详情请求超时")
        agent.apply_tool_observation(self.state,payload,{"status":"ok",
            "request":{"tool":"place","destination_id":"guide:a"},"result":{
                "map_place":{"id":"chosen-poi","location":"113.2,23.1"},"query_status":"resolved"}})
        self.assertNotIn("map_issue",stop)
        self.assertEqual(stop["query_status"],"resolved")
        self.assertEqual(stop["name"],"当前景点")


    def test_changed_transport_constraints_or_guide_feedback_do_not_share_cache(self):
        agent = RouteAgent(SimpleNamespace(),self.store)
        route = self.query().tools[0].model_dump(mode="json")
        first = agent.tool_key(self.state,self.payload,route,{})
        changed = {**self.state,"requirements":{**self.state["requirements"],"allow_taxi":True}}
        self.assertNotEqual(first,agent.tool_key(changed,self.payload,route,{}))
        feedback = {"tool":"guide_feedback","request":"减少活动"}
        alternate = {"tool":"guide_feedback","request":"增加室内活动"}
        self.assertNotEqual(agent.tool_key(self.state,self.payload,feedback,{}),agent.tool_key(self.state,self.payload,alternate,{}))
        place = {"tool":"place","city":"广州","destination_id":"guide:a"}
        decorated = {**place,"origin_id":"guide:b","request":"不用的出发点和备注"}
        self.assertEqual(agent.tool_key(self.state,self.payload,place,{}),agent.tool_key(self.state,self.payload,decorated,{}))
