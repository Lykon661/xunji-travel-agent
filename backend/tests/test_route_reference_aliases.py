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


def ready(ids=ALIASES,**extra):
    return RouteTurn(status="ready",order=OrderChoice(stop_ids=list(ids),**extra))


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

    async def test_order_timing_constraints_and_source_references_round_trip_without_mutating_the_pool(self):
        original = copy.deepcopy(self.payload)
        agent = self.agent([ready(durations={"stop_1":45},start_times={"stop_1":"11:00"})])
        result = await agent.plan(self.state,"安排当天",self.payload,AsyncMock(),daily=True)
        sent = self.model.ask.call_args.args[2]
        self.assertEqual(list(sent["available_stops"]),ALIASES)
        self.assertEqual(sent["available_stops"]["stop_1"]["id"],"stop_1")
        self.assertEqual(sent["current_stop_ids"],ALIASES)
        self.assertEqual(sent["allowed_stop_ids"],ALIASES)
        self.assertEqual(sent["locked_ids"],["stop_1"])
        self.assertEqual(set(sent["opening_constraints"]),{"stop_1"})
        self.assertEqual(result.order.stop_ids,IDS)
        self.assertEqual(result.order.durations,{IDS[0]:45})
        self.assertEqual(result.order.start_times,{IDS[0]:"11:00"})
        self.assertEqual(self.payload,original)

    async def test_tools_and_cached_observations_keep_real_ids_while_model_observations_use_short_ids(self):
        tool_turn = RouteTurn(status="tools",tools=[RouteToolRequest(tool="route",city=CITY,
            origin_id="stop_1",destination_id="stop_2")])
        agent = self.agent([tool_turn,ready()])
        tool = AsyncMock(return_value={"origin_id":IDS[0],"destination_id":IDS[1],"minutes":9,"mode":"walking"})
        first = await agent.plan(self.state,"规划并查路线",self.payload,tool,daily=True)
        request = tool.call_args.args[0]
        self.assertEqual((request.origin_id,request.destination_id),tuple(IDS[:2]))
        observation = self.model.ask.call_args.args[2]["tool_observations"][0]
        self.assertEqual(observation["request"]["origin_id"],"stop_1")
        self.assertEqual(observation["result"]["destination_id"],"stop_2")
        with self.store.connect() as db:
            raw = db.execute("SELECT request FROM queries WHERE trip_id=?",(self.state["trip_id"],)).fetchone()[0]
        self.assertEqual(json.loads(raw)["origin_id"],IDS[0])
        again = await agent.plan(self.state,"规划并查路线",self.payload,tool,daily=True)
        self.assertEqual(first.order.stop_ids,IDS)
        self.assertEqual(again.order.stop_ids,IDS)
        self.assertEqual(self.model.ask.await_count,2)
        tool.assert_awaited_once()

    async def test_candidate_dates_and_stop_ids_decode_before_global_validation(self):
        turn = RouteTurn(status="ready",candidate=Candidate(title="候选",reason="所选攻略",
            days=[CandidateDay(date=DATE,city=CITY,stop_ids=ALIASES)]))
        agent = self.agent([turn])
        result = await agent.plan(self.state,"安排整趟",self.payload,AsyncMock())
        self.assertEqual(result.candidate.days[0].stop_ids,IDS)

    async def test_current_reported_typo_gets_a_short_id_correction_and_is_not_cached_as_completed(self):
        bad_ids = [TYPO+":"+suffix for suffix in ("6","8","9")]
        agent = self.agent([ready(bad_ids),ready(durations={"stop_1":45},start_times={"stop_1":"11:00"})])
        planner = Planner.__new__(Planner)
        planner.route_agent,planner.emit = agent,Mock()
        result = await planner.daily_order(self.state,self.day,list(IDS),set(),{})
        self.assertEqual(result.order.stop_ids,IDS)
        self.assertEqual(self.model.ask.await_count,2)
        correction = self.model.ask.call_args.args[2]
        self.assertEqual(correction["reference_correction"]["allowed_stop_ids"],ALIASES)
        self.assertEqual(correction["reference_correction"]["invalid_ids"],bad_ids)
        with self.store.connect() as db:
            saved = [json.loads(row[0]) for row in db.execute("SELECT document FROM agent_results WHERE trip_id=?",(self.state["trip_id"],))]
        self.assertTrue(saved)
        self.assertFalse(any(TYPO in json.dumps(value,ensure_ascii=False) for value in saved))

    async def test_repeated_unknown_reference_is_rejected_and_never_replayed_as_success(self):
        agent = self.agent([ready(["stop_999"]),ready(["stop_999"])])
        for _ in range(2):
            result = await agent.plan(self.state,"完全相同的日内请求",self.payload,AsyncMock(),daily=True)
            self.assertEqual(result.order.stop_ids,["stop_999"])
            self.assertFalse(agent.result_cacheable(self.state,self.payload,result))
        self.assertEqual(self.model.ask.await_count,2)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM agent_results WHERE trip_id=?",(self.state["trip_id"],)).fetchone()[0],0)

    async def test_same_alias_on_another_day_cannot_replay_a_different_full_id(self):
        agent = self.agent([ready(["stop_1"]),ready(["stop_1"])])
        first_pool = {IDS[0]:self.stops[IDS[0]]}
        second_pool = {IDS[1]:self.stops[IDS[1]]}
        first = await agent.plan(self.state,"安排一天",{"available_stops":first_pool},AsyncMock(),daily=True)
        second = await agent.plan(self.state,"安排一天",{"available_stops":second_pool},AsyncMock(),daily=True)
        self.assertEqual(first.order.stop_ids,[IDS[0]])
        self.assertEqual(second.order.stop_ids,[IDS[1]])

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
