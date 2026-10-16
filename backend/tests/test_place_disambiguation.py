"""User-run synthetic cases for general POI disambiguation; no live providers."""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.adapters import Amap, ToolError
from app.models import ResourceChoice
from app.planner import Planner


class PlaceDisambiguation(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.planner = Planner.__new__(Planner)
        self.planner.models = SimpleNamespace(ask=AsyncMock(return_value=ResourceChoice(selected_id=None,reason="原文未说明是哪处分店")))
        self.planner.amap = Amap.__new__(Amap)
        self.planner.amap.cache = {}
        self.rows = [{"id":"east","name":"合成艺术馆东馆","address":"东路1号","location":"120,30","typecode":"140100"},
                     {"id":"west","name":"合成艺术馆西馆","address":"西路2号","location":"121,31","typecode":"140100"}]
        async def call(trip_id,tool,args):
            return {"pois":copy.deepcopy(self.rows)} if tool == "maps_text_search" else next(copy.deepcopy(row) for row in self.rows if row["id"] == args["id"])
        self.planner.amap.call = AsyncMock(side_effect=call)
        self.state = {"trip_id":"trip","run_id":"run","generation":"generation","draft_revision":1,
                      "requirements":{"preferences":"优先遵循攻略原文"},"decision_history":[],"decision_resume":"discover"}
        self.stops = {"guide:a":{"id":"guide:a","city":"杭州","name":"合成艺术馆","evidence":{"quote":"游览合成艺术馆"}}}

    async def question(self):
        stops = await self.planner.locate_stops(self.state,self.stops)
        clarification = self.planner.place_clarification(stops,["guide:a"],[{"date":"2026-11-10","stop_ids":["guide:a"]}])
        return {**self.state,"stops":stops,**clarification}

    async def test_unresolved_matching_retains_candidates_and_asks_a_concrete_place_question(self):
        state = await self.question()
        question = state["decision"]
        self.assertIn("东路1号",question["options"][0])
        self.assertIn("西路2号",question["options"][1])
        self.assertEqual(question["affected_dates"],["2026-11-10"])
        self.assertEqual(len(state["stops"]["guide:a"]["map_candidates"]),2)
        self.assertIsNone(state["stops"]["guide:a"]["map_place"])
        self.assertNotIn("接受缺口",question["question"])

    async def test_user_option_binds_the_exact_poi_without_a_second_model_guess(self):
        state = await self.question()
        option = state["decision"]["options"][1]
        self.planner.models.ask.reset_mock()
        def answer(pending):
            return {"round_id":pending["round_id"],"draft_revision":pending["draft_revision"],
                    "answers":{state["decision"]["id"]:option}}
        with patch("app.planner.interrupt",side_effect=answer):
            result = await self.planner.decision_gate(state)
        point = result["stops"]["guide:a"]["map_place"]
        self.assertEqual((point["id"],point["location"]),("west","121,31"))
        self.assertIn("明确选择",point["match_basis"])
        self.assertEqual(result["decision_history"][-1]["place_selection"]["poi_id"],"west")
        self.assertEqual(result["decision"],{})
        self.planner.models.ask.assert_not_awaited()
        reloaded = await self.planner.locate_stops({**self.state,"decision_history":result["decision_history"]},self.stops)
        self.assertEqual(reloaded["guide:a"]["map_place"]["id"],"west")
        self.planner.models.ask.assert_not_awaited()

    async def test_same_name_stops_keep_separate_source_contexts_and_choices(self):
        stops = copy.deepcopy(self.stops)
        stops["guide:a"]["evidence"]["quote"] = "选择东路1号的合成艺术馆"
        stops["guide:b"] = {**copy.deepcopy(stops["guide:a"]),"id":"guide:b","evidence":{"quote":"选择西路2号的合成艺术馆"}}
        async def choose(schema,instruction,payload):
            return ResourceChoice(selected_id="east" if payload["stop_id"] == "guide:a" else "west",reason=payload["evidence"][0]["quote"])
        self.planner.models.ask.side_effect = choose
        result = await self.planner.locate_stops(self.state,stops)
        self.assertEqual(result["guide:a"]["map_place"]["id"],"east")
        self.assertEqual(result["guide:b"]["map_place"]["id"],"west")
        searches = [call for call in self.planner.amap.call.call_args_list if call.args[1] == "maps_text_search"]
        self.assertEqual(len(searches),1)

    async def test_failed_coordinates_keep_the_user_choice_without_claiming_a_location(self):
        state = await self.question()
        self.planner.amap.place = AsyncMock(side_effect=ToolError("amap_network","合成高德连接失败"))
        option = state["decision"]["options"][0]
        def answer(pending):
            return {"round_id":pending["round_id"],"draft_revision":pending["draft_revision"],
                    "answers":{state["decision"]["id"]:option}}
        with patch("app.planner.interrupt",side_effect=answer):
            result = await self.planner.decision_gate(state)
        stop = result["stops"]["guide:a"]
        self.assertIsNone(stop["map_place"])
        self.assertEqual(stop["map_selected_id"],"east")
        self.assertIn("连接失败",stop["map_issue"])
        self.assertIsNone(self.planner.place_clarification(result["stops"],["guide:a"]))

    async def test_changed_preferences_are_reassessed_instead_of_overriding_them_with_an_old_choice(self):
        history = [{"place_selection":{"stop_id":"guide:a","city":"杭州","name":"合成艺术馆","poi_id":"east",
                                        "preferences":"优先遵循攻略原文"}}]
        self.planner.models.ask.return_value = ResourceChoice(selected_id="west",reason="用户最新要求西路2号")
        state = {**self.state,"decision_history":history,"requirements":{"preferences":"改为西路2号的场馆"}}
        result = await self.planner.locate_stops(state,self.stops)
        self.assertEqual(result["guide:a"]["map_place"]["id"],"west")
        self.planner.models.ask.assert_awaited_once()
