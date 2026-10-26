"""Route reference aliases with synthetic data."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from app.adapters import ToolError
from app.agents.route import RouteAgent
from app.agents.runtime import replace_references
from app.models import Candidate, CandidateDay, OrderChoice, Requirements, RouteToolRequest, RouteTurn
from app.planner import Planner
from app.store import Store


SOURCE = "7eeab0fbbdab438788319dbdfde98bd4"
TYPO = "7eeab0fbbdab438788319dbde98bd4"
IDS = [SOURCE+":"+suffix for suffix in ("6","8","9")]
ALIASES = ["stop_1","stop_2","stop_3"]
DATE, CITY = "2026-11-10", "广州"


class RouteReferenceAliases(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name))
        req = Requirements(origin=CITY,cities=[CITY],start_date=DATE,end_date=DATE,
            people=1,rooms=1,budget=3000).model_dump(mode="json")
        trip = self.store.create_trip("合成路线编号",req)
        run = self.store.start_run(trip["id"],0,"generation",{})
        self.stops = {identity:{"id":identity,"name":name,"city":CITY,"source_id":SOURCE,
            "source_title":"合成攻略","duration_minutes":45,"evidence":{"quote":name}}
            for identity,name in zip(IDS,("花城汇","二沙岛艺术公园","宏城公园"))}
        self.day = {"date":DATE,"city":CITY,"stop_ids":list(IDS)}
        self.state = {"trip_id":trip["id"],"run_id":run,"generation":"generation","requirements":req,
            "stops":self.stops,"selected_route":{"days":[self.day]},"stop_constraints":{},"locks":[]}
        self.payload = {"city":CITY,"available_stops":self.stops,"current_stop_ids":list(IDS),
            "allowed_stop_ids":list(IDS),"locked_ids":[IDS[0]],
            "opening_constraints":{IDS[0]:{"windows":[[660,1320]]}}}

    def agent(self,responses):
        self.model = SimpleNamespace(ask=AsyncMock(side_effect=responses))
        return RouteAgent(self.model,self.store)


    async def test_candidate_dates_and_stop_ids_decode_before_global_validation(self):
        turn = RouteTurn(status="ready",candidate=Candidate(title="候选",reason="所选攻略",
            days=[CandidateDay(date=DATE,city=CITY,stop_ids=ALIASES)]))
        agent = self.agent([turn])
        result = await agent.plan(self.state,"安排整趟",self.payload,AsyncMock())
        self.assertEqual(result.candidate.days[0].stop_ids,IDS)


    def test_no_fuzzy_recovery_and_no_dictionary_value_is_silently_overwritten(self):
        agent = RouteAgent(SimpleNamespace(),self.store)
        references = agent.model_references(self.state,self.payload)
        typo = TYPO+":6"
        self.assertEqual(replace_references(typo,references),typo)
        with self.assertRaises(ToolError):
            replace_references({"stop_1":45,IDS[0]:90},references)
        pool = {**self.stops,"stop_1":{"name":"stop_2","city":CITY}}
        aliases = agent.model_references(self.state,{"available_stops":pool})
        self.assertFalse(set(aliases) & {"stop_1","stop_2"})
