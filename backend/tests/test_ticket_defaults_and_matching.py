"""Synthetic user-run regressions; no provider calls, builds or service execution."""
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

    async def test_a_matched_poi_without_ticket_info_is_not_a_place_mismatch(self):
        flyai = self.client([{"id":"museum","name":"广东省博物馆","ticketInfo":None}])
        result = await flyai.ticket("trip","广州","广东省博物馆",DATE)
        self.assertEqual(result["price"],0)
        self.assertTrue(result["assumed_free"])
        self.assertEqual(result["status"],"price_unavailable")
        self.assertEqual(result["queried_at"],"saved-time")
        self.assertEqual(result["evidence_id"],"query:ticket")

    async def test_supplier_free_flag_and_user_default_remain_distinct(self):
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

    async def test_multiple_ticket_types_keep_known_prices_and_default_only_the_missing_one(self):
        flyai = self.client([{"id":"museum","name":"合成场馆","ticketInfo":[
            {"ticketName":"成人票","price":50},{"ticketName":"其他票","price":None}]}])
        result = await flyai.ticket("trip","广州","合成场馆",DATE)
        self.assertIsNone(result["price"])
        self.assertEqual(result["status"],"options")
        self.assertEqual([item["price"] for item in result["options"]],[50,0])
        self.assertTrue(result["options"][1]["assumed_free"])

    def test_default_preserves_failure_diagnostics_and_never_updates_query_time(self):
        quote = {"price":None,"status":"error","code":"flyai_network","note":"连接失败",
                 "queried_at":"old-time","evidence_id":"query:old"}
        original = copy.deepcopy(quote)
        result = default_ticket_price(quote)
        self.assertEqual(quote,original)
        self.assertEqual(default_ticket_price(result),result)
        self.assertEqual(result["price"],0)
        self.assertTrue(result["assumed_free"])
        self.assertEqual(result["status"],"error")
        self.assertEqual(result["queried_at"],"old-time")
        self.assertIn("连接失败",result["note"])

    def test_normalized_empty_ticket_list_retains_the_poi_and_link(self):
        rows = normalize_pois({"items":[{"id":"a","name":"合成场馆","ticketInfo":[],
                                         "jumpUrl":"https://www.fliggy.com/detail"}]},{"city":"广州"})
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]["price"],0)
        self.assertTrue(rows[0]["assumed_free"])
        self.assertEqual(rows[0]["booking_url"],"https://www.fliggy.com/detail")

    def test_budget_uses_the_disclosed_zero_default_without_marking_ticket_cost_missing(self):
        quote = default_ticket_price({"price":None})
        itinerary = {"transport":[],"hotels":[],"days":[{"city":"广州","date":DATE,"activities":[{
            "id":"museum","name":"合成博物馆","kind":"visit","start":"09:00","end":"10:00",
            "evidence":{"quote":"合成攻略"},"ticket_claim":"收费","ticket_quote":quote,"move_before":{}}]}]}
        req = Requirements(origin="广州",cities=["广州"],start_date=DATE,end_date=DATE,people=1,rooms=1,
                           budget=1000,return_to_origin=False).model_dump(mode="json")
        result = check_itinerary(itinerary,req)
        self.assertEqual(result["budget"]["estimated_tickets"],0)
        self.assertFalse(any("合成博物馆" in text for text in result["budget"]["unknown"]))
        self.assertTrue(any("默认免费" in text for text in result["constraint_notes"]))
