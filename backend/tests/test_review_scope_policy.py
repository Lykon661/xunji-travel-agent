"""Review scope policy with synthetic data and mocked providers."""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from app.adapters import ToolError
from app.agents.reviewer import ReviewerAgent
from app.decisions import confirmation_blockers
from app.models import Constraint, Requirements, ReviewReport, REVIEW_DIMENSIONS
from app.planner import Planner
from app.reviewing import review_payload, review_scope
from app.rules import check_itinerary


DATE = "2026-10-16"
MUSEUM_HOURS = "7月-8月 周二-周日 09:00-21:00开放 最晚进入20:00； 1月-6月, 9月-12月 周二-周日 09:00-17:00开放 最晚进入16:00； 周一 全天不开放(法定节假日除外)"
REQ = Requirements(origin="广州",cities=["广州"],start_date=DATE,end_date=DATE,
                   people=1,rooms=1,budget=3000,return_to_origin=False).model_dump(mode="json")


def plan():
    return {"days":[{"date":DATE,"city":"广州","activities":[{
        "id":"guide:a","name":"合成场馆","kind":"visit","start":"09:00","end":"10:00",
        "source_id":"guide","evidence":{"quote":"合成攻略"},"map_place":{"location":"113.3,23.1"},
        "move_before":{}}],"available_start":540,"tail_deadline":1200,"return_move":{},"gaps":[]}],
        "hotels":[],"transport":[],"issues":[],"budget":{"status":"incomplete","unknown":["票价未计入"],
        "known_subtotal":0,"known_plus_estimates":100,"estimated_tickets":0}}


def report(message=None, path="/days/0", summary=None):
    value = {"status":"pass_with_warnings","summary":summary or "费用待核实，未发现开放时间冲突",
             "checks":[{"dimension":key,"result":"warning" if key == "budget" else "ok","note":"合成检查"}
                       for key in sorted(REVIEW_DIMENSIONS)],"findings":[]}
    if message:
        value.update(status="revise",summary=summary or message,findings=[{
            "code":"synthetic","severity":"blocker","owner":"route","action":"repair","target":"guide:a",
            "dates":[DATE],"field_path":path,"message":message,"evidence_ids":["requirements"],"suggestion":"按真实依据处理"}])
    return ReviewReport(**value)


class ScopePolicy(unittest.TestCase):
    def setUp(self):
        self.plan = plan()
        self.payload = {"plan":self.plan,"requirements":REQ,"evidence_index":{"requirements":REQ}}

    def transfer_report(self):
        first = self.plan["days"][0]["activities"][0]
        first.update(name="永庆坊",start="12:57",end="15:27")
        destination = copy.deepcopy(first)
        destination.update(id="guide:b",name="北京路",start="16:30",end="19:00",
            move_before={"provider":"amap","evidence_id":"query:route","minutes":43,"origin":"永庆坊","destination":"北京路"})
        self.plan["days"][0]["activities"].append(destination)
        value = report("永庆坊15:27结束，北京路16:30开始，预留63分钟，路线43分钟，换乘缓冲不足",path="/days/0/activities/1")
        value.findings[0].evidence_ids = ["query:route"]
        next(check for check in value.checks if check.dimension == "timing").result = "blocker"
        return value,destination


    def test_calendar_is_program_computed_and_unselected_hotel_does_not_enable_legs(self):
        self.plan["hotels"] = [{"city":"广州","selected":{"name":"仅推荐酒店"}}]
        scope = review_scope(self.plan)["days"][0]
        self.assertEqual(scope["weekday"],"星期五")
        self.assertFalse(scope["hotel_location_confirmed"])
        self.assertFalse(scope["first_leg_required"])
        self.assertFalse(scope["last_leg_required"])


    def test_selected_train_still_requires_station_transfer_without_a_hotel(self):
        self.plan["transport"] = [{"context":{"date":DATE,"origin":"广州","destination":"深圳"},
                                   "selected":{"name":"用户采用车次"}}]
        scope = review_scope(self.plan)["days"][0]
        self.assertFalse(scope["first_leg_required"])
        self.assertTrue(scope["last_leg_required"])


    def test_scope_is_present_in_payload_and_evidence_catalog(self):
        payload = review_payload({"requirements":REQ,"selected_source_ids":[]},self.plan,[],[])
        self.assertEqual(payload["review_scope"]["days"][0]["weekday"],"星期五")
        self.assertIn("review_scope",{item["evidence_id"] for item in payload["evidence_catalog"]})

    def museum(self):
        activity = self.plan["days"][0]["activities"][0]
        activity.update(start="13:13",end="16:13")
        activity["map_place"].update(id="museum",evidence_id="query:museum",opentime2=MUSEUM_HOURS)
        return activity

    def opening_report(self,message):
        value = report(message,path="/days/0/activities/0")
        value.findings[0].evidence_ids = ["query:museum"]
        return value


class TransportConnectionFacts(unittest.TestCase):


    def test_departure_slack_already_includes_the_station_buffer(self):
        from app.reviewing import review_scope
        itinerary = {"transport":[{"label":"广州 → 武汉","buffer_minutes":60,"context":{"origin":"广州","destination":"武汉","date":"2026-12-13"},
                                   "selected":{"departure":"2026-12-13 17:03:00","departure_place":"广州南站"}}],
                     "days":[{"date":"2026-12-13","city":"广州","return_end":"15:47","return_move":{"minutes":47},
                              "activities":[{"kind":"visit","name":"广州塔","start":"14:25","end":"14:40"}]}]}
        connection = review_scope(itinerary)["days"][0]["transport_connections"][0]
        self.assertEqual((connection["latest_station_arrival"],connection["slack_minutes"],connection["within_station_buffer"]),
                         ("16:03",16,True))
