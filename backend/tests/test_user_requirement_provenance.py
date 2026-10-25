import copy
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app.adapters import default_ticket_price
from app.authority import assert_system_action
from app.conflicts import apply_option
from app.models import Constraint, GuideBrief, OrderChoice, Requirements
from app.planner import Planner
from app.rules import check_itinerary


DATE = "2026-11-11"
REQ = Requirements(origin="北京", cities=["杭州"], start_date=DATE, end_date=DATE,
                   people=1, rooms=1, budget=1000, transport="train", return_to_origin=False).model_dump(mode="json")
CTX = {"origin":"北京", "destination":"杭州", "date":DATE}


def train(identity="original", **updates):
    return {"id":identity, "kind":"train", "name":"G1", "departure":DATE+" 09:00", "arrival":DATE+" 11:00",
            "departure_place":"北京南", "arrival_place":"杭州东", "seat_class":"二等座", "context":copy.deepcopy(CTX),
            "price":100, "price_basis":"person_segment", "currency":"CNY", **updates}


def stop(identity="a", **updates):
    return {"id":identity, "name":"公园", "city":"杭州", "source_id":"guide", "source_title":"攻略",
            "evidence":{"quote":"攻略介绍公园"}, "duration_minutes":60, **updates}


class ProvenanceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.planner = Planner.__new__(Planner)
        self.planner.emit = Mock()
        self.planner.models = SimpleNamespace(ask=AsyncMock())
        self.planner.amap = SimpleNamespace(cache={}, place=AsyncMock())
        self.planner.resource_agent = SimpleNamespace(search=AsyncMock(), choose=AsyncMock())
        self.planner.enrich_transport = AsyncMock(side_effect=lambda state, ctx, options:options)
        self.planner.guide_agent = SimpleNamespace(brief=AsyncMock(return_value=GuideBrief(
            status="ready", summary="攻略建议", required_ids=["a"], excluded_ids=["b"])))
        self.state = {"trip_id":"trip", "requirements":copy.deepcopy(REQ), "stops":{"a":stop(), "b":stop("b", name="博物馆")},
                      "locks":[], "mode":"modify", "change":{"scopes":["full"]}, "stop_constraints":{"required":[], "excluded":[]},
                      "candidates":[{"id":"candidate", "days":[{"date":DATE, "city":"杭州", "stop_ids":["a", "b"]}]}]}

    async def test_guide_suggestions_without_user_text_do_not_become_hard_constraints(self):
        result = await self.planner.guide_analysis(self.state)
        self.assertEqual(result["guide_suggestions"], {"required":["a"], "excluded":["b"]})
        for field in ("required_stop_ids", "excluded_stop_ids"):
            self.assertEqual(result["requirements"][field], [])
        self.assertEqual(result["stop_constraints"], {"required":[], "excluded":[]})
        assert_system_action(self.state, {**self.state, **result})

    async def test_guide_marks_are_grounded_in_user_preferences_messages_and_answers(self):
        sources = ({"requirements":{**REQ, "preferences":"要去公园，不去博物馆"}},
                   {"messages":[{"role":"user", "content":"要去公园，不去博物馆"}]},
                   {"interview":{"history":[{"answers":{"q":"要去公园，不去博物馆"}}]}},
                   {"decision_history":[{"answers":{"q":"要去公园，不去博物馆"}}]})
        for source in sources:
            with self.subTest(source=source):
                result = await self.planner.guide_analysis({**self.state, **source})
                self.assertEqual(result["requirements"]["required_stop_ids"], ["a"])
                self.assertEqual(result["requirements"]["excluded_stop_ids"], ["b"])
                self.assertEqual(result["stop_constraint_sources"], {"required":{"a":"message"}, "excluded":{"b":"message"}})

    async def test_alias_is_evidence_and_system_notes_are_not(self):
        self.state["stops"]["a"]["alias_names"] = ["湖滨公园"]
        result = await self.planner.guide_analysis({**self.state, "message":"必去湖滨公园",
            "decision_history":[{"source":"system", "note":"排除博物馆"}]})
        self.assertEqual(result["requirements"]["required_stop_ids"], ["a"])
        self.assertEqual(result["requirements"]["excluded_stop_ids"], [])

    async def test_existing_form_marks_are_preserved_without_new_text(self):
        self.state["requirements"].update(required_stop_ids=["b"], excluded_stop_ids=["a"])
        result = await self.planner.guide_analysis(self.state)
        self.assertEqual(result["requirements"]["required_stop_ids"], ["b"])
        self.assertEqual(result["requirements"]["excluded_stop_ids"], ["a"])

    def transport_state(self, selected_by_user=True):
        selected = train()
        entry = {"id":"transport:北京:杭州", "context":copy.deepcopy(CTX), "label":"北京 → 杭州",
                 "selected_by_user":selected_by_user, "selected":selected, "options":[selected]}
        itinerary = {"days":[], "hotels":[], "transport":[entry], "issues":[]}
        return {**self.state, "itinerary":itinerary, "old_itinerary":copy.deepcopy(itinerary),
                "selected_route":{"days":[{"date":DATE, "city":"杭州", "stop_ids":[]}]}}

    async def test_transport_requery_refreshes_only_the_user_selected_train(self):
        state = self.transport_state()
        fresh = train("fresh", price=120)
        other = train("other", name="G2", price=20)
        self.planner.resource_agent.search.return_value = {"options":[other, fresh], "evidence_id":"query:fresh"}
        result = await self.planner.transport(state)
        entry = result["itinerary"]["transport"][0]
        self.assertTrue(entry["selected_by_user"])
        self.assertEqual(entry["selected"]["name"], "G1")
        self.assertEqual(entry["selected"]["price"], 120)
        self.planner.resource_agent.choose.assert_not_awaited()
        assert_system_action(state, {**state, **result})

    async def test_unavailable_user_selected_train_is_retained_without_substitution(self):
        state = self.transport_state()
        self.planner.resource_agent.search.return_value = {"options":[train("other", name="G2")]}
        result = await self.planner.transport(state)
        entry = result["itinerary"]["transport"][0]
        self.assertEqual(entry["selected"], state["itinerary"]["transport"][0]["selected"])
        self.assertTrue(entry["selected_by_user"])
        self.assertTrue(entry["errors"])
        self.planner.resource_agent.choose.assert_not_awaited()

    async def test_submit_persists_transport_adoption_and_raw_preferences(self):
        state = self.transport_state(False)
        state["itinerary"]["transport"][0]["selected"]["queried_at"] = datetime.now(timezone.utc).isoformat()
        doc = {"requirements":{**REQ,"preferences":"原始偏好"}, "messages":[], "locks":[], "selected_source_ids":[],
               "decision_history":[], "itinerary":state["itinerary"], "selected_route":state["selected_route"]}
        self.planner.active = Mock(return_value={"version":0, "updated_at":"saved", "active_thread":None, "document":doc})
        self.planner.selected_sources = Mock(return_value=[])
        self.planner.source_stops = Mock(return_value=state["stops"])
        self.planner.store = Mock()
        self.planner.store.start_run.return_value = "run"
        self.planner.graph = SimpleNamespace()
        self.planner.gates, self.planner.tasks = {}, {}
        self.planner.execute = AsyncMock()
        run = await self.planner.submit("trip", 0, resource_choice={
            "updated_at":"saved", "entry_id":"transport:北京:杭州", "option_id":"original"})
        task = self.planner.tasks.get(run)
        if task:
            await task
        invocation = self.planner.execute.await_args.args[1]
        self.assertTrue(invocation["itinerary"]["transport"][0]["selected_by_user"])
        self.assertEqual(invocation["preferences_original"], "原始偏好")
        self.assertIn("原始偏好", invocation["user_inputs"])


    async def test_automatic_poi_matching_records_system_source(self):
        async def place(trip, city, name, selector):
            rows = [{"id":"automatic", "name":name, "location":"120,30"}]
            selected, _ = await selector(rows)
            return {**rows[0], "id":selected, "evidence_id":"query:poi"} if selected else None
        self.planner.amap.place.side_effect = place
        result = await self.planner.locate_stops(self.state, {"a":self.state["stops"]["a"]})
        self.assertEqual(result["a"]["map_selected_id"], "automatic")
        self.assertEqual(result["a"]["map_selected_by"], "system")

    async def test_user_poi_survives_unrelated_preferences_and_missing_candidates(self):
        selected = stop(map_selected_id="chosen", map_selected_by="user")
        history = [{"place_selection":{"stop_id":"a", "city":"杭州", "name":"公园", "poi_id":"chosen", "preferences":"旧偏好"}}]
        state = {**self.state, "requirements":{**REQ, "preferences":"预算宽裕一些"}, "decision_history":history}
        async def place(trip, city, name, selector):
            selected_id, _ = await selector([{"id":"other", "name":name, "location":"120,30"}])
            self.assertIsNone(selected_id)
            return None
        self.planner.amap.place.side_effect = place
        result = await self.planner.locate_stops(state, {"a":selected})
        self.assertEqual(result["a"]["map_selected_id"], "chosen")
        self.assertEqual(result["a"]["map_selected_by"], "user")
        self.assertIsNone(result["a"]["map_place"])
        self.planner.models.ask.assert_not_awaited()

    async def test_legacy_poi_remains_protected_without_inventing_a_source(self):
        selected = stop(map_selected_id="chosen")
        async def place(trip, city, name, selector):
            rows = [{"id":"chosen", "name":name, "location":"120,30"}]
            identity, _ = await selector(rows)
            return {**rows[0], "id":identity, "evidence_id":"query:poi"}
        self.planner.amap.place.side_effect = place
        before = {**self.state, "stops":{"a":selected}}
        result = await self.planner.locate_stops(before, before["stops"])
        self.assertNotIn("map_selected_by", result["a"])
        assert_system_action(before, {**before, "stops":result})

    async def test_structured_selection_marks_user_poi(self):
        from app.actions import execute
        selected = stop(map_candidates=[{"id":"chosen","name":"公园","location":"120,30"}])
        value = {**self.state,"stops":{"a":selected}}
        updates,_,_ = execute(value,[{"type":"select_poi","stop_id":"a","poi_id":"chosen"}],"user")
        self.assertEqual(updates["stops"]["a"]["map_selected_by"],"user")

    async def schedule(self, state, ordering):
        self.planner.movement = AsyncMock(return_value=({}, {"id":"poi", "location":"120,30"}))
        self.planner.map_order = AsyncMock(return_value=(["a"], "单点"))
        return await self.planner.schedule_activity_day(state, {"date":DATE, "city":"杭州"}, ordering, [],
            (9*60, 18*60, [], [], {"name":None, "start_name":None, "label":"当日结束"}), {})




    def test_unknown_action_is_rejected_and_skip_cannot_drop_required(self):
        with self.assertRaisesRegex(ValueError, "Unknown action"):
            apply_option(self.state, {"type":"typo"}, "无效")
        self.state["requirements"]["required_stop_ids"] = ["a"]
        with self.assertRaisesRegex(ValueError, "protected"):
            apply_option(self.state, {"type":"skip", "stop_id":"a", "date":DATE}, "系统调整")
