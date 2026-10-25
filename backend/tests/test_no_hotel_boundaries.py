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

    def test_selected_departure_checks_station_and_ignores_first_hotel_leg(self):
        value = plan()
        value["transport"] = [{"id":"train","label":"广州到深圳","buffer_minutes":60,
            "context":{"date":DAY,"origin":"广州","destination":"深圳"},
            "selected":{"kind":"train","departure":"18:00","arrival":"19:00",
                        "departure_place":"广州南站","arrival_place":"深圳北站"}}]
        day = value["days"][0]
        day.update(end_place="广州南站",tail_arrival=660,return_move={"minutes":30,"mode":"transit"})
        self.assertEqual(daily_route_scope(value,day),
                         {"hotel_location_confirmed":False,"first_leg_required":False,"last_leg_required":True})
        self.assertEqual(confirmation_blockers(value,requirements=REQ),[])
        self.assertEqual(check_itinerary(value,REQ)["issues"],[])
        day["return_move"] = {"minutes":None}
        self.assertTrue(any("结束路段" in text for text in confirmation_blockers(value,requirements=REQ)))
        value["transport"][0]["selected"]["departure"] = None
        self.assertTrue(any("到离时间" in text for text in confirmation_blockers(value,requirements=REQ)))

    def test_selected_arrival_preserves_real_station_transfer_checks(self):
        value = plan()
        value["transport"] = [{"id":"train","label":"深圳到广州","buffer_minutes":60,
            "context":{"date":DAY,"origin":"深圳","destination":"广州"},
            "selected":{"kind":"train","departure":"07:00","arrival":"08:00",
                        "departure_place":"深圳北站","arrival_place":"广州南站"}}]
        day = value["days"][0]
        day["start_place"] = "广州南站"
        day["activities"][0]["move_before"] = {"minutes":20,"mode":"transit"}
        day["available_start"] = 480
        self.assertEqual(confirmation_blockers(value,requirements=REQ),[])
        self.assertEqual(check_itinerary(value,REQ)["issues"],[])
        day["activities"][0]["move_before"] = {"minutes":None}
        self.assertTrue(any("实际交通耗时" in text for text in confirmation_blockers(value,requirements=REQ)))
        day["activities"][0]["move_before"] = {"minutes":90,"mode":"transit"}
        issues = check_itinerary(value,REQ)["issues"]
        self.assertTrue(any(item["code"] == "move_gap" for item in issues))
        self.assertFalse(any("住宿" in item["message"] or "出发地址" in item["message"] for item in issues))


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
