"""Synthetic provider data and mocks. Written for user execution; no real network/CLI calls."""
import copy
import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app.adapters import Amap, map_urls, normalize_resources, navigation_link
from app.models import Candidate, Requirements
from app.reviewing import bind_report, review_basis
from app.planner import Planner
from app.resource_planning import transport_issues, resource_changes
from app.rules import resource_cost, validate_candidate, check_itinerary


REQ = Requirements(origin="北京",cities=["杭州"],start_date="2026-11-10",end_date="2026-11-10",people=3,children=1,rooms=1,budget=6000).model_dump(mode="json")
CONTEXT = {"origin":"北京","destination":"杭州","date":"2026-11-10"}
PLACE = {"id":"station","name":"杭州东","location":"120.21,30.29"}


class QuoteBoundaries(unittest.TestCase):
    def test_child_is_not_priced_as_an_adult(self):
        option = {"id":"train","name":"G1","kind":"train","price":100,"price_basis":"person_segment","currency":"CNY","context":CONTEXT}
        self.assertIsNone(resource_cost(option,REQ))
        checked = check_itinerary({"days":[],"hotels":[],"transport":[{"id":"out","label":"北京到杭州","context":CONTEXT,"selected":option}]},REQ)
        self.assertEqual(checked["budget"]["known_subtotal"],0)
        self.assertEqual(checked["budget"]["estimated_resources"],200)
        self.assertTrue(any("儿童" in message for message in checked["budget"]["unknown"]))

    def test_cross_midnight_and_hard_time_limit_are_not_silently_accepted(self):
        req = {**REQ,"earliest_departure":"08:30","latest_arrival":"21:00"}
        option = {"departure":"2026-11-10 07:00","arrival":"2026-11-11 01:00"}
        issues = transport_issues(option,CONTEXT,req)
        self.assertTrue(any("跨日" in issue for issue in issues))
        self.assertTrue(any("早于" in issue for issue in issues))

    def test_return_deadline_and_direct_constraint(self):
        req = {**REQ,"latest_return":"20:00","latest_arrival":"23:00"}
        option = {"departure":"2026-11-10 18:00","arrival":"2026-11-10 21:00","legs":[{},{}]}
        issues = transport_issues(option,{**CONTEXT,"destination":"北京"},req)
        self.assertTrue(any("到达" in issue for issue in issues))
        self.assertTrue(any("直达" in issue for issue in issues))

    def test_provider_segments_and_price_share_the_same_option(self):
        legs = [{"marketingTransportNo":"G1","depDateTime":"2026-11-10 09:00","arrDateTime":"2026-11-10 11:00","depStationName":"北京南","arrStationName":"济南西","seatClassName":"二等座"},
                {"marketingTransportNo":"G2","depDateTime":"2026-11-10 12:00","arrDateTime":"2026-11-10 16:00","depStationName":"济南西","arrStationName":"杭州东"}]
        option = normalize_resources({"data":{"itemList":[{"adultPrice":"¥400","journeys":[{"segments":legs}],"jumpUrl":"https://www.fliggy.com/item"}]}},"train",CONTEXT)[0]
        self.assertEqual(option["price"],400)
        self.assertEqual(option["seat_class"],"二等座")
        self.assertEqual(len(option["legs"]),2)
        self.assertEqual(option["arrival_place"],"杭州东")

    def test_approved_experience_cannot_move_to_another_date(self):
        req = Requirements(**{**REQ,"end_date":"2026-11-11"})
        candidate = Candidate(title="两天",reason="已确认体验",days=[
            {"date":"2026-11-10","city":"杭州","stop_ids":[]},
            {"date":"2026-11-11","city":"杭州","stop_ids":["product:p"]}])
        with self.assertRaises(ValueError):
            validate_candidate(candidate,req,{"product:p":{"city":"杭州","assigned_date":"2026-11-10"}})

    def test_map_output_rejects_foreign_hosts_and_active_content(self):
        links = map_urls({"url":"https://uri.amap.com/navigation?to=120,30","other":["https://amap.com.evil.example/","javascript:alert(1)","amapuri://route/plan"]})
        self.assertEqual(len(links),2)
        self.assertTrue(navigation_link(None,PLACE).startswith("https://uri.amap.com/navigation"))

    def test_refresh_reports_price_and_schedule_changes(self):
        old = {"transport":[{"id":"out","label":"交通","selected":{"name":"G1","price":100}}],"days":[]}
        new = {"transport":[{"id":"out","label":"交通","selected":{"name":"G1","price":150}}],"days":[]}
        self.assertTrue(any("100 → 150" in line for line in resource_changes(old,new)))


