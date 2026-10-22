"""Route tool-loop completion with mocked external calls."""
import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pydantic import ValidationError

from app.adapters import ToolError
from app.agents.route import DailyRouteCompletion, RouteAgent, RouteCompletion, resolve_stop_id
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

    def test_completion_schema_cannot_request_more_tools_or_return_the_wrong_shape(self):
        request = RouteTurn(status="tools",tools=[RouteToolRequest(tool="weather",city="广州")])
        for schema in (RouteCompletion,DailyRouteCompletion):
            with self.assertRaises(ValidationError):
                schema.model_validate(request.model_dump(mode="json"))
        with self.assertRaises(ValidationError):
            DailyRouteCompletion(status="ready",candidate=Candidate(title="一天",reason="草稿",days=[{"date":"2026-11-10","city":"广州"}]))
        with self.assertRaises(ValidationError):
            RouteCompletion(status="ready",order=OrderChoice(stop_ids=["guide:a"]))

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

    def ready(self):
        return RouteTurn(status="ready",order=OrderChoice(stop_ids=["guide:a","guide:b"],explanation="按既有依据安排"))

    def query(self, origin="guide:a", destination="guide:b", note="", mode="auto"):
        return RouteTurn(status="tools",tools=[RouteToolRequest(tool="route",city="广州",origin_id=origin,
                                                               destination_id=destination,request=note,mode=mode)])

    async def test_fourth_call_is_a_structurally_tool_free_completion(self):
        model = SimpleNamespace(ask=AsyncMock(side_effect=[self.query(),self.query("guide:b","guide:c"),
                                                          self.query("guide:a","guide:c"),self.ready()]))
        tool = AsyncMock(return_value={"minutes":15,"mode":"transit"})
        result = await RouteAgent(model,self.store).plan(self.state,"安排当天",self.payload,tool,daily=True)
        self.assertEqual(result.status,"ready")
        self.assertEqual(model.ask.await_count,4)
        self.assertEqual(tool.await_count,3)
        final = model.ask.call_args
        self.assertIs(final.args[0],DailyRouteCompletion)
        self.assertEqual(final.args[2]["remaining_tool_rounds"],0)
        self.assertEqual(len(final.args[2]["tool_observations"]),3)

    async def test_repeated_failure_with_different_notes_is_not_reexecuted(self):
        model = SimpleNamespace(ask=AsyncMock(side_effect=[self.query(note="先查询"),self.query(note="重新核实"),self.ready()]))
        tool = AsyncMock(side_effect=ToolError("agent_tool_scope","必须使用现有完整 ID"))
        result = await RouteAgent(model,self.store).plan(self.state,"安排当天",self.payload,tool,daily=True)
        self.assertEqual(result.status,"ready")
        self.assertEqual(tool.await_count,1)
        self.assertEqual(model.ask.await_count,3)
        self.assertIs(model.ask.call_args.args[0],DailyRouteCompletion)
        observations = model.ask.call_args.args[2]["tool_observations"]
        self.assertEqual(len(observations),1)
        self.assertEqual(observations[0]["status"],"error")

    async def test_different_modes_for_the_same_leg_do_not_share_a_tool_cache_entry(self):
        model = SimpleNamespace(ask=AsyncMock(side_effect=[self.query(mode="walking"),self.query(mode="transit"),self.ready()]))
        tool = AsyncMock(side_effect=[{"minutes":12,"mode":"walking"},{"minutes":18,"mode":"transit"}])
        await RouteAgent(model,self.store).plan(self.state,"比较用户允许的步行与公共交通",self.payload,tool,daily=True)
        self.assertEqual(tool.await_count,2)
        self.assertEqual([call.args[0].mode for call in tool.call_args_list],["walking","transit"])
        observations = model.ask.call_args.args[2]["tool_observations"]
        self.assertEqual([item["request"]["mode"] for item in observations],["walking","transit"])

    async def test_unique_names_and_ids_share_one_tool_request(self):
        turn = RouteTurn(status="tools",tools=[
            RouteToolRequest(tool="route",origin_id="东山口",destination_id="正佳广场",request="查询交通"),
            RouteToolRequest(tool="route",city="广州",origin_id="guide:a",destination_id="guide:b",request="同一路线")])
        model = SimpleNamespace(ask=AsyncMock(side_effect=[turn,self.ready()]))
        tool = AsyncMock(return_value={"minutes":15,"mode":"transit"})
        await RouteAgent(model,self.store).plan(self.state,"安排当天",self.payload,tool,daily=True)
        self.assertEqual(tool.await_count,1)
        actual = tool.call_args.args[0]
        self.assertEqual((actual.city,actual.origin_id,actual.destination_id),("广州","guide:a","guide:b"))

    async def test_success_is_reused_when_only_the_planning_instruction_changes(self):
        model = SimpleNamespace(ask=AsyncMock(side_effect=[self.query(note="先查"),self.ready(),self.query(note="后查"),self.ready()]))
        tool = AsyncMock(return_value={"minutes":15,"mode":"transit"})
        agent = RouteAgent(model,self.store)
        await agent.plan(self.state,"安排当天",self.payload,tool,daily=True)
        await agent.plan(self.state,"根据新排程反馈重排",self.payload,tool,daily=True)
        self.assertEqual(tool.await_count,1)
        self.assertEqual(model.ask.await_count,4)
        self.assertIs(model.ask.call_args.args[0],RouteTurn)

    async def test_cached_first_leg_still_allows_a_followup_leg_query(self):
        model = SimpleNamespace(ask=AsyncMock(side_effect=[self.query(),self.ready(),self.query(),
                                                          self.query("guide:b","guide:c"),self.ready()]))
        tool = AsyncMock(return_value={"minutes":15,"mode":"transit"})
        agent = RouteAgent(model,self.store)
        await agent.plan(self.state,"先查询AB",self.payload,tool,daily=True)
        await agent.plan(self.state,"结合AB继续决定是否查询BC",self.payload,tool,daily=True)
        self.assertEqual(tool.await_count,2)
        self.assertEqual(model.ask.await_count,5)
        for call in model.ask.call_args_list[2:]:
            self.assertIs(call.args[0],RouteTurn)
        self.assertEqual(len(model.ask.call_args.args[2]["tool_observations"]),2)

    async def test_newly_attached_place_coordinates_do_not_trigger_the_same_lookup_again(self):
        request = RouteTurn(status="tools",tools=[RouteToolRequest(tool="place",city="广州",destination_id="guide:a")])
        model = SimpleNamespace(ask=AsyncMock(side_effect=[request,request,self.ready()]))
        async def locate(_request):
            self.stops["guide:a"]["map_place"] = {"location":"113.2,23.1"}
            self.stops["guide:a"]["canonical_place_id"] = "poi-a"
            return {"map_place":self.stops["guide:a"]["map_place"],"query_status":"resolved"}
        tool = AsyncMock(side_effect=locate)
        await RouteAgent(model,self.store).plan(self.state,"安排当天",self.payload,tool,daily=True)
        self.assertEqual(tool.await_count,1)
        self.assertIs(model.ask.call_args.args[0],DailyRouteCompletion)

    async def test_cached_place_replays_only_missing_coordinates_in_the_current_pool(self):
        request = RouteTurn(status="tools",tools=[RouteToolRequest(tool="place",city="广州",destination_id="guide:a")])
        model = SimpleNamespace(ask=AsyncMock(side_effect=[request,self.ready(),request,self.ready()]))
        place = {"id":"poi-a","name":"东山口","location":"113.2,23.1","evidence_id":"query:123"}
        tool = AsyncMock(return_value={"name":"旧名称不可回填","duration_minutes":999,"map_place":place,"query_status":"resolved"})
        agent = RouteAgent(model,self.store)
        pristine = copy.deepcopy(self.payload)
        await agent.plan(self.state,"先查询地点",self.payload,tool,daily=True)
        await agent.plan(self.state,"新排程复用地点",pristine,tool,daily=True)
        self.assertEqual(tool.await_count,1)
        stop = pristine["available_stops"]["guide:a"]
        self.assertEqual(stop["map_place"],place)
        self.assertEqual(stop["name"],"东山口")
        self.assertNotIn("duration_minutes",stop)
        # A completed-result cache hit must replay observations too, with no model or tool call.
        again = {"available_stops":copy.deepcopy(self.stops)}
        again["available_stops"]["guide:a"].pop("map_place",None)
        await agent.plan(self.state,"先查询地点",again,tool,daily=True)
        self.assertEqual(again["available_stops"]["guide:a"]["map_place"],place)
        self.assertEqual(model.ask.await_count,4)
        newer = {"id":"newer-poi","location":"113.3,23.2"}
        stop["map_place"] = newer
        agent.apply_tool_observation(self.state,pristine,{"status":"ok","request":{"tool":"place","destination_id":"guide:a"},"result":{"map_place":place}})
        self.assertEqual(stop["map_place"],newer)

    async def test_unresolved_result_is_a_gap_and_is_not_persisted_as_success(self):
        model = SimpleNamespace(ask=AsyncMock(side_effect=[self.query(),self.query(note="再查"),self.ready(),
                                                          self.query(),self.ready()]))
        tool = AsyncMock(return_value={"minutes":None,"query_status":"unresolved","note":"公共交通无可用依据"})
        agent = RouteAgent(model,self.store)
        await agent.plan(self.state,"安排当天",self.payload,tool,daily=True)
        observations = model.ask.call_args.args[2]["tool_observations"]
        self.assertEqual(observations[0]["status"],"unresolved")
        self.assertIsNone(observations[0]["result"]["minutes"])
        self.assertEqual(tool.await_count,1)
        await agent.plan(self.state,"有新上下文后继续",self.payload,tool,daily=True)
        self.assertEqual(tool.await_count,2)

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

    async def test_name_correction_cannot_expand_the_daily_stop_pool(self):
        model = SimpleNamespace(ask=AsyncMock(side_effect=[
            RouteTurn(status="tools",tools=[RouteToolRequest(tool="place",city="广州",destination_id="广州塔")]),self.ready()]))
        tool = AsyncMock(side_effect=ToolError("agent_tool_scope","不在当天景点池"))
        state = {**self.state,"stops":self.stops}
        await RouteAgent(model,self.store).plan(state,"安排当天",{"available_stops":{"guide:a":self.stops["guide:a"]}},tool,daily=True)
        self.assertEqual(tool.call_args.args[0].destination_id,"广州塔")
        observation = model.ask.call_args.args[2]["tool_observations"][0]
        self.assertEqual(observation["status"],"error")

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
