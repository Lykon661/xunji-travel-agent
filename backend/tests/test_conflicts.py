"""Synthetic day-capacity and stop-pool questions built by code; no model, map or supplier calls."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app.agents.runtime import digest
from app.adapters import ToolError
from app.agents.route import RouteAgent
from app.conflicts import apply_option
from app.constraints import merge
from app.models import ConstraintDraft
from app.constraints import active
from app.models import Requirements
from app.rules import constraint_issues
from app.planner import Planner


A, B = "2026-11-10", "2026-11-11"
REQ = Requirements(origin="上海",cities=["杭州"],start_date=A,end_date=B,people=2,rooms=1,budget=6000).model_dump(mode="json")
STOPS = {identity:{"id":identity,"name":name,"city":"杭州","duration_minutes":minutes,"source_id":"s1"}
         for identity,name,minutes in (("xh","西湖",240),("ly","灵隐寺",180),("ld","雷峰塔",60))}


def visit(identity, start, end):
    return {"id":identity,"kind":"visit","name":STOPS[identity]["name"],"start":start,"end":end}


def day(date, *activities, start=540, deadline=1020):
    return {"date":date,"city":"杭州","available_start":start,"tail_deadline":deadline,"activities":list(activities),"gaps":[]}


def state(required=("xh","ly")):
    return {"requirements":{**REQ,"required_stop_ids":list(required)},"stops":dict(STOPS),"locks":[],"change":{},
            "stop_constraints":{"required":list(required),"excluded":[]},"selected_source_ids":["s1"],
            "selected_route":{"days":[{"date":A,"city":"杭州","stop_ids":["xh"]},{"date":B,"city":"杭州","stop_ids":["ld"]}]},
            "candidates":[{"id":"draft","days":[{"date":A,"city":"杭州","stop_ids":["xh"]},{"date":B,"city":"杭州","stop_ids":["ld"]}]}]}


def itinerary():
    return {"days":[day(A,visit("xh","09:00","13:00")),day(B,visit("ld","09:00","10:00"))]}


class Capacity(unittest.TestCase):


    def test_exclude_moves_a_required_stop_to_excluded(self):
        updates,_,_ = apply_option(state(),{"type":"exclude","stop_id":"ly","date":A},"这次不去灵隐寺")
        self.assertEqual(updates["stop_constraints"],{"required":["xh"],"excluded":["ly"]})
        self.assertEqual(updates["requirements"]["excluded_stop_ids"],["ly"])


def minimum(count, **scope):
    return merge([],[ConstraintDraft(kind="daily_min_stops",count=count,scope=scope,quote="每天至少三个景点").model_dump(mode="json")],"form")


def accepted(count):
    return merge([],[ConstraintDraft(kind="daily_min_stops",strength="soft",count=count,quote="每天至少三个景点").model_dump(mode="json")],"decision")


def uneven():
    """Day A holds all three stops and day B none, under an accepted (soft) minimum of two."""
    current = {**state(required=()),"stops":dict(STOPS)}
    current["requirements"] = {**current["requirements"],"constraints":accepted(2)}
    current["candidates"] = [{"id":"draft","days":[{"date":A,"city":"杭州","stop_ids":["xh","ly","ld"]},{"date":B,"city":"杭州","stop_ids":[]}]}]
    current["selected_route"] = {"days":current["candidates"][0]["days"]}
    plan = {"days":[day(A,visit("xh","09:00","11:00"),visit("ly","11:30","13:00"),visit("ld","13:30","14:00")),day(B)]}
    return current,plan


if __name__ == "__main__":
    unittest.main()
