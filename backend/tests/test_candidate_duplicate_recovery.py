"""Mocked regressions for the user to run; no external tools or live models."""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from app import ablation
from app.models import Candidate, OrderChoice, Requirements, RouteTurn
from app.places import canonicalize_stops, represented_ids
from app.planner import Planner
from app.rules import merge_candidate_duplicates, validate_candidate


REQ = {"origin":"北京","cities":["广州"],"start_date":"2026-11-10","end_date":"2026-11-11",
       "people":1,"rooms":1,"budget":5000}


def stops():
    return canonicalize_stops({
        "guide-a:street":{"id":"guide-a:street","city":"广州","name":"北京路步行街","source_id":"guide-a",
            "map_place":{"id":"pedestrian","name":"北京路步行街","location":"113.267679,23.120946","type":"风景名胜"}},
        "guide-b:road":{"id":"guide-b:road","city":"广州","name":"北京路","source_id":"guide-b",
            "map_place":{"id":"road","name":"北京路","location":"113.269133,23.121060","type":"地名地址信息;道路名"}},
        "guide-b:museum":{"id":"guide-b:museum","city":"广州","name":"博物馆","source_id":"guide-b",
            "map_place":{"id":"museum","name":"博物馆","location":"113.326369,23.114763"}}})


def route(second=None):
    return Candidate(title="两天广州",reason="合成路线",days=[
        {"date":"2026-11-10","city":"广州","stop_ids":["guide-a:street"]},
        {"date":"2026-11-11","city":"广州","stop_ids":second or ["guide-b:road","guide-b:museum"]}])


class CandidateMerge(unittest.TestCase):
    def test_cross_guide_alias_is_removed_before_strict_validation_and_sources_survive(self):
        source = stops()
        original = route()
        before = original.model_dump(mode="json")
        merged = merge_candidate_duplicates(original,Requirements.model_validate(REQ),source)
        validate_candidate(merged,Requirements.model_validate(REQ),source)
        self.assertEqual(merged.days[1].stop_ids,["guide-b:museum"])
        self.assertIn("北京路",merged.days[1].adjustment)
        self.assertIn("guide-b:road",represented_ids([source["guide-a:street"]]))
        self.assertEqual(original.model_dump(mode="json"),before)
        # Confirm/save paths still reject an unnormalized duplicate instead of silently changing it.
        with self.assertRaises(ValueError):
            validate_candidate(original,Requirements.model_validate(REQ),source)

    def test_same_day_duplicate_id_is_removed_and_an_empty_day_keeps_the_trip_date(self):
        original = route(["guide-b:road"])
        original.days[0].stop_ids.append("guide-a:street")
        req = Requirements.model_validate(REQ)
        merged = validate_candidate(merge_candidate_duplicates(original,req,stops()),req,stops())
        self.assertEqual(merged.days[0].stop_ids,["guide-a:street"])
        self.assertEqual(merged.days[1].stop_ids,[])
        self.assertEqual([day.date.isoformat() for day in merged.days],["2026-11-10","2026-11-11"])
        self.assertTrue(any("2026-11-11" in gap for gap in merged.gaps))

    def test_locked_occurrence_is_retained_and_two_conflicting_locks_are_not_discarded(self):
        req = Requirements.model_validate(REQ)
        merged = merge_candidate_duplicates(route(),req,stops(),["guide-b:road"])
        self.assertEqual(merged.days[0].stop_ids,[])
        self.assertEqual(merged.days[1].stop_ids,["guide-b:road","guide-b:museum"])
        validate_candidate(merged,req,stops())
        protected = merge_candidate_duplicates(route(),req,stops(),["guide-a:street","guide-b:road"])
        with self.assertRaises(ValueError):
            validate_candidate(protected,req,stops())

    def test_date_specific_repeat_permission_and_adopted_experience_are_preserved(self):
        req = Requirements.model_validate({**REQ,"repeat_exceptions":[{"place_id":"guide-b:road","role":"visit",
            "dates":["2026-11-11"],"authorization":"用户明确要求再访"}]})
        merged = merge_candidate_duplicates(route(),req,stops())
        self.assertEqual(merged.days[1].stop_ids,["guide-b:road","guide-b:museum"])
        validate_candidate(merged,req,stops())
        source = stops()
        source["guide-b:road"].update(source_kind="flyai",assigned_date="2026-11-11")
        source["guide-b:road"]["map_place"] = dict(source["guide-a:street"]["map_place"])
        merged = merge_candidate_duplicates(route(),Requirements.model_validate(REQ),source)
        self.assertEqual(merged.days[1].stop_ids,["guide-b:road","guide-b:museum"])
        self.assertEqual(merged.days[0].stop_ids,[])

    def test_unknown_ids_wrong_cities_and_wrong_assigned_dates_still_fail(self):
        req = Requirements.model_validate(REQ)
        invalid = route()
        invalid.days[1].stop_ids.append("foreign")
        with self.assertRaises(ValueError):
            validate_candidate(merge_candidate_duplicates(invalid,req,stops()),req,stops())
        source = stops()
        source["guide-b:road"]["assigned_date"] = "2026-11-10"
        with self.assertRaises(ValueError):
            validate_candidate(merge_candidate_duplicates(route(),req,source),req,source)
        invalid = route()
        invalid.days[1].city = "深圳"
        with self.assertRaises(ValueError):
            validate_candidate(merge_candidate_duplicates(invalid,req,stops()),req,stops())


