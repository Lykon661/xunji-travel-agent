"""Synthetic production-wiring cases for the user to run; never call real providers."""
import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from app.adapters import ToolError
from app.agents.route import RouteAgent
from app.models import OrderChoice, Requirements, RouteToolRequest, RouteTurn
from app.places import canonicalize_stops
from app.planner import Planner
from app.store import Store


DATE, NEXT_DATE, CITY = "2026-11-10", "2026-11-11", "广州"


def source_stop(identity, name, **extra):
    return {"id":identity,"name":name,"city":CITY,"source_id":"guide","source_title":"合成攻略",
            "duration_minutes":90,"evidence":{"quote":name},**extra}


class DailyToolFlow(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name))
        requirements = Requirements(origin="北京",cities=[CITY],start_date=DATE,end_date=NEXT_DATE,
                                    people=1,rooms=1,budget=5000).model_dump(mode="json")
        trip = self.store.create_trip("合成旅行",requirements)
        run = self.store.start_run(trip["id"],0,"generation",{})
        self.day = {"date":DATE,"city":CITY,"stop_ids":["guide:a"]}
        later = {"date":NEXT_DATE,"city":CITY,"stop_ids":["guide:c"]}
        stops = canonicalize_stops({"guide:a":source_stop("guide:a","东山口"),
            "guide:b":source_stop("guide:b","正佳广场"),"guide:c":source_stop("guide:c","广州塔")})
        self.state = {"trip_id":trip["id"],"run_id":run,"generation":"generation","requirements":requirements,
            "stops":stops,"locks":[],"stop_constraints":{},"selected_route":{"days":[self.day,later]},
            "itinerary":{"days":[{"date":DATE,"city":CITY,"activities":[{"id":"guide:a","name":"东山口"}]},
                                  {"date":NEXT_DATE,"city":CITY,"activities":[{"id":"guide:c","name":"广州塔"}]}],
                         "transport":[],"hotels":[]}}
        self.planner = Planner.__new__(Planner)
        self.planner.store = self.store
        self.planner.emit = Mock()
        self.planner.amap = SimpleNamespace(cache={})
        self.planner.locate_stops = AsyncMock(side_effect=lambda state,pool:canonicalize_stops(
            {identity:{**stop,"map_place":None} for identity,stop in pool.items()}))
        self.planner.movement = AsyncMock(return_value=({"minutes":None,"estimate_minutes":45,"note":"未取得路线"},None))
        self.planner.map_order = AsyncMock(side_effect=lambda state,city,ids,*anchors:(list(ids),"合成顺序"))


    async def test_real_location_projection_preserves_opening_evidence_for_the_scheduler(self):
        place = {"id":"zoo-poi","name":"长隆野生动物世界","location":"113.315593,23.002660",
                 "evidence_id":"query:synthetic-opening","opentime2":"10月7日-10月31日:9:30-18:00","open_time":""}
        self.planner.amap.place = AsyncMock(return_value=place)
        self.planner.models = SimpleNamespace()
        result = await Planner.locate_stops(self.planner,self.state,{"guide:a":self.state["stops"]["guide:a"]})
        actual = result["guide:a"]["map_place"]
        self.assertEqual(actual["opentime2"],place["opentime2"])
        self.assertEqual(actual["open_time"],"")
        self.assertEqual(actual["evidence_id"],place["evidence_id"])
        self.planner.amap.place.assert_awaited_once()


    async def test_route_note_cannot_change_the_actual_returned_transport_mode(self):
        self.planner.movement.return_value = ({"minutes":12,"mode":"walking","walking_meters":800},None)
        request = RouteToolRequest(tool="route",city=CITY,origin_id="guide:a",destination_id="guide:b",request="必须只查地铁")
        value = await self.planner.agent_tool(self.state,request)
        self.assertEqual(value["query_status"],"resolved")
        self.assertEqual(value["mode"],"walking")
        self.assertIn("request备注不改变交通方式",value["tool_scope"])


    async def test_explicit_driving_cannot_bypass_public_transport_and_no_taxi_constraints(self):
        self.state["requirements"].update(local_transport="public",allow_taxi=False)
        self.planner.amap.route = AsyncMock()
        request = RouteToolRequest(tool="route",city=CITY,origin_id="guide:a",destination_id="guide:b",mode="driving")
        with self.assertRaises(ToolError) as failure:
            await self.planner.agent_tool(self.state,request)
        self.assertEqual(failure.exception.code,"agent_tool_scope")
        self.planner.amap.route.assert_not_awaited()
        self.planner.locate_stops.assert_not_awaited()
