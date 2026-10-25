"""Ticket defaults and matching with synthetic provider data."""
import copy
import unittest
from unittest.mock import AsyncMock

from app.adapters import FlyAI, default_ticket_price, normalize_pois
from app.models import Requirements
from app.rules import check_itinerary


DATE = "2026-11-10"


class TicketDefaultsAndMatching(unittest.IsolatedAsyncioTestCase):
    def client(self, rows):
        flyai = FlyAI.__new__(FlyAI)
        flyai.search = AsyncMock(return_value={"status":"ok","queried_at":"saved-time","evidence_id":"query:ticket",
                                              "raw":{"data":{"itemList":rows}}})
        return flyai


    async def test_supplier_free_flag_and_system_placeholder_remain_distinct(self):
        flyai = self.client([{"id":"museum","name":"广东省博物馆","freePoiStatus":"FREE","ticketInfo":None}])
        result = await flyai.ticket("trip","广州","广东省博物馆",DATE)
        self.assertEqual(result["price"],0)
        self.assertTrue(result["reported_free"])
        self.assertFalse(result["assumed_free"])
        self.assertEqual(result["status"],"free")

    async def test_former_name_matches_by_address_and_alias_description(self):
        row = {"id":"museum","name":"西汉南越王博物馆","address":"解放北路867号",
               "description":"南越王博物院(王墓展区)位于解放北路867号，前身为西汉南越王博物馆。",
               "ticketInfo":{"price":50}}
        flyai = self.client([row])
        for place in (None,{"address":"解放北路867号(地铁站附近)"}):
            result = await flyai.ticket("trip","广州","南越王博物院(王墓展区)",DATE,place=place)
            self.assertEqual(result["supplier_id"],"museum")
            self.assertEqual(result["price"],50)
            self.assertNotIn("assumed_free",result)

    async def test_an_explicit_short_name_matches_without_arbitrary_fuzzy_selection(self):
        flyai = self.client([{"id":"museum","name":"广东省博物馆","description":"广东省博物馆，简称省博。",
                              "ticketInfo":{"price":30}}])
        result = await flyai.ticket("trip","广州","省博",DATE)
        self.assertEqual(result["supplier_id"],"museum")
        self.assertEqual(result["price"],30)

    async def test_ticket_matching_uses_alias_and_address_instead_of_supplier_coordinates(self):
        row = {"id":"museum","name":"西汉南越王博物馆","address":"解放北路867号",
               "description":"南越王博物院(王墓展区)的前身为西汉南越王博物馆。",
               "longitude":"113.267474","latitude":"23.144033","ticketInfo":{"price":50}}
        place = {"id":"amap-museum","name":"南越王博物院(王墓展区)","address":"解放北路867号",
                 "location":"113.261015,23.137823","evidence_id":"query:map"}
        original = copy.deepcopy(place)
        result = await self.client([row]).ticket("trip","广州",place["name"],DATE,place=place)
        self.assertEqual(result["supplier_id"],"museum")
        self.assertEqual(result["price"],50)
        self.assertNotIn("location",result)
        self.assertEqual(place,original)

    async def test_different_exhibition_areas_and_similar_museum_names_are_not_chosen(self):
        for row,name in (({"id":"palace","name":"南越王博物院(王宫展区)","address":"解放北路867号",
                          "description":"旧称西汉南越王博物馆，王墓展区为另一展区。"},"南越王博物院(王墓展区)"),
                         ({"id":"other","name":"广东省凉茶博物馆","address":"金峰园路2号"},"广东省博物馆")):
            with self.subTest(name=name):
                flyai = self.client([{**row,"ticketInfo":{"price":80}}])
                flyai.search.return_value["options"] = [{"name":row["name"],"price":80}]
                result = await flyai.ticket("trip","广州",name,DATE,place={"address":"解放北路867号"})
                self.assertEqual(result["status"],"place_confirmation")
                self.assertNotIn("supplier_id",result)
                self.assertEqual(result["price"],0)

    async def test_an_internal_zoo_poi_is_not_chosen_as_the_whole_park(self):
        flyai = self.client([{"id":"child","name":"长隆野生动物世界-长颈鹿广场","address":"香江大道593号",
                              "ticketInfo":{"price":10}}])
        flyai.search.return_value["options"] = [{"name":"长颈鹿广场","price":10}]
        result = await flyai.ticket("trip","广州","长隆野生动物世界",DATE,place={"address":"香江大道593号"})
        self.assertEqual(result["status"],"place_confirmation")
        self.assertNotIn("supplier_id",result)

    async def test_unknown_price_date_is_not_called_a_different_date(self):
        flyai = self.client([{"id":"zoo","name":"广州长隆野生动物世界",
                              "ticketInfo":{"price":"¥323","priceDate":None}}])
        result = await flyai.ticket("trip","广州","长隆野生动物园",DATE)
        self.assertEqual(result["price"],323)
        self.assertIsNone(result["price_date"])
        self.assertIn("日期未提供",result["note"])
        self.assertNotIn("日期不同",result["note"])

    async def test_top_level_reference_price_is_kept_when_no_ticket_detail_was_returned(self):
        flyai = self.client([{"id":"museum","name":"合成场馆","ticketInfo":None,"minPrice":45}])
        result = await flyai.ticket("trip","广州","合成场馆",DATE)
        self.assertEqual(result["price"],45)
        self.assertNotIn("assumed_free",result)