class DiscoveryRecovery(unittest.IsolatedAsyncioTestCase):
    async def test_daily_order_cannot_reintroduce_an_ordinary_alias_from_the_pool(self):
        planner = Planner.__new__(Planner)
        planner.emit = Mock()
        planner.route_agent = SimpleNamespace(plan=AsyncMock(return_value=RouteTurn(
            status="ready",order=OrderChoice(stop_ids=["guide-a:street"]))))
        day = {"date":"2026-11-10","city":"广州","stop_ids":["guide-a:street"]}
        state = {"requirements":REQ,"stops":stops(),"selected_route":{"days":[day]},"stop_constraints":{}}
        await planner.daily_order(state,day,["guide-a:street"],set(),{})
        payload = planner.route_agent.plan.call_args.args[2]
        self.assertIn("guide-a:street",payload["available_stops"])
        self.assertNotIn("guide-b:road",payload["available_stops"])
        self.assertEqual(len(payload["canonical_place_groups"]),1)

    async def test_duplicate_model_proposal_continues_without_repeating_the_model_call(self):
        planner = Planner.__new__(Planner)
        planner.emit = Mock()
        extraction = {"title":"攻略整体说明","seasons":["秋"],"cautions":["注意开放时间"],"gaps":["当天营业待核实"],
                      "stops":[{"id":"street","name":"北京路","evidence":{"quote":"攻略中的北京路"}}]}
        sources = [{"id":"guide-a","title":"攻略甲","extraction":copy.deepcopy(extraction)},
                   {"id":"guide-b","title":"攻略乙","extraction":copy.deepcopy(extraction)}]
        before_sources = copy.deepcopy(sources)
        planner.selected_sources = Mock(return_value=sources)
        planner.locate_stops = AsyncMock(side_effect=lambda state,source:source)
        planner.weather_context = AsyncMock(return_value={})
        proposal = route()
        planner.route_agent = SimpleNamespace(plan=AsyncMock(return_value=RouteTurn(status="ready",candidate=proposal)))
        state = {"trip_id":"trip","requirements":REQ,"selected_source_ids":["guide-a","guide-b"],
                 "stops":stops(),"warnings":[],"locks":[],"stop_constraints":{"required":["guide-b:road"],"excluded":[]}}
        state["stops"]["guide-a:street"]["evidence"] = {"kind":"text","quote":"攻略甲的步行街原文"}
        state["stops"]["guide-b:road"]["evidence"] = {"kind":"text","quote":"攻略乙的道路原文"}
        with ablation.use("no_clustering"):
            result = await planner.discover(copy.deepcopy(state))
        self.assertEqual(len(result["candidates"]),1)
        self.assertEqual(result["candidates"][0]["days"][1]["stop_ids"],["guide-b:museum"])
        planner.route_agent.plan.assert_awaited_once()
        payload = planner.route_agent.plan.call_args.args[2]
        self.assertIn("guide-b:road",payload["available_stops"])
        self.assertNotIn("guide-a:street",payload["available_stops"])
        self.assertEqual(len(payload["canonical_place_groups"]),1)
        self.assertEqual(sources,before_sources)
        for guide in payload["guides"]:
            self.assertNotIn("stops",guide["guide"])
            self.assertEqual(guide["guide"]["seasons"],extraction["seasons"])
            self.assertEqual(guide["guide"]["cautions"],extraction["cautions"])
            self.assertEqual(guide["guide"]["gaps"],extraction["gaps"])
        selected = payload["available_stops"]["guide-b:road"]
        self.assertEqual(selected["evidence"],state["stops"]["guide-b:road"]["evidence"])
        quotes = {(reference.get("evidence") or {}).get("quote") for reference in selected["source_references"]}
        self.assertTrue({"攻略甲的步行街原文","攻略乙的道路原文"} <= quotes)
