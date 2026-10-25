import unittest
from unittest.mock import AsyncMock

from app.conflicts import day_demand
from app.decisions import confirmation_blockers
from app.models import Requirements
from app.planner import Planner
from app.routing import daily_movements, daily_route_scope
from app.rules import check_itinerary


DAY = "2026-10-26"
REQ = Requirements(origin="深圳",cities=["广州"],start_date=DAY,end_date=DAY,
                   people=1,rooms=1,budget=3000).model_dump(mode="json")


def plan():
    return {"days":[{"date":DAY,"city":"广州","available_start":540,"tail_deadline":1200,
        "start_place":"旧酒店","end_place":"旧酒店","tail_arrival":1500,
        "activities":[{"id":"park","kind":"visit","name":"合成公园","start":"09:00","end":"10:00",
            "evidence":{"quote":"公园"},"map_place":{"location":"113,23"},
            "move_before":{"minutes":1000,"mode":"driving","walking_meters":100000}}],
        "return_move":{"minutes":None,"estimate_minutes":1000,"mode":"driving","walking_meters":100000},
        "transfers":[{"label":"到达后前往住宿放行李","move":{"minutes":None}}]}],
        "hotels":[{"id":"hotel","city":"广州","selected":{"name":"推荐酒店"}}],"transport":[],"issues":[]}


class NoHotelBoundaries(unittest.TestCase):
    def test_stale_hotel_legs_do_not_affect_rules_confirmation_or_capacity(self):
        value = plan()
        value["issues"] = [{"code":"move_gap","target":"park","blocking":True,"message":"前往首站缺少出发地址"},
                           {"code":"tail_conflict","target":DAY,"blocking":True,"message":"返回住宿超时"}]
        self.assertEqual(confirmation_blockers(value,requirements=REQ),[])
        result = check_itinerary(value,REQ)
        self.assertEqual(result["issues"],[])
        self.assertFalse(any("移动时间" in text or "返回住宿" in text for text in result["information_unknown"]))
        self.assertEqual(daily_movements(value,value["days"][0]),[{},{}])
        items = day_demand({"requirements":REQ,"stops":{}},value,value["days"][0],{})
        self.assertEqual(items[0]["move"],0)




class NoHotelWindow(unittest.IsolatedAsyncioTestCase):
    async def test_window_does_not_report_missing_address_or_query_hotel_legs(self):
        planner = Planner.__new__(Planner)
        planner.movement = AsyncMock()
        value = plan()
        _,_,gaps,transfers,tail = await planner.window({"requirements":REQ,"itinerary":value},value["days"][0])
        self.assertEqual(gaps,[])
        self.assertEqual(transfers,[])
        self.assertIsNone(tail["start_name"])
        self.assertIsNone(tail["name"])
        planner.movement.assert_not_awaited()
