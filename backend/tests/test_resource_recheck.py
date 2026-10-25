"""Synthetic regressions for user execution; no provider, model or service calls."""
import copy
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from app.adapters import ToolError
from app.decisions import confirmation_blockers
from app.models import REVIEW_DIMENSIONS, Requirements
from app.planner import Planner
from app.resource_planning import resource_context_issues
from app.reviewing import bind_report, review_basis, review_blockers


REQ = Requirements(origin="北京",cities=["杭州"],start_date="2026-11-11",end_date="2026-11-12",
                   people=1,rooms=1,budget=10000,transport="train",return_to_origin=False).model_dump(mode="json")
CTX = {"origin":"北京","destination":"杭州","date":"2026-11-11"}
HOTEL_CTX = {"city":"杭州","check_in":"2026-11-11","check_out":"2026-11-12"}


def train():
    return {"id":"train-a","kind":"train","name":"G1","departure":"2026-11-11 09:00",
            "arrival":"2026-11-11 11:00","departure_place":"北京南","arrival_place":"杭州东",
            "seat_class":"二等座","context":copy.deepcopy(CTX),"price":100,"currency":"CNY",
            "price_basis":"person_segment","queried_at":(datetime.now(timezone.utc)-timedelta(minutes=11)).isoformat()}


def plan():
    return {"transport":[{"id":"transport:北京:杭州","label":"北京 → 杭州","context":copy.deepcopy(CTX),
                          "selected":train(),"options":[],"buffer_minutes":60,"reason":"原班次"}],
            "hotels":[],"issues":[],"budget":{"unknown":[]},"days":[
                {"date":day,"city":"杭州","activities":[{"id":str(index),"kind":"visit","name":"合成景点"+str(index),
                 "start":"12:00","end":"13:00","evidence":{"quote":"合成景点"},
                 "map_place":{"id":str(index),"location":f"120.{index+1},30.2"},"move_before":{}}],
                 "gaps":[],"available_start":540,"tail_deadline":1200,"return_move":{}}
                for index,day in enumerate(("2026-11-11","2026-11-12"))]}


def report_for(itinerary):
    state = {"requirements":REQ,"selected_source_ids":[],"draft_revision":1}
    value = {"status":"pass","summary":"合成审查","findings":[],
             "checks":[{"dimension":key,"result":"ok"} for key in sorted(REVIEW_DIMENSIONS)]}
    return state,bind_report(value,state,itinerary,review_basis(state,itinerary,[],[]),[])


