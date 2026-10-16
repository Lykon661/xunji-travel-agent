"""Source-only regressions for the user to run; no live queries or model calls."""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from app.adapters import FlyAI
from app.places import canonicalize_stops, duplicate_issues
from app.planner import Planner
from app.rules import ticket_policy, ticket_query_required
from app.store import now


def street_stops():
    return {
        "guide-a:street":{"id":"guide-a:street","source_id":"guide-a","city":"广州","name":"北京路步行街",
            "map_place":{"id":"pedestrian-poi","name":"北京路步行街","location":"113.267679,23.120946","type":"风景名胜"}},
        "guide-b:road":{"id":"guide-b:road","source_id":"guide-b","city":"广州","name":"北京路",
            "map_place":{"id":"road-poi","name":"北京路","location":"113.269133,23.121060","type":"地名地址信息;道路名"}}}


def street_plan():
    return {"issues":[],"days":[{"city":stop["city"],"date":f"2026-11-{10+index}","gaps":[],
        "activities":[{**stop,"kind":"visit","canonical_place_id":f"old-poi:{index}"}]}
        for index,stop in enumerate(street_stops().values())]}


class StreetAliases(unittest.TestCase):
    def test_different_road_and_pedestrian_pois_share_sources_and_block_old_drafts(self):
        resolved = canonicalize_stops(street_stops())
        first,second = resolved.values()
        self.assertEqual(first["canonical_place_id"],second["canonical_place_id"])
        self.assertEqual({ref["stop_id"] for ref in first["source_references"]},set(resolved))
        plan = street_plan()
        original = copy.deepcopy(plan)
        self.assertEqual(duplicate_issues(plan)[0]["code"],"duplicate_place")
        self.assertEqual(plan,original)
        req = {"repeat_exceptions":[{"place_id":"guide-b:road","role":"visit",
            "dates":["2026-11-11"],"authorization":"用户明确要求第二天再去"}]}
        self.assertEqual(duplicate_issues(plan,req),[])

    def test_other_cities_distant_roads_and_independent_venues_are_not_merged(self):
        for fields in ({"city":"北京"},{"map_place":{"id":"far-road","name":"北京路","location":"113.3,23.12","type":"道路名"}},
                       {"name":"北京路大佛寺","map_place":{"id":"temple","name":"大佛寺","location":"113.269133,23.121060"}}):
            stops = street_stops()
            stops["guide-b:road"].update(fields)
            resolved = list(canonicalize_stops(stops).values())
            self.assertNotEqual(resolved[0]["canonical_place_id"],resolved[1]["canonical_place_id"])

    def test_unlocated_aliases_share_only_an_identity_not_invented_map_evidence(self):
        stops = street_stops()
        for stop in stops.values():
            stop.pop("map_place")
        resolved = list(canonicalize_stops(stops).values())
        self.assertEqual(resolved[0]["canonical_place_id"],resolved[1]["canonical_place_id"])
        self.assertEqual(resolved[0]["match_status"],"unresolved")
        self.assertNotIn("map_place",resolved[0])

    def test_same_unlocated_venue_name_is_not_enough_to_merge_distinct_branches(self):
        resolved = list(canonicalize_stops({
            "a":{"id":"a","city":"广州","name":"同名展览馆","evidence":{"quote":"一号馆"}},
            "b":{"id":"b","city":"广州","name":"同名展览馆","evidence":{"quote":"二号馆"}}}).values())
        self.assertNotEqual(resolved[0]["canonical_place_id"],resolved[1]["canonical_place_id"])


