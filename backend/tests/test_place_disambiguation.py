"""POI disambiguation with synthetic provider responses."""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.adapters import Amap, ToolError
from app.models import PlaceChoices, ResourceChoice
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



    async def test_same_name_stops_keep_separate_source_contexts_and_choices(self):
        stops = copy.deepcopy(self.stops)
        stops["guide:a"]["evidence"]["quote"] = "选择东路1号的合成艺术馆"
        stops["guide:b"] = {**copy.deepcopy(stops["guide:a"]),"id":"guide:b","evidence":{"quote":"选择西路2号的合成艺术馆"}}
        async def choose(schema,instruction,payload):
            self.assertIs(schema,PlaceChoices)
            return PlaceChoices(choices=[{"key":key,"selected_id":"east" if "东路" in place["evidence"][0]["quote"] else "west",
                                          "reason":place["evidence"][0]["quote"]} for key,place in payload["places"].items()])
        self.planner.models.ask.side_effect = choose
        result = await self.planner.locate_stops(self.state,stops)
        self.assertEqual(result["guide:a"]["map_place"]["id"],"east")
        self.assertEqual(result["guide:b"]["map_place"]["id"],"west")
        # Both ambiguous places of the city share one model judgment with short keys.
        self.planner.models.ask.assert_awaited_once()
        self.assertEqual(set(self.planner.models.ask.call_args.args[2]["places"]),{"p1","p2"})
        searches = [call for call in self.planner.amap.call.call_args_list if call.args[1] == "maps_text_search"]
        self.assertEqual(len(searches),1)


    async def test_unrelated_preferences_preserve_the_user_selected_poi(self):
        history = [{"place_selection":{"stop_id":"guide:a","city":"杭州","name":"合成艺术馆","poi_id":"east",
                                        "preferences":"优先遵循攻略原文"}}]
        self.planner.models.ask.return_value = ResourceChoice(selected_id="west",reason="用户最新要求西路2号")
        state = {**self.state,"decision_history":history,"requirements":{"preferences":"希望减少步行"}}
        result = await self.planner.locate_stops(state,self.stops)
        self.assertEqual(result["guide:a"]["map_place"]["id"],"east")
        self.assertEqual(result["guide:a"]["map_selected_by"],"user")
        self.planner.models.ask.assert_not_awaited()
