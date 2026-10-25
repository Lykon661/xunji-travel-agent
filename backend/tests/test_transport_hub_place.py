"""Stations and airports with several terminals or gates are pinned by terminal or by the user, never guessed."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.adapters import Amap
from app.nodes.scheduling import SchedulingNodes
from app.planner import Planner
from app.store import Conflict

SEARCH = {"pois":[
    {"id":"airport","name":"广州白云国际机场","typecode":"150104","address":"机场大道东888号"},
    {"id":"t2","name":"广州白云国际机场T2航站楼","typecode":"150104"},
    {"id":"t3","name":"广州白云国际机场T3航站楼","typecode":"150104"},
    {"id":"t3-departure","name":"广州白云国际机场T3航站楼(国内出发)","typecode":"150105"},
    {"id":"p2","name":"广州白云国际机场P2停车场(入口)","typecode":"150907"},
    {"id":"bus","name":"广州白云国际机场(T2号航站楼)(公交站)","typecode":"150700"}]}
LOCATIONS = {"airport":"113.30,23.39","t2":"113.31,23.39","t3":"113.32,23.40","t3-departure":"113.32,23.41"}


class Nodes(SchedulingNodes):
    def __init__(self, amap):
        self.amap = amap


def itinerary(terminal=None, origin="武汉"):
    flight = {"arrival_place":"白云国际机场","arrival_terminal":terminal,"departure_place":"天河国际机场"}
    return {"transport":[{"id":"flight","label":"武汉 → 广州","context":{"origin":origin,"destination":"广州","date":"2026-12-12"},"selected":flight}]}


def state(history=()):
    return {"trip_id":"t","requirements":{"origin":"武汉","origin_address":""},"decision_history":list(history)}


class TransportHubs(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.amap = Amap(SimpleNamespace())
        self.amap.cache.clear()

        async def call(trip_id, tool, args):
            if tool == "maps_text_search":
                return SEARCH
            return {"pois":[{"id":args["id"],"location":LOCATIONS[args["id"]]}]}
        self.amap.call = AsyncMock(side_effect=call)
        self.nodes = Nodes(self.amap)

    async def test_flight_terminal_pins_the_terminal_building_without_asking(self):
        self.assertIsNone(await self.nodes.resolve_hubs(state(),itinerary("T3")))
        place = await self.amap.place("t","广州","白云国际机场")
        self.assertEqual((place["id"],place["location"]),("t3","113.32,23.40"))

    async def test_unknown_terminal_asks_with_only_terminal_and_hall_options(self):
        result = await self.nodes.resolve_hubs(state(),itinerary())
        self.assertEqual(result["unmet"][0]["kind"],"hub_identity")
        self.assertEqual(result["unmet"][0]["dates"],["2026-12-12"])
        self.assertEqual({item["id"] for item in result["hub_candidates"]},{"airport","t2","t3","t3-departure"})
        self.assertIsNone(await self.amap.place("t","广州","白云国际机场"))

    async def test_users_choice_is_reused_for_every_later_lookup(self):
        history = [{"hub_selection":{"city":"广州","name":"白云国际机场","poi_id":"t2"}}]
        self.assertIsNone(await self.nodes.resolve_hubs(state(history),itinerary()))
        self.assertEqual((await self.amap.place("t","广州","白云国际机场"))["id"],"t2")

    async def test_origin_city_hub_is_not_asked_without_a_home_address(self):
        trip = itinerary("T3")
        trip["transport"][0]["selected"]["arrival_place"] = None
        self.assertIsNone(await self.nodes.resolve_hubs(state(),trip))
        self.amap.call.assert_not_awaited()




if __name__ == "__main__":
    unittest.main()