class ResourceRecheck(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.planner = Planner.__new__(Planner)
        self.planner.emit = Mock()
        self.planner.resource_agent = SimpleNamespace(
            search=AsyncMock(return_value={"options":[],"status":"empty"}),
            choose=AsyncMock(return_value=(None,"未查询到资源")))
        self.planner.enrich_transport = AsyncMock(side_effect=lambda state,ctx,options:options)
        self.planner.enrich_hotels = AsyncMock(side_effect=lambda state,city,options:options)
        self.planner.hotel_areas = Mock(return_value=["湖滨路"])
        itinerary = plan()
        self.state = {"trip_id":"trip","requirements":copy.deepcopy(REQ),"locks":[],"stops":{},
                      "selected_source_ids":[],"mode":"modify","change":{"scopes":["full"]},
                      "old_itinerary":copy.deepcopy(itinerary),"itinerary":itinerary,
                      "selected_route":{"id":"candidate","days":[{"date":day["date"],"city":day["city"],
                          "stop_ids":[activity["id"] for activity in day["activities"]]} for day in itinerary["days"]]}}

    async def test_failed_search_preserves_a_legacy_choice_but_blocks_its_old_date(self):
        old = self.state["old_itinerary"]["transport"][0]
        old["context"]["date"] = "2026-11-10"
        old["selected"]["context"]["date"] = "2026-11-10"
        result = await self.planner.transport(self.state)
        entry = result["itinerary"]["transport"][0]
        self.assertEqual(entry["context"]["date"],"2026-11-10")
        self.assertEqual(entry["selected"],old["selected"])
        self.assertTrue(entry["errors"])
        self.assertTrue(resource_context_issues(result["itinerary"],REQ))

    async def test_failed_search_does_not_reuse_an_adopted_hotel_from_old_dates(self):
        old_ctx = {**HOTEL_CTX,"check_in":"2026-11-10","check_out":"2026-11-11"}
        self.state["old_itinerary"]["hotels"] = [{"id":"hotel:杭州","city":"杭州","context":old_ctx,
            "selected_by_user":True,"selected":{"id":"old-hotel","kind":"hotel","context":old_ctx}}]
        result = await self.planner.hotels(self.state)
        entry = result["itinerary"]["hotels"][0]
        self.assertEqual(entry["context"],HOTEL_CTX)
        self.assertIsNone(entry["selected"])
        self.assertFalse(entry["selected_by_user"])


    def test_quote_context_and_adopted_hotel_dates_are_checked_without_locks(self):
        itinerary = plan()
        itinerary["transport"][0]["selected"]["context"]["date"] = "2026-11-10"
        itinerary["hotels"] = [{"id":"hotel:杭州","city":"杭州","selected_by_user":True,
            "context":{**HOTEL_CTX,"check_in":"2026-11-10"},"selected":{}}]
        self.assertEqual({issue["target"] for issue in resource_context_issues(itinerary,REQ)},
                         {"transport:北京:杭州","hotel:杭州"})
        itinerary["hotels"][0]["selected_by_user"] = False
        self.assertEqual(len(resource_context_issues(itinerary,REQ)),1)

    async def test_recheck_refreshes_the_locked_original_train_without_replanning(self):
        self.state["locks"] = ["transport:北京:杭州","0"]
        original = copy.deepcopy(self.state["itinerary"])
        fresh = {**train(),"id":"new-provider-id","price":120,"queried_at":datetime.now(timezone.utc).isoformat()}
        self.planner.resource_agent.search.return_value = {"options":[fresh],"status":"ok","evidence_id":"query:new"}
        result = await self.planner.refresh_expired_resources(self.state)
        self.assertEqual(result["itinerary"]["days"],original["days"])
        self.assertEqual(result["itinerary"]["transport"][0]["selected"]["price"],120)
        self.assertEqual(self.state["itinerary"],original)
        self.assertEqual(self.state["locks"],["transport:北京:杭州","0"])
        self.assertTrue(all(call.kwargs["fresh"] for call in self.planner.resource_agent.search.await_args_list))
        self.planner.resource_agent.choose.assert_not_awaited()
        _,report = report_for(result["itinerary"])
        self.assertGreater(datetime.fromisoformat(report["expires_at"]),datetime.now(timezone.utc))


    async def test_failed_refresh_keeps_the_original_quote_time_and_blocked_status(self):
        original = copy.deepcopy(self.state["itinerary"]["transport"][0]["selected"])
        self.planner.resource_agent.search.side_effect = ToolError("flyai_network","合成网络失败")
        result = await self.planner.refresh_expired_resources(self.state)
        self.assertEqual(result["itinerary"]["transport"][0]["selected"],original)
        _,report = report_for(result["itinerary"])
        self.assertLessEqual(datetime.fromisoformat(report["expires_at"]),datetime.now(timezone.utc))

    async def test_fresh_quotes_and_unadopted_hotels_are_not_queried_again(self):
        itinerary = self.state["itinerary"]
        itinerary["transport"][0]["selected"]["queried_at"] = datetime.now(timezone.utc).isoformat()
        itinerary["hotels"] = [{"id":"hotel:杭州","city":"杭州","selected_by_user":False,
                                "selected":{"queried_at":"old"}}]
        self.assertEqual(await self.planner.refresh_expired_resources(self.state),{})
        self.planner.resource_agent.search.assert_not_awaited()

    async def test_recheck_refreshes_the_same_adopted_hotel_and_keeps_map_evidence(self):
        itinerary = self.state["itinerary"]
        itinerary["transport"] = []
        selected = {"id":"hotel-a","supplier_id":None,"kind":"hotel","name":"合成酒店","bed_type":"双床房",
                    "address":"湖滨路1号","context":HOTEL_CTX,"price":200,"currency":"CNY","price_basis":"room_night",
                    "queried_at":train()["queried_at"],"map_place":{"id":"hotel-poi","location":"120.1,30.2"}}
        itinerary["hotels"] = [{"id":"hotel:杭州","city":"杭州","context":HOTEL_CTX,"selected_by_user":True,
                                "selected":selected,"options":[selected]}]
        fresh = {**selected,"id":"hotel-new-query-id","price":220,"queried_at":datetime.now(timezone.utc).isoformat()}
        fresh.pop("map_place")
        self.planner.resource_agent.search.return_value = {"options":[fresh],"status":"ok"}
        result = await self.planner.refresh_expired_resources(self.state)
        entry = result["itinerary"]["hotels"][0]
        self.assertTrue(entry["selected_by_user"])
        self.assertEqual(entry["selected"]["price"],220)
        self.assertEqual(entry["selected"]["map_place"],selected["map_place"])
        self.assertEqual(result["itinerary"]["days"],itinerary["days"])
        self.assertEqual(self.planner.resource_agent.search.await_args.args[3]["key-words"],selected["name"])
        self.planner.resource_agent.choose.assert_not_awaited()

    def test_missing_quote_time_cannot_produce_a_fresh_report(self):
        itinerary = plan()
        itinerary["transport"][0]["selected"].pop("queried_at")
        _,report = report_for(itinerary)
        self.assertLessEqual(datetime.fromisoformat(report["expires_at"]),datetime.now(timezone.utc))

    async def test_candidate_recheck_uses_quote_refresh_without_activity_generation(self):
        itinerary = self.state["itinerary"]
        candidate = {**self.state["selected_route"],"gaps":[],"draft":itinerary}
        self.planner.refresh_expired_resources = AsyncMock(return_value={})
        self.planner.refresh_day_routes = AsyncMock()
        self.planner.check = AsyncMock(side_effect=lambda state:{"itinerary":state["itinerary"]})
        self.planner.activities = AsyncMock()
        self.planner.assess_result = AsyncMock(return_value={})
        state = {**self.state,"candidates":[candidate],"assessment_refresh":True,
                 "assessment_dates":["2026-11-11"],"assessment_base":"candidate","previews":{}}
        await self.planner.assess(state)
        self.planner.refresh_expired_resources.assert_awaited_once()
        self.planner.activities.assert_not_awaited()
        self.assertEqual(self.planner.assess_result.await_args.args[2]["days"],itinerary["days"])

    async def test_continue_after_review_failure_only_checks_the_existing_draft(self):
        itinerary = self.state["itinerary"]
        itinerary["recovery_note"] = "本轮结果未采用；原因：本轮有 6 个原有路段的查询依据失效或丢失"
        current = {**self.state["selected_route"],"gaps":[],"draft":copy.deepcopy(itinerary)}
        self.planner.check = AsyncMock(side_effect=lambda state:{"itinerary":state["itinerary"]})
        self.planner.assess_result = AsyncMock(return_value={})
        for operation in ("transport","hotels","activities","tickets","refresh_expired_resources","refresh_day_routes"):
            setattr(self.planner,operation,AsyncMock())
        state = {**self.state,"candidates":[current],"repair_only":True,"assessment_dates":[],
                 "assessment_base":current["id"],"previews":{current["id"]:{"itinerary":itinerary}}}
        await self.planner.assess(state)
        resumed = self.planner.assess_result.await_args.args[2]
        self.assertNotIn("recovery_note",resumed)
        self.assertNotIn("recovery_note",self.planner.assess_result.await_args.args[0]["old_itinerary"])
        self.assertEqual(resumed,{key:value for key,value in itinerary.items() if key != "recovery_note"})
        self.assertIn("recovery_note",itinerary)
        self.assertIn("recovery_note",current["draft"])
        for operation in ("transport","hotels","activities","tickets","refresh_expired_resources","refresh_day_routes"):
            getattr(self.planner,operation).assert_not_awaited()