class JoinedPlanning(unittest.IsolatedAsyncioTestCase):
    async def test_map_session_scopes_exit_in_the_owner_task_with_parallel_callers(self):
        amap = Amap.__new__(Amap)
        amap.api_key = "synthetic-key"; amap.store = Mock(); amap.batch_state = ContextVar("test_batch",default=None)
        owners = []
        @asynccontextmanager
        async def transport(url):
            owners.append(("enter",asyncio.current_task()))
            try:
                yield (None,None,None)
            finally:
                owners.append(("exit",asyncio.current_task()))
        session = AsyncMock()
        session.list_tools.return_value = SimpleNamespace(tools=[SimpleNamespace(name="maps_weather",inputSchema={"properties":{"city":{"type":"string"}},"required":["city"]})])
        session.call_tool.return_value = SimpleNamespace(isError=False,structuredContent={"forecasts":[]})
        @asynccontextmanager
        async def client(read,write):
            yield session
        with patch("mcp.client.streamable_http.streamablehttp_client",new=transport),patch("mcp.ClientSession",new=client):
            async with amap.batch():
                await asyncio.gather(amap.call("trip","maps_weather",{"city":"杭州"}),amap.call("trip","maps_weather",{"city":"苏州"}))
        self.assertIs(owners[0][1],owners[1][1])
        session.initialize.assert_awaited_once()
        session.list_tools.assert_awaited_once()
        self.assertEqual(session.call_tool.await_count,2)

    async def test_one_day_trip_starts_at_arrival_station_without_fake_hotel_transfer(self):
        planner = Planner.__new__(Planner)
        planner.movement = AsyncMock()
        state = {"requirements":REQ,"selected_route":{"days":[{"date":"2026-11-10","city":"杭州","stop_ids":[]}]},"stops":{},
                 "itinerary":{"hotels":[],"transport":[{"label":"到杭州","buffer_minutes":60,"context":CONTEXT,
                     "selected":{"arrival":"2026-11-10 10:00","arrival_place":"杭州东"}}]}}
        start,_,_,transfers,tail = await planner.window(state,{"date":"2026-11-10","city":"杭州"})
        self.assertEqual(start,660)
        self.assertEqual(tail["start_name"],"杭州东")
        self.assertEqual(transfers,[])
        planner.movement.assert_not_awaited()

    async def test_lodging_search_ignores_discarded_guide_stop(self):
        planner = Planner.__new__(Planner)
        state = {"selected_route":{"days":[{"city":"杭州","date":"2026-11-10","stop_ids":["chosen"]}]},
                 "stops":{"discarded":{"name":"远郊景区","city":"杭州"},"chosen":{"name":"西湖","city":"杭州","map_place":{"name":"西湖","location":"120.15,30.24"}}}}
        self.assertEqual(planner.hotel_areas(state,"杭州"),["西湖"])

    async def test_user_lodging_area_overrides_automatic_route_area(self):
        planner = Planner.__new__(Planner)
        self.assertEqual(planner.hotel_areas({"requirements":{"hotel_areas":{"杭州":"湖滨路"}}},"杭州"),["湖滨路"])

    async def test_resource_assessment_retains_unknown_cost_and_checkpoint_preview(self):
        planner = Planner.__new__(Planner); planner.emit = Mock()
        planner.store = Mock()
        planner.store.review_queries.return_value = []
        planner.audit_state = lambda state:state
        async def unavailable_audit(state,plan):
            return bind_report({"status":"review_unavailable","summary":"此用例隔离资源评估，不运行模型重审","checks":[],"findings":[]},
                               state,plan,review_basis(state,plan,[],[]),[])
        planner.audit = AsyncMock(side_effect=unavailable_audit)
        planner.selected_sources = Mock(return_value=[])
        planner.tickets = AsyncMock(return_value={})
        route = {"id":"candidate","title":"一天","reason":"攻略依据","gaps":[],"days":[{"date":"2026-11-10","city":"杭州","stop_ids":["guide:1"],"adjustment":""}]}
        itinerary = {"transport":[],"hotels":[],"days":[{"date":"2026-11-10","city":"杭州","available_start":600,"tail_deadline":1100,
            "activities":[{"id":"guide:1","name":"合成景点","kind":"visit"}],"gaps":["门票待确认"]}],"issues":[],"budget":{"known_subtotal":0,"unknown":["门票待确认"]}}
        planner.transport = AsyncMock(return_value={})
        planner.hotels = AsyncMock(return_value={})
        planner.activities = AsyncMock(return_value={"itinerary":copy.deepcopy(itinerary)})
        planner.check = AsyncMock(return_value={"itinerary":copy.deepcopy(itinerary)})
        result = await planner.assess({"trip_id":"synthetic","requirements":REQ,"selected_source_ids":[],"stops":{},"candidates":[route]})
        self.assertIn("candidate",result["previews"])
        self.assertEqual(result["candidates"][0]["feasibility"]["budget"]["unknown"],["门票待确认"])
        self.assertEqual(route["gaps"],[])
