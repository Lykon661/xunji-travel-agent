"""Recorded-shape fixtures and mocks only; no provider, CLI or model execution."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from app.adapters import Amap, FlyAI, ToolError, normalize_resources
from app.planner import Planner
from app.rules import check_itinerary


REQ = {"origin":"广州","cities":["广州"],"start_date":"2026-10-13","end_date":"2026-10-17",
       "people":1,"rooms":1,"budget":3000,"preferences":"不打车","transport":"any"}


class PoiCoordinates(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.amap = Amap.__new__(Amap); self.amap.cache = {}; self.amap.call = AsyncMock()

    async def test_search_without_coordinates_follows_poi_id_to_detail(self):
        self.amap.call.side_effect = [{"pois":[{"id":"museum","name":"广东省博物馆","address":"珠江东路2号"}]},
                                    {"id":"museum","location":"113.32,23.11","city":"广州市"}]
        place = await self.amap.place("trip","广州","广东省博物馆")
        self.assertEqual(place["location"],"113.32,23.11")
        self.assertEqual(self.amap.call.await_args_list[1].args[1:],("maps_search_detail",{"id":"museum"}))
        await self.amap.place("trip","广州","广东省博物馆")
        self.assertEqual(self.amap.call.await_count,2)

    async def test_ambiguous_place_is_not_automatically_first_hit(self):
        self.amap.call.return_value = {"pois":[{"id":"a","name":"同名酒店"},{"id":"b","name":"同名酒店"}]}
        self.assertIsNone(await self.amap.place("trip","广州","同名酒店"))
        self.assertEqual(self.amap.call.await_count,1)

    async def test_alias_resolver_can_select_only_returned_poi_id(self):
        self.amap.call.side_effect = [{"pois":[{"id":"main","name":"广州大剧院"},{"id":"other","name":"其他剧院"}]},
                                    {"location":"113.32,23.11"}]
        selector = AsyncMock(return_value=("main","攻略中的大剧院为广州大剧院"))
        place = await self.amap.place("trip","广州","大剧院",selector=selector)
        self.assertEqual(place["name"],"广州大剧院")
        self.assertEqual(self.amap.call.await_args_list[-1].args[-1],{"id":"main"})


class ProviderPrices(unittest.TestCase):
    def test_official_train_wrapper_keeps_price_times_and_station_names_together(self):
        payload = {"status":0,"data":{"itemList":[{"adultPrice":"¥553","jumpUrl":"https://www.fliggy.com/booking",
            "journeys":[{"segments":[{"marketingTransportNo":"G11","depDateTime":"2026-10-13 08:00:00",
                "arrDateTime":"2026-10-13 12:28:00","depStationName":"北京南","arrStationName":"上海虹桥"}]}]}]}}
        options = normalize_resources(payload,"train",{"date":"2026-10-13","origin":"北京","destination":"上海"})
        self.assertEqual(len(options),1)
        self.assertEqual((options[0]["name"],options[0]["price"],options[0]["price_basis"]),("G11",553,"person_segment"))
        self.assertEqual(options[0]["arrival_place"],"上海虹桥")
        self.assertEqual(options[0]["departure"],"2026-10-13 08:00:00")
        self.assertEqual(options[0]["booking_url"],"https://www.fliggy.com/booking")

    def test_current_cli_fare_fields_are_read_for_trains_and_flights(self):
        segment = {"marketingTransportNo":"MU2542","depDateTime":"2026-12-12 07:20:00","arrDateTime":"2026-12-12 08:55:00",
                   "depStationName":"天河国际机场","arrStationName":"白云国际机场","depTerm":"T3","arrTerm":"T3"}
        context = {"date":"2026-12-12","origin":"武汉","destination":"广州"}
        flight = normalize_resources({"status":0,"data":{"itemList":[{"ticketPrice":"350.00","jumpUrl":"https://www.fliggy.com/f",
            "journeys":[{"journeyType":"直达","segments":[segment]}]}]}},"flight",context)
        train = normalize_resources({"status":0,"data":{"itemList":[{"price":"415.00","jumpUrl":"https://www.fliggy.com/t",
            "journeys":[{"journeyType":"直达","segments":[{**segment,"marketingTransportNo":"G1009"}]}]}]}},"train",context)
        for options,name,price in ((flight,"MU2542",350),(train,"G1009",415)):
            self.assertEqual(len(options),1)
            self.assertEqual((options[0]["name"],options[0]["price"],options[0]["price_basis"]),(name,price,"person_segment"))
            self.assertEqual((options[0]["departure"],options[0]["arrival_place"]),("2026-12-12 07:20:00","白云国际机场"))

    def test_wrong_date_adult_price_is_not_attached_to_a_route(self):
        payload = {"adultPrice":"¥100","journeys":[{"segments":[{"marketingTransportNo":"G1","depDateTime":"2026-10-12 08:00:00"}]}]}
        self.assertEqual(normalize_resources(payload,"train",{"date":"2026-10-13"}),[])

    def test_four_nights_are_estimated_and_not_mislabeled_confirmed_total(self):
        ctx = {"city":"广州","check_in":"2026-10-13","check_out":"2026-10-17"}
        hotel = normalize_resources({"data":{"itemList":[{"shId":"hotel-id","name":"合成酒店","address":"合成地址","price":"¥200"}]}},"hotel",ctx)[0]
        result = check_itinerary({"days":[],"transport":[],"hotels":[{"id":"hotel:广州","context":ctx,"label":"广州住宿","selected":hotel,"selected_by_user":True}]},REQ)
        self.assertEqual(result["budget"]["known_subtotal"],0)
        self.assertEqual(result["budget"]["estimated_resources"],800)
        self.assertEqual(result["budget"]["known_plus_estimates"],800)
        self.assertEqual(result["budget"]["status"],"incomplete")

    def test_movement_and_opening_times_are_not_counted_as_unknown_fees(self):
        itinerary = {"hotels":[],"transport":[],"days":[{"date":"2026-10-13","city":"广州","meal_estimate":100,
            "activities":[{"id":"a","kind":"visit","name":"合成景点","start":"09:45","end":"10:45","evidence":{"quote":"合成景点"},
                "move_before":{"minutes":None,"estimate_minutes":45,"allowed_modes":["walking"]}}],"return_move":{"minutes":0,"mode":"walking"}}]}
        result = check_itinerary(itinerary,REQ)
        self.assertTrue(any("移动时间" in note for note in result["information_unknown"]))
        self.assertTrue(any("开放时间" in note for note in result["information_unknown"]))
        self.assertFalse(any("移动时间" in note or "开放时间" in note for note in result["budget"]["unknown"]))


class TicketQuotes(unittest.IsolatedAsyncioTestCase):
    async def test_null_or_other_date_price_stays_unconfirmed(self):
        flyai = FlyAI.__new__(FlyAI)
        flyai.search = AsyncMock(return_value={"queried_at":"now","raw":{"data":{"itemList":[
            {"name":"合成景点","ticketInfo":{"price":"¥50","priceDate":"2026-10-12","ticketName":"成人票"}}]}}})
        quote = await flyai.ticket("trip","广州","合成景点","2026-10-13")
        self.assertEqual(quote["price"],50)
        self.assertEqual(quote["price_basis"],"starting")
        self.assertIn("日期不同",quote["note"])

    async def test_missing_price_uses_the_explicit_user_free_default(self):
        flyai = FlyAI.__new__(FlyAI)
        flyai.search = AsyncMock(return_value={"queried_at":"now","raw":{"data":{"itemList":[
            {"name":"合成景点","ticketInfo":{"price":None,"priceDate":"2026-10-13"}}]}}})
        quote = await flyai.ticket("trip","广州","合成景点","2026-10-13")
        self.assertEqual(quote["price"],0)
        self.assertTrue(quote["assumed_free"])
        self.assertIn("未返回",quote["note"])


class ResumedResourceQuery(unittest.IsolatedAsyncioTestCase):
    async def test_resume_retries_missing_hotel_instead_of_carrying_missing_cli_error(self):
        planner = Planner.__new__(Planner); planner.emit = Mock()
        planner.flyai = SimpleNamespace(availability=lambda:{"ready":True},ticket=AsyncMock())
        new_hotel = {"id":"hotel:广州","city":"广州","selected":{"name":"新酒店"},"selected_by_user":True}
        planner.hotels = AsyncMock(return_value={"itinerary":{"days":[],"transport":[],"hotels":[new_hotel]}})
        planner.locate_stops = AsyncMock(return_value={}); planner.weather_context = AsyncMock(return_value={})
        planner.movement = AsyncMock(return_value=({"minutes":25,"mode":"transit"},None))
        old = {"days":[],"transport":[],"hotels":[{"id":"hotel:广州","city":"广州","selected":None,"selected_by_user":True,"errors":["未找到 flyai"]}]}
        state = {"requirements":REQ,"trip_id":"trip","mode":"modify","change":{"scopes":["activity"]},"locks":[],"stops":{},
            "old_itinerary":old,"itinerary":old,"selected_route":{"days":[{"date":"2026-10-13","city":"广州","stop_ids":[]}]}}
        result = await planner.activities(state)
        planner.hotels.assert_awaited_once()
        self.assertEqual(result["itinerary"]["hotels"],[new_hotel])
        self.assertTrue(result["resource_retry_done"])


class CurrentActivityRefresh(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_keeps_current_visits_and_stops_ticket_queries_after_provider_failure(self):
        planner = Planner.__new__(Planner); planner.emit = Mock();planner.store = Mock()
        planner.flyai = SimpleNamespace(availability=lambda:{"ready":True})
        planner.resource_agent = SimpleNamespace(ticket=AsyncMock(side_effect=ToolError("flyai_failed","查询权限异常")))
        planner.models = SimpleNamespace(ask=AsyncMock())
        stops = {identity:{"id":identity,"city":"广州","name":identity,"source_id":"guide","source_title":"合成攻略",
            "evidence":{"quote":identity},"duration_minutes":30,"ticket_claim":"门票50元"} for identity in ("保留一","保留二","已删除")}
        planner.locate_stops = AsyncMock(side_effect=lambda state,items:items)
        planner.weather_context = AsyncMock(return_value={})
        planner.map_order = AsyncMock(side_effect=lambda state,city,ids,origin,destination:(ids,"更新路线"))
        planner.window = AsyncMock(return_value=(540,1200,[],[],{"name":"酒店","label":"返回住宿"}))
        planner.movement = AsyncMock(return_value=({"minutes":5,"mode":"walking"},None))
        old = {"transport":[],"hotels":[{"id":"hotel:广州","city":"广州","selected":{"name":"酒店"}}],
            "days":[{"date":"2026-10-13","city":"广州","activities":[{"id":identity,"kind":"visit","start":start,"end":end}
                for identity,start,end in (("保留一","09:00","10:00"),("保留二","10:30","11:30"))]}]}
        state = {"requirements":REQ,"trip_id":"trip","mode":"modify","change":{"scopes":["hotel","transport","activity"],"refresh_resources":True},
            "locks":[],"stops":stops,"old_itinerary":old,"itinerary":old,
            "selected_route":{"days":[{"date":"2026-10-13","city":"广州","stop_ids":list(stops)}]}}
        result = await planner.activities(state)
        visits = [item for item in result["itinerary"]["days"][0]["activities"] if item["kind"] == "visit"]
        self.assertEqual([item["id"] for item in visits],["保留一","保留二"])
        planner.models.ask.assert_not_awaited()
        planner.resource_agent.ticket.assert_awaited_once()
        self.assertIn("暂停",visits[1]["ticket_quote"]["note"])
