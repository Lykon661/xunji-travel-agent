"""Synthetic regression cases for the user to run; no live model or provider calls."""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from app.adapters import ToolError
from app.models import Constraint, OrderChoice, RouteTurn
from app.planner import Planner
from app.routing import early_finish, reference_opening
from app.rules import check_itinerary


DATE = "2026-11-10"
ZOO_DATE = "2026-10-16"
ZOO_HOURS = "10月1日-10月6日:8:30-19:00；10月7日-10月31日:9:30-18:00注:营业时间与设施运营计划如有调整，请以官网/现场公示为准"
MUSEUM_HOURS = "7月-8月 周二-周日 09:00-21:00开放 最晚进入20:00； 1月-6月, 9月-12月 周二-周日 09:00-17:00开放 最晚进入16:00； 周一 全天不开放(法定节假日除外)"
REQ = {"origin":"北京","cities":["杭州"],"start_date":DATE,"end_date":DATE,
       "people":1,"rooms":1,"budget":5000,"preferences":"多点玩的项目，不怕走路","transport":"train"}


def stop(identity, duration=None, **extra):
    return {"id":identity,"name":identity,"city":"杭州","duration_minutes":duration,
            "source_id":"guide","source_title":"合成攻略","evidence":{"quote":identity},**extra}


def ready(ids, **kwargs):
    return RouteTurn(status="ready",order=OrderChoice(stop_ids=ids,**kwargs))


def opening_place(claim="周一至周日 11:00-22:00"):
    return {"id":"mall","name":"花城汇","location":"113.324606,23.118082",
            "evidence_id":"query:synthetic-opening","opentime2":claim,"open_time":"11:00-22:00"}


class OpeningReferences(unittest.TestCase):
    def test_explicit_map_closure_is_not_replaced_by_guide_hours(self):
        museum = stop("museum",opening_claim="09:00-17:00",
            map_place=opening_place("周二至周日 09:00-17:00；周一 闭馆"))
        monday = reference_opening(museum,"2026-10-12")
        self.assertTrue(monday["closed"])
        self.assertEqual(monday["windows"],[])
        self.assertEqual(reference_opening(museum,"2026-10-13")["windows"],[[540,1020]])



    def test_evidenced_daily_map_hours_work_without_a_guide_claim(self):
        value = reference_opening(stop("mall",45,map_place=opening_place()))
        self.assertEqual(value["windows"],[[660,1320]])
        self.assertEqual(value["evidence_id"],"query:synthetic-opening")
        self.assertIn("不代表",value["note"])






class CoveragePolicy(unittest.TestCase):
    def test_spare_afternoon_is_detected_from_visits_instead_of_waiting_blocks(self):
        day = {"available_start":540,"tail_deadline":1200,"activities":[
            {"kind":"visit","end":"14:30"},{"kind":"rest","end":"19:00"}]}
        self.assertEqual(early_finish(day)["last_visit_end"],"14:30")

    def test_early_return_or_long_station_transfer_does_not_request_filler(self):
        day = {"available_start":540,"tail_deadline":900,"activities":[{"kind":"visit","end":"14:00"}]}
        self.assertIsNone(early_finish(day))
        day.update(tail_deadline=1080,return_move={"minutes":160})
        self.assertIsNone(early_finish(day))

    def test_short_available_window_and_complete_afternoon_are_exempt(self):
        day = {"available_start":900,"tail_deadline":1200,"activities":[{"kind":"visit","end":"16:00"}]}
        self.assertIsNone(early_finish(day))
        day.update(available_start=540,activities=[{"kind":"visit","end":"17:30"}])
        self.assertIsNone(early_finish(day))
