import copy
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import httpx

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import StateGraph, START, END
from langgraph.types import Command, interrupt as legacy_interrupt

from app import authority
from app.coordinator import run
from app.conflicts import visit_minutes
from app.models import InterviewResponse, REVIEW_DIMENSIONS, Requirements, wish_stop_id
from app.nodes.coordinating import pending_batch, public_document
from app.planner import Planner
from app.reviewing import bind_report, review_basis
from app.rules import check_itinerary
from app.store import Conflict, Store
from app.workflow.state import TripState
from app.unmet import fingerprint
from tests.test_actions import DAY, OTHER, state
from tests.test_coordinator import CASES, agent, question, unmet


class CoordinatorGraph(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name))
        self.initial = state()
        self.initial["requirements"]["self_arranged_round_trip"] = True
        self.initial["requirements"]["preferences"] = ""
        self.initial["requirements"]["constraints_basis"] = ""
        for index,stop in enumerate(self.initial["stops"].values()):
            stop["map_place"] = {"id":"map-"+stop["id"],"name":stop["name"],"location":f"120.{index+1},30"}
            stop["ticket_claim"] = "免费"
        for day in self.initial["itinerary"]["days"]:
            day["walking_meters"] = 0
            for index,item in enumerate(day["activities"]):
                item.update(self.initial["stops"][item["id"]])
                item["start"],item["end"] = item["start"].zfill(5),item["end"].zfill(5)
                item["move_before"] = {"minutes":10,"mode":"walking","walking_meters":100} if index else {}
        self.trip = self.store.create_trip("主理人图回归",self.initial["requirements"])
        self.store.add_source(self.trip["id"],{"id":"guide","title":"合成攻略","kind":"manual","text":"合成资料",
            "extraction_version":4,"extraction_cities":["杭州"],"extraction":{"stops":list(self.initial["stops"].values())}})
        doc = self.store.trip(self.trip["id"])["document"]
        doc["selected_source_ids"] = ["guide"]
        self.store.update(self.trip["id"],doc)
        self.planner = Planner.__new__(Planner)
        self.planner.store,self.planner.tasks,self.planner.gates = self.store,{},{}
        self.planner._checkpointer = InMemorySaver()
        self.planner.models = SimpleNamespace(ask=AsyncMock(side_effect=AssertionError("Unexpected direct model call")))
        self.planner.amap = SimpleNamespace(cache={},batch=nullcontext)
        self.planner.flyai = SimpleNamespace(cache={})
        self.planner.coordinator_agent = agent()
        self.planner.coordinate = AsyncMock(return_value={})
        self.planner.after_coordinate = lambda value:"guides"
        self.planner.prepare_guides = AsyncMock(side_effect=lambda value:{"stops":copy.deepcopy(self.initial["stops"])})
        self.planner.guide_analysis = AsyncMock(return_value={"unmet":[],"coordinator_resume":"discover"})
        self.planner.discover = AsyncMock(side_effect=self.discover)
        self.planner.assess = AsyncMock(side_effect=self.assess)
        self.planner.activities = AsyncMock(side_effect=self.activities)
        self.planner.tickets = AsyncMock(side_effect=lambda value:{"itinerary":copy.deepcopy(value["itinerary"])})
        self.planner.audit = AsyncMock(side_effect=self.audit)
        self.problems = []

    async def discover(self,value):
        return {"candidates":copy.deepcopy(self.initial["candidates"]),"coordinator_resume":"assess","unmet":[]}

    async def assess(self,value):
        plan = copy.deepcopy(self.initial["itinerary"])
        for day in plan["days"]:
            for item in day["activities"]:
                item.update({key:value["stops"][item["id"]][key] for key in ("map_place","name")})
        plan.update(check_itinerary(plan,value["requirements"],value["stops"]))
        report = await self.audit({**value,"draft_revision":1},plan)
        route = copy.deepcopy(value["candidates"][0])
        route.update(draft=copy.deepcopy(plan),review=report,draft_revision=1)
        return {"itinerary":plan,"candidates":[route],"review":report,"draft_revision":1,
                "unmet":copy.deepcopy(self.problems),"coordinator_resume":"review_gate"}

    async def activities(self,value):
        plan = copy.deepcopy(value["itinerary"])
        for day in plan["days"]:
            if day["date"] not in value["change"]["dates"]:
                continue
            route = next(row for row in value["selected_route"]["days"] if row["date"] == day["date"])
            existing = {item["id"]:item for item in day["activities"]}
            day["activities"] = []
            clock = 540
            lunch = False
            for index,identity in enumerate(route["stop_ids"]):
                item = copy.deepcopy(existing.get(identity) or {**value["stops"][identity],"kind":"visit","move_before":{}})
                duration = visit_minutes(value["stops"][identity],value["requirements"],day["date"],
                    value.get("system_durations",{}).get(day["date"],{}).get(identity,90))
                if not lunch and clock+duration > 720:
                    day["activities"].append({"id":"lunch-"+day["date"],"kind":"rest","meal":"lunch","name":"午餐",
                                              "start":"12:00","end":"13:00"})
                    clock = max(clock,780)+(10 if index else 0)
                    lunch = True
                item["move_before"] = {"minutes":10,"mode":"walking","walking_meters":100} if index else {}
                item.update(start=f"{clock//60:02}:{clock%60:02}",end=f"{(clock+duration)//60:02}:{(clock+duration)%60:02}",duration_minutes=duration)
                clock += duration+30
                day["activities"].append(item)
        return {"itinerary":plan,"stops":copy.deepcopy(value["stops"])}

    async def audit(self,value,plan):
        return bind_report({"status":"pass","summary":"合成审查通过","findings":[],"unmet":[],
            "checks":[{"dimension":dimension,"result":"ok","note":"合成检查"} for dimension in sorted(REVIEW_DIMENSIONS)]},
            value,plan,review_basis(value,plan,self.store.sources(self.trip["id"]),[]),[])

    def context(self):
        thread = "graph-"+uuid4().hex
        previous = self.store.trip(self.trip["id"])["active_run"]
        if previous:
            self.store.finish_run(previous,"waiting",{})
        run_id = self.store.start_run(self.trip["id"],0,thread,{})
        initial = copy.deepcopy(self.initial)
        initial.update(trip_id=self.trip["id"],run_id=run_id,generation=thread,mode="new",base_version=0,
            candidates=[],itinerary={},selected_route={},warnings=[],change={},message="",draft_revision=0)
        return initial,{"configurable":{"thread_id":thread}},run_id

    async def invoke(self):
        initial,config,run_id = self.context()
        result = await self.planner.graph.ainvoke(initial,config)
        snapshot = await self.planner.graph.aget_state(config)
        return result,snapshot,config,run_id

    def duration_question(self):
        return question({"type":"shorten","stop_id":"a","date":DAY,"minutes":60},
                        {"type":"shorten","stop_id":"a","date":DAY,"minutes":50},refs=[{"type":"stop","id":"a"}])

    def user_problem(self):
        self.problems = [unmet()]
        self.planner.coordinator_agent = agent({"needs_user":[{"unmet_indexes":[0],"reason_kind":"needs_user_authority",
            "question":self.duration_question()}]})
