class AliasRepairAndTickets(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.planner = Planner.__new__(Planner)
        self.planner.emit = Mock()
        self.planner.store = Mock()
        self.planner.flyai = SimpleNamespace(availability=lambda:{"ready":True})
        self.planner.resource_agent = SimpleNamespace(ticket=AsyncMock(return_value={
            "price":50,"status":"reference","queried_at":now(),"note":"合成参考价"}))

    async def test_repair_keeps_one_visit_and_both_sources_and_refreshes_affected_legs(self):
        self.planner.refresh_day_routes = AsyncMock()
        result = await self.planner.repair({"itinerary":street_plan(),"locks":[],"requirements":{},
                                           "stops":canonicalize_stops(street_stops())})
        days = result["itinerary"]["days"]
        self.assertEqual(len(days[0]["activities"]),1)
        self.assertEqual(days[1]["activities"],[])
        self.assertEqual({ref["stop_id"] for ref in days[0]["activities"][0]["source_references"]},set(street_stops()))
        self.assertEqual(self.planner.refresh_day_routes.call_args.args[2],{"2026-11-11"})

    async def test_museums_and_zoos_without_ticket_claims_are_queried_but_free_sites_are_not(self):
        activities = [{"id":str(index),"kind":"visit","name":name} for index,name in enumerate(
            ("广东省博物馆","长隆野生动物园","北京路步行街"))]
        activities += [{"id":"free","kind":"visit","name":"免费博物馆","ticket_claim":"免费开放"}]
        itinerary = {"days":[{"city":"广州","date":"2026-11-10","activities":activities}]}
        await self.planner.query_ticket_prices({"trip_id":"trip"},itinerary,{"2026-11-10"},missing_only=True)
        self.assertEqual(self.planner.resource_agent.ticket.await_count,2)
        self.assertEqual([item.get("ticket_quote",{}).get("price") for item in activities],[50,50,None,None])
        self.assertEqual(ticket_policy(activities[0]),"unspecified")
        self.assertTrue(activities[0]["ticket_query_required"])
        self.assertFalse(ticket_query_required(activities[-1]))

    async def test_explicit_ticket_refresh_bypasses_the_recent_quote_guard(self):
        activity = {"id":"zoo","name":"动物园","kind":"visit","ticket_quote":{
            "price":None,"status":"empty","visit_date":"2026-11-10","queried_at":now()}}
        itinerary = {"days":[{"city":"广州","date":"2026-11-10","activities":[activity]}]}
        await self.planner.query_ticket_prices({"trip_id":"trip","change":{"resource_ids":["ticket:zoo"],"refresh_resources":True}},
                                              itinerary,{"2026-11-10"},missing_only=True)
        self.planner.resource_agent.ticket.assert_awaited_once()
        self.assertEqual(activity["ticket_quote"]["price"],50)

    async def test_sourced_supplier_alias_is_matched_but_two_supplier_ids_remain_ambiguous(self):
        flyai = FlyAI.__new__(FlyAI)
        row = {"id":"zoo","name":"长隆野生动物园","ticketInfo":{"price":50,"ticketName":"成人票"}}
        flyai.search = AsyncMock(return_value={"queried_at":"now","status":"ok","raw":{"items":[row]}})
        result = await flyai.ticket("trip","广州","长隆野生动物世界","2026-11-10",aliases=["长隆野生动物园"])
        self.assertEqual(result["price"],50)
        flyai.search.return_value["raw"]["items"].append({**row,"id":"other-zoo"})
        result = await flyai.ticket("trip","广州","长隆野生动物世界","2026-11-10",aliases=["长隆野生动物园"])
        self.assertEqual(result["price"],0)
        self.assertTrue(result["assumed_free"])
        self.assertEqual(result["status"],"place_confirmation")

    async def test_evidence_recheck_preserves_a_recent_successful_ticket_quote(self):
        activity = {"id":"zoo","name":"动物园","kind":"visit","ticket_quote":{
            "price":50,"status":"reference","visit_date":"2026-11-10","queried_at":now()}}
        original = copy.deepcopy(activity["ticket_quote"])
        itinerary = {"days":[{"city":"广州","date":"2026-11-10","activities":[activity]}]}
        await self.planner.query_ticket_prices({"trip_id":"trip","change":{"refresh_resources":True}},
                                              itinerary,{"2026-11-10"},missing_only=True)
        self.planner.resource_agent.ticket.assert_not_awaited()
        self.assertEqual(activity["ticket_quote"],original)

    async def test_an_empty_primary_lookup_tries_only_one_sourced_alias(self):
        flyai = FlyAI.__new__(FlyAI)
        flyai.search = AsyncMock(side_effect=[{"status":"empty","raw":{"items":[]}},
            {"queried_at":"now","status":"ok","raw":{"items":[{"id":"zoo","name":"长隆野生动物园",
                "ticketInfo":{"price":50}}]}}])
        result = await flyai.ticket("trip","广州","长隆野生动物世界","2026-11-10",aliases=["长隆野生动物园"])
        self.assertEqual(result["price"],50)
        self.assertEqual([call.args[3]["keyword"] for call in flyai.search.call_args_list],["长隆野生动物世界","长隆野生动物园"])
